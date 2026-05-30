# app.py — Reddit Leads (Howl) — all-in-one

from flask import Flask, request, redirect, url_for, session, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text, func, case
import praw, json, requests, os, logging, io
import anthropic as anthropic_sdk

# ------------ Flask & DB ------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-' + os.urandom(16).hex())
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ------------ Logging ------------
logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
log = logging.getLogger("reddit-leads")

# ------------ Env Config ------------
GHL_API_KEY      = os.environ.get('GHL_API_KEY', '')
GHL_LOCATION_ID  = os.environ.get('GHL_LOCATION_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
AI_MODEL          = os.environ.get('AI_MODEL', 'claude-sonnet-4-6')
AI_MIN_SCORE     = int(os.environ.get('AI_MIN_SCORE', '6'))
ENABLE_DB_ADMIN  = os.environ.get('ENABLE_DB_ADMIN', '0') == '1'
TASKS_TOKEN      = os.environ.get('TASKS_TOKEN', '')

# ------------ Models ------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    # Per-scrape AI configuration
    ai_guidance = db.Column(db.Text)
    ai_enabled  = db.Column(db.Boolean, default=True)
    scrape_type = db.Column(db.String(20), default='lead')  # 'lead' or 'seo'

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
    # AI fields
    ai_score = db.Column(db.Integer)       # 1..10 (nullable when AI disabled)
    ai_reasoning = db.Column(db.Text)
    # dedupe
    reddit_post_id = db.Column(db.String(50))
    # archive / hide
    is_hidden = db.Column(db.Boolean, default=False)
    suggested_response = db.Column(db.Text)

class UserProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    business_name = db.Column(db.String(200))
    description = db.Column(db.Text)
    services = db.Column(db.Text)
    tone = db.Column(db.String(200))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ProfileFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    extracted_text = db.Column(db.Text)
    storage_url = db.Column(db.Text)  # reserved for future S3/object storage
    file_size = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# --- Auto-migration: ensure columns/indexes exist (runs at import) ---
def ensure_db_upgrade():
    try:
        with app.app_context():
            db.create_all()  # base tables
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
            db.session.execute(text("ALTER TABLE scrape ADD COLUMN IF NOT EXISTS scrape_type VARCHAR(20);"))
            db.session.execute(text("UPDATE scrape SET scrape_type = 'lead' WHERE scrape_type IS NULL;"))
            # result table - new columns
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS suggested_response TEXT;"))
            db.session.commit()
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
        user_agent="howl-reddit-leads"
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
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            flash('Admin access required')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return inner

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
            'source': 'Reddit Scraper',
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

def get_anthropic_client():
    return anthropic_sdk.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_user_context(user_id: int) -> str:
    profile = UserProfile.query.filter_by(user_id=user_id).first()
    files = ProfileFile.query.filter_by(user_id=user_id).all()
    parts = []
    if profile:
        if profile.business_name:
            parts.append(f"Business Name: {profile.business_name}")
        if profile.description:
            parts.append(f"Business Description: {profile.description}")
        if profile.services:
            parts.append(f"Services/Products: {profile.services}")
        if profile.tone:
            parts.append(f"Tone/Voice: {profile.tone}")
    for f in files:
        if f.extracted_text:
            parts.append(f"--- Knowledge File: {f.filename} ---\n{f.extracted_text[:4000]}")
    return "\n\n".join(parts)

def ai_score_post(title: str, body: str, keywords: list[str], guidance: str | None = None,
                  scrape_type: str = 'lead', user_context: str = '') -> tuple[int, str]:
    if not ANTHROPIC_API_KEY:
        return 0, "AI disabled (missing ANTHROPIC_API_KEY)"
    try:
        guidance_text = (guidance or "").strip()
        context_block = f"\n\nBUSINESS CONTEXT:\n{user_context}" if user_context else ""

        if scrape_type == 'seo':
            system_prompt = "You are a concise SEO opportunity analyst for Reddit content marketing."
            scoring_guide = """Score this Reddit post as an SEO/content marketing opportunity (1-10):
- 9-10: High-traffic question in your niche, perfect for an authoritative helpful reply that builds brand visibility
- 7-8: Good opportunity — relevant topic, engaged audience, room to add genuine value
- 4-6: Marginal — loosely related or low engagement
- 1-3: Not an opportunity (wrong topic, rant, already resolved, too niche)"""
        else:
            system_prompt = "You are a concise lead-qualification assistant."
            scoring_guide = """Score this Reddit post for lead intent (1-10):
- 9-10: Direct ask for help/hiring in scope
- 7-8: Strong buying signals or urgent pain in scope
- 4-6: Vague interest/learning; maybe relevant but weak
- 1-3: Not a lead or out-of-scope"""

        prompt = f"""{scoring_guide}

If GUIDANCE is provided, bias your judgment toward that use-case.{context_block}

GUIDANCE: {guidance_text if guidance_text else "(none)"}

Return STRICT JSON: {{"score": int 1-10, "reason": string <= 240 chars}}. No extra text.

Title: {title}
Body: {(body or '')[:1500]}
Matched keywords: {", ".join(keywords)}"""

        client = get_anthropic_client()
        msg = client.messages.create(
            model=AI_MODEL,
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}]
        )
        content = msg.content[0].text.strip()
        j = json.loads(content)
        score = max(1, min(10, int(j.get("score", 0))))
        reason = str(j.get("reason", ""))[:1000]
        return score, reason
    except Exception as e:
        log.warning("AI scoring error: %s", e)
        return 0, "AI unavailable"

def generate_suggested_response(title: str, body: str, subreddit: str,
                                 scrape_type: str = 'lead', user_context: str = '') -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        context_block = f"\n\nYOUR BUSINESS CONTEXT:\n{user_context}" if user_context else ""
        if scrape_type == 'seo':
            instruction = "Write a helpful, genuine Reddit reply that adds real value to the thread and naturally (not forcefully) showcases your expertise. Be conversational, not salesy. Max 150 words."
        else:
            instruction = "Write a friendly, helpful Reddit reply that addresses the poster's need and naturally introduces how your business could help. Be genuine, not pushy. Max 150 words."

        prompt = f"""{instruction}{context_block}

Subreddit: r/{subreddit}
Post title: {title}
Post body: {(body or '')[:1000]}

Write only the reply text, nothing else."""

        client = get_anthropic_client()
        msg = client.messages.create(
            model=AI_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.warning("Response generation error: %s", e)
        return ""

def extract_file_text(filename: str, file_bytes: bytes) -> str:
    ext = filename.rsplit('.', 1)[-1].lower()
    try:
        if ext == 'txt':
            return file_bytes.decode('utf-8', errors='ignore')
        elif ext == 'pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        elif ext in ('docx', 'doc'):
            from docx import Document
            doc = Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        log.warning("File extraction error (%s): %s", filename, e)
    return ""

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

    total_scrapes = db.session.query(func.count(Scrape.id)).filter_by(user_id=user_id).scalar() or 0
    active_scrapes = db.session.query(func.count(Scrape.id)).filter_by(user_id=user_id, is_active=True).scalar() or 0

    res_base = db.session.query(Result.id).join(Scrape, Result.scrape_id == Scrape.id)\
        .filter(Scrape.user_id == user_id, Result.created_at >= since)\
        .filter((Result.is_hidden == False) | (Result.is_hidden == None))
    total_results = res_base.count()

    qualified = db.session.query(Result.id).join(Scrape, Result.scrape_id == Scrape.id)\
        .filter(Scrape.user_id == user_id, Result.created_at >= since)\
        .filter((Result.is_hidden == False) | (Result.is_hidden == None))\
        .filter(Result.ai_score >= min_score).count()

    return {"total_scrapes": total_scrapes, "active_scrapes": active_scrapes,
            "total_results": total_results, "qualified": qualified, "since": since}

def daily_counts(user_id: int, days: int = 7):
    since = datetime.utcnow() - timedelta(days=days-1)
    rows = db.session.query(
        func.date_trunc('day', Result.created_at).label('d'),
        func.count(Result.id),
        func.sum(case((Result.ai_score >= AI_MIN_SCORE, 1), else_=0))
    ).join(Scrape, Result.scrape_id == Scrape.id)\
     .filter(Scrape.user_id == user_id, Result.created_at >= since)\
     .filter((Result.is_hidden == False) | (Result.is_hidden == None))\
     .group_by('d').order_by('d').all()

    by_day = {r[0].date(): (int(r[1]), int(r[2] or 0)) for r in rows}
    labels, totals, quals = [], [], []
    for i in range(days):
        day = (since.date() + timedelta(days=i))
        t, q = by_day.get(day, (0, 0))
        labels.append(day.strftime('%b %d')); totals.append(t); quals.append(q)
    return labels, totals, quals

# ---------- Theming shell (Bootstrap + Icons + Chart.js) ----------
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

  /* Form controls respect theme */
  .form-control, .form-select {
    background-color: var(--panel);
    color: var(--text);
    border-color: var(--border);
  }
  .form-control:focus, .form-select:focus {
    background-color: var(--panel);
    color: var(--text);
    border-color: var(--brand-primary);
    box-shadow: 0 0 0 0.2rem rgba(255,176,0,.25);
  }
  .form-control::placeholder { color: var(--muted); }
  .form-label { color: var(--text); }
  .form-check-label { color: var(--text); }
  .form-check-input {
    background-color: var(--panel);
    border-color: var(--border);
  }
</style>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" defer></script>
"""

def page_wrap(inner_html: str, page_title: str = "") -> str:
    title = f"{page_title} – Reddit Scraper" if page_title else "Reddit Scraper"
    theme = session.get("theme", "dark")
    toggle_label = "Light Mode" if theme == "dark" else "Dark Mode"
    return f"""{BOOTSTRAP_SHELL}
<title>{title}</title>
<body data-theme="{theme}">
<nav class="navbar navbar-expand-lg">
  <div class="container-fluid">
    <a class="navbar-brand fw-semibold" style="color:var(--brand-primary)" href="/dashboard">
      <i class="bi bi-rocket-takeoff"></i> Reddit Leads
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav" aria-controls="nav" aria-expanded="false">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div id="nav" class="collapse navbar-collapse">
      <ul class="navbar-nav me-auto mb-2 mb-lg-0">
        <li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/create-scrape">New Scrape</a></li>
        <li class="nav-item"><a class="nav-link" style="color:var(--text)" href="/profile"><i class="bi bi-building"></i> Profile</a></li>
        {'<li class="nav-item"><a class="nav-link" style="color:var(--brand-primary)" href="/admin"><i class="bi bi-shield-lock"></i> Admin</a></li>' if session.get("is_admin") else ''}
      </ul>
      <a class="btn btn-sm btn-outline-light me-2" href="/theme/toggle"><i class="bi bi-moon-stars"></i> {toggle_label}</a>
      <span class="muted me-3">Hi, {session.get("username","guest")}</span>
      {'<a class="btn btn-outline-light btn-sm" href="/logout">Logout</a>' if session.get('user_id') else '<a class="btn btn-outline-light btn-sm" href="/login">Login</a>'}
    </div>
  </div>
</nav>

<div class="container py-4">{inner_html}</div>
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

            stype = scrape.scrape_type or 'lead'
            user_context = get_user_context(scrape.user_id)
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

                        if Result.query.filter_by(scrape_id=scrape.id, reddit_post_id=post_id).first():
                            continue

                        if scrape.ai_enabled:
                            ai_score_val, ai_reason = ai_score_post(
                                title, body, found,
                                guidance=scrape.ai_guidance,
                                scrape_type=stype,
                                user_context=user_context
                            )
                            suggested = generate_suggested_response(
                                title, body, subreddit_name,
                                scrape_type=stype,
                                user_context=user_context
                            )
                        else:
                            ai_score_val, ai_reason = None, "AI disabled for this scrape"
                            suggested = ""

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
                            is_hidden=False,
                            suggested_response=suggested
                        )
                        db.session.add(result)
                        results_count += 1

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

@app.route('/reset-db')
def reset_db():
    if not ENABLE_DB_ADMIN:
        abort(404)
    try:
        db.drop_all(); db.create_all()
        admin = User(username='admin', email='admin@example.com',
                     password_hash=generate_password_hash('admin123'), is_admin=True)
        db.session.add(admin); db.session.commit()
        return "Database reset!"
    except Exception as e:
        return f"Error: {e}"

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    html = '''
      <h1 class="mb-2">Reddit Scraper Platform</h1>
      <p class="muted">Monitor Reddit for keywords and push qualified leads to GoHighLevel.</p>
      <a class="btn btn-primary me-2" href="/login">Login</a>
      <a class="btn btn-outline-light" href="/register">Register</a>
    '''
    return page_wrap(html, "Home")

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        u = request.form['username'].strip()
        e = request.form['email'].strip()
        p = request.form['password']
        if User.query.filter_by(username=u).first():
            flash('Username already exists'); return redirect(url_for('register'))
        user = User(username=u, email=e, password_hash=generate_password_hash(p))
        db.session.add(user); db.session.commit()
        flash('Account created!'); return redirect(url_for('login'))
    html = '''
      <h2 class="mb-3">Register</h2>
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
            session['is_admin'] = user.is_admin
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    html = '''
      <h2 class="mb-3">Login</h2>
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

# ------------ Dashboard (metrics + chart + recent + scrapes) ------------
@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    kpis = kpis_for_user(user_id, days=7)
    labels, totals, quals = daily_counts(user_id, days=7)

    recent = db.session.query(Result, Scrape).join(Scrape, Result.scrape_id == Scrape.id)\
        .filter(Scrape.user_id == user_id)\
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
        <h5 class="mb-0">Leads over last 7 days</h5>
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

    # Recent results
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
        <h5 class="mb-0">Recent results</h5>
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

    # Scrapes table
    scrapes = Scrape.query.filter_by(user_id=user_id).order_by(Scrape.created_at.desc()).all()
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

    return page_wrap(cards + chart + recent_table + scrapes_table, "Dashboard")

# ------------ Create/Edit Scrape ------------
@app.route('/create-scrape', methods=['GET', 'POST'])
@login_required
def create_scrape():
    if request.method == 'POST':
        scrape = Scrape(
            name=request.form['name'],
            subreddits=request.form['subreddits'],
            keywords=request.form['keywords'],
            limit=int(request.form.get('limit', 50)),
            user_id=session['user_id'],
            ai_guidance=request.form.get('ai_guidance', ''),
            ai_enabled=(request.form.get('ai_enabled', 'on') == 'on'),
            scrape_type=request.form.get('scrape_type', 'lead')
        )
        db.session.add(scrape); db.session.commit()
        flash('Scrape created! It will run automatically every hour.')
        return redirect(url_for('dashboard'))

    html = '''
      <h2 class="mb-3">Create New Scrape</h2>
      <form method="POST" class="card card-body" style="max-width:720px">
        <label class="form-label"><b>Name</b></label>
        <input class="form-control mb-3" type="text" name="name" required>

        <label class="form-label"><b>Scrape Type</b></label>
        <select class="form-select mb-3" name="scrape_type">
          <option value="lead">Lead Generation — find people who need your services</option>
          <option value="seo">SEO Opportunity — find threads to engage and build authority</option>
        </select>

        <label class="form-label"><b>Subreddits (comma-separated)</b></label>
        <input class="form-control mb-3" type="text" name="subreddits" placeholder="bookkeeping,smallbusiness,accounting" required>

        <label class="form-label"><b>Keywords (comma-separated)</b></label>
        <input class="form-control mb-3" type="text" name="keywords" placeholder="help,need,looking for" required>

        <label class="form-label"><b>Posts to check per subreddit</b></label>
        <input class="form-control mb-3" type="number" name="limit" value="50">

        <label class="form-label"><b>AI Guidance (optional)</b></label>
        <textarea class="form-control mb-3" name="ai_guidance" rows="4"
          placeholder="Describe the goal for AI (e.g., 'B2B bookkeeping leads in US, ongoing monthly work. Exclude students/DIYers/job seekers.')"></textarea>

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
        s.scrape_type = request.form.get('scrape_type', 'lead')
        db.session.commit()
        flash('Scrape updated.')
        return redirect(url_for('dashboard'))

    checked = 'checked' if (s.ai_enabled is None or s.ai_enabled) else ''
    stype = s.scrape_type or 'lead'
    html = f'''
      <h2 class="mb-3">Edit Scrape</h2>
      <form method="POST" class="card card-body" style="max-width:720px">
        <label class="form-label"><b>Name</b></label>
        <input class="form-control mb-3" type="text" name="name" value="{s.name}" required>

        <label class="form-label"><b>Scrape Type</b></label>
        <select class="form-select mb-3" name="scrape_type">
          <option value="lead" {"selected" if stype=="lead" else ""}>Lead Generation — find people who need your services</option>
          <option value="seo" {"selected" if stype=="seo" else ""}>SEO Opportunity — find threads to engage and build authority</option>
        </select>

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
    if s.user_id != session['user_id']:
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

    ai_status = f"""
      <span class="badge {'text-bg-success' if s.ai_enabled else 'text-bg-secondary'}">
        {'AI scoring ON' if s.ai_enabled else 'AI scoring OFF'}
      </span>
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
        response_html = ""
        if r.suggested_response:
            safe_response = r.suggested_response.replace('`', '&#96;').replace('"', '&quot;')
            response_html = f'''
              <tr class="{'opacity-50' if r.is_hidden else ''}">
                <td colspan="8" style="background:var(--table-head); padding:12px 16px;">
                  <div class="d-flex justify-content-between align-items-start gap-2">
                    <div>
                      <span class="badge text-bg-success me-2"><i class="bi bi-chat-dots"></i> Suggested Reply</span>
                      <span style="white-space:pre-wrap">{r.suggested_response}</span>
                    </div>
                    <button class="btn btn-sm btn-outline-secondary flex-shrink-0"
                      onclick="navigator.clipboard.writeText(`{safe_response}`)" title="Copy">
                      <i class="bi bi-clipboard"></i>
                    </button>
                  </div>
                </td>
              </tr>'''
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
          {response_html}
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
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))
    r.is_hidden = True; db.session.commit()
    flash('Post hidden')
    return redirect(url_for('view_results', scrape_id=r.scrape_id, **{k: v for k, v in request.args.items()}))

@app.route('/result/<int:result_id>/unhide', methods=['POST', 'GET'])
@login_required
def unhide_result(result_id):
    r = Result.query.get_or_404(result_id)
    s = Scrape.query.get(r.scrape_id)
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))
    r.is_hidden = False; db.session.commit()
    flash('Post unhidden')
    return redirect(url_for('view_results', scrape_id=r.scrape_id, show_hidden=1))

@app.route('/results/<int:scrape_id>/hide-below', methods=['POST'])
@login_required
def hide_below(scrape_id):
    s = Scrape.query.get_or_404(scrape_id)
    if s.user_id != session['user_id']:
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
    if s.user_id != session['user_id']:
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
    if s.user_id != session['user_id']:
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
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))
    Result.query.filter_by(scrape_id=scrape_id).delete()
    db.session.delete(s); db.session.commit()
    flash('Scrape deleted')
    return redirect(url_for('dashboard'))

# ------------ Business Profile ------------
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user_id = session['user_id']
    prof = UserProfile.query.filter_by(user_id=user_id).first()

    if request.method == 'POST':
        if not prof:
            prof = UserProfile(user_id=user_id)
            db.session.add(prof)
        prof.business_name = request.form.get('business_name', '')
        prof.description = request.form.get('description', '')
        prof.services = request.form.get('services', '')
        prof.tone = request.form.get('tone', '')
        db.session.commit()
        flash('Profile saved!')
        return redirect(url_for('profile'))

    files = ProfileFile.query.filter_by(user_id=user_id).order_by(ProfileFile.created_at.desc()).all()

    file_rows = ""
    for f in files:
        size_kb = round((f.file_size or 0) / 1024, 1)
        chars = len(f.extracted_text or "")
        file_rows += f"""
        <tr>
          <td><i class="bi bi-file-earmark-text me-2"></i>{f.filename}</td>
          <td>{size_kb} KB</td>
          <td>{chars:,} chars extracted</td>
          <td>{f.created_at.strftime('%Y-%m-%d')}</td>
          <td><a class="btn btn-sm btn-outline-danger" href="/profile/file/{f.id}/delete"
               onclick="return confirm('Delete this file?')">Delete</a></td>
        </tr>"""

    html = f"""
      <h2 class="mb-1">Business Profile</h2>
      <p class="muted mb-4">This information is injected into every AI scoring and response generation call.</p>
      <div class="row g-4">
        <div class="col-lg-6">
          <div class="card p-4">
            <h5 class="mb-3">Business Info</h5>
            <form method="POST">
              <label class="form-label"><b>Business Name</b></label>
              <input class="form-control mb-3" name="business_name" value="{(prof.business_name or '') if prof else ''}">

              <label class="form-label"><b>Description</b></label>
              <textarea class="form-control mb-3" name="description" rows="4"
                placeholder="What does your business do? Who do you serve?">{(prof.description or '') if prof else ''}</textarea>

              <label class="form-label"><b>Services / Products</b></label>
              <textarea class="form-control mb-3" name="services" rows="3"
                placeholder="List your main services or products">{(prof.services or '') if prof else ''}</textarea>

              <label class="form-label"><b>Tone / Voice</b></label>
              <input class="form-control mb-3" name="tone"
                placeholder="e.g. Professional, friendly, expert, casual"
                value="{(prof.tone or '') if prof else ''}">

              <button class="btn btn-primary" type="submit">Save Profile</button>
            </form>
          </div>
        </div>
        <div class="col-lg-6">
          <div class="card p-4">
            <h5 class="mb-3">Knowledge Files</h5>
            <p class="muted small">Upload PDFs, Word docs, or text files. Content is extracted and used as context for AI.</p>
            <form method="POST" action="/profile/upload" enctype="multipart/form-data" class="mb-3">
              <input class="form-control mb-2" type="file" name="file" accept=".pdf,.docx,.txt" required>
              <button class="btn btn-outline-primary btn-sm" type="submit">
                <i class="bi bi-upload"></i> Upload File
              </button>
            </form>
            <div class="table-responsive">
              <table class="table table-sm align-middle">
                <thead><tr><th>File</th><th>Size</th><th>Content</th><th>Added</th><th></th></tr></thead>
                <tbody>{file_rows or '<tr><td colspan="5" class="text-center py-3 muted">No files uploaded yet.</td></tr>'}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    """
    return page_wrap(html, "Business Profile")

@app.route('/profile/upload', methods=['POST'])
@login_required
def profile_upload():
    user_id = session['user_id']
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file selected')
        return redirect(url_for('profile'))

    filename = f.filename
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ('pdf', 'docx', 'doc', 'txt'):
        flash('Unsupported file type. Use PDF, DOCX, or TXT.')
        return redirect(url_for('profile'))

    file_bytes = f.read()
    extracted = extract_file_text(filename, file_bytes)

    pf = ProfileFile(
        user_id=user_id,
        filename=filename,
        extracted_text=extracted,
        file_size=len(file_bytes)
    )
    db.session.add(pf)
    db.session.commit()
    flash(f'"{filename}" uploaded — {len(extracted):,} characters extracted.')
    return redirect(url_for('profile'))

@app.route('/profile/file/<int:file_id>/delete')
@login_required
def profile_file_delete(file_id):
    pf = ProfileFile.query.get_or_404(file_id)
    if pf.user_id != session['user_id']:
        flash('Access denied')
        return redirect(url_for('profile'))
    db.session.delete(pf)
    db.session.commit()
    flash('File deleted.')
    return redirect(url_for('profile'))

# ------------ Admin ------------
@app.route('/admin')
@admin_required
def admin_panel():
    users = User.query.order_by(User.created_at.desc()).all()

    rows = ""
    for u in users:
        total_scrapes = db.session.query(func.count(Scrape.id)).filter_by(user_id=u.id).scalar() or 0
        active_scrapes = db.session.query(func.count(Scrape.id)).filter_by(user_id=u.id, is_active=True).scalar() or 0
        total_results = db.session.query(func.count(Result.id))\
            .join(Scrape, Result.scrape_id == Scrape.id)\
            .filter(Scrape.user_id == u.id).scalar() or 0
        admin_badge = '<span class="badge text-bg-warning">Admin</span>' if u.is_admin else ''
        rows += f"""
        <tr>
          <td>{u.id}</td>
          <td>{u.username} {admin_badge}</td>
          <td>{u.email}</td>
          <td>{u.created_at.strftime('%Y-%m-%d')}</td>
          <td>{active_scrapes} / {total_scrapes}</td>
          <td>{total_results}</td>
          <td class="text-nowrap">
            <a class="btn btn-sm btn-outline-primary" href="/admin/login-as/{u.id}">Login As</a>
            {'<a class="btn btn-sm btn-outline-warning" href="/admin/toggle-admin/' + str(u.id) + '">Toggle Admin</a>' if u.id != session['user_id'] else ''}
          </td>
        </tr>
        """

    html = f"""
      <div class="d-flex justify-content-between align-items-center mb-3">
        <h2 class="mb-0">Admin Panel</h2>
        <a class="btn btn-primary" href="/admin/create-user"><i class="bi bi-person-plus"></i> Add User</a>
      </div>
      <div class="card p-3">
        <div class="table-responsive">
          <table class="table table-sm align-middle">
            <thead>
              <tr>
                <th>ID</th><th>Username</th><th>Email</th><th>Joined</th>
                <th>Active / Total Scrapes</th><th>Total Results</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="7" class="text-center py-4">No users.</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    """
    return page_wrap(html, "Admin")

@app.route('/admin/create-user', methods=['GET', 'POST'])
@admin_required
def admin_create_user():
    if request.method == 'POST':
        u = request.form['username'].strip()
        e = request.form['email'].strip()
        p = request.form['password']
        is_admin = 'is_admin' in request.form
        if User.query.filter_by(username=u).first():
            flash('Username already exists')
        elif User.query.filter_by(email=e).first():
            flash('Email already exists')
        else:
            user = User(username=u, email=e, password_hash=generate_password_hash(p), is_admin=is_admin)
            db.session.add(user); db.session.commit()
            flash(f'User {u} created!')
            return redirect(url_for('admin_panel'))

    html = '''
      <h2 class="mb-3">Create New User</h2>
      <form method="POST" class="card card-body" style="max-width:520px">
        <label class="form-label"><b>Username</b></label>
        <input class="form-control mb-3" type="text" name="username" required>
        <label class="form-label"><b>Email</b></label>
        <input class="form-control mb-3" type="email" name="email" required>
        <label class="form-label"><b>Password</b></label>
        <input class="form-control mb-3" type="password" name="password" required>
        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" name="is_admin" id="is_admin">
          <label class="form-check-label" for="is_admin">Grant admin access</label>
        </div>
        <button class="btn btn-primary" type="submit">Create User</button>
      </form>
      <a class="d-inline-block mt-3" href="/admin">← Back to Admin</a>
    '''
    return page_wrap(html, "Create User")

@app.route('/admin/login-as/<int:user_id>')
@admin_required
def admin_login_as(user_id):
    user = User.query.get_or_404(user_id)
    session['_admin_id'] = session['user_id']
    session['_admin_username'] = session['username']
    session['user_id'] = user.id
    session['username'] = user.username
    session['is_admin'] = False
    flash(f'Viewing as {user.username}. <a href="/admin/return">Return to admin</a>')
    return redirect(url_for('dashboard'))

@app.route('/admin/return')
@login_required
def admin_return():
    if '_admin_id' not in session:
        return redirect(url_for('dashboard'))
    session['user_id'] = session.pop('_admin_id')
    session['username'] = session.pop('_admin_username')
    session['is_admin'] = True
    flash('Back to your admin account.')
    return redirect(url_for('admin_panel'))

@app.route('/admin/toggle-admin/<int:user_id>')
@admin_required
def admin_toggle_admin(user_id):
    if user_id == session['user_id']:
        flash('Cannot change your own admin status')
        return redirect(url_for('admin_panel'))
    user = User.query.get_or_404(user_id)
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f'{user.username} is {"now" if user.is_admin else "no longer"} an admin.')
    return redirect(url_for('admin_panel'))

# ------------ Cron webhook (optional) ------------
@app.route('/tasks/run-all', methods=['POST'])
def tasks_run_all():
    if TASKS_TOKEN and request.headers.get('X-TASKS-TOKEN') != TASKS_TOKEN:
        return "Forbidden", 403
    run_all_scrapes()
    return jsonify({"ok": True})

# ------------ Debug ------------
@app.route('/debug/version')
def debug_version():
    return "howl-ui-ai-v7-theme"

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

# ------------ Local run ------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com',
                         password_hash=generate_password_hash('admin123'), is_admin=True)
            db.session.add(admin); db.session.commit()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
