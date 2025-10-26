# app.py — SignalBot (SaaS-ready Reddit intent signals)

from flask import Flask, request, redirect, url_for, session, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text, func, case
import praw, json, requests, os, logging

# ------------ Flask & DB ------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-' + os.urandom(16).hex())
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ------------ Logging ------------
logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
log = logging.getLogger("signalbot")

# ------------ Env Config ------------
# Integrations
GHL_API_KEY      = os.environ.get('GHL_API_KEY', '')
GHL_LOCATION_ID  = os.environ.get('GHL_LOCATION_ID', '')

# OpenAI / AI scoring
OPENAI_API_KEY   = os.environ.get('OPENAI_API_KEY', '')
AI_MODEL         = os.environ.get('AI_MODEL', 'gpt-4o-mini')
AI_MIN_SCORE     = int(os.environ.get('AI_MIN_SCORE', '6'))
AI_TIMEOUT_SEC     = float(os.environ.get('AI_TIMEOUT_SEC', '12'))  # per-call timeout
AI_MAX_CONCURRENCY = int(os.environ.get('AI_MAX_CONCURRENCY', '5')) # not used in this serial build; safe to keep

# Admin / utilities
ENABLE_DB_ADMIN  = os.environ.get('ENABLE_DB_ADMIN', '0') == '1'
TASKS_TOKEN      = os.environ.get('TASKS_TOKEN', '')
SUPERADMIN_USERNAME = os.environ.get('SUPERADMIN_USERNAME', '')

# Reusable HTTP session for OpenAI (connection pooling)
OPENAI_SESSION = requests.Session()
OPENAI_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=2)
OPENAI_SESSION.mount("https://", OPENAI_ADAPTER)

# ------------ Models ------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # agency relationships
    parent_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    parent = db.relationship('User', remote_side=[id])
    role = db.Column(db.String(20), default='owner')  # owner/admin/member (simple RBAC)


class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    max_users = db.Column(db.Integer, default=1)         # seats under the account root
    max_scrapes = db.Column(db.Integer, default=5)       # active scrapes limit (account root pooled)
    ai_posts_quota = db.Column(db.Integer, default=3000) # posts/month scored by AI (account root pooled)
    price = db.Column(db.Float, default=0.0)
    is_agency = db.Column(db.Boolean, default=False)
    price_id = db.Column(db.String(100))  # Stripe price id (future)


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # account root owner
    plan_id = db.Column(db.Integer, db.ForeignKey('plan.id'), nullable=False)
    start_date = db.Column(db.DateTime, default=datetime.utcnow)
    end_date = db.Column(db.DateTime)
    active = db.Column(db.Boolean, default=True)
    current_period_end = db.Column(db.DateTime)
    plan = db.relationship('Plan', backref='subscriptions')


class Scrape(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    subreddits = db.Column(db.Text, nullable=False)
    keywords = db.Column(db.Text, nullable=False)
    limit = db.Column(db.Integer, default=50)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_run = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    ai_guidance = db.Column(db.Text)
    ai_enabled  = db.Column(db.Boolean, default=True)


class Result(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scrape_id = db.Column(db.Integer, db.ForeignKey('scrape.id'), nullable=False)
    title = db.Column(db.Text, nullable=False)
    author = db.Column(db.String(100))
    subreddit = db.Column(db.String(100))
    url = db.Column(db.Text)
    score = db.Column(db.Integer)  # Reddit upvotes
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    keywords_found = db.Column(db.Text)
    ai_score = db.Column(db.Integer)       # 1..10 (nullable when AI disabled)
    ai_reasoning = db.Column(db.Text)
    reddit_post_id = db.Column(db.String(50))
    is_hidden = db.Column(db.Boolean, default=False)


class UsageMonth(db.Model):
    """Per-account-root monthly usage counters."""
    id = db.Column(db.Integer, primary_key=True)
    root_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    ai_posts_count = db.Column(db.Integer, default=0)        # posts scored by AI this month
    posts_processed_count = db.Column(db.Integer, default=0) # total matched processed (optional)

    __table_args__ = (
        db.UniqueConstraint('root_user_id', 'year', 'month', name='uniq_usage_root_month'),
    )

# --- Auto-migration/seed ---
def ensure_db_upgrade():
    try:
        with app.app_context():
            db.create_all()
            # result table upgrades
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS ai_score SMALLINT;"))
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS ai_reasoning TEXT;"))
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS reddit_post_id TEXT;"))
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN;"))
            db.session.execute(text("UPDATE result SET is_hidden = FALSE WHERE is_hidden IS NULL;"))
            db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uniq_result_scrape_post ON result (scrape_id, reddit_post_id);"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_result_scrape_hidden_created ON result (scrape_id, is_hidden, created_at DESC);"))
            # scrape table upgrades
            db.session.execute(text("ALTER TABLE scrape ADD COLUMN IF NOT EXISTS ai_guidance TEXT;"))
            db.session.execute(text("ALTER TABLE scrape ADD COLUMN IF NOT EXISTS ai_enabled BOOLEAN;"))
            db.session.execute(text("UPDATE scrape SET ai_enabled = TRUE WHERE ai_enabled IS NULL;"))
            # user upgrades
            db.session.execute(text("ALTER TABLE user ADD COLUMN IF NOT EXISTS parent_id INTEGER;"))
            db.session.execute(text("ALTER TABLE user ADD COLUMN IF NOT EXISTS role VARCHAR(20);"))
            db.session.execute(text("UPDATE user SET role = COALESCE(role, 'owner');"))
            db.session.commit()

            # Seed plans
            if not Plan.query.first():
                db.session.add_all([
                    Plan(name='Starter', max_users=1,  max_scrapes=5,   ai_posts_quota=3000,   price=29.0,  is_agency=False),
                    Plan(name='Pro',     max_users=3,  max_scrapes=10,  ai_posts_quota=10000,  price=79.0,  is_agency=False),
                    Plan(name='Agency',  max_users=50, max_scrapes=200, ai_posts_quota=100000, price=299.0, is_agency=True),
                ])
                db.session.commit()
                log.info("Seeded plans (Starter/Pro/Agency).")

            print("✅ DB upgrade ensured.")
    except Exception as e:
        db.session.rollback()
        print("⚠️ DB upgrade error:", e)

ensure_db_upgrade()

# ------------ Helpers ------------
def get_reddit_instance():
    return praw.Reddit(
        client_id=os.environ.get('REDDIT_CLIENT_ID', ''),
        client_secret=os.environ.get('REDDIT_CLIENT_SECRET', ''),
        user_agent="signalbot"
    )

def login_required(f):
    @wraps(f)
    def inner(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return inner

def admin_required(f):
    @wraps(f)
    def inner(*args, **kwargs):
        uid = session.get('user_id')
        user = User.query.get(uid) if uid else None
        if not user or not user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return inner

def get_account_root(user_id: int) -> User:
    """Return the account root user (agency owner or self)."""
    u = User.query.get(user_id)
    if not u:
        return None
    return u.parent if u.parent_id else u

def get_active_subscription(root_user_id: int):
    return Subscription.query.filter_by(user_id=root_user_id, active=True).first()

def get_plan_for_user(user_id: int):
    root = get_account_root(user_id)
    sub = get_active_subscription(root.id) if root else None
    return sub.plan if sub else None

def get_or_create_usage_month(root_user_id: int) -> UsageMonth:
    now = datetime.utcnow()
    y, m = now.year, now.month
    usage = UsageMonth.query.filter_by(root_user_id=root_user_id, year=y, month=m).first()
    if not usage:
        usage = UsageMonth(root_user_id=root_user_id, year=y, month=m, ai_posts_count=0, posts_processed_count=0)
        db.session.add(usage); db.session.commit()
    return usage

def current_ai_usage(user_id: int):
    root = get_account_root(user_id)
    plan = get_plan_for_user(user_id)
    usage = get_or_create_usage_month(root.id) if root else None
    return (usage.ai_posts_count if usage else 0, plan.ai_posts_quota if plan else 0)

def count_scrapes_for_account(user_id: int) -> int:
    """Count all scrapes under the same account root (owner + sub-users)."""
    root = get_account_root(user_id)
    user_ids = [root.id] + [u.id for u in User.query.filter_by(parent_id=root.id).all()]
    return Scrape.query.filter(Scrape.user_id.in_(user_ids)).count()

def can_create_scrape(user_id: int):
    plan = get_plan_for_user(user_id)
    if not plan:
        return False, "No active subscription"
    current = count_scrapes_for_account(user_id)
    if current >= plan.max_scrapes:
        return False, f"Scrape limit reached ({plan.max_scrapes}). Upgrade to add more."
    return True, None

def can_add_user(user_id: int):
    root = get_account_root(user_id)
    plan = get_plan_for_user(user_id)
    if not plan or not plan.is_agency:
        return False, "Your plan does not allow sub-users"
    current_users = User.query.filter_by(parent_id=root.id).count()
    if current_users >= plan.max_users:
        return False, f"User limit reached ({plan.max_users})"
    return True, None

def send_to_ghl(result_data):
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        return False
    try:
        headers = {'Authorization': f'Bearer {GHL_API_KEY}', 'Content-Type': 'application/json'}
        tags = result_data.get('keywords_found', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',') if t.strip()]
        payload = {
            'locationId': GHL_LOCATION_ID,
            'firstName': result_data.get('author', 'Reddit User'),
            'source': 'SignalBot',
            'tags': tags,
            'customFields': {
                'reddit_post_url': result_data.get('url', ''),
                'reddit_title': result_data.get('title', ''),
                'subreddit': result_data.get('subreddit', '')
            }
        }
        resp = requests.post('https://rest.gohighlevel.com/v1/contacts/', headers=headers, json=payload, timeout=15)
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("GHL send failed: %s %s", resp.status_code, resp.text[:300])
        return ok
    except Exception as e:
        log.exception("GHL error: %s", e)
        return False

def ai_score_post(title: str, body: str, keywords: list[str], guidance: str | None = None) -> tuple[int, str]:
    """Return (score, reason). If unavailable, (0, 'AI unavailable'). Counts toward AI posts quota."""
    if not OPENAI_API_KEY:
        return 0, "AI disabled (missing OPENAI_API_KEY)"
    try:
        guidance_text = (guidance or "").strip()
        prompt = f"""
You are scoring Reddit posts for lead intent. A high-quality lead means the author is asking for help, hiring, seeking services, requesting recommendations, or describing a solvable pain where outreach is welcome.

If GUIDANCE is provided, bias your judgment toward that use-case and weigh relevance accordingly.

GUIDANCE (optional):
{guidance_text if guidance_text else "(none)"}

Score 1-10, JSON: {{ "score": int, "reason": string<=240 }}
Title: {title}
Body: {(body or '')[:1500]}
Matched keywords: {", ".join(keywords)}
        """.strip()

        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "You are a concise lead-qualification assistant."},
                {"role": "user",   "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }
        resp = OPENAI_SESSION.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers, json=payload, timeout=AI_TIMEOUT_SEC
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        j = json.loads(content)
        score = int(j.get("score", 0))
        reason = str(j.get("reason", ""))[:1000]
        score = max(1, min(10, score))
        return score, reason
    except Exception as e:
        log.warning("AI scoring error: %s", e)
        return 0, "AI unavailable"

def badge_for_status(is_active: bool) -> str:
    return '<span class="badge text-bg-success">Active</span>' if is_active else '<span class="badge text-bg-secondary">Paused</span>'

def score_badge(score) -> str:
    if score is None:
        return '<span class="badge text-bg-secondary">—</span>'
    try:
        s = int(score)
    except:
        return '<span class="badge text-bg-secondary">—</span>'
    if s >= 9:  return f'<span class="badge text-bg-success">{s}</span>'
    if s >= 7:  return f'<span class="badge text-bg-warning">{s}</span>'
    return f'<span class="badge text-bg-danger">{s}</span>'

# ---------- Metrics helpers ----------
def kpis_for_user(user_id: int, days: int = 7, min_score: int = None):
    min_score = AI_MIN_SCORE if min_score is None else min_score
    since = datetime.utcnow() - timedelta(days=days)

    # account pool (owner + subs)
    root = get_account_root(user_id)
    pool_ids = [root.id] + [u.id for u in User.query.filter_by(parent_id=root.id).all()]

    total_scrapes = db.session.query(func.count(Scrape.id)).filter(Scrape.user_id.in_(pool_ids)).scalar() or 0
    active_scrapes = db.session.query(func.count(Scrape.id)).filter(Scrape.user_id.in_(pool_ids), Scrape.is_active == True).scalar() or 0

    res_base = db.session.query(Result.id).join(Scrape, Result.scrape_id == Scrape.id)\
        .filter(Scrape.user_id.in_(pool_ids), Result.created_at >= since)\
        .filter((Result.is_hidden == False) | (Result.is_hidden == None))
    total_results = res_base.count()

    qualified = db.session.query(Result.id).join(Scrape, Result.scrape_id == Scrape.id)\
        .filter(Scrape.user_id.in_(pool_ids), Result.created_at >= since)\
        .filter((Result.is_hidden == False) | (Result.is_hidden == None))\
        .filter(Result.ai_score >= min_score).count()

    return {"total_scrapes": total_scrapes, "active_scrapes": active_scrapes,
            "total_results": total_results, "qualified": qualified, "since": since}

def daily_counts(user_id: int, days: int = 7):
    since = datetime.utcnow() - timedelta(days=days-1)
    root = get_account_root(user_id)
    pool_ids = [root.id] + [u.id for u in User.query.filter_by(parent_id=root.id).all()]

    rows = db.session.query(
        func.date_trunc('day', Result.created_at).label('d'),
        func.count(Result.id),
        func.sum(case((Result.ai_score >= AI_MIN_SCORE, 1), else_=0))
    ).join(Scrape, Result.scrape_id == Scrape.id)\
     .filter(Scrape.user_id.in_(pool_ids), Result.created_at >= since)\
     .filter((Result.is_hidden == False) | (Result.is_hidden == None))\
     .group_by('d').order_by('d').all()

    by_day = {r[0].date(): (int(r[1]), int(r[2] or 0)) for r in rows}
    labels, totals, quals = [], [], []
    for i in range(days):
        day = (since.date() + timedelta(days=i))
        t, q = by_day.get(day, (0, 0))
        labels.append(day.strftime('%b %d')); totals.append(t); quals.append(q)
    return labels, totals, quals

# ---------- Theming + SVG Wolf-Bot Logo ----------
BOOTSTRAP_SHELL = """
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
<meta name="viewport" content="width=device-width, initial-scale=1" />

<style>
  /* Brand Palette */
  :root {
    --brand-primary: #ffb000;   /* amber */
    --brand-navy:    #04004f;   /* deep navy */
    --brand-bg-dark: #0f1028;   /* charcoal */
    --brand-cyan:    #04a8d2;   /* cyan accent */
    --brand-purple:  #8E24C1;   /* purple accent */

    /* Semantic tokens (light defaults; overridden for dark below) */
    --bg: #ffffff;
    --panel: #ffffff;
    --muted: #475569;
    --text: #0f172a;
    --border: #e2e8f0;
    --table-head: #f1f5f9;

    --btn-outline: var(--text);
    --link: var(--brand-navy);
    --card-shadow: rgba(0,0,0,.08);
  }

  [data-theme="dark"]{
    --bg: var(--brand-bg-dark);
    --panel: #0e141b;
    --muted: #97a6b8;
    --text: #e5eef7;
    --border: #1c2733;
    --table-head: #121a23;

    --btn-outline: #e5eef7;
    --link: #8ab4ff;
    --card-shadow: rgba(0,0,0,.35);
  }

  html, body{height:100%}
  body{background:var(--bg); color:var(--text)}
  a{color:var(--link)}

  .navbar{background:var(--panel); border-bottom:1px solid var(--border)}
  .card{background:var(--panel); border:1px solid var(--border); box-shadow: 0 6px 20px var(--card-shadow)}
  .muted{color:var(--muted)}

  .table{
    --bs-table-color: var(--text);
    --bs-table-bg: transparent;
    --bs-table-border-color: var(--border);
  }
  .table thead{background:var(--table-head)}

  .badge.text-bg-success{background:#16a34a!important}
  .badge.text-bg-warning{background:#f59e0b!important; color:#0b0f14}
  .badge.text-bg-danger{background:#ef4444!important}

  .btn-primary{
    background:var(--brand-primary);
    border-color:var(--brand-primary);
    color:#1f2937;
  }
  .btn-primary:hover{filter:brightness(0.95)}
  .btn-outline-light, .btn-outline-dark, .btn-outline-primary, .btn-outline-secondary,
  .btn-outline-warning, .btn-outline-danger, .btn-outline-info{
    border-color: var(--btn-outline);
    color: var(--btn-outline);
  }

  .logo-wrap{display:flex; align-items:center; gap:.5rem; color:var(--brand-primary); text-decoration:none}
  .logo-text{font-weight:600}
  .wolfbot{width:24px; height:24px}
</style>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" defer></script>
"""

# Simple inline SVG "wolf-bot": circle head + ears + eyes + small chin-bot line
WOLFBOT_SVG = """
<svg class="wolfbot" viewBox="0 0 24 24" aria-hidden="true">
  <defs>
    <linearGradient id="wb" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0%" stop-color="#ffb000"/>
      <stop offset="100%" stop-color="#8E24C1"/>
    </linearGradient>
  </defs>
  <!-- head -->
  <circle cx="12" cy="12" r="9" fill="url(#wb)" opacity="0.18" />
  <!-- ears -->
  <path d="M6 8 L9 6 L9 9 Z" fill="#ffb000"/><path d="M18 8 L15 6 L15 9 Z" fill="#ffb000"/>
  <!-- face mask -->
  <path d="M7 12 a5 5 0 0 0 10 0 v0" fill="#04004f" opacity="0.25"/>
  <!-- eyes -->
  <rect x="8.5" y="11" width="2.5" height="2" rx="0.4" fill="#e6f4ff"/>
  <rect x="13" y="11" width="2.5" height="2" rx="0.4" fill="#e6f4ff"/>
  <!-- nose/robot chin -->
  <rect x="11" y="15.2" width="2" height="1.2" rx="0.3" fill="#04a8d2"/>
</svg>
"""

def page_wrap(inner_html: str, page_title: str = "") -> str:
    title = f"{page_title} – SignalBot" if page_title else "SignalBot"
    theme = session.get("theme", "dark")
    toggle_label = "Light Mode" if theme == "dark" else "Dark Mode"

    # usage widget (AI used/quota) for current account
    usage_html = ''
    if session.get('user_id'):
        used, quota = current_ai_usage(session['user_id'])
        pct = 0 if not quota else min(100, int(100 * used / quota))
        usage_html = f'''
        <div class="d-none d-md-flex align-items-center me-3" title="AI posts scored this month">
          <div class="me-2 muted small">AI {used}/{quota}</div>
          <div style="width:120px; height:8px; background:var(--border); border-radius:4px; overflow:hidden">
            <div style="width:{pct}%; height:100%; background:var(--brand-cyan)"></div>
          </div>
        </div>
        '''

    # Admin link logic
    admin_link = ''
    if session.get("user_id"):
        me = User.query.get(session["user_id"])
        if me and me.is_admin:
            admin_link = '<li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/admin">Admin</a></li>'

    # Agency invite visibility
    can_invite_html = ''
    if session.get('user_id'):
        plan = get_plan_for_user(session['user_id'])
        if plan and plan.is_agency:
            can_invite_html = '<li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/agency/invite">Invite User</a></li>'

    return f"""{BOOTSTRAP_SHELL}
<title>{title}</title>
<body data-theme="{theme}">
<nav class="navbar navbar-expand-lg">
  <div class="container-fluid">
    <a class="logo-wrap" href="/dashboard" title="SignalBot">
      {WOLFBOT_SVG}
      <span class="logo-text">SignalBot</span>
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav" aria-controls="nav" aria-expanded="false">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div id="nav" class="collapse navbar-collapse">
      <ul class="navbar-nav me-auto mb-2 mb-lg-0">
        <li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/create-scrape">New Scrape</a></li>
        {can_invite_html}
        {admin_link}
      </ul>
      {usage_html}
      <a class="btn btn-sm btn-outline-light me-2" href="/theme/toggle"><i class="bi bi-moon-stars"></i> {toggle_label}</a>
      <span class="muted me-3">Hi, {session.get("username","guest")}</span>
      {'<a class="btn btn-outline-light btn-sm" href="/logout">Logout</a>' if session.get('user_id') else '<a class="btn btn-outline-light btn-sm" href="/login">Login</a>'}
    </div>
  </div>
</nav>

<div class="container py-4">
  <div class="mb-3 muted">Find buying signals before your competitors do.</div>
  {inner_html}
</div>
</body>
"""

@app.route('/theme/<mode>')
def set_theme(mode):
    cur = session.get("theme", "dark")
    if mode == "toggle":
        session["theme"] = "light" if cur == "dark" else "dark"
    elif mode in ("dark", "light"):
        session["theme"] = mode
    return redirect(request.referrer or url_for('dashboard'))

# ------------ Scraper core ------------
def run_scrape(scrape_id):
    with app.app_context():
        scrape = Scrape.query.get(scrape_id)
        if not scrape or not scrape.is_active:
            return
        try:
            reddit = get_reddit_instance()
            subreddit_list = [s.strip() for s in scrape.subreddits.split(',') if s.strip()]
            keyword_list  = [k.strip().lower() for k in scrape.keywords.split(',') if k.strip()]

            results_count = 0
            for subreddit_name in subreddit_list:
                try:
                    subreddit = reddit.subreddit(subreddit_name)
                    for post in subreddit.new(limit=scrape.limit):
                        title = post.title or ""
                        body  = getattr(post, "selftext", "") or ""
                        text_all = f"{title} {body}".lower()
                        found = [kw for kw in keyword_list if kw in text_all]
                        if not found:
                            continue

                        url = f"https://reddit.com{post.permalink}"
                        post_id = getattr(post, "id", None) or url

                        # dedupe per-scrape/post
                        if Result.query.filter_by(scrape_id=scrape.id, reddit_post_id=post_id).first():
                            continue

                        # Track total processed
                        root = get_account_root(scrape.user_id)
                        usage = get_or_create_usage_month(root.id)
                        usage.posts_processed_count = (usage.posts_processed_count or 0) + 1

                        # --- AI scoring with quota enforcement ---
                        ai_score_val, ai_reason = None, "AI disabled for this scrape"
                        if scrape.ai_enabled:
                            plan = get_plan_for_user(scrape.user_id)
                            if plan:
                                if usage.ai_posts_count < plan.ai_posts_quota:
                                    ai_score_val, ai_reason = ai_score_post(title, body, found, guidance=scrape.ai_guidance)
                                    usage.ai_posts_count += 1   # count toward "AI posts"
                                else:
                                    ai_score_val, ai_reason = None, f"AI quota exceeded ({usage.ai_posts_count}/{plan.ai_posts_quota})"
                            else:
                                ai_score_val, ai_reason = None, "No active subscription"

                        result = Result(
                            scrape_id=scrape.id,
                            title=title,
                            author=str(post.author),
                            subreddit=subreddit_name,
                            url=url,
                            score=post.score,
                            keywords_found=','.join(found),
                            ai_score=ai_score_val,
                            ai_reasoning=ai_reason,
                            reddit_post_id=post_id,
                            is_hidden=False
                        )
                        db.session.add(result)
                        results_count += 1

                        # Auto-send only when AI is ON and meets threshold
                        if scrape.ai_enabled and (ai_score_val or 0) >= AI_MIN_SCORE:
                            send_to_ghl({
                                'author': str(post.author),
                                'url': url,
                                'title': title,
                                'subreddit': subreddit_name,
                                'keywords_found': found
                            })

                except Exception as e:
                    log.exception("Error scraping r/%s: %s", subreddit_name, e)
                    continue

            scrape.last_run = datetime.utcnow()
            db.session.commit()
            log.info("Scrape %s completed. New results: %s", scrape.id, results_count)
        except Exception as e:
            log.exception("Error running scrape %s: %s", scrape_id, e)
            db.session.rollback()

def run_all_scrapes():
    with app.app_context():
        for s in Scrape.query.filter_by(is_active=True).all():
            run_scrape(s.id)

# ------------ Auth & basic pages ------------
@app.route('/init-db')
def init_db():
    if not ENABLE_DB_ADMIN:
        abort(404)
    try:
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com',
                         password_hash=generate_password_hash('admin123'), is_admin=True)
            db.session.add(admin); db.session.commit()
        return "Database initialized!"
    except Exception as e:
        return f"Error: {e}"

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    html = '''
      <h1 class="mb-2">SignalBot</h1>
      <p class="muted">Reddit intent signals routed to your CRM — automatically.</p>
      <a class="btn btn-primary me-2" href="/login">Login</a>
      <a class="btn btn-outline-light" href="/register">Register</a>
    '''
    return page_wrap(html, "Welcome")

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        u = request.form['username'].strip()
        e = request.form['email'].strip()
        p = request.form['password']
        if User.query.filter_by(username=u).first():
            flash('Username already exists'); return redirect(url_for('register'))
        user = User(username=u, email=e, password_hash=generate_password_hash(p), role='owner')
        db.session.add(user); db.session.commit()
        # Auto-subscribe to Starter
        starter = Plan.query.filter_by(name='Starter').first()
        if starter:
            sub = Subscription(user_id=user.id, plan_id=starter.id, active=True)
            db.session.add(sub); db.session.commit()
        flash('Account created! You are on the Starter plan.')
        return redirect(url_for('login'))
    html = '''
      <h2 class="mb-3">Create your SignalBot account</h2>
      <form method="POST" class="card card-body" style="max-width:520px">
        <input class="form-control mb-2" type="text" name="username" placeholder="Username" required>
        <input class="form-control mb-2" type="email" name="email" placeholder="Email" required>
        <input class="form-control mb-3" type="password" name="password" placeholder="Password" required>
        <button class="btn btn-primary" type="submit">Sign Up</button>
      </form>
      <a class="d-inline-block mt-3" href="/">Back</a>
    '''
    return page_wrap(html, "Register")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form['username'].strip(); p = request.form['password']
        user = User.query.filter_by(username=u).first()
        if user and check_password_hash(user.password_hash, p):
            session['user_id'] = user.id; session['username'] = user.username
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    html = '''
      <h2 class="mb-3">Login to SignalBot</h2>
      <form method="POST" class="card card-body" style="max-width:520px">
        <input class="form-control mb-2" type="text" name="username" placeholder="Username" required>
        <input class="form-control mb-3" type="password" name="password" placeholder="Password" required>
        <button class="btn btn-primary" type="submit">Login</button>
      </form>
      <a class="d-inline-block mt-3" href="/register">Sign Up</a>
    '''
    return page_wrap(html, "Login")

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ------------ Dashboard ------------
@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    kpis = kpis_for_user(user_id, days=7)
    labels, totals, quals = daily_counts(user_id, days=7)

    # Plan card
    plan = get_plan_for_user(user_id)
    plan_html = f"""
    <div class="alert alert-secondary mt-3">
      <strong>Current Plan:</strong> {plan.name if plan else 'None'}
      <div class="small mt-1">{(plan.max_users if plan else 0)} users · {(plan.max_scrapes if plan else 0)} scrapes · {(plan.ai_posts_quota if plan else 0)} AI posts/mo</div>
    </div>
    """

    # account pool (owner + subs)
    root = get_account_root(user_id)
    pool_ids = [root.id] + [u.id for u in User.query.filter_by(parent_id=root.id).all()]

    # Recent results
    recent = db.session.query(Result, Scrape).join(Scrape, Result.scrape_id == Scrape.id)\
        .filter(Scrape.user_id.in_(pool_ids))\
        .filter((Result.is_hidden == False) | (Result.is_hidden == None))\
        .order_by(Result.created_at.desc()).limit(10).all()

    cards = f"""
    <div class="row g-3">
      <div class="col-6 col-md-3">
        <div class="card p-3">
          <div class="muted">Scrapes</div>
          <div class="fs-3 fw-bold">{kpis['total_scrapes']}</div>
          <div class="muted"><i class="bi bi-circle-fill text-success me-1"></i>{kpis['active_scrapes']} active</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card p-3">
          <div class="muted">Results (7d)</div>
          <div class="fs-3 fw-bold">{kpis['total_results']}</div>
          <div class="muted">since {kpis['since'].strftime('%b %d')}</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card p-3">
          <div class="muted">Qualified ≥ {AI_MIN_SCORE} (7d)</div>
          <div class="fs-3 fw-bold">{kpis['qualified']}</div>
          <div class="muted">auto-sent to GHL</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card p-3">
          <div class="muted">AI Threshold</div>
          <div class="fs-3 fw-bold">{AI_MIN_SCORE}</div>
          <div class="muted">set via AI_MIN_SCORE</div>
        </div>
      </div>
    </div>
    """

    chart = f"""
    <div class="card mt-4 p-3">
      <div class="d-flex justify-content-between align-items-center">
        <h5 class="mb-0">Signals over last 7 days</h5>
      </div>
      <canvas id="leadsChart" height="120" class="mt-3"></canvas>
    </div>
    <script>
    document.addEventListener('DOMContentLoaded', function() {{
      const ctx = document.getElementById('leadsChart');
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: {json.dumps(labels)},
          datasets: [
            {{ label: 'Total matches', data: {json.dumps(totals)}, borderWidth: 2, tension: .25 }},
            {{ label: 'Qualified (AI)', data: {json.dumps(quals)}, borderWidth: 2, borderDash: [5,5], tension: .25 }}
          ]
        }},
        options: {{
          plugins: {{ legend: {{ labels: {{ color: getComputedStyle(document.body).getPropertyValue('--text').trim() }} }} }},
          scales: {{
            x: {{ ticks: {{ color: getComputedStyle(document.body).getPropertyValue('--muted').trim() }},
                  grid:  {{ color: getComputedStyle(document.body).getPropertyValue('--border').trim() }} }},
            y: {{ ticks: {{ color: getComputedStyle(document.body).getPropertyValue('--muted').trim() }},
                  grid:  {{ color: getComputedStyle(document.body).getPropertyValue('--border').trim() }}, beginAtZero: true }}
          }}
        }}
      }});
    }});
    </script>
    """

    # Recent results table
    recent_rows = ""
    for r, s in recent:
        badge = score_badge(r.ai_score)
        actions = [f'<a class="btn btn-sm btn-outline-primary" target="_blank" href="{r.url}">Open</a>']
        if (r.ai_score or 0) < AI_MIN_SCORE:
            actions.append(f'<a class="btn btn-sm btn-outline-success" href="/send-to-ghl/{r.id}">Send</a>')
        if r.is_hidden:
            actions.append(f'<a class="btn btn-sm btn-outline-secondary" href="/result/{r.id}/unhide">Unhide</a>')
        else:
            actions.append(f'<a class="btn btn-sm btn-outline-danger" href="/result/{r.id}/hide">Hide</a>')
        recent_rows += f"""
        <tr>
          <td>{r.created_at.strftime('%Y-%m-%d %H:%M')}</td>
          <td><span class="muted">r/</span>{r.subreddit}</td>
          <td>{(r.title or '')[:80]}{'...' if (r.title and len(r.title)>80) else ''}</td>
          <td>{badge}</td>
          <td class="text-nowrap">{' '.join(actions)}</td>
        </tr>
        """

    recent_table = f"""
    <div class="card mt-4 p-3">
      <div class="d-flex justify-content-between align-items-center">
        <h5 class="mb-0">Recent signals</h5>
      </div>
      <div class="table-responsive mt-2">
        <table class="table table-sm align-middle">
          <thead>
            <tr><th>Date</th><th>Subreddit</th><th>Title</th><th>AI</th><th>Actions</th></tr>
          </thead>
          <tbody>{recent_rows or '<tr><td colspan="5" class="text-center py-4">No recent results.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    """

    # Scrapes table (pooled)
    scrapes = Scrape.query.filter(Scrape.user_id.in_(pool_ids)).order_by(Scrape.created_at.desc()).all()
    scrape_rows = ""
    for s in scrapes:
        last_run = s.last_run.strftime('%Y-%m-%d %H:%M') if s.last_run else 'Never'
        status_html = badge_for_status(s.is_active)
        result_count = db.session.query(func.count(Result.id))\
            .filter(Result.scrape_id == s.id)\
            .filter((Result.is_hidden == False) | (Result.is_hidden == None)).scalar() or 0
        ai_col = '<span class="badge text-bg-success">AI</span>' if (s.ai_enabled is None or s.ai_enabled) \
                 else '<span class="badge text-bg-secondary">No AI</span>'
        scrape_rows += f"""
        <tr>
          <td>{s.name}</td>
          <td><code>{s.subreddits}</code></td>
          <td><code>{s.keywords}</code></td>
          <td>{ai_col}</td>
          <td>{status_html}</td>
          <td>{last_run}</td>
          <td><a href="/results/{s.id}">{result_count} results</a></td>
          <td class="text-nowrap">
            <a class="btn btn-sm btn-outline-primary" href="/results/{s.id}">View Results</a>
            <a class="btn btn-sm btn-outline-info" href="/edit-scrape/{s.id}">Edit</a>
            <a class="btn btn-sm btn-outline-secondary" href="/run-scrape/{s.id}">Run</a>
            <a class="btn btn-sm btn-outline-warning" href="/toggle-scrape/{s.id}">Toggle</a>
            <a class="btn btn-sm btn-outline-danger" href="/delete-scrape/{s.id}" onclick="return confirm('Delete this scrape?')">Delete</a>
          </td>
        </tr>
        """

    scrapes_table = f"""
    <div class="card mt-4 p-3">
      <div class="d-flex justify-content-between align-items-center">
        <h5 class="mb-0">Your scrapes</h5>
        <a class="btn btn-sm btn-outline-light" href="/create-scrape"><i class="bi bi-plus"></i> New Scrape</a>
      </div>
      <div class="table-responsive mt-2">
        <table class="table table-sm align-middle">
          <thead>
            <tr>
              <th>Name</th><th>Subreddits</th><th>Keywords</th><th>AI</th><th>Status</th><th>Last Run</th><th>Results</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>{scrape_rows or '<tr><td colspan="8" class="text-center py-4">No scrapes yet.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    """

    return page_wrap(cards + plan_html + chart + recent_table + scrapes_table, "Dashboard")

# ------------ Create/Edit Scrape ------------
@app.route('/create-scrape', methods=['GET', 'POST'])
@login_required
def create_scrape():
    if request.method == 'POST':
        ok, reason = can_create_scrape(session['user_id'])
        if not ok:
            flash(reason); return redirect(url_for('dashboard'))

        scrape = Scrape(
            name=request.form['name'],
            subreddits=request.form['subreddits'],
            keywords=request.form['keywords'],
            limit=int(request.form.get('limit', 50)),
            user_id=session['user_id'],
            ai_guidance=request.form.get('ai_guidance', ''),
            ai_enabled=(request.form.get('ai_enabled', 'on') == 'on')
        )
        db.session.add(scrape); db.session.commit()
        flash('Scrape created! It will run automatically every hour.')
        return redirect(url_for('dashboard'))

    # show remaining scrapes
    plan = get_plan_for_user(session['user_id'])
    current = count_scrapes_for_account(session['user_id'])
    remain = max(0, (plan.max_scrapes if plan else 0) - current)

    html = f'''
      <h2 class="mb-3">Create New Scrape</h2>
      <div class="alert alert-secondary">You can create <b>{remain}</b> more scrapes on your current plan.</div>
      <form method="POST" class="card card-body" style="max-width:720px">
        <label class="form-label"><b>Name</b></label>
        <input class="form-control mb-3" type="text" name="name" required>

        <label class="form-label"><b>Subreddits (comma-separated)</b></label>
        <input class="form-control mb-3" type="text" name="subreddits" placeholder="bookkeeping,smallbusiness,accounting" required>

        <label class="form-label"><b>Keywords (comma-separated)</b></label>
        <input class="form-control mb-3" type="text" name="keywords" placeholder="help,need,looking for" required>

        <label class="form-label"><b>Posts to check per subreddit</b></label>
        <input class="form-control mb-3" type="number" name="limit" value="50">

        <label class="form-label"><b>AI Guidance (optional)</b></label>
        <textarea class="form-control mb-3" name="ai_guidance" rows="4"
          placeholder="Describe the goal (e.g., 'B2B bookkeeping leads in US, ongoing monthly work. Exclude students/DIYers/job seekers.')"></textarea>

        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" id="ai_enabled" name="ai_enabled" checked>
          <label class="form-check-label" for="ai_enabled"><b>Use AI scoring for this scrape</b></label>
        </div>

        <button class="btn btn-primary" type="submit">Create Scrape</button>
      </form>
      <a class="d-inline-block mt-3" href="/dashboard">← Back to Dashboard</a>
    '''
    return page_wrap(html, "Create Scrape")

@app.route('/edit-scrape/<int:scrape_id>', methods=['GET', 'POST'])
@login_required
def edit_scrape(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))

    if request.method == 'POST':
        s.name = request.form['name']
        s.subreddits = request.form['subreddits']
        s.keywords = request.form['keywords']
        s.limit = int(request.form.get('limit', s.limit or 50))
        s.ai_guidance = request.form.get('ai_guidance', '')
        s.ai_enabled = (request.form.get('ai_enabled') == 'on')
        db.session.commit()
        flash('Scrape updated.')
        return redirect(url_for('dashboard'))

    checked = 'checked' if (s.ai_enabled is None or s.ai_enabled) else ''
    html = f'''
      <h2 class="mb-3">Edit Scrape</h2>
      <form method="POST" class="card card-body" style="max-width:720px">
        <label class="form-label"><b>Name</b></label>
        <input class="form-control mb-3" type="text" name="name" value="{s.name}" required>

        <label class="form-label"><b>Subreddits (comma-separated)</b></label>
        <input class="form-control mb-3" type="text" name="subreddits" value="{s.subreddits}" required>

        <label class="form-label"><b>Keywords (comma-separated)</b></label>
        <input class="form-control mb-3" type="text" name="keywords" value="{s.keywords}" required>

        <label class="form-label"><b>Posts to check per subreddit</b></label>
        <input class="form-control mb-3" type="number" name="limit" value="{s.limit or 50}">

        <label class="form-label"><b>AI Guidance (optional)</b></label>
        <textarea class="form-control mb-3" name="ai_guidance" rows="5">{(s.ai_guidance or '')}</textarea>

        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" id="ai_enabled" name="ai_enabled" {checked}>
          <label class="form-check-label" for="ai_enabled"><b>Use AI scoring for this scrape</b></label>
        </div>

        <button class="btn btn-primary" type="submit">Save Changes</button>
      </form>
      <a class="d-inline-block mt-3" href="/dashboard">← Back to Dashboard</a>
    '''
    return page_wrap(html, "Edit Scrape")

# ------------ Results + Hide / Unhide / Bulk Hide ------------
@app.route('/results/<int:scrape_id>')
@login_required
def view_results(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    # owner/sub can view if same account root
    root = get_account_root(session['user_id'])
    owner_root = get_account_root(s.user_id)
    if root.id != owner_root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))

    try:
        min_score = int(request.args.get('min_score', '0'))
    except:
        min_score = 0
    show_hidden = request.args.get('show_hidden', '0') == '1'

    q = Result.query.filter_by(scrape_id=scrape_id)
    if not show_hidden:
        q = q.filter((Result.is_hidden == False) | (Result.is_hidden == None))
    if min_score:
        q = q.filter(Result.ai_score >= min_score)

    results = q.order_by(Result.created_at.desc()).all()

    plan = get_plan_for_user(session['user_id'])
    usage = get_or_create_usage_month(root.id)
    ai_status = f"""
      <span class="badge {'text-bg-success' if s.ai_enabled else 'text-bg-secondary'}">
        {'AI scoring ON' if s.ai_enabled else 'AI scoring OFF'}
      </span>
      <span class="badge text-bg-info ms-2">AI {usage.ai_posts_count}/{plan.ai_posts_quota if plan else 0} this month</span>
    """

    guidance_block = f'''
    <div class="card p-3 mb-3">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <div class="muted">AI Guidance</div>
          <div>{(s.ai_guidance or "<span class='muted'>(none set)</span>")}</div>
        </div>
        <div>{ai_status}</div>
      </div>
    </div>
    '''

    toolbar = f'''
    <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
      <form class="row row-cols-lg-auto g-2 align-items-center" method="GET">
        <input type="hidden" name="show_hidden" value="{1 if show_hidden else 0}">
        <div class="col-12">
          <label class="form-label me-2">Min Score</label>
          <input class="form-control form-control-sm" type="number" min="0" max="10" name="min_score" value="{min_score}">
        </div>
        <div class="col-12">
          <button class="btn btn-sm btn-outline-primary" type="submit">Apply</button>
        </div>
      </form>
      <div class="d-flex align-items-center gap-2">
        <form method="GET">
          <input type="hidden" name="min_score" value="{min_score}">
          <input type="hidden" name="show_hidden" value="{0 if show_hidden else 1}">
          <button class="btn btn-sm btn-outline-light" type="submit">
            {'Hide Hidden' if show_hidden else 'Show Hidden'}
          </button>
        </form>
        <form method="POST" action="/results/{s.id}/hide-below">
          <input type="hidden" name="threshold" value="{max(min_score, AI_MIN_SCORE)}">
          <button class="btn btn-sm btn-outline-warning" onclick="return confirm('Hide all posts below threshold?')">
            Hide all &lt; {max(min_score, AI_MIN_SCORE)}
          </button>
        </form>
        <a class="btn btn-sm btn-outline-light" href="/run-scrape/{s.id}"><i class="bi bi-arrow-repeat"></i> Run Now</a>
        <a class="btn btn-sm btn-outline-light" href="/dashboard">Back</a>
      </div>
    </div>
    '''

    rows = ""
    for r in results:
        ai_html = f"""{score_badge(r.ai_score)}
            {f'<div class="text-muted small">{r.ai_reasoning}</div>' if r.ai_reasoning else ''}"""
        actions = []
        actions.append(f'<a class="btn btn-sm btn-outline-primary" target="_blank" href="{r.url}">Open</a>')
        if (r.ai_score or 0) < AI_MIN_SCORE:
            actions.append(f'<a class="btn btn-sm btn-outline-success" href="/send-to-ghl/{r.id}?min_score={min_score}&show_hidden={1 if show_hidden else 0}">Send</a>')
        if r.is_hidden:
            actions.append(f'<a class="btn btn-sm btn-outline-secondary" href="/result/{r.id}/unhide">Unhide</a>')
        else:
            actions.append(f'<a class="btn btn-sm btn-outline-danger" href="/result/{r.id}/hide?min_score={min_score}&show_hidden={1 if show_hidden else 0}">Hide</a>')

        hidden_tag = '<span class="badge text-bg-secondary ms-2">Hidden</span>' if r.is_hidden else ''
        rows += f'''
          <tr class="{'opacity-50' if r.is_hidden else ''}">
            <td>{r.created_at.strftime('%Y-%m-%d %H:%M')}</td>
            <td>r/{r.subreddit}</td>
            <td>{(r.title or "")[:100]}{"..." if len(r.title or "")>100 else ""} {hidden_tag}</td>
            <td>u/{r.author}</td>
            <td>{r.score}</td>
            <td><code>{r.keywords_found or ""}</code></td>
            <td>{ai_html}</td>
            <td class="text-nowrap">{' '.join(actions)}</td>
          </tr>
        '''

    if not results:
        rows = '<tr><td colspan="8" class="text-center py-4">No results for current filters.</td></tr>'

    html = f'''
      <h1 class="mb-2">Results for: {s.name}</h1>
      {guidance_block}
      {toolbar}
      <table class="table table-sm align-middle">
        <thead class="table-light">
          <tr>
            <th>Date</th><th>Subreddit</th><th>Title</th><th>Author</th>
            <th>Upvotes</th><th>Keywords</th><th>AI</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    '''
    return page_wrap(html, f"Results – {s.name}")

@app.route('/result/<int:result_id>/hide', methods=['POST', 'GET'])
@login_required
def hide_result(result_id):
    r = Result.query.get_or_404(result_id)
    s = Scrape.query.get(r.scrape_id)
    root = get_account_root(session['user_id'])
    if get_account_root(s.user_id).id != root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))
    r.is_hidden = True; db.session.commit()
    flash('Post hidden')
    return redirect(url_for('view_results', scrape_id=r.scrape_id, **{k: v for k, v in request.args.items()}))

@app.route('/result/<int:result_id>/unhide', methods=['POST', 'GET'])
@login_required
def unhide_result(result_id):
    r = Result.query.get_or_404(result_id)
    s = Scrape.query.get(r.scrape_id)
    root = get_account_root(session['user_id'])
    if get_account_root(s.user_id).id != root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))
    r.is_hidden = False; db.session.commit()
    flash('Post unhidden')
    return redirect(url_for('view_results', scrape_id=r.scrape_id, show_hidden=1))

@app.route('/results/<int:scrape_id>/hide-below', methods=['POST'])
@login_required
def hide_below(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    root = get_account_root(session['user_id'])
    if get_account_root(s.user_id).id != root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))
    try:
        threshold = int(request.form.get('threshold', AI_MIN_SCORE))
    except:
        threshold = AI_MIN_SCORE
    q = Result.query.filter_by(scrape_id=scrape_id).filter((Result.ai_score < threshold) | (Result.ai_score == None))
    updated = q.update({Result.is_hidden: True}, synchronize_session=False)
    db.session.commit()
    flash(f'Hidden {updated} posts below score {threshold}')
    return redirect(url_for('view_results', scrape_id=scrape_id, show_hidden=0))

# ------------ Actions ------------
@app.route('/send-to-ghl/<int:result_id>')
@login_required
def send_to_ghl_manual(result_id):
    r = Result.query.get_or_404(result_id)
    s = Scrape.query.get(r.scrape_id)
    root = get_account_root(session['user_id'])
    if get_account_root(s.user_id).id != root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))
    ok = send_to_ghl({
        'author': r.author, 'url': r.url, 'title': r.title,
        'subreddit': r.subreddit, 'keywords_found': (r.keywords_found or '').split(',')
    })
    flash('Sent to GoHighLevel' if ok else 'Failed to send to GoHighLevel')
    return redirect(url_for('view_results', scrape_id=r.scrape_id))

@app.route('/run-scrape/<int:scrape_id>')
@login_required
def run_scrape_now(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    root = get_account_root(session['user_id'])
    if get_account_root(s.user_id).id != root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))
    run_scrape(scrape_id)
    flash('Scrape completed! Check results.')
    return redirect(url_for('dashboard'))

@app.route('/toggle-scrape/<int:scrape_id>')
@login_required
def toggle_scrape(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))
    s.is_active = not s.is_active; db.session.commit()
    flash(f'Scrape {"activated" if s.is_active else "paused"}')
    return redirect(url_for('dashboard'))

@app.route('/delete-scrape/<int:scrape_id>')
@login_required
def delete_scrape(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    root = get_account_root(session['user_id'])
    if get_account_root(s.user_id).id != root.id:
        flash('Access denied'); return redirect(url_for('dashboard'))
    Result.query.filter_by(scrape_id=scrape_id).delete()
    db.session.delete(s); db.session.commit()
    flash('Scrape deleted')
    return redirect(url_for('dashboard'))

# ------------ Agency: Invite Sub-User ------------
@app.route('/agency/invite', methods=['GET', 'POST'])
@login_required
def invite_user():
    root = get_account_root(session['user_id'])
    plan = get_plan_for_user(session['user_id'])
    if not plan or not plan.is_agency:
        flash("Only Agency accounts can invite users")
        return redirect(url_for('dashboard'))

    if request.method == 'POST']:
        ok, reason = can_add_user(session['user_id'])
        if not ok:
            flash(reason); return redirect(url_for('invite_user'))

        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password']

        if User.query.filter_by(username=username).first():
            flash("Username already exists"); return redirect(url_for('invite_user'))

        new_user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            parent_id=root.id,
            role='member'
        )
        db.session.add(new_user)
        db.session.commit()
        flash('User invited!')
        return redirect(url_for('dashboard'))

    # show seat usage
    used = User.query.filter_by(parent_id=root.id).count()
    remain = max(0, plan.max_users - used)

    html = f'''
    <h2 class="mb-3">Invite Sub-User</h2>
    <div class="alert alert-secondary">You can invite <b>{remain}</b> more users on your current plan.</div>
    <form method="POST" class="card card-body" style="max-width:480px">
      <input class="form-control mb-2" name="username" placeholder="Username" required>
      <input class="form-control mb-2" name="email" placeholder="Email" required>
      <input class="form-control mb-3" type="password" name="password" placeholder="Temporary Password" required>
      <button class="btn btn-primary" type="submit">Invite</button>
    </form>
    <a href="/dashboard" class="d-inline-block mt-3">← Back</a>
    '''
    return page_wrap(html, "Invite User")

# ------------ Super Admin Console ------------
@app.route('/admin')
@admin_required
def admin_home():
    owners = User.query.filter_by(parent_id=None).order_by(User.created_at.desc()).all()
    rows = ""
    for o in owners:
        sub = get_active_subscription(o.id)
        plan = sub.plan.name if sub and sub.plan else "—"
        active = (sub.active if sub else False)
        # pooled counts
        user_count = User.query.filter_by(parent_id=o.id).count() + 1  # include owner
        pool_ids = [o.id] + [u.id for u in User.query.filter_by(parent_id=o.id).all()]
        scrape_count = Scrape.query.filter(Scrape.user_id.in_(pool_ids)).count()
        usage = get_or_create_usage_month(o.id)
        rows += f"""
        <tr>
          <td>{o.username}<div class="small muted">{o.email}</div></td>
          <td>{plan}</td>
          <td>{'Active' if active else 'Canceled'}</td>
          <td>{user_count}</td>
          <td>{scrape_count}</td>
          <td>{usage.ai_posts_count}</td>
          <td class="text-nowrap">
            <a class="btn btn-sm btn-outline-primary" href="/admin/impersonate/{o.id}">Impersonate</a>
            <a class="btn btn-sm btn-outline-info" href="/admin/manage/{o.id}">Manage</a>
          </td>
        </tr>
        """
    html = f"""
    <h2 class="mb-3">Admin — Accounts</h2>
    <div class="card p-3">
      <div class="table-responsive">
        <table class="table table-sm align-middle">
          <thead><tr>
            <th>Owner</th><th>Plan</th><th>Status</th><th>Seats</th><th>Scrapes</th><th>AI used (mo)</th><th>Actions</th>
          </tr></thead>
          <tbody>{rows or '<tr><td colspan="7" class="text-center py-4">No accounts.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    """
    return page_wrap(html, "Admin")

@app.route('/admin/manage/<int:owner_id>', methods=['GET','POST'])
@admin_required
def admin_manage(owner_id):
    owner = User.query.get_or_404(owner_id)
    sub = get_active_subscription(owner.id)
    plans = Plan.query.order_by(Plan.id.asc()).all()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'set_plan':
            plan_id = int(request.form['plan_id'])
            if sub:
                sub.plan_id = plan_id
                sub.active = True
                db.session.commit()
            else:
                sub = Subscription(user_id=owner.id, plan_id=plan_id, active=True, start_date=datetime.utcnow())
                db.session.add(sub); db.session.commit()
            flash('Plan updated.')
        elif action == 'cancel':
            if sub:
                sub.active = False
                sub.end_date = datetime.utcnow()
                db.session.commit()
                flash('Subscription canceled.')
        elif action == 'reactivate':
            if sub:
                sub.active = True
                sub.end_date = None
                db.session.commit()
                flash('Subscription reactivated.')
        return redirect(url_for('admin_manage', owner_id=owner_id))

    usage = get_or_create_usage_month(owner.id)
    pool_ids = [owner.id] + [u.id for u in User.query.filter_by(parent_id=owner.id).all()]
    user_count = len(pool_ids)
    scrape_count = Scrape.query.filter(Scrape.user_id.in_(pool_ids)).count()

    plan_options = "".join([
        f'<option value="{p.id}" {"selected" if (sub and sub.plan_id==p.id) else ""}>{p.name} — seats:{p.max_users} scrapes:{p.max_scrapes} AI:{p.ai_posts_quota}/mo</option>'
        for p in plans
    ])

    html = f"""
    <h2 class="mb-3">Manage Account — {owner.username}</h2>
    <div class="row g-3">
      <div class="col-md-6">
        <div class="card p-3">
          <h5>Subscription</h5>
          <div class="mb-2"><b>Plan:</b> {(sub.plan.name if sub and sub.plan else '—')}</div>
          <div class="mb-2"><b>Status:</b> {('Active' if (sub and sub.active) else 'Canceled')}</div>
          <form method="POST" class="d-flex gap-2">
            <input type="hidden" name="action" value="set_plan">
            <select class="form-select" name="plan_id">{plan_options}</select>
            <button class="btn btn-primary" type="submit">Update Plan</button>
          </form>
          <div class="mt-3 d-flex gap-2">
            <form method="POST">
              <input type="hidden" name="action" value="cancel">
              <button class="btn btn-outline-danger" onclick="return confirm('Cancel this subscription?')">Cancel</button>
            </form>
            <form method="POST">
              <input type="hidden" name="action" value="reactivate">
              <button class="btn btn-outline-success">Reactivate</button>
            </form>
          </div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card p-3">
          <h5>Usage</h5>
          <div>Seats (owner + sub-users): <b>{user_count}</b></div>
          <div>Scrapes (pooled): <b>{scrape_count}</b></div>
          <div>AI posts this month: <b>{usage.ai_posts_count}</b></div>
        </div>
      </div>
    </div>
    <div class="mt-3">
      <a class="btn btn-outline-secondary" href="/admin">← Back</a>
      <a class="btn btn-outline-primary ms-2" href="/admin/impersonate/{owner.id}">Impersonate</a>
    </div>
    """
    return page_wrap(html, "Manage Account")

@app.route('/admin/impersonate/<int:owner_id>')
@admin_required
def admin_impersonate(owner_id):
    session['impersonator_id'] = session.get('user_id')
    session['user_id'] = owner_id
    u = User.query.get(owner_id)
    session['username'] = u.username if u else 'impersonated'
    flash(f'Impersonating {u.username if u else owner_id}')
    return redirect(url_for('dashboard'))

@app.route('/admin/stop-impersonate')
def admin_stop_impersonate():
    imp = session.get('impersonator_id')
    if not imp:
        return redirect(url_for('dashboard'))
    session['user_id'] = imp
    u = User.query.get(imp)
    session['username'] = u.username if u else 'admin'
    session.pop('impersonator_id', None)
    flash('Stopped impersonation.')
    return redirect(url_for('admin_home'))

# ------------ Cron webhook (optional for Railway Cron) ------------
@app.route('/tasks/run-all', methods=['POST'])
def tasks_run_all():
    if TASKS_TOKEN and request.headers.get('X-TASKS-TOKEN') != TASKS_TOKEN:
        return "Forbidden", 403
    run_all_scrapes()
    return jsonify({"ok": True})

# ------------ Debug ------------
@app.route('/debug/version')
def debug_version():
    return "signalbot-v1"

@app.route('/debug/ai')
def debug_ai():
    score, reason = ai_score_post(
        "Need a bookkeeper for my small business",
        "Looking for ongoing monthly bookkeeping and payroll.",
        ["need","bookkeeper","looking"],
        guidance="B2B bookkeeping leads, monthly recurring"
    )
    return jsonify({"score": score, "reason": reason})

# ------------ Scheduler ------------
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_all_scrapes, trigger="interval", hours=1)
scheduler.start()

# ------------ Local run / setup ------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com',
                         password_hash=generate_password_hash('admin123'), is_admin=True, role='owner')
            db.session.add(admin); db.session.commit()
        # auto-promote SUPERADMIN_USERNAME (optional)
        if SUPERADMIN_USERNAME:
            su = User.query.filter_by(username=SUPERADMIN_USERNAME).first()
            if su and not su.is_admin:
                su.is_admin = True; db.session.commit()
        # ensure plans exist
        if not Plan.query.first():
            db.session.add_all([
                Plan(name='Starter', max_users=1,  max_scrapes=5,   ai_posts_quota=3000,   price=29.0,  is_agency=False),
                Plan(name='Pro',     max_users=3,  max_scrapes=10,  ai_posts_quota=10000,  price=79.0,  is_agency=False),
                Plan(name='Agency',  max_users=50, max_scrapes=200, ai_posts_quota=100000, price=299.0, is_agency=True),
            ])
            db.session.commit()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
