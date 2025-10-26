# app.py
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional, List, Dict

from flask import (
    Flask, render_template_string, request, redirect, url_for, flash,
    session, abort, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect

###############################################################################
# App & DB
###############################################################################
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

DB_URL = os.getenv("DATABASE_URL", "sqlite:///signalbot.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

###############################################################################
# Branding / Theme
###############################################################################
BRAND = {
    "name": "SignalBot",
    "logo_text": "🐺🤖",  # Wolf robot glyph; replace with SVG if you have one
    # Brand palette (provided): #ffb000, #04004f, #0f1028, #04a8d2, #8E24C1
    "colors": {
        "accent": "#ffb000",
        "ink": "#04004f",
        "bgd": "#0f1028",
        "cyanspark": "#04a8d2",
        "violet": "#8E24C1",
    }
}

###############################################################################
# Simple RBAC / Tiers
###############################################################################
TIER_LIMITS = {
    # tier_name: (max_users, max_concurrent_scrapes)
    "starter": (1, 5),    # (1 user, up to 5 scrapes running at once)
    "pro":     (3, 10),   # (up to 3 users, 10 scrapes running)
    "agency":  (50, 30),  # generous defaults; you can tune later
}

ROLES = ("owner", "admin", "member")

###############################################################################
# Models
###############################################################################
class Organization(db.Model):
    __tablename__ = "organizations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    tier = db.Column(db.String(32), nullable=False, default="starter")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def max_users(self) -> int:
        return TIER_LIMITS.get(self.tier, (1, 5))[0]

    def max_concurrent_scrapes(self) -> int:
        return TIER_LIMITS.get(self.tier, (1, 5))[1]


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)
    # demo-only: plaintext, swap to proper hash (werkzeug.security) in prod
    password = db.Column(db.String(200), nullable=False, default="password")
    is_superadmin = db.Column(db.Boolean, default=False)

    org_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=True)
    role = db.Column(db.String(16), nullable=False, default="member")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    org = db.relationship("Organization", backref="users")


class ScrapeJob(db.Model):
    __tablename__ = "scrapes"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    name = db.Column(db.String(200), nullable=False)
    query = db.Column(db.Text, nullable=True)

    use_ai_scoring = db.Column(db.Boolean, default=True)  # can be disabled
    goal_text = db.Column(db.Text, nullable=True)         # instructions for scoring

    status = db.Column(db.String(32), default="idle")     # idle | queued | running | done | error
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    org = db.relationship("Organization", backref="scrapes")
    user = db.relationship("User", backref="scrapes")


class Post(db.Model):
    __tablename__ = "posts"
    id = db.Column(db.Integer, primary_key=True)
    scrape_id = db.Column(db.Integer, db.ForeignKey("scrapes.id"), nullable=False)

    title = db.Column(db.String(300), nullable=False)
    url = db.Column(db.String(500), nullable=True)
    content = db.Column(db.Text, nullable=True)

    score = db.Column(db.Float, nullable=True)  # optional AI score
    hidden = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    scrape = db.relationship("ScrapeJob", backref="posts")


###############################################################################
# DB bootstrap / lightweight "migration"
###############################################################################
def ensure_db_upgrade():
    """Create tables if missing and add any new columns we rely on."""
    # Make sure base tables exist
    db.create_all()

    # Safely add posts.hidden if it doesn't exist (works on SQLite and Postgres)
    with db.engine.begin() as conn:
        insp = inspect(conn)
        if "posts" in insp.get_table_names():
            existing_cols = {c["name"] for c in insp.get_columns("posts")}
            if "hidden" not in existing_cols:
                if db.engine.name == "sqlite":
                    # SQLite can't always set defaults via ALTER reliably; just add the column.
                    conn.execute(text("ALTER TABLE posts ADD COLUMN hidden BOOLEAN"))
                else:
                    # Postgres/MySQL: add with a default
                    conn.execute(text("ALTER TABLE posts ADD COLUMN hidden BOOLEAN DEFAULT FALSE"))


        if db.engine.name == "sqlite":
            # For SQLite, check pragma table_info
            cols = conn.execute(text("PRAGMA table_info(posts)")).fetchall()
            colnames = {c[1] for c in cols}
            if "hidden" not in colnames:
                # SQLite can't ALTER TABLE ADD COLUMN with default expression
                conn.execute(text("ALTER TABLE posts ADD COLUMN hidden BOOLEAN"))
        # Similar could be done for other new columns if you add them later.


with app.app_context():
    ensure_db_upgrade()

###############################################################################
# Helpers
###############################################################################
def current_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

def login_required(fn):
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Please sign in.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper

def require_role(*allowed_roles):
    def deco(fn):
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user.is_superadmin:
                return fn(*args, **kwargs)
            if user.role not in allowed_roles:
                abort(403)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco

def org_running_scrapes_count(org_id: int) -> int:
    return ScrapeJob.query.filter(
        ScrapeJob.org_id == org_id,
        ScrapeJob.status.in_(("queued", "running"))
    ).count()

def org_user_count(org_id: int) -> int:
    return User.query.filter_by(org_id=org_id).count()

def score_text(goal: str, content: str) -> float:
    """
    Cheap, deterministic scoring in lieu of a real LLM:
    - Count goal keywords in content; normalize.
    """
    if not goal or not content:
        return 0.0
    # Keywords from goal: top 10 tokens (alphanumeric, length>=4)
    tokens = re.findall(r"[A-Za-z0-9]{4,}", goal.lower())
    keywords = list(dict.fromkeys(tokens))[:10]  # keep order, unique, cap 10

    hits = 0
    text_l = content.lower()
    for k in keywords:
        if k in text_l:
            hits += 1
    if not keywords:
        return 0.0
    raw = hits / len(keywords)
    # small bonus for length
    bonus = min(len(content) / 2000.0, 0.25)
    return round(min(raw + bonus, 1.0) * 100.0, 2)

def ensure_demo_superadmin():
    """Creates a superadmin and a demo org on first run."""
    if not Organization.query.first():
        org = Organization(name="SignalBot Demo Org", tier="starter")
        db.session.add(org)
        db.session.commit()
    if not User.query.filter_by(email="admin@signalbot.local").first():
        admin = User(
            email="admin@signalbot.local",
            name="Super Admin",
            password="admin",  # demo only
            is_superadmin=True,
            org_id=None,
            role="owner"
        )
        db.session.add(admin)
        db.session.commit()

with app.app_context():
    ensure_demo_superadmin()

###############################################################################
# Auth
###############################################################################
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.password == password:
            session["user_id"] = user.id
            flash("Welcome back!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template_string(TPL_LOGIN, brand=BRAND)

@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("login"))

###############################################################################
# Onboarding (create org & first user)
###############################################################################
@app.route("/onboard", methods=["GET", "POST"])
def onboard():
    # If you're already logged in, skip
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        tier = request.form.get("tier", "starter")

        if not name or not email or not password:
            flash("Please fill all fields.", "warning")
            return render_template_string(TPL_ONBOARD, brand=BRAND, tiers=TIER_LIMITS)

        if Organization.query.filter_by(name=name).first():
            flash("Org name already in use.", "danger")
            return render_template_string(TPL_ONBOARD, brand=BRAND, tiers=TIER_LIMITS)

        org = Organization(name=name, tier=tier)
        db.session.add(org)
        db.session.commit()

        owner = User(
            email=email,
            name=email.split("@")[0].title(),
            password=password,
            is_superadmin=False,
            org_id=org.id,
            role="owner"
        )
        db.session.add(owner)
        db.session.commit()

        session["user_id"] = owner.id
        flash("Organization created. Welcome!", "success")
        return redirect(url_for("dashboard"))

    return render_template_string(TPL_ONBOARD, brand=BRAND, tiers=TIER_LIMITS)

###############################################################################
# Dashboard
###############################################################################
@app.route("/")
@login_required
def dashboard():
    user = current_user()
    if user.is_superadmin:
        scrapes = ScrapeJob.query.order_by(ScrapeJob.created_at.desc()).all()
        orgs = Organization.query.all()
    else:
        scrapes = ScrapeJob.query.filter_by(org_id=user.org_id).order_by(ScrapeJob.created_at.desc()).all()
        orgs = [user.org] if user.org else []
    running = org_running_scrapes_count(user.org_id) if (user and user.org_id) else 0
    return render_template_string(
        TPL_DASH,
        brand=BRAND,
        user=user,
        scrapes=scrapes,
        orgs=orgs,
        running=running,
        tier_limits=TIER_LIMITS
    )

###############################################################################
# Scrape CRUD / Run
###############################################################################
@app.route("/scrape/new", methods=["POST"])
@login_required
def scrape_new():
    user = current_user()
    org = user.org if not user.is_superadmin else Organization.query.first()
    if not org and not user.is_superadmin:
        flash("No organization found.", "danger")
        return redirect(url_for("dashboard"))

    # Enforce user limit (only for non-superadmin)
    if not user.is_superadmin and org_user_count(org.id) > org.max_users():
        flash("User limit reached for your plan.", "danger")
        return redirect(url_for("dashboard"))

    name = request.form.get("name", "").strip() or "New Scrape"
    query = request.form.get("query", "").strip()
    use_ai_scoring = request.form.get("use_ai_scoring") == "on"
    goal_text = request.form.get("goal_text", "").strip()

    sj = ScrapeJob(
        org_id=(org.id if org else None) if not user.is_superadmin else org.id,
        user_id=user.id,
        name=name,
        query=query,
        use_ai_scoring=use_ai_scoring,
        goal_text=goal_text
    )
    db.session.add(sj)
    db.session.commit()
    flash("Scrape created.", "success")
    return redirect(url_for("scrape_detail", scrape_id=sj.id))

@app.route("/scrape/<int:scrape_id>")
@login_required
def scrape_detail(scrape_id: int):
    user = current_user()
    sj = ScrapeJob.query.get_or_404(scrape_id)
    if not user.is_superadmin and sj.org_id != user.org_id:
        abort(403)

    filter_mode = request.args.get("filter", "all")  # all | visible | hidden
    q = Post.query.filter_by(scrape_id=sj.id)
    if filter_mode == "visible":
        q = q.filter_by(hidden=False)
    elif filter_mode == "hidden":
        q = q.filter_by(hidden=True)

    posts = q.order_by(Post.created_at.desc()).all()
    return render_template_string(
        TPL_SCRAPE,
        brand=BRAND,
        user=user,
        scrape=sj,
        posts=posts,
        filter_mode=filter_mode
    )

@app.route("/scrape/<int:scrape_id>/run", methods=["POST"])
@login_required
def scrape_run(scrape_id: int):
    user = current_user()
    sj = ScrapeJob.query.get_or_404(scrape_id)
    if not user.is_superadmin and sj.org_id != user.org_id:
        abort(403)

    # Enforce concurrent scrape limit (by org)
    org = sj.org
    current_running = org_running_scrapes_count(org.id)
    if sj.status in ("queued", "running"):
        flash("This scrape is already running.", "info")
        return redirect(url_for("scrape_detail", scrape_id=sj.id))

    if not user.is_superadmin and current_running >= org.max_concurrent_scrapes():
        flash("Your plan’s concurrent scrape limit is reached.", "danger")
        return redirect(url_for("scrape_detail", scrape_id=sj.id))

    sj.status = "queued"
    db.session.commit()

    # Kick off a worker thread (demo)
    t = threading.Thread(target=_run_scrape_worker, args=(sj.id,), daemon=True)
    t.start()

    flash("Scrape queued. It will populate shortly.", "success")
    return redirect(url_for("scrape_detail", scrape_id=sj.id))

def _run_scrape_worker(scrape_id: int):
    with app.app_context():
        sj = ScrapeJob.query.get(scrape_id)
        if not sj:
            return
        try:
            sj.status = "running"
            db.session.commit()

            # DEMO: fabricate 12 posts so you can test instantly.
            # Replace this block with real scraping logic.
            fabricated: List[Dict] = []
            base = datetime.utcnow().strftime("%Y-%m-%d")
            for i in range(1, 13):
                fabricated.append({
                    "title": f"[{base}] Signal #{i}: Market shift in vertical X",
                    "url": f"https://example.com/signals/{i}",
                    "content": (
                        f"This is a synthetic post {i}. It mentions growth, churn, "
                        f"pricing strategy, and partnerships in B2B SaaS."
                        f" Query='{sj.query or ''}'"
                    )
                })

            for item in fabricated:
                p = Post(
                    scrape_id=sj.id,
                    title=item["title"],
                    url=item.get("url"),
                    content=item.get("content")
                )
                if sj.use_ai_scoring:
                    p.score = score_text(sj.goal_text or sj.query or "", p.content or "")
                db.session.add(p)
                db.session.commit()
                time.sleep(0.1)  # tiny delay to simulate streaming in

            sj.status = "done"
            db.session.commit()
        except Exception as e:
            sj.status = "error"
            db.session.commit()
            app.logger.exception(f"Scrape {scrape_id} error: {e}")

###############################################################################
# Post detail / Hide toggle
###############################################################################
@app.route("/post/<int:post_id>")
@login_required
def post_detail(post_id: int):
    user = current_user()
    p = Post.query.get_or_404(post_id)
    if not user.is_superadmin and p.scrape.org_id != user.org_id:
        abort(403)
    return render_template_string(
        TPL_POST,
        brand=BRAND,
        user=user,
        post=p,
        scrape=p.scrape
    )

@app.route("/post/<int:post_id>/toggle_hide", methods=["POST"])
@login_required
def post_toggle_hide(post_id: int):
    user = current_user()
    p = Post.query.get_or_404(post_id)
    if not user.is_superadmin and p.scrape.org_id != user.org_id:
        abort(403)
    p.hidden = not p.hidden
    db.session.commit()
    return redirect(url_for("scrape_detail", scrape_id=p.scrape_id))

###############################################################################
# Admin (Superadmin): view all customers
###############################################################################
@app.route("/admin/customers")
@login_required
def admin_customers():
    user = current_user()
    if not user.is_superadmin:
        abort(403)
    orgs = Organization.query.order_by(Organization.created_at.desc()).all()
    return render_template_string(TPL_ADMIN, brand=BRAND, user=user, orgs=orgs, limits=TIER_LIMITS)

@app.route("/admin/customers/<int:org_id>/set_tier", methods=["POST"])
@login_required
def admin_set_tier(org_id: int):
    user = current_user()
    if not user.is_superadmin:
        abort(403)
    tier = request.form.get("tier", "starter")
    org = Organization.query.get_or_404(org_id)
    org.tier = tier
    db.session.commit()
    flash("Tier updated.", "success")
    return redirect(url_for("admin_customers"))

###############################################################################
# Templates (inline for single-file convenience)
###############################################################################
BASE_CSS = """
:root {
  --sb-accent: {{ brand.colors.accent }};
  --sb-ink: {{ brand.colors.ink }};
  --sb-bgd: {{ brand.colors.bgd }};
  --sb-cyan: {{ brand.colors.cyanspark }};
  --sb-violet: {{ brand.colors.violet }};
  --sb-card: #12132b;
  --sb-text: #e6e8f0;
  --sb-muted: #9aa4b2;
  --sb-border: #252845;
  --sb-badge: #191b39;
}

:root.light {
  --sb-bgd: #f7f8fb;
  --sb-card: #ffffff;
  --sb-text: #0f1028;
  --sb-muted: #4f5564;
  --sb-border: #e9ecf2;
  --sb-badge: #f0f3fa;
}

* { box-sizing: border-box; }
body {
  margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue";
  background: var(--sb-bgd); color: var(--sb-text);
}
a { color: var(--sb-cyan); text-decoration: none; }
a:hover { text-decoration: underline; }

.nav {
  display:flex; align-items:center; justify-content:space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--sb-border);
  background: linear-gradient(180deg, rgba(255,176,0,0.08), transparent 40%) var(--sb-bgd);
}
.brand { display:flex; align-items:center; gap:10px; font-weight:800; letter-spacing: .3px; }
.brand .logo {
  font-size: 20px; background: radial-gradient(circle at 30% 30%, var(--sb-violet), var(--sb-cyan));
  width: 34px; height: 34px; display:grid; place-items:center; border-radius:10px;
  box-shadow: 0 6px 18px rgba(8,12,36,.35);
}
.brand .name { color: var(--sb-text); }
.right { display:flex; align-items:center; gap:10px; }

.btn {
  padding: 8px 12px; border-radius: 10px; border:1px solid var(--sb-border);
  background: var(--sb-card); color: var(--sb-text); cursor:pointer;
}
.btn.primary {
  background: linear-gradient(180deg, var(--sb-accent), #ff9d00);
  color: #1a1400; border: none; font-weight: 700;
  box-shadow: 0 8px 20px rgba(255,176,0,.25);
}
.btn.ghost { background: transparent; }
.btn.danger { background: #3a1a1a; border-color: #5a2a2a; color: #ffb3b3; }

.wrap { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
.card {
  background: var(--sb-card); border:1px solid var(--sb-border); border-radius: 16px; padding: 16px;
  box-shadow: 0 10px 24px rgba(6,8,20,.25);
}
.row { display:grid; gap: 12px; }
.grid { display:grid; gap:12px; grid-template-columns: repeat(12, 1fr); }
.col-8 { grid-column: span 8; } .col-4 { grid-column: span 4; }
.col-12 { grid-column: span 12; }

.table { width: 100%; border-collapse: collapse; }
.table th, .table td { padding: 10px 8px; border-bottom: 1px solid var(--sb-border); }
.badge {
  display:inline-flex; align-items:center; gap:8px;
  padding: 6px 10px; background: var(--sb-badge); border:1px solid var(--sb-border); border-radius: 999px;
}
.input, textarea, select {
  width:100%; padding:10px 12px; background: var(--sb-badge); color: var(--sb-text);
  border:1px solid var(--sb-border); border-radius: 10px;
}
label { font-size: 12px; color: var(--sb-muted); }
.small { font-size: 12px; color: var(--sb-muted); }
hr.sep { border:0; border-top:1px dashed var(--sb-border); margin: 12px 0; }
.help { color: var(--sb-muted); font-size: 12px; margin-top: 6px; }
.flash { padding: 10px 12px; border-radius: 10px; margin: 10px 0; }
.flash.success { background: #15371a; border:1px solid #1f6a2a; color: #a6f0b6; }
.flash.danger { background: #3a1a1a; border:1px solid #5a2a2a; color: #ffb3b3; }
.flash.warning { background: #3a321a; border:1px solid #6a5a2a; color: #ffe9a6; }
.flash.info { background: #17283c; border:1px solid #274d7a; color: #b7daff; }
.kpi { font-weight: 800; letter-spacing:.3px; }
.toggle { display:flex; align-items:center; gap: 8px; }
.toggle input { transform: scale(1.2); }
.footer { margin: 30px 0; text-align: center; color: var(--sb-muted); }
"""

BASE_HEAD = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{ brand.name }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>{{ base_css|safe }}</style>
  <script>
    // Theme bootstrap from localStorage
    (function() {
      try {
        var t = localStorage.getItem('sb-theme') || 'dark';
        if (t === 'light') document.documentElement.classList.add('light');
      } catch(e) {}
    })();
    function toggleTheme() {
      var html = document.documentElement;
      var isLight = html.classList.toggle('light');
      try { localStorage.setItem('sb-theme', isLight ? 'light' : 'dark'); } catch(e){}
    }
  </script>
</head>
<body>
  <div class="nav">
    <div class="brand">
      <div class="logo">{{ brand.logo_text }}</div>
      <div class="name">{{ brand.name }}</div>
    </div>
    <div class="right">
      <button class="btn" onclick="toggleTheme()">Toggle Theme</button>
      {% if user %}
        <span class="badge">Signed in as {{ user.name }}{% if user.is_superadmin %} · Superadmin{% endif %}</span>
        <a class="btn ghost" href="{{ url_for('logout') }}">Sign out</a>
      {% endif %}
    </div>
  </div>
  <div class="wrap">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}
          <div class="flash {{ cat }}">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
"""

BASE_FOOT = """
    <div class="footer small">© {{ brand.name }} — Find the signal. Cut the noise.</div>
  </div>
</body>
</html>
"""

TPL_LOGIN = BASE_HEAD + """
<div class="grid">
  <div class="col-4"></div>
  <div class="col-4">
    <div class="card">
      <div style="font-size:18px; font-weight:800; margin-bottom:10px;">Sign in</div>
      <form method="post">
        <label>Email</label>
        <input class="input" name="email" type="email" placeholder="you@example.com" />
        <div style="height:8px"></div>
        <label>Password</label>
        <input class="input" name="password" type="password" placeholder="••••••••" />
        <div style="height:12px"></div>
        <button class="btn primary" type="submit">Sign in</button>
        <div class="help" style="margin-top:8px">
          Demo superadmin: <code>admin@signalbot.local / admin</code>
          · Or <a href="{{ url_for('onboard') }}">create your org</a>
        </div>
      </form>
    </div>
  </div>
</div>
""" + BASE_FOOT

TPL_ONBOARD = BASE_HEAD + """
<div class="grid">
  <div class="col-3"></div>
  <div class="col-6">
    <div class="card">
      <div style="font-size:18px; font-weight:800; margin-bottom:10px;">Create your organization</div>
      <form method="post">
        <label>Organization name</label>
        <input class="input" name="name" placeholder="Acme Co." />
        <div style="height:8px"></div>
        <label>Your email</label>
        <input class="input" name="email" type="email" placeholder="you@example.com" />
        <div style="height:8px"></div>
        <label>Password</label>
        <input class="input" name="password" type="password" placeholder="••••••••" />
        <div style="height:8px"></div>
        <label>Plan</label>
        <select class="input" name="tier">
          {% for t, lim in tiers.items() %}
            <option value="{{ t }}">{{ t|capitalize }} — {{ lim[0] }} users / {{ lim[1] }} concurrent scrapes</option>
          {% endfor %}
        </select>
        <div style="height:12px"></div>
        <button class="btn primary" type="submit">Create org</button>
      </form>
    </div>
  </div>
</div>
""" + BASE_FOOT

TPL_DASH = BASE_HEAD + """
<div class="row">
  <div class="grid">
    <div class="col-8">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div style="font-weight:800; letter-spacing:.3px;">Your Scrapes</div>
          <div class="small">Running now: <span class="kpi">{{ running }}</span>
            {% if not user.is_superadmin and user.org %}
              / {{ user.org.max_concurrent_scrapes() }}
            {% endif %}
          </div>
        </div>
        <table class="table" style="margin-top:10px;">
          <thead><tr>
            <th>Name</th><th>Status</th><th>AI</th><th>Posts</th><th></th>
          </tr></thead>
          <tbody>
            {% for s in scrapes %}
              <tr>
                <td><a href="{{ url_for('scrape_detail', scrape_id=s.id) }}">{{ s.name }}</a></td>
                <td><span class="badge">{{ s.status }}</span></td>
                <td>{% if s.use_ai_scoring %}On{% else %}<span class="small">Off</span>{% endif %}</td>
                <td>{{ s.posts|length }}</td>
                <td>
                  <form method="post" action="{{ url_for('scrape_run', scrape_id=s.id) }}">
                    <button class="btn" type="submit">Run</button>
                  </form>
                </td>
              </tr>
            {% else %}
              <tr><td colspan="5" class="small">No scrapes yet.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    <div class="col-4">
      <div class="card">
        <div style="font-weight:800; letter-spacing:.3px;">New Scrape</div>
        <form method="post" action="{{ url_for('scrape_new') }}">
          <div style="height:8px"></div>
          <label>Name</label>
          <input class="input" name="name" placeholder="Competitor announcements" />
          <div style="height:8px"></div>
          <label>Seed / Query</label>
          <textarea class="input" name="query" rows="2" placeholder="Keywords, seed URLs, etc."></textarea>
          <div style="height:8px"></div>
          <div class="toggle">
            <input type="checkbox" name="use_ai_scoring" checked id="use_ai_scoring">
            <label for="use_ai_scoring">Use AI scoring</label>
          </div>
          <div class="help">Uncheck to ingest only—no scoring step.</div>
          <div style="height:8px"></div>
          <label>Goal / Scoring Instructions (what should the bot look for?)</label>
          <textarea class="input" name="goal_text" rows="3" placeholder="e.g., prioritize posts about pricing changes and churn in B2B SaaS"></textarea>
          <div style="height:12px"></div>
          <button class="btn primary" type="submit">Create</button>
        </form>
      </div>

      {% if user.is_superadmin %}
      <div class="card" style="margin-top:12px;">
        <div style="font-weight:800; letter-spacing:.3px;">Superadmin</div>
        <a class="btn" style="margin-top:8px" href="{{ url_for('admin_customers') }}">All Customers</a>
      </div>
      {% endif %}
    </div>
  </div>
</div>
""" + BASE_FOOT

TPL_SCRAPE = BASE_HEAD + """
<div class="row">
  <div class="card">
    <div style="display:flex; align-items:center; justify-content:space-between;">
      <div style="display:flex; align-items:center; gap:10px;">
        <div class="badge">Scrape</div>
        <div style="font-weight:800;">{{ scrape.name }}</div>
      </div>
      <div style="display:flex; gap:8px;">
        <form method="post" action="{{ url_for('scrape_run', scrape_id=scrape.id) }}">
          <button class="btn" type="submit">Run</button>
        </form>
        <a class="btn ghost" href="{{ url_for('dashboard') }}">Back</a>
      </div>
    </div>
    <div class="small" style="margin-top:6px;">
      Status: <span class="badge">{{ scrape.status }}</span> · AI scoring: {% if scrape.use_ai_scoring %}On{% else %}Off{% endif %}
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div style="display:flex; align-items:center; justify-content:space-between;">
      <div style="display:flex; gap:10px; align-items:center;">
        <div class="badge">Posts</div>
        <div class="small">Total: {{ posts|length }}</div>
      </div>
      <div>
        <form method="get" action="">
          <input type="hidden" name="filter" value="">
          <div style="display:flex; gap:8px; align-items:center;">
            <label>Filter</label>
            <select class="input" name="filter" onchange="this.form.submit()">
              <option value="all" {% if filter_mode=='all' %}selected{% endif %}>All</option>
              <option value="visible" {% if filter_mode=='visible' %}selected{% endif %}>Visible only</option>
              <option value="hidden" {% if filter_mode=='hidden' %}selected{% endif %}>Hidden only</option>
            </select>
          </div>
        </form>
      </div>
    </div>

    <table class="table" style="margin-top:10px;">
      <thead><tr>
        <th style="width:42%;">Title</th>
        <th>Score</th>
        <th>Hidden</th>
        <th>When</th>
        <th></th>
      </tr></thead>
      <tbody>
        {% for p in posts %}
          <tr>
            <td>
              <a href="{{ url_for('post_detail', post_id=p.id) }}">{{ p.title }}</a>
              {% if p.url %}
                <div class="small"><a href="{{ p.url }}" target="_blank">Open source ↗</a></div>
              {% endif %}
            </td>
            <td>{% if p.score is not none %}{{ '%.2f'|format(p.score) }}{% else %}<span class="small">—</span>{% endif %}</td>
            <td>{% if p.hidden %}Yes{% else %}No{% endif %}</td>
            <td class="small">{{ p.created_at.strftime("%Y-%m-%d %H:%M") }}</td>
            <td>
              <form method="post" action="{{ url_for('post_toggle_hide', post_id=p.id) }}">
                <button class="btn {% if p.hidden %}danger{% endif %}" type="submit">
                  {% if p.hidden %}Unhide{% else %}Hide{% endif %}
                </button>
              </form>
            </td>
          </tr>
        {% else %}
          <tr><td colspan="5" class="small">No posts yet. Click “Run”.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
""" + BASE_FOOT

TPL_POST = BASE_HEAD + """
<div class="grid">
  <div class="col-8">
    <div class="card">
      <div class="badge">Post</div>
      <h2 style="margin:8px 0 0 0;">{{ post.title }}</h2>
      <div class="small" style="margin-top:6px;">From: {% if post.url %}<a href="{{ post.url }}" target="_blank">{{ post.url }}</a>{% else %}Unknown{% endif %}</div>
      <hr class="sep">
      <div style="white-space:pre-wrap; line-height:1.5;">{{ post.content or '' }}</div>
    </div>
  </div>
  <div class="col-4">
    <div class="card">
      <div style="font-weight:800;">Meta</div>
      <div class="small">Scrape: <a href="{{ url_for('scrape_detail', scrape_id=scrape.id) }}">{{ scrape.name }}</a></div>
      <div class="small">Created: {{ post.created_at.strftime("%Y-%m-%d %H:%M") }}</div>
      <div style="height:8px"></div>
      <div class="small">AI Score</div>
      <div class="kpi" style="font-size:22px;">
        {% if post.score is not none %}{{ '%.2f'|format(post.score) }}{% else %}—{% endif %}
      </div>
      <div style="height:12px"></div>
      <form method="post" action="{{ url_for('post_toggle_hide', post_id=post.id) }}">
        <button class="btn {% if post.hidden %}danger{% endif %}" type="submit">
          {% if post.hidden %}Unhide{% else %}Hide{% endif %}
        </button>
        <a class="btn ghost" href="{{ url_for('scrape_detail', scrape_id=scrape.id) }}">Back</a>
      </form>
    </div>
  </div>
</div>
""" + BASE_FOOT

TPL_ADMIN = BASE_HEAD + """
<div class="card">
  <div style="display:flex; align-items:center; justify-content:space-between;">
    <div style="font-weight:800;">Customers</div>
    <a class="btn ghost" href="{{ url_for('dashboard') }}">Back</a>
  </div>
  <table class="table" style="margin-top:10px;">
    <thead><tr>
      <th>Org</th><th>Tier</th><th>Users</th><th>Scrapes</th><th>Set Tier</th>
    </tr></thead>
    <tbody>
      {% for o in orgs %}
        <tr>
          <td>{{ o.name }}</td>
          <td>{{ o.tier|capitalize }}</td>
          <td>{{ o.users|length }} / {{ limits[o.tier][0] }}</td>
          <td>{{ o.scrapes|length }}</td>
          <td>
            <form method="post" action="{{ url_for('admin_set_tier', org_id=o.id) }}">
              <select class="input" name="tier" style="width:auto; display:inline-block;">
                {% for t in limits.keys() %}
                  <option value="{{ t }}" {% if t==o.tier %}selected{% endif %}>{{ t|capitalize }}</option>
                {% endfor %}
              </select>
              <button class="btn" type="submit">Update</button>
            </form>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="5" class="small">No orgs yet.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
""" + BASE_FOOT

###############################################################################
# Jinja globals
###############################################################################
@app.context_processor
def inject_globals():
    return {
        "base_css": BASE_CSS
    }

###############################################################################
# Main
###############################################################################
if __name__ == "__main__":
    # Local dev: flask run via python app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
