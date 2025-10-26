import os
from datetime import datetime, timezone

from flask import Flask, jsonify, request, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

db = SQLAlchemy()

def normalize_db_url(url: str) -> str:
    # Heroku-style URLs still show up sometimes
    if url and url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url

def create_app() -> Flask:
    app = Flask(__name__)

    database_url = normalize_db_url(os.getenv("DATABASE_URL", "sqlite:///app.db"))
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    # Defer touching the DB until the server actually receives traffic.
    # This avoids boot-time connect() and any on_connect weirdness.
    @app.before_first_request
    def _create_tables_once():
        db.create_all()

    register_routes(app)
    return app

# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------

class Post(db.Model):
    __tablename__ = "posts"

    id = db.Column(db.Integer, primary_key=True)
    reddit_id = db.Column(db.String(24), unique=True, index=True, nullable=False)
    title = db.Column(db.Text, nullable=False)
    author = db.Column(db.String(255))
    url = db.Column(db.Text)
    permalink = db.Column(db.Text)
    created_utc = db.Column(db.DateTime(timezone=True))
    score = db.Column(db.Integer)
    num_comments = db.Column(db.Integer)

    hidden = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

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
# Routes
# --------------------------------------------------------------------------------------

def register_routes(app: Flask):

    @app.get("/healthz")
    def healthz():
        try:
            # Lightweight check that also initializes a connection only when called
            db.session.execute(text("SELECT 1"))
            return jsonify({"status": "ok"}), 200
        except Exception as e:
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
            }
        })

    @app.get("/posts")
    def list_posts():
        # pagination + filter
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
        for field in ("reddit_id", "title"):
            if not data.get(field):
                abort(400, description=f"Missing required field: {field}")

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

    # Error handlers
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

def parse_iso_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

# --------------------------------------------------------------------------------------
# WSGI
# --------------------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
