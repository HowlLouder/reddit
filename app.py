import os
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import praw
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
import requests

# ============================================================================
# CONFIGURATION
# ============================================================================
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///scraper.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Logging setup
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# AI Configuration
AI_MAX_CONCURRENCY = int(os.getenv('AI_MAX_CONCURRENCY', 5))
AI_MIN_SCORE = int(os.getenv('AI_MIN_SCORE', 7))
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

# Reddit API Setup
reddit = praw.Reddit(
    client_id=os.getenv('REDDIT_CLIENT_ID', 'YOUR_CLIENT_ID'),
    client_secret=os.getenv('REDDIT_CLIENT_SECRET', 'YOUR_CLIENT_SECRET'),
    user_agent=os.getenv('REDDIT_USER_AGENT', 'keyword_scraper by u/YOUR_USERNAME')
)

# GHL Configuration
GHL_WEBHOOK_URL = os.getenv('GHL_WEBHOOK_URL', '')

# ============================================================================
# DATABASE MODELS
# ============================================================================
class Scrape(db.Model):
    __tablename__ = 'scrapes'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    subreddits = db.Column(db.Text, nullable=False)  # Comma-separated
    keywords = db.Column(db.Text, nullable=False)    # Comma-separated
    limit = db.Column(db.Integer, default=100)
    ai_enabled = db.Column(db.Boolean, default=False)
    ai_guidance = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='pending')  # pending, running, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    results_count = db.Column(db.Integer, default=0)
    
    results = db.relationship('Result', backref='scrape', lazy=True, cascade='all, delete-orphan')


class Result(db.Model):
    __tablename__ = 'results'
    
    id = db.Column(db.Integer, primary_key=True)
    scrape_id = db.Column(db.Integer, db.ForeignKey('scrapes.id'), nullable=False)
    reddit_post_id = db.Column(db.String(50), nullable=False)
    title = db.Column(db.Text, nullable=False)
    author = db.Column(db.String(100))
    subreddit = db.Column(db.String(100))
    url = db.Column(db.Text)
    score = db.Column(db.Integer, default=0)
    keywords_found = db.Column(db.Text)  # Comma-separated
    ai_score = db.Column(db.Integer, nullable=True)
    ai_reasoning = db.Column(db.Text, nullable=True)
    is_hidden = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Unique constraint to prevent duplicate posts per scrape
    __table_args__ = (
        db.UniqueConstraint('scrape_id', 'reddit_post_id', name='unique_scrape_post'),
    )


# ============================================================================
# AI SCORING FUNCTION
# ============================================================================
def ai_score_post(title, body, keywords_found, guidance=""):
    """
    Score a Reddit post using Claude API
    Returns: (score: int, reasoning: str)
    """
    if not ANTHROPIC_API_KEY:
        log.warning("No Anthropic API key configured")
        return None, "No API key configured"
    
    try:
        prompt = f"""You are analyzing a Reddit post for lead quality. 
        
Title: {title}
Body: {body[:500]}  # Truncate long posts
Keywords found: {', '.join(keywords_found)}

{f'Additional guidance: {guidance}' if guidance else ''}

Rate this post from 1-10 based on:
- How relevant it is to the keywords
- Whether it represents a genuine lead/opportunity
- Quality and seriousness of the post

Respond in this exact format:
SCORE: [number 1-10]
REASON: [brief explanation]"""

        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 300,
                'messages': [
                    {'role': 'user', 'content': prompt}
                ]
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log.error(f"Anthropic API error: {response.status_code} - {response.text}")
            return None, f"API error: {response.status_code}"
        
        content = response.json()['content'][0]['text']
        
        # Parse response
        score_line = [line for line in content.split('\n') if line.startswith('SCORE:')]
        reason_line = [line for line in content.split('\n') if line.startswith('REASON:')]
        
        if score_line and reason_line:
            score = int(score_line[0].replace('SCORE:', '').strip())
            reason = reason_line[0].replace('REASON:', '').strip()
            return score, reason
        else:
            return None, "Could not parse AI response"
            
    except Exception as e:
        log.exception(f"Error in AI scoring: {e}")
        return None, f"Error: {str(e)}"


# ============================================================================
# GHL INTEGRATION
# ============================================================================
def send_to_ghl(data):
    """
    Send lead data to Go High Level webhook
    """
    if not GHL_WEBHOOK_URL:
        log.warning("No GHL webhook URL configured")
        return False
    
    try:
        payload = {
            'author': data.get('author'),
            'url': data.get('url'),
            'title': data.get('title'),
            'subreddit': data.get('subreddit'),
            'keywords': ', '.join(data.get('keywords_found', [])),
            'timestamp': datetime.utcnow().isoformat()
        }
        
        response = requests.post(GHL_WEBHOOK_URL, json=payload, timeout=10)
        
        if response.status_code == 200:
            log.info(f"Successfully sent to GHL: {data.get('url')}")
            return True
        else:
            log.error(f"GHL webhook error: {response.status_code}")
            return False
            
    except Exception as e:
        log.exception(f"Error sending to GHL: {e}")
        return False


# ============================================================================
# MAIN SCRAPING FUNCTION
# ============================================================================
def run_scrape(scrape_id):
    """
    Execute a scrape job - THIS IS WHERE YOUR CODE SHOULD BE!
    This function runs when triggered, not at module import time.
    """
    scrape = Scrape.query.get(scrape_id)
    if not scrape:
        log.error(f"Scrape ID {scrape_id} not found")
        return
    
    try:
        # Update status
        scrape.status = 'running'
        db.session.commit()
        
        # Parse subreddits and keywords
        subreddit_list = [s.strip() for s in scrape.subreddits.split(',') if s.strip()]
        keyword_list = [k.strip().lower() for k in scrape.keywords.split(',') if k.strip()]
        
        if not subreddit_list or not keyword_list:
            raise ValueError("Subreddits and keywords are required")
        
        log.info(f"Starting scrape {scrape.id}: {len(subreddit_list)} subreddits, {len(keyword_list)} keywords")
        
        results_count = 0
        
        # ====================================================================
        # YOUR SCRAPING CODE STARTS HERE (NOW INSIDE A FUNCTION!)
        # ====================================================================
        for subreddit_name in subreddit_list:
            try:
                subreddit = reddit.subreddit(subreddit_name)
                log.info(f"Scraping r/{subreddit_name}...")
                
                # 1) Gather matched posts (lightweight pass)
                matched = []
                for post in subreddit.new(limit=scrape.limit):
                    title = post.title or ""
                    body = getattr(post, "selftext", "") or ""
                    text_all = f"{title} {body}".lower()
                    found = [kw for kw in keyword_list if kw in text_all]
                    
                    if not found:
                        continue
                    
                    url = f"https://reddit.com{post.permalink}"
                    post_id = getattr(post, "id", None) or url
                    
                    # Skip duplicates
                    if Result.query.filter_by(scrape_id=scrape.id, reddit_post_id=post_id).first():
                        log.debug(f"Skipping duplicate post: {post_id}")
                        continue
                    
                    matched.append({
                        "title": title,
                        "body": body,
                        "found": found,
                        "url": url,
                        "post_id": post_id,
                        "author": str(post.author),
                        "score": post.score,
                        "subreddit_name": subreddit_name
                    })
                
                log.info(f"Found {len(matched)} matching posts in r/{subreddit_name}")
                
                # 2) Score in parallel (only if AI enabled)
                scored = []
                if scrape.ai_enabled and matched:
                    log.info(f"AI scoring {len(matched)} posts with {AI_MAX_CONCURRENCY} workers...")
                    
                    def _score_item(item):
                        s, r = ai_score_post(
                            item["title"], 
                            item["body"], 
                            item["found"], 
                            guidance=scrape.ai_guidance
                        )
                        item["ai_score"] = s
                        item["ai_reason"] = r
                        return item
                    
                    with ThreadPoolExecutor(max_workers=AI_MAX_CONCURRENCY) as ex:
                        futures = [ex.submit(_score_item, m) for m in matched]
                        for fut in as_completed(futures):
                            try:
                                scored.append(fut.result())
                            except Exception as e:
                                log.exception(f"Error scoring post: {e}")
                else:
                    # AI disabled => just pass through
                    for m in matched:
                        m["ai_score"] = None
                        m["ai_reason"] = "AI disabled for this scrape"
                        scored.append(m)
                
                # 3) Persist & (optionally) send to GHL
                for item in scored:
                    result = Result(
                        scrape_id=scrape.id,
                        title=item["title"],
                        author=item["author"],
                        subreddit=item["subreddit_name"],
                        url=item["url"],
                        score=item["score"],
                        keywords_found=",".join(item["found"]),
                        ai_score=item["ai_score"],
                        ai_reasoning=item["ai_reason"],
                        reddit_post_id=item["post_id"],
                        is_hidden=False
                    )
                    db.session.add(result)
                    results_count += 1
                    
                    # Send high-scoring posts to GHL
                    if scrape.ai_enabled and (item["ai_score"] or 0) >= AI_MIN_SCORE:
                        send_to_ghl({
                            'author': item["author"],
                            'url': item["url"],
                            'title': item["title"],
                            'subreddit': item["subreddit_name"],
                            'keywords_found': item["found"]
                        })
                
                # Commit after each subreddit
                db.session.commit()
                log.info(f"Completed r/{subreddit_name}: {len(scored)} results saved")
                
            except Exception as e:
                log.exception(f"Error scraping r/{subreddit_name}: {e}")
                db.session.rollback()
                continue
        
        # ====================================================================
        # YOUR SCRAPING CODE ENDS HERE
        # ====================================================================
        
        # Update scrape status
        scrape.status = 'completed'
        scrape.completed_at = datetime.utcnow()
        scrape.results_count = results_count
        db.session.commit()
        
        log.info(f"✅ Scrape {scrape.id} completed successfully: {results_count} total results")
        
    except Exception as e:
        log.exception(f"Fatal error in scrape {scrape_id}: {e}")
        scrape.status = 'failed'
        db.session.commit()
        raise


# ============================================================================
# FLASK ROUTES
# ============================================================================
@app.route('/')
def index():
    return jsonify({
        'status': 'ok',
        'service': 'Reddit Keyword Scraper',
        'version': '2.0'
    })


@app.route('/scrapes', methods=['POST'])
def create_scrape():
    """Create a new scrape job"""
    data = request.get_json()
    
    scrape = Scrape(
        name=data.get('name', 'Untitled Scrape'),
        subreddits=data.get('subreddits', ''),
        keywords=data.get('keywords', ''),
        limit=data.get('limit', 100),
        ai_enabled=data.get('ai_enabled', False),
        ai_guidance=data.get('ai_guidance', '')
    )
    
    db.session.add(scrape)
    db.session.commit()
    
    return jsonify({
        'id': scrape.id,
        'status': 'created',
        'message': 'Scrape job created'
    }), 201


@app.route('/scrapes/<int:scrape_id>/run', methods=['POST'])
def trigger_scrape(scrape_id):
    """Trigger a scrape to run"""
    scrape = Scrape.query.get_or_404(scrape_id)
    
    if scrape.status == 'running':
        return jsonify({'error': 'Scrape is already running'}), 400
    
    try:
        # Run synchronously (for production, use Celery/background jobs)
        run_scrape(scrape_id)
        return jsonify({
            'id': scrape.id,
            'status': 'completed',
            'results_count': scrape.results_count
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scrapes/<int:scrape_id>', methods=['GET'])
def get_scrape(scrape_id):
    """Get scrape details"""
    scrape = Scrape.query.get_or_404(scrape_id)
    
    return jsonify({
        'id': scrape.id,
        'name': scrape.name,
        'status': scrape.status,
        'subreddits': scrape.subreddits,
        'keywords': scrape.keywords,
        'results_count': scrape.results_count,
        'created_at': scrape.created_at.isoformat(),
        'completed_at': scrape.completed_at.isoformat() if scrape.completed_at else None
    })


@app.route('/scrapes/<int:scrape_id>/results', methods=['GET'])
def get_results(scrape_id):
    """Get results for a scrape"""
    scrape = Scrape.query.get_or_404(scrape_id)
    results = Result.query.filter_by(scrape_id=scrape_id, is_hidden=False).all()
    
    return jsonify({
        'scrape_id': scrape.id,
        'total': len(results),
        'results': [
            {
                'id': r.id,
                'title': r.title,
                'author': r.author,
                'subreddit': r.subreddit,
                'url': r.url,
                'score': r.score,
                'keywords_found': r.keywords_found,
                'ai_score': r.ai_score,
                'ai_reasoning': r.ai_reasoning
            }
            for r in results
        ]
    })


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    try:
        # Test database connection
        db.session.execute(text('SELECT 1'))
        return jsonify({'status': 'healthy', 'database': 'connected'})
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================
def init_db():
    """Initialize database tables"""
    with app.app_context():
        db.create_all()
        log.info("Database tables created")


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8080, debug=True)
