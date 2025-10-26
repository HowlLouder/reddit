import os
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from flask import Flask, jsonify, request, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

# --------------------------------------------------------------------------------------
# Config & DB
# --------------------------------------------------------------------------------------

db = SQLAlchemy()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("reddit-scraper-api")

DEFAULT_DB = "sqlite:///app.db"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB)

def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = normalize_db_url(DATABASE_URL)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        ensure_db_upgrade()

    register_routes(app)
    return app

def normalize_db_url(url: str) -> str:
    # Support deprecated postgres:// scheme
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url

# Enable foreign keys on SQLite
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()
    except Exception:
        pass

# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------

class Post(db.Model):
    __tablename__ = "posts"

    id = db.Column(db.Integer, primary_key=True)
    reddit_id = db.Column(db.String(24), unique=True, index=True, nullable=False)  # thing id
    title = db.Column(db.Text, nullable=False)
    author = db.Column(db.String(255), nullable=True)
    url = db.Column(db.Text, nullable=True)
    permalink = db.Column(db.Text, nullable=True)
    created_utc = db.Column(db.DateTime(timezone=True), nullable=True)
    score = db.Column(db.Integer, nullable=True)
    num_comments = db.Column(db.Integer, nullable=True)

    hidden = db.Column(db.Boolean, nullable=False, default=False, server_default=text("FALSE"))

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "reddit_id": self.reddit_id,
            "title": self.title,
            "author": self.author,
            "url": self.url,
            "permalink": self.permalink,
            "created_utc": self.created_utc.isoformat() if self.created_utc else None,
            "score": self.score,
            "num_comments": self.num_comments,
            "hidden": self.hidden,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

# --------------------------------------------------------------------------------------
# DB bootstrap / light migration
# --------------------------------------------------------------------------------------

def ensure_db_upgrade():
    """
    Safe, idempotent bootstrap:
    - create tables if missing
    - ensure 'hidden' column exists
    No autocommit hacks; uses proper transaction context.
    """
    engine = db.engine
    inspector = inspect(engine)

    # 1) Create tables
    with engine.begin() as conn:
        db.metadata.create_all(bind=conn)

    # 2) Ensure 'hidden' column exists on posts
    columns = {c["name"] for c in inspector.get_columns("posts")}
    if "hidden" not in columns:
        engine_name = engine.url.get_backend_name()
        log.info("Adding missing 'hidden' column to posts...")
        with engine.begin() as conn:
            if engine_name == "postgresql":
                conn.execute(text('ALTER TABLE posts ADD COLUMN "hidden" BOOLEAN NOT NULL DEFAULT FALSE;'))
            else:
                # SQLite supports ADD COLUMN with default; existing rows get default.
                conn.execute(text('ALTER TABLE posts ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0;'))

# --------------------------------------------------------------------------------------
# Scraper (public JSON; no PRAW dependency)
# --------------------------------------------------------------------------------------

REDDIT_UA = os.getenv("REDDIT_USER_AGENT", "SignalBot/1.0 (+https://example.com)")

def fetch_subreddit_new(subreddit: str, limit: int = 25, after: Optional[str] = None) -> Tuple[list, Optional[str]]:
    params = {"limit": max(1, min(limit, 100))}
    if after:
        params["after"] = after

    resp = requests.get(
        f"https://www.reddit.com/r/{subreddit}/new.json",
        headers={"User-Agent": REDDIT_UA},
        params=params,
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Reddit returned {resp.status_code}: {resp.text[:400]}")

    data = resp.json().get("data", {})
    children = data.get("children", [])
    after_token = data.get("after")

    posts = []
    for ch in children:
        d = ch.get("data", {})
        posts.append({
            "reddit_id": d.get("id"),
            "title": d.get("title") or "(untitled)",
            "author": d.get("author"),
            "url": d.get("url_overridden_by_dest") or d.get("url"),
            "permalink": f"https://reddit.com{d.get('permalink')}" if d.get("permalink") else None,
            "created_utc": datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc) if d.get("created_utc") else None,
            "score": d.get("score"),
            "num_comments": d.get("num_comments"),
        })
    return posts, after_token

def upsert_posts(posts: list) -> int:
    """
    Insert posts by reddit_id; ignore duplicates gracefully.
    Returns number of newly inserted rows.
    """
    inserted = 0
    for p in posts:
        if not p.get("reddit_id"):
            continue
        exists = Post.query.filter_by(reddit_id=p["reddit_id"]).first()
        if exists:
            # Optional: update mutable fields (score/comments/title)
            exists.title = p.get("title") or exists.title
            exists.author = p.get("author") or exists.author
            exists.url = p.get("url") or exists.url
            exists.permalink = p.get("permalink") or exists.permalink
            exists.score = p.get("score") if p.get("score") is not None else exists.score
            exists.num_comments = p.get("num_comments") if p.get("num_comments") is not None else exists.num_comments
            # keep created_utc if present, but don't overwrite if missing
        else:
            post = Post(
                reddit_id=p["reddit_id"],
                title=p.get("title") or "(untitled)",
                author=p.get("author"),
                url=p.get("url"),
                permalink=p.get("permalink"),
                created_utc=p.get("created_utc"),
                score=p.get("score"),
                num_comments=p.get("num_comments"),
            )
            db.session.add(post)
            inserted += 1
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    return inserted

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

def register_routes(app: Flask):

    @app.get("/healthz")
    def healthz():
        # Quick connectivity probe
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            log.exception("Health check failed")
            return jsonify({"status": "error", "detail": str(e)}), 500

    @app.get("/")
    def root():
        return jsonify({
            "name": "Reddit scraper API",
            "endpoints": {
                "GET /healthz": "Health check",
                "GET /posts": "List posts (query: page, per_page, include_hidden)",
                "PATCH /posts/<id>/hide": "Hide a post",
                "PATCH /posts/<id>/unhide": "Unhide a post",
                "POST /posts": "Create a post (JSON body)",
                "POST /scrape": "Scrape subreddit (JSON body: subreddit, limit, pages)",
            }
        })

    # -------- Posts --------

    @app.get("/posts")
    def list_posts():
        try:
            page = max(1, int(request.args.get("page", 1)))
            per_page = max(1, min(100, int(request.args.get("per_page", 20))))
        except ValueError:
            abort(400, description="Invalid page/per_page")

        include_hidden = request.args.get("include_hidden", "false").lower() in ("1", "true", "yes")

        q = Post.query
        if not include_hidden:
            q = q.filter_by(hidden=False)

        q = q.order_by(Post.created_utc.desc().nullslast(), Post.id.desc())
        pagination = q.paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "items": [p.to_dict() for p in pagination.items],
        })

    @app.post("/posts")
    def create_post():
        data = request.get_json(silent=True) or {}
        require_fields = ["reddit_id", "title"]
        for f in require_fields:
            if not data.get(f):
                abort(400, description=f"Missing required field: {f}")

        p = Post(
            reddit_id=data["reddit_id"],
            title=data["title"],
            author=data.get("author"),
            url=data.get("url"),
            permalink=data.get("permalink"),
            created_utc=parse_iso_dt(data.get("created_utc")),
            score=data.get("score"),
            num_comments=data.get("num_comments"),
            hidden=bool(data.get("hidden", False)),
        )
        db.session.add(p)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, description="reddit_id already exists")

        return jsonify(p.to_dict()), 201

    @app.patch("/posts/<int:post_id>/hide")
    def hide_post(post_id: int):
        p = Post.query.get_or_404(post_id)
        p.hidden = True
        db.session.commit()
        return jsonify(p.to_dict())

    @app.patch("/posts/<int:post_id>/unhide")
    def unhide_post(post_id: int):
        p = Post.query.get_or_404(post_id)
        p.hidden = False
        db.session.commit()
        return jsonify(p.to_dict())

    # -------- Scrape --------

    @app.post("/scrape")
    def scrape():
        """
        Body:
        {
          "subreddit": "all",
          "limit": 25,     # per page (1..100), default 25
          "pages": 1       # pagination pages to follow, default 1
        }
        """
        body = request.get_json(silent=True) or {}
        subreddit = (body.get("subreddit") or "").strip()
        if not subreddit:
            abort(400, description="subreddit is required")

        limit = int(body.get("limit", 25))
        pages = max(1, min(10, int(body.get("pages", 1))))

        total_inserted = 0
        after = None
        for _ in range(pages):
            posts, after = fetch_subreddit_new(subreddit, limit=limit, after=after)
            inserted = upsert_posts(posts)
            total_inserted += inserted
            if not after:
                break

        return jsonify({"subreddit": subreddit, "inserted": total_inserted}), 200

    # -------- Errors --------

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": "bad_request", "detail": e.description}), 400

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(409)
    def conflict(e):
        return jsonify({"error": "conflict", "detail": e.description}), 409

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "internal_server_error"}), 500


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Handle both naive and tz-aware
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# WSGI
# --------------------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
