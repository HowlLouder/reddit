# app.py (full drop-in)

from flask import Flask, request, redirect, url_for, session, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import praw, json, requests, os, logging
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text, func, case

# ------------ Flask & DB ------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-' + str(os.urandom(24).hex()))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ------------ Logging ------------
logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
log = logging.getLogger("reddit-scraper")

# ------------ Env Config ------------
GHL_API_KEY = os.environ.get('GHL_API_KEY', '')
GHL_LOCATION_ID = os.environ.get('GHL_LOCATION_ID', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
AI_MODEL = os.environ.get('AI_MODEL', 'gpt-4o-mini')
AI_MIN_SCORE = int(os.environ.get('AI_MIN_SCORE', '6'))
ENABLE_DB_ADMIN = os.environ.get('ENABLE_DB_ADMIN', '0') == '1'
TASKS_TOKEN = os.environ.get('TASKS_TOKEN', '')

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
    ai_score = db.Column(db.Integer)        # 1..10
    ai_reasoning = db.Column(db.Text)
    # dedupe
    reddit_post_id = db.Column(db.String(50))
    # archive/hide
    is_hidden = db.Column(db.Boolean, default=False)

# --- Ensure DB upgrade even under gunicorn (runs at import) ---
def ensure_db_upgrade():
    try:
        with app.app_context():
            db.create_all()  # base tables
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS ai_score SMALLINT;"))
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS ai_reasoning TEXT;"))
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS reddit_post_id TEXT;"))
            db.session.execute(text("ALTER TABLE result ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN;"))
            db.session.execute(text("UPDATE result SET is_hidden = FALSE WHERE is_hidden IS NULL;"))
            db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uniq_result_scrape_post ON result (scrape_id, reddit_post_id);"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_result_scrape_hidden_created ON result (scrape_id, is_hidden, created_at DESC);"))
            db.session.commit()
            print("✅ DB upgrade ensured (ai_* + is_hidden).")
    except Exception as e:
        db.session.rollback()
        print("⚠️ DB upgrade at import failed:", e)

ensure_db_upgrade()
# --- end upgrade block ---

# ------------ Helpers ------------
def get_reddit_instance():
    return praw.Reddit(
        client_id=os.environ.get('REDDIT_CLIENT_ID', ''),
        client_secret=os.environ.get('REDDIT_CLIENT_SECRET', ''),
        user_agent="scraper_platform"
    )

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def send_to_ghl(result_data):
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        return False
    try:
        headers = {'Authorization': f'Bearer {GHL_API_KEY}', 'Content-Type': 'application/json'}
        tags = result_data.get('keywords_found', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',') if t.strip()]
        contact_data = {
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
        resp = requests.post('https://rest.gohighlevel.com/v1/contacts/', headers=headers, json=contact_data, timeout=15)
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("GHL send failed: %s %s", resp.status_code, resp.text[:300])
        return ok
    except Exception as e:
        log.exception("Error sending to GHL: %s", e)
        return False

def ai_score_post(title: str, body: str, keywords: list[str]) -> tuple[int, str]:
    """Return (score, reason). If unavailable, (0, 'AI unavailable')."""
    if not OPENAI_API_KEY:
        return 0, "AI disabled (missing OPENAI_API_KEY)"
    try:
        prompt = f"""
You are scoring Reddit posts for lead intent. A high-quality lead means the author is asking for help, hiring, seeking services, requesting recommendations, or describing a solvable pain where outreach is welcome.

Score from 1-10:
- 9-10: Direct ask for help/hiring (e.g., "Need a bookkeeper", "Hiring a marketer")
- 7-8: Strong buying signals or urgent pain
- 4-6: Vague interest/learning; maybe relevant but weak
- 1-3: Not a lead (opinions, news, jokes, off-topic)

Return STRICT JSON with fields: score (int 1-10), reason (<= 240 chars). No extra text.
Title: {title}
Body: {(body or '')[:1500]}
Matched keywords: {", ".join(keywords)}
        """.strip()

        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "You are a concise lead-qualification assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=20)
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

def run_scrape(scrape_id):
    with app.app_context():
        scrape = Scrape.query.get(scrape_id)
        if not scrape or not scrape.is_active:
            return
        try:
            reddit = get_reddit_instance()
            subreddit_list = [s.strip() for s in scrape.subreddits.split(',') if s.strip()]
            keyword_list = [k.strip().lower() for k in scrape.keywords.split(',') if k.strip()]

            results_count = 0
            for subreddit_name in subreddit_list:
                try:
                    subreddit = reddit.subreddit(subreddit_name)
                    for post in subreddit.new(limit=scrape.limit):
                        title = post.title or ""
                        body = getattr(post, "selftext", "") or ""
                        post_text = f"{title} {body}".lower()

                        found_keywords = [kw for kw in keyword_list if kw in post_text]
                        if not found_keywords:
                            continue

                        post_url = f"https://reddit.com{post.permalink}"
                        post_id = getattr(post, "id", None) or post_url

                        existing = Result.query.filter_by(scrape_id=scrape.id, reddit_post_id=post_id).first()
                        if existing:
                            continue

                        # --- AI scoring ---
                        ai_score_val, ai_reason = ai_score_post(title, body, found_keywords)
                        log.info("AI score %s for r/%s: %s", ai_score_val, subreddit_name, title[:100])

                        result = Result(
                            scrape_id=scrape.id,
                            title=title,
                            author=str(post.author),
                            subreddit=subreddit_name,
                            url=post_url,
                            score=post.score,
                            keywords_found=','.join(found_keywords),
                            ai_score=ai_score_val,
                            ai_reasoning=ai_reason,
                            reddit_post_id=post_id,
                            is_hidden=False
                        )
                        db.session.add(result)
                        results_count += 1

                        if ai_score_val >= AI_MIN_SCORE:
                            send_to_ghl({
                                'author': str(post.author),
                                'url': post_url,
                                'title': title,
                                'subreddit': subreddit_name,
                                'keywords_found': found_keywords
                            })

                except Exception as e:
                    log.exception("Error scraping r/%s: %s", subreddit_name, e)
                    continue

            scrape.last_run = datetime.utcnow()
            db.session.commit()
            log.info("Scrape %s completed. Found %s new results.", scrape.id, results_count)
        except Exception as e:
            log.exception("Error running scrape %s: %s", scrape_id, e)
            db.session.rollback()

def run_all_scrapes():
    with app.app_context():
        scrapes = Scrape.query.filter_by(is_active=True).all()
        for scrape in scrapes:
            run_scrape(scrape.id)

# ---------- Dashboard shell (Bootstrap + Icons + Chart.js) ----------
BOOTSTRAP_SHELL = """
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body{background:#0b0f14;color:#e5eef7}
  .navbar{background:#0e141b;border-bottom:1px solid #1c2733}
  .card{background:#0e141b;border:1px solid #1c2733}
  .table{--bs-table-color:#e5eef7;--bs-table-bg:transparent;--bs-table-border-color:#1c2733}
  .table thead{background:#121a23}
  .badge.text-bg-success{background:#16a34a!important}
  .badge.text-bg-warning{background:#f59e0b!important;color:#0b0f14}
  .badge.text-bg-danger{background:#ef4444!important}
  a{color:#8ab4ff}
  .muted{color:#97a6b8}
</style>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" defer></script>
"""

def page_wrap(inner_html: str, page_title: str = "") -> str:
    title = f"{page_title} – Reddit Scraper" if page_title else "Reddit Scraper"
    return f"""{BOOTSTRAP_SHELL}
<title>{title}</title>

<nav class="navbar navbar-expand-lg">
  <div class="container-fluid">
    <a class="navbar-brand text-light fw-semibold" href="/dashboard"><i class="bi bi-rocket-takeoff"></i> Reddit Leads</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav" aria-controls="nav" aria-expanded="false">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div id="nav" class="collapse navbar-collapse">
      <ul class="navbar-nav me-auto mb-2 mb-lg-0">
        <li class="nav-item"><a class="nav-link text-light" href="/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link text-light" href="/create-scrape">New Scrape</a></li>
      </ul>
      <span class="muted me-3">Hi, {session.get("username","guest")}</span>
      {'<a class="btn btn-outline-light btn-sm" href="/logout">Logout</a>' if session.get('user_id') else '<a class="btn btn-outline-light btn-sm" href="/login">Login</a>'}
    </div>
  </div>
</nav>

<div class="container py-4">{inner_html}</div>
"""

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

# ------------ Routes ------------
@app.route('/init-db')
def init_db():
    if not ENABLE_DB_ADMIN:
        abort(404)
    try:
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com', password_hash=generate_password_hash('admin123'), is_admin=True)
            db.session.add(admin)
            db.session.commit()
        return "Database initialized successfully!"
    except Exception as e:
        return f"Error initializing database: {str(e)}"

@app.route('/reset-db')
def reset_db():
    if not ENABLE_DB_ADMIN:
        abort(404)
    try:
        db.drop_all()
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@example.com', password_hash=generate_password_hash('admin123'), is_admin=True)
            db.session.add(admin)
            db.session.commit()
        return "Database reset successfully!"
    except Exception as e:
        return f"Error resetting database: {str(e)}"

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
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password']
        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('register'))
        user = User(username=username, email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        flash('Account created!')
        return redirect(url_for('login'))
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
        username = request.form['username'].strip()
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
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
          plugins: {{ legend: {{ labels: {{ color: '#e5eef7' }} }} }},
          scales: {{
            x: {{ ticks: {{ color: '#97a6b8' }}, grid: {{ color: '#1c2733' }} }},
            y: {{ ticks: {{ color: '#97a6b8' }}, grid: {{ color: '#1c2733' }}, beginAtZero: true }}
          }}
        }}
      }});
    }});
    </script>
    """

    rows = ""
    for r, s in recent:
        badge = score_badge(r.ai_score)
        actions = [f'<a class="btn btn-sm btn-outline-primary" target="_blank" href="{r.url}">Open</a>']
        if (r.ai_score or 0) < AI_MIN_SCORE:
            actions.append(f'<a class="btn btn-sm btn-outline-success" href="/send-to-ghl/{r.id}">Send</a>')
        if r.is_hidden:
            actions.append(f'<a class="btn btn-sm btn-outline-secondary" href="/result/{r.id}/unhide">Unhide</a>')
        else:
            actions.append(f'<a class="btn btn-sm btn-outline-danger" href="/result/{r.id}/hide">Hide</a>')
        rows += f"""
        <tr>
          <td>{r.created_at.strftime('%Y-%m-%d %H:%M')}</td>
          <td><span class="muted">r/</span>{r.subreddit}</td>
          <td>{(r.title or '')[:80]}{'...' if (r.title and len(r.title)>80) else ''}</td>
          <td>{badge}</td>
          <td class="text-nowrap">{' '.join(actions)}</td>
        </tr>
        """

    table = f"""
    <div class="card mt-4 p-3">
      <div class="d-flex justify-content-between align-items-center">
        <h5 class="mb-0">Recent results</h5>
        <a class="btn btn-sm btn-outline-light" href="/create-scrape"><i class="bi bi-plus"></i> New Scrape</a>
      </div>
      <div class="table-responsive mt-2">
        <table class="table table-sm align-middle">
          <thead>
            <tr><th>Date</th><th>Subreddit</th><th>Title</th><th>AI</th><th>Actions</th></tr>
          </thead>
          <tbody>{rows or '<tr><td colspan="5" class="text-center py-4">No recent results.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    """

    return page_wrap(cards + chart + table, "Dashboard")

@app.route('/create-scrape', methods=['GET', 'POST'])
@login_required
def create_scrape():
    if request.method == 'POST':
        scrape = Scrape(
            name=request.form['name'],
            subreddits=request.form['subreddits'],
            keywords=request.form['keywords'],
            limit=int(request.form.get('limit', 50)),
            user_id=session['user_id']
        )
        db.session.add(scrape)
        db.session.commit()
        flash('Scrape created! It will run automatically every hour.')
        return redirect(url_for('dashboard'))
    html = '''
        <h2 class="mb-3">Create New Scrape</h2>
        <form method="POST" class="card card-body" style="max-width:720px">
            <label class="form-label"><b>Name</b></label>
            <input class="form-control mb-3" type="text" name="name" required>
            <label class="form-label"><b>Subreddits (comma-separated)</b></label>
            <input class="form-control mb-3" type="text" name="subreddits" placeholder="bookkeeping,smallbusiness,accounting" required>
            <label class="form-label"><b>Keywords (comma-separated)</b></label>
            <input class="form-control mb-3" type="text" name="keywords" placeholder="help,need,looking for" required>
            <label class="form-label"><b>Posts to check per subreddit</b></label>
            <input class="form-control mb-3" type="number" name="limit" value="50">
            <button class="btn btn-primary" type="submit">Create Scrape</button>
        </form>
        <a class="d-inline-block mt-3" href="/dashboard">← Back to Dashboard</a>
    '''
    return page_wrap(html, "Create Scrape")

# ---------- Hide / Unhide / Bulk hide ----------
@app.route('/result/<int:result_id>/hide', methods=['POST', 'GET'])
@login_required
def hide_result(result_id):
    r = Result.query.get_or_404(result_id)
    s = Scrape.query.get(r.scrape_id)
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))
    r.is_hidden = True
    db.session.commit()
    flash('Post hidden')
    return redirect(url_for('view_results', scrape_id=r.scrape_id, **{k: v for k, v in request.args.items()}))

@app.route('/result/<int:result_id>/unhide', methods=['POST', 'GET'])
@login_required
def unhide_result(result_id):
    r = Result.query.get_or_404(result_id)
    s = Scrape.query.get(r.scrape_id)
    if s.user_id != session['user_id']:
        flash('Access denied'); return redirect(url_for('dashboard'))
    r.is_hidden = False
    db.session.commit()
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

@app.route('/results/<int:scrape_id>')
@login_required
def view_results(scrape_id):
    scrape = Scrape.query.get_or_404(scrape_id)
    if scrape.user_id != session['user_id']:
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
        <form method="POST" action="/results/{scrape_id}/hide-below">
          <input type="hidden" name="threshold" value="{max(min_score, AI_MIN_SCORE)}">
          <button class="btn btn-sm btn-outline-warning" onclick="return confirm('Hide all posts below threshold?')">
            Hide all &lt; {max(min_score, AI_MIN_SCORE)}
          </button>
        </form>
        <a class="btn btn-sm btn-outline-light" href="/run-scrape/{scrape.id}"><i class="bi bi-arrow-repeat"></i> Run Now</a>
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
        <h1 class="mb-2">Results for: {scrape.name}</h1>
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
    return page_wrap(html, f"Results – {scrape.name}")

@app.route('/send-to-ghl/<int:result_id>')
@login_required
def send_to_ghl_manual(result_id):
    result = Result.query.get_or_404(result_id)
    scrape = Scrape.query.get(result.scrape_id)
    if scrape.user_id != session['user_id']:
        flash('Access denied')
        return redirect(url_for('dashboard'))
    sent = send_to_ghl({
        'author': result.author,
        'url': result.url,
        'title': result.title,
        'subreddit': result.subreddit,
        'keywords_found': (result.keywords_found or '').split(',')
    })
    flash('Sent to GoHighLevel' if sent else 'Failed to send to GoHighLevel')
    return redirect(url_for('view_results', scrape_id=result.scrape_id))

@app.route('/run-scrape/<int:scrape_id>')
@login_required
def run_scrape_now(scrape_id):
    scrape = Scrape.query.get_or_404(scrape_id)
    if scrape.user_id != session['user_id']:
        flash('Access denied')
        return redirect(url_for('dashboard'))
    run_scrape(scrape_id)
    flash('Scrape completed! Check results.')
    return redirect(url_for('dashboard'))

@app.route('/toggle-scrape/<int:scrape_id>')
@login_required
def toggle_scrape(scrape_id):
    scrape = Scrape.query.get_or_404(scrape_id)
    if scrape.user_id != session['user_id']:
        flash('Access denied')
        return redirect(url_for('dashboard'))
    scrape.is_active = not scrape.is_active
    db.session.commit()
    status = "activated" if scrape.is_active else "paused"
    flash(f'Scrape {status}')
    return redirect(url_for('dashboard'))

@app.route('/delete-scrape/<int:scrape_id>')
@login_required
def delete_scrape(scrape_id):
    scrape = Scrape.query.get_or_404(scrape_id)
    if scrape.user_id != session['user_id']:
        flash('Access denied')
        return redirect(url_for('dashboard'))
    Result.query.filter_by(scrape_id=scrape_id).delete()
    db.session.delete(scrape)
    db.session.commit()
    flash('Scrape deleted')
    return redirect(url_for('dashboard'))

# Optional: cron-safe webhook (Railway Cron can POST here)
@app.route('/tasks/run-all', methods=['POST'])
def tasks_run_all():
    if TASKS_TOKEN and request.headers.get('X-TASKS-TOKEN') != TASKS_TOKEN:
        return "Forbidden", 403
    run_all_scrapes()
    return jsonify({"ok": True})

# Debug routes
@app.route('/debug/version')
def debug_version():
    return "ui-ai-v6"

@app.route('/debug/ai')
def debug_ai():
    score, reason = ai_score_post(
        "Need a bookkeeper for my small business",
        "Looking for ongoing monthly bookkeeping and payroll.",
        ["need","bookkeeper","looking"]
    )
    return jsonify({"score": score, "reason": reason})

# ------------ Scheduler ------------
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_all_scrapes, trigger="interval", hours=1)
scheduler.start()

# ------------ Dev server (local) ------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(
                username='admin',
                email='admin@example.com',
                password_hash=generate_password_hash('admin123'),
                is_admin=True
            )
            db.session.add(admin)
            db.session.commit()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
