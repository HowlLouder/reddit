# app.py
# A minimal Flask app for a Reddit-scraper style project, compatible with
# SQLAlchemy 2.x and Gunicorn. Works with Postgres (Railway/Render/etc.) or SQLite.

from __future__ import annotations

import os
import datetime as dt
from typing import Optional

from flask import Flask, jsonify, request, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect

# -----------------------------------------------------------------------------
# Configuration helpers
# -----------------------------------------------------------------------------

def normalize_db_url(url: Optional[str]) -> str:
    """
    Normalize DATABASE_URL from environments like Heroku/Railway.
    - Convert legacy 'postgres://' to 'postgresql://'
    - If nothing provided, default to SQLite file 'app.db'
    """
    if not url:
        return "sqlite:///app.db"
    if url.startswith("postgres://"):
        # SQLAlchemy needs 'postgresql://' (driver assumed)
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SQLALCHEMY_DATABASE_URI = normalize_db_url(os.getenv("DATABASE_URL"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Keep connections alive on some hosts
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

# -----------------------------------------------------------------------------
# App / DB setup
# -----------------------------------------------------------------------------

db = SQLAlchemy()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        # Ensure tables exist and apply safe, idempotent schema tweaks
        ensure_db_upgrade()

    # Routes
    register_routes(app)
    register_error_handlers(app)

    return app


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

class Post(db.Model):
    __tablename__ = "posts"

    id = db.Column(db.Integer, primary_key=True)

    # Common fields a Reddit scraper might store — adjust as needed.
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=True)
    subreddit = db.Column(db.String(120), nullable=True)
    author = db.Column(db.String(120), nullable=True)
    score = db.Column(db.Integer, nullable=True)
    created_utc = db.Column(db.DateTime, nullable=True)

    # Newly enforced column (the one that was crashing before when added)
    hidden = db.Column(db.Boolean, nullable=True, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "subreddit": self.subreddit,
            "author": self.author,
            "score": self.score,
            "created_utc": self.created_utc.isoformat() if self.created_utc else None,
            "hidden": bool(self.hidden),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# -----------------------------------------------------------------------------
# One-time, safe schema setup (no autocommit hacks, SQLA 2.x safe)
# -----------------------------------------------------------------------------

def ensure_db_upgrade():
    """
    Create tables if missing and add any new columns we rely on in a safe,
    idempotent way that works on both Postgres and SQLite with SQLAlchemy 2.x.
    """
    # Create declared models if they don't exist
    db.create_all()

    # Use a begin() context so DDL runs in a transaction when supported
    engine = db.engine
    with engine.begin() as conn:
        insp = inspect(conn)

        # Make sure 'posts' table exists (db.create_all should have done this)
        if "posts" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("posts")}

            # Add 'hidden' column if it's missing
            if "hidden" not in cols:
                if engine.name == "sqlite":
                    # SQLite: no default clause to keep it simple/portable
                    conn.execute(text("ALTER TABLE posts ADD COLUMN hidden BOOLEAN"))
                else:
                    # Postgres / others: add with default false
                    conn.execute(
                        text("ALTER TABLE posts ADD COLUMN hidden BOOLEAN DEFAULT FALSE")
                    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

def register_routes(app: Flask):

    @app.get("/healthz")
    def healthz():
        # Simple DB round-trip
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return jsonify({"ok": True}), 200
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/")
    def root():
        return jsonify({
            "name": "Reddit scraper API",
            "endpoints": {
                "GET /posts": "List posts (query: page, per_page, include_hidden)",
                "POST /posts": "Create a post (JSON body)",
                "PATCH /posts/<id>/hide": "Hide a post",
                "PATCH /posts/<id>/unhide": "Unhide a post",
                "GET /healthz": "Health check",
            }
        })

    @app.get("/posts")
    def list_posts():
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", 20)), 1), 100)
        include_hidden = request.args.get("include_hidden", "false").lower() in ("1", "true", "yes")

        q = Post.query
        if not include_hidden:
            q = q.filter((Post.hidden == False) | (Post.hidden.is_(None)))  # noqa: E712

        q = q.order_by(Post.created_at.desc())
        items = q.paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            "page": page,
            "per_page": per_page,
            "total": items.total,
            "pages": items.pages,
            "items": [p.to_dict() for p in items.items],
        })

    @app.post("/posts")
    def create_post():
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            abort(400, description="title is required")

        p = Post(
            title=title,
            url=data.get("url"),
            subreddit=data.get("subreddit"),
            author=data.get("author"),
            score=data.get("score"),
            created_utc=parse_iso_to_dt(data.get("created_utc")),
            hidden=bool(data.get("hidden", False)),
        )
        db.session.add(p)
        db.session.commit()
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


# -----------------------------------------------------------------------------
# Error handlers
# -----------------------------------------------------------------------------

def register_error_handlers(app: Flask):
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify(error="bad_request", message=str(e)), 400

    @app.errorhandler(404)
    def not_found(e):
        return jsonify(error="not_found", message="resource not found"), 404

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify(error="internal_server_error", message=str(e)), 500


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def parse_iso_to_dt(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        # Accept both seconds and full ISO strings
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Gunicorn entrypoint
# -----------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    # Handy for local dev: python app.py
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
