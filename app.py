from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import praw
import json
from functools import wraps
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import os
from openai import OpenAI

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-' + str(os.urandom(24).hex()))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Configuration from environment variables
GHL_API_KEY = os.environ.get('GHL_API_KEY', '')
GHL_LOCATION_ID = os.environ.get('GHL_LOCATION_ID', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Database Models
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
    score = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    keywords_found = db.Column(db.Text)
    ai_score = db.Column(db.Integer)  # 1-10 quality score
    ai_reasoning = db.Column(db.Text)  # Why the AI scored it this way

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

def analyze_post_with_ai(title, text, keywords, subreddit):
    """Use AI to analyze if a post is a real opportunity"""
    if not openai_client:
        return 5, "AI not configured"
    
    try:
        prompt = f"""Analyze this Reddit post to determine if it's a genuine business opportunity for someone offering services related to these keywords: {keywords}

Subreddit: r/{subreddit}
Title: {title}
Content: {text[:500]}

Is this person:
1. Expressing a need or pain point?
2. Asking for help or recommendations?
3. A potential customer for {keywords} services?

Or are they just casually mentioning these keywords?

Respond with:
Score: [1-10, where 10 is definitely a sales opportunity]
Reason: [Brief explanation in one sentence]

Format: Score: X | Reason: [explanation]"""

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a lead qualification expert. Analyze Reddit posts to identify genuine business opportunities."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.3
        )
        
        result = response.choices[0].message.content.strip()
        
        # Parse the response
        if "Score:" in result and "Reason:" in result:
            score_part = result.split("Reason:")[0].replace("Score:", "").strip()
            reason_part = result.split("Reason:")[1].strip()
            
            # Extract numeric score
            score = int(''.join(filter(str.isdigit, score_part.split()[0])))
            score = max(1, min(10, score))  # Ensure 1-10 range
            
            return score, reason_part
        else:
            return 5, result[:200]
            
    except Exception as e:
        print(f"AI analysis error: {e}")
        return 5, f"Analysis error: {str(e)[:100]}"

def send_to_ghl(result_data):
    """Send scraped result to GoHighLevel as a contact - only if AI verified"""
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        return False
    
    # Only send if AI score is 7 or higher
    if result_data.get('ai_score', 0) < 7:
        print(f"Skipping GHL - AI score too low: {result_data.get('ai_score')}")
        return False
    
    try:
        headers = {
            'Authorization': f'Bearer {GHL_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        contact_data = {
            'locationId': GHL_LOCATION_ID,
            'firstName': result_data.get('author', 'Reddit User'),
            'source': 'Reddit Scraper',
            'tags': result_data.get('keywords_found', []),
            'customFields': {
                'reddit_post_url': result_data.get('url', ''),
                'reddit_title': result_data.get('title', ''),
                'subreddit': result_data.get('subreddit', ''),
                'ai_score': str(result_data.get('ai_score', 0)),
                'ai_reasoning': result_data.get('ai_reasoning', '')
            }
        }
        
        response = requests.post(
            'https://rest.gohighlevel.com/v1/contacts/',
            headers=headers,
            json=contact_data
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Error sending to GHL: {e}")
        return False

def run_scrape(scrape_id):
    """Execute a scraping job"""
    with app.app_context():
        scrape = Scrape.query.get(scrape_id)
        if not scrape or not scrape.is_active:
            return
        
        try:
            reddit = get_reddit_instance()
            subreddit_list = [s.strip() for s in scrape.subreddits.split(',')]
            keyword_list = [k.strip().lower() for k in scrape.keywords.split(',')]
            
            results_count = 0
            
            for subreddit_name in subreddit_list:
                try:
                    subreddit = reddit.subreddit(subreddit_name)
                    
                    for post in subreddit.new(limit=scrape.limit):
                        post_text = f"{post.title} {post.selftext}".lower()
                        
                        found_keywords = [kw for kw in keyword_list if kw in post_text]
                        
                        if found_keywords:
                            # Check if we already have this result
                            existing = Result.query.filter_by(
                                scrape_id=scrape.id,
                                url=f"https://reddit.com{post.permalink}"
                            ).first()
                            
                            if not existing:
                                # Analyze with AI
                                ai_score, ai_reasoning = analyze_post_with_ai(
                                    post.title,
                                    post.selftext,
                                    ', '.join(found_keywords),
                                    subreddit_name
                                )
                                
                                result = Result(
                                    scrape_id=scrape.id,
                                    title=post.title,
                                    author=str(post.author),
                                    subreddit=subreddit_name,
                                    url=f"https://reddit.com{post.permalink}",
                                    score=post.score,
                                    keywords_found=','.join(found_keywords),
                                    ai_score=ai_score,
                                    ai_reasoning=ai_reasoning
                                )
                                db.session.add(result)
                                results_count += 1
                                
                                # Send to GHL only if AI verified (7+)
                                send_to_ghl({
                                    'author': str(post.author),
                                    'url': f"https://reddit.com{post.permalink}",
                                    'title': post.title,
                                    'subreddit': subreddit_name,
                                    'keywords_found': found_keywords,
                                    'ai_score': ai_score,
                                    'ai_reasoning': ai_reasoning
                                })
                
                except Exception as e:
                    print(f"Error scraping r/{subreddit_name}: {e}")
                    continue
            
            scrape.last_run = datetime.utcnow()
            db.session.commit()
            print(f"Scrape {scrape.id} completed. Found {results_count} new results.")
            
        except Exception as e:
            print(f"Error running scrape {scrape_id}: {e}")
            db.session.rollback()

def run_all_scrapes():
    """Run all active scrapes"""
    with app.app_context():
        scrapes = Scrape.query.filter_by(is_active=True).all()
        for scrape in scrapes:
            run_scrape(scrape.id)

@app.route('/init-db')
def init_db():
    """Initialize database tables - run this once after deployment"""
    try:
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
        return "Database initialized successfully! You can now register and login."
    except Exception as e:
        return f"Error initializing database: {str(e)}"

@app.route('/reset-db')
def reset_db():
    """Drop all tables and recreate them - WARNING: deletes all data"""
    try:
        db.drop_all()
        db.create_all()
        # Create default admin user
        if not User.query.filter_by(username='admin').first():
            admin = User(
                username='admin',
                email='admin@example.com',
                password_hash=generate_password_hash('admin123'),
                is_admin=True
            )
            db.session.add(admin)
            db.session.commit()
        return "Database reset successfully! All tables recreated. You need to register again."
    except Exception as e:
        return f"Error resetting database: {str(e)}"

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return '''
        <h1>Reddit Scraper Platform</h1>
        <p>Automatically monitor Reddit for keywords and send leads to GoHighLevel</p>
        <a href="/login">Login</a> | <a href="/register">Register</a>
    '''

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('register'))
        
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        db.session.add(user)
        db.session.commit()
        
        flash('Account created!')
        return redirect(url_for('login'))
    
    return '''
        <h2>Register</h2>
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required><br>
            <input type="email" name="email" placeholder="Email" required><br>
            <input type="password" name="password" placeholder="Password" required><br>
            <button type="submit">Sign Up</button>
        </form>
        <a href="/">Back</a>
    '''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('dashboard'))
        
        flash('Invalid credentials')
    
    return '''
        <h2>Login</h2>
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required><br>
            <input type="password" name="password" placeholder="Password" required><br>
            <button type="submit">Login</button>
        </form>
        <a href="/register">Sign Up</a>
    '''

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    scrapes = Scrape.query.filter_by(user_id=session['user_id']).all()
    
    html = f'''
        <h1>Welcome {session["username"]}!</h1>
        <a href="/logout">Logout</a> | <a href="/create-scrape">Create New Scrape</a>
        
        <h2>Your Scrapes</h2>
        <table border="1" style="width:100%; border-collapse: collapse;">
            <tr style="background-color: #f2f2f2;">
                <th style="padding: 8px;">Name</th>
                <th style="padding: 8px;">Subreddits</th>
                <th style="padding: 8px;">Keywords</th>
                <th style="padding: 8px;">Status</th>
                <th style="padding: 8px;">Last Run</th>
                <th style="padding: 8px;">Results</th>
                <th style="padding: 8px;">Actions</th>
            </tr>
    '''
    
    for scrape in scrapes:
        result_count = Result.query.filter_by(scrape_id=scrape.id).count()
        verified_count = Result.query.filter_by(scrape_id=scrape.id).filter(Result.ai_score >= 7).count()
        status = "✅ Active" if scrape.is_active else "⏸️ Paused"
        last_run = scrape.last_run.strftime('%Y-%m-%d %H:%M') if scrape.last_run else 'Never'
        
        html += f'''
            <tr>
                <td style="padding: 8px;">{scrape.name}</td>
                <td style="padding: 8px;">{scrape.subreddits}</td>
                <td style="padding: 8px;">{scrape.keywords}</td>
                <td style="padding: 8px;">{status}</td>
                <td style="padding: 8px;">{last_run}</td>
                <td style="padding: 8px;"><a href="/results/{scrape.id}">{result_count} total ({verified_count} verified)</a></td>
                <td style="padding: 8px;">
                    <a href="/run-scrape/{scrape.id}">▶️ Run Now</a> | 
                    <a href="/toggle-scrape/{scrape.id}">⏯️ Toggle</a> |
                    <a href="/delete-scrape/{scrape.id}" onclick="return confirm('Delete this scrape?')">🗑️ Delete</a>
                </td>
            </tr>
        '''
    
    if not scrapes:
        html += '<tr><td colspan="7" style="padding: 20px; text-align: center;">No scrapes yet. Create your first one!</td></tr>'
    
    html += '''
        </table>
        <p><small>ℹ️ Scrapes run automatically every hour. Only AI-verified leads (score 7+) are sent to GoHighLevel.</small></p>
    '''
    
    return html

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
    
    return '''
        <h2>Create New Scrape</h2>
        <form method="POST" style="max-width: 500px;">
            <label><b>Name:</b></label><br>
            <input type="text" name="name" style="width: 100%; padding: 8px; margin: 5px 0 15px 0;" required><br>
            
            <label><b>Subreddits (comma-separated):</b></label><br>
            <input type="text" name="subreddits" placeholder="bookkeeping,smallbusiness,accounting" style="width: 100%; padding: 8px; margin: 5px 0 15px 0;" required><br>
            
            <label><b>Keywords (comma-separated):</b></label><br>
            <input type="text" name="keywords" placeholder="help,need,looking for" style="width: 100%; padding: 8px; margin: 5px 0 15px 0;" required><br>
            
            <label><b>Posts to check per subreddit:</b></label><br>
            <input type="number" name="limit" value="50" style="width: 100%; padding: 8px; margin: 5px 0 15px 0;"><br>
            
            <button type="submit" style="padding: 10px 20px; background: #4CAF50; color: white; border: none; cursor: pointer;">Create Scrape</button>
        </form>
        <br>
        <a href="/dashboard">← Back to Dashboard</a>
    '''

@app.route('/results/<int:scrape_id>')
@login_required
def view_results(scrape_id):
    scrape = Scrape.query.get_or_404(scrape_id)
    
    if scrape.user_id != session['user_id']:
        flash('Access denied')
        return redirect(url_for('dashboard'))
    
    # Get filter parameter
    verified_only = request.args.get('verified', 'true').lower() == 'true'
    
    # Query results based on filter
    query = Result.query.filter_by(scrape_id=scrape_id)
    if verified_only:
        query = query.filter(Result.ai_score >= 7)
    results = query.order_by(Result.created_at.desc()).all()
    
    # Get counts
    total_count = Result.query.filter_by(scrape_id=scrape_id).count()
    verified_count = Result.query.filter_by(scrape_id=scrape_id).filter(Result.ai_score >= 7).count()
    
    # Toggle button
    toggle_url = f"/results/{scrape_id}?verified={'false' if verified_only else 'true'}"
    current_filter = "AI Verified Only (7+)" if verified_only else "Show All"
    toggle_text = "Show All" if verified_only else "AI Verified Only"
    
    html = f'''
        <h1>Results for: {scrape.name}</h1>
        <a href="/dashboard">← Back to Dashboard</a>
        
        <div style="margin: 20px 0;">
            <b>Filter:</b> {current_filter} | 
            <a href="{toggle_url}" style="padding: 5px 15px; background: #2196F3; color: white; text-decoration: none; border-radius: 3px;">
                {toggle_text}
            </a>
        </div>
        
        <p><b>Showing {len(results)} of {total_count} posts</b> ({verified_count} verified by AI)</p>
        
        <table border="1" style="width:100%; border-collapse: collapse;">
            <tr style="background-color: #f2f2f2;">
                <th style="padding: 8px;">AI Score</th>
                <th style="padding: 8px;">Date</th>
                <th style="padding: 8px;">Subreddit</th>
                <th style="padding: 8px;">Title</th>
                <th style="padding: 8px;">Author</th>
                <th style="padding: 8px;">Upvotes</th>
                <th style="padding: 8px;">Keywords</th>
                <th style="padding: 8px;">AI Reasoning</th>
                <th style="padding: 8px;">Link</th>
            </tr>
    '''
    
    for result in results:
        # Color code based on AI score
        if result.ai_score >= 8:
            score_color = "#4CAF50"  # Green
            score_emoji = "✅"
        elif result.ai_score >= 6:
            score_color = "#FF9800"  # Orange
            score_emoji = "⚠️"
        else:
            score_color = "#f44336"  # Red
            score_emoji = "❌"
        
        html += f'''
            <tr>
                <td style="padding: 8px; text-align: center; background-color: {score_color}; color: white; font-weight: bold;">
                    {score_emoji} {result.ai_score}/10
                </td>
                <td style="padding: 8px;">{result.created_at.strftime('%Y-%m-%d %H:%M')}</td>
                <td style="padding: 8px;">r/{result.subreddit}</td>
                <td style="padding: 8px;">{result.title[:80]}...</td>
                <td style="padding: 8px;">u/{result.author}</td>
                <td style="padding: 8px;">{result.score}</td>
                <td style="padding: 8px;">{result.keywords_found}</td>
                <td style="padding: 8px;"><i>{result.ai_reasoning}</i></td>
                <td style="padding: 8px;"><a href="{result.url}" target="_blank">🔗 View</a></td>
            </tr>
        '''
    
    if not results:
        filter_msg = "No AI-verified results yet. Try 'Show All' to see unverified posts." if verified_only else "No results yet. Run the scrape to find matching posts!"
        html += f'<tr><td colspan="9" style="padding: 20px; text-align: center;">{filter_msg}</td></tr>'
    
    html += '</table>'
    return html

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

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_all_scrapes, trigger="interval", hours=1)
scheduler.start()

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
