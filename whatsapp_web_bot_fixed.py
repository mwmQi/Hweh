
#!/usr/bin/env python3
"""
WhatsApp Web Bot with Web Interface
A comprehensive WhatsApp bot with a web interface for session management,
configuration, and monitoring. Perfect for deployment on platforms like Render.
"""

import os
import json
import time
import base64
import asyncio
import aiohttp
import aiofiles
import uuid
import sqlite3
import logging
import threading
import qrcode
from PIL import Image
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session as flask_session
from flask_cors import CORS
import secrets

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
ADMIN_PHONE = os.getenv('ADMIN_PHONE', '+1234567890')
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(32))

# API Configuration (if still needed for other features)
FLUX_API_URL = "https://text2img.hideme.eu.org/image"
NSFW_API_URL = "https://nsfw.hosters.club/"
AI_API_URL = "https://api-chatgpt4.eternalowner06.workers.dev/"
API_BASE_URL = "https://computation-sizes-reasonable-moms.trycloudflare.com/api/search"

# Create directories
os.makedirs("wa-files", exist_ok=True)
os.makedirs("static", exist_ok=True)

# Global variables
executor = ThreadPoolExecutor(max_workers=50)
bot_instance = None
session_status = {"logged_in": False, "session_valid": False, "last_check": 0, "qr_generated": False, "waiting_for_scan": False}

# Database setup
DB_FILE = "whatsapp_bot_users.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (phone_number TEXT PRIMARY KEY, name TEXT, registered_at TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_config
                     (key TEXT PRIMARY KEY, value TEXT)''')
        conn.commit()

init_db()

def get_config(key, default=None):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
            result = c.fetchone()
            return result[0] if result else default
    except Exception as e:
        logger.error(f"Error getting config {key}: {e}")
        return default

def set_config(key, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
    except Exception as e:
        logger.error(f"Error setting config {key}: {e}")

class WhatsAppBot:
    def __init__(self, session_data=None, headless=True):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.session_data = session_data
        self.headless = headless
        self.is_running = False
        self.current_qr_base64 = None

    def start_playwright(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def stop_playwright(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def load_session(self):
        if self.session_data:
            # Load cookies
            if 'cookies' in self.session_data:
                self.context.add_cookies(self.session_data['cookies'])
            
            # Load local storage (Playwright doesn't have direct API for all local storage, need to execute JS)
            self.page.goto("https://web.whatsapp.com") # Navigate to the page first
            if 'local_storage' in self.session_data:
                for key, value in self.session_data['local_storage'].items():
                    self.page.evaluate(f"localStorage.setItem('{key}', '{value}');")
            
            # Load session storage (similar to local storage)
            if 'session_storage' in self.session_data:
                for key, value in self.session_data['session_storage'].items():
                    self.page.evaluate(f"sessionStorage.setItem('{key}', '{value}');")
            
            self.page.goto("https://web.whatsapp.com") # Refresh to apply storage
            logger.info("Session data loaded.")
            return True
        return False

    def get_qr_code(self):
        try:
            self.start_playwright()
            self.page.goto("https://web.whatsapp.com")
            self.page.wait_for_selector("[data-ref] canvas", timeout=30000)
            qr_code_element = self.page.query_selector("[data-ref] canvas")
            qr_code_base64 = self.page.evaluate("canvas => canvas.toDataURL('image/png').substring(22);", qr_code_element)
            self.current_qr_base64 = qr_code_base64
            session_status["qr_generated"] = True
            session_status["waiting_for_scan"] = True
            return qr_code_base64
        except Exception as e:
            logger.error(f"Error getting QR code: {e}")
            return None

    def wait_for_login_and_extract_session(self):
        try:
            self.page.wait_for_selector("#side", timeout=300000) # Wait for main chat list
            logger.info("Login successful! Extracting session data...")
            
            # Extract cookies
            cookies = self.context.cookies()
            
            # Extract local storage
            local_storage = self.page.evaluate("""
                var ls = {};
                for (var i = 0; i < localStorage.length; i++) {
                    var key = localStorage.key(i);
                    ls[key] = localStorage.getItem(key);
                }
                return ls;
            """)
            
            # Extract session storage
            session_storage = self.page.evaluate("""
                var ss = {};
                for (var i = 0; i < sessionStorage.length; i++) {
                    var key = sessionStorage.key(i);
                    ss[key] = sessionStorage.getItem(key);
                }
                return ss;
            """)
            
            session_data = {
                'cookies': cookies,
                'local_storage': local_storage,
                'session_storage': session_storage,
            }
            
            session_json = json.dumps(session_data, default=str)
            set_config('session_string', session_json) # Save as JSON string
            
            session_status["logged_in"] = True
            session_status["session_valid"] = True
            session_status["last_check"] = time.time()
            session_status["waiting_for_scan"] = False
            logger.info("Session extracted and saved successfully!")
            return True
        except Exception as e:
            logger.error(f"Error during login and session extraction: {e}")
            session_status["waiting_for_scan"] = False
            return False

    def get_phone_link_code(self, phone_number):
        try:
            self.start_playwright()
            self.page.goto("https://web.whatsapp.com")
            self.page.wait_for_selector("//span[@role='button' and contains(text(), 'Link with phone number')]").click()
            self.page.wait_for_selector("input[aria-label='Phone number']").fill(phone_number)
            self.page.wait_for_selector("//div[@role='button' and contains(text(), 'Next')]").click()
            
            code_elements = self.page.wait_for_selector("div[data-testid='link-code']")
            code = code_elements.text_content().replace(" ", "") # Remove spaces
            
            session_status["qr_generated"] = False # Not a QR, but a link code
            session_status["waiting_for_scan"] = True
            return {"success": True, "code": code}
        except Exception as e:
            logger.error(f"Error getting phone link code: {e}")
            screenshot_b64 = self.page.screenshot(encoding='base64') if self.page else None
            return {"success": False, "error": str(e), "screenshot": screenshot_b64}

    def start(self):
        try:
            self.start_playwright()
            saved_session = get_config('session_string')
            if saved_session:
                self.session_data = json.loads(saved_session)
                if self.load_session():
                    self.page.goto("https://web.whatsapp.com")
                    self.page.wait_for_selector("#side", timeout=60000) # Wait for main chat list
                    self.is_running = True
                    session_status["logged_in"] = True
                    session_status["session_valid"] = True
                    session_status["last_check"] = time.time()
                    logger.info("Bot started and session restored successfully!")
                    return True
            logger.warning("No valid session found or failed to restore. Please generate a new session.")
            return False
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            return False

    def stop(self):
        self.is_running = False
        self.stop_playwright()
        session_status["logged_in"] = False
        session_status["session_valid"] = False
        logger.info("WhatsApp bot stopped")

    # Placeholder for sending messages (to be implemented with Playwright)
    def send_message(self, chat_id, message):
        try:
            # Example: Find chat, type message, send
            # This is a simplified example and needs proper selectors and error handling
            self.page.wait_for_selector(f"span[title='{chat_id}']").click()
            self.page.wait_for_selector("div[data-testid='compose-box']").fill(message)
            self.page.press("div[data-testid='compose-box']", "Enter")
            logger.info(f"Message sent to {chat_id}: {message}")
            return True
        except Exception as e:
            logger.error(f"Error sending message to {chat_id}: {e}")
            return False

    # Placeholder for receiving messages (requires continuous polling or webhooks)
    def start_message_listener(self):
        logger.info("Message listener started (Playwright). This would involve polling or more advanced techniques.")
        # For a full implementation, you would continuously check for new messages
        # by observing the DOM or using WhatsApp Web's internal APIs if possible.

# Flask Web Application
app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)

# HTML Templates
MAIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WhatsApp Web Bot - Control Panel</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; color: #333; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { text-align: center; color: white; margin-bottom: 30px; }
        .header h1 { font-size: 2.5rem; margin-bottom: 10px; }
        .header p { font-size: 1.1rem; opacity: 0.9; }
        .card { background: white; border-radius: 15px; padding: 25px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); }
        .card h2 { color: #4a5568; margin-bottom: 15px; font-size: 1.5rem; }
        .status-card { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }
        .status-item { text-align: center; padding: 20px; border-radius: 10px; background: #f7fafc; }
        .status-item.online { background: #c6f6d5; border: 2px solid #38a169; }
        .status-item.offline { background: #fed7d7; border: 2px solid #e53e3e; }
        .status-item h3 { margin-bottom: 10px; font-size: 1.2rem; }
        .status-value { font-size: 2rem; font-weight: bold; color: #2d3748; }
        .btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 25px; border-radius: 8px; cursor: pointer; font-size: 1rem; text-decoration: none; display: inline-block; margin: 5px; }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(0,0,0,0.2); }
        .btn-danger { background: linear-gradient(135deg, #e53e3e 0%, #c53030 100%); }
        .btn-success { background: linear-gradient(135deg, #38a169 0%, #2f855a 100%); }
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 5px; font-weight: 600; color: #4a5568; }
        .form-group textarea { width: 100%; padding: 12px; border: 2px solid #e2e8f0; border-radius: 8px; font-size: 1rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .footer { text-align: center; color: white; margin-top: 40px; opacity: 0.8; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header"><h1>🚀 WhatsApp Web Bot</h1><p>Control Panel & Session Management</p></div>
        <div class="card">
            <h2>📊 Bot Status</h2>
            <div class="status-card">
                <div class="status-item {{ 'online' if session_status.logged_in else 'offline' }}"><h3>Bot Status</h3><div class="status-value">{{ 'Online' if session_status.logged_in else 'Offline' }}</div></div>
                <div class="status-item {{ 'online' if session_status.session_valid else 'offline' }}"><h3>Session Status</h3><div class="status-value">{{ 'Valid' if session_status.session_valid else 'Invalid' }}</div></div>
            </div>
        </div>
        <div class="grid">
            <div class="card">
                <h2>🔐 Session Management</h2>
                {% if session_status.logged_in %}
                    <div style="color: green; margin-bottom: 10px;">✅ Session is active!</div>
                    <a href="/regenerate-session" class="btn btn-danger">Logout Session</a>
                {% else %}
                    <div style="color: red; margin-bottom: 10px;">❌ No active session. Please connect.</div>
                    <a href="/generate-session" class="btn">Generate via QR Code</a>
                    <a href="/link-with-phone" class="btn">Link with Phone Number</a>
                    <form method="POST" action="/load-session-json" style="margin-top: 20px;">
                        <div class="form-group">
                            <label for="session_json">Load Session from JSON:</label>
                            <textarea name="session_json" id="session_json" rows="8" placeholder="Paste your session JSON here..."></textarea>
                        </div>
                        <button type="submit" class="btn">Load Session</button>
                    </form>
                {% endif %}
            </div>
            <div class="card">
                <h2>⚙️ Bot Control</h2>
                <a href="/start-bot" class="btn btn-success">Start Bot</a>
                <a href="/stop-bot" class="btn btn-danger">Stop Bot</a>
                <a href="/restart-bot" class="btn">Restart Bot</a>
            </div>
        </div>
        <div class="footer"><p>WhatsApp Web Bot v2.0 | Made with ❤️ for automation</p></div>
    </div>
    <script>
        // Auto-refresh status every 30 seconds
        setInterval(function() {
            location.reload();
        }, 30000);
    </script>
</body>
</html>
"""

QR_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Generate Session via QR Code</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        .container { max-width: 600px; background: white; border-radius: 15px; padding: 40px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); text-align: center; }
        h1 { color: #4a5568; margin-bottom: 10px; }
        p { color: #718096; font-size: 1.1rem; }
        .qr-code { max-width: 300px; margin: 20px auto; border: 3px solid #e2e8f0; border-radius: 10px; }
        .btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 25px; border-radius: 8px; cursor: pointer; text-decoration: none; display: inline-block; margin-top: 20px;}
    </style>
    <script>
        let checkInterval = setInterval(() => {
            fetch('/check-session-status')
                .then(response => response.json())
                .then(data => {
                    if (data.logged_in) {
                        clearInterval(checkInterval);
                        window.location.href = '/';
                    }
                });
        }, 5000);
        setTimeout(() => clearInterval(checkInterval), 300000);
    </script>
</head>
<body>
    <div class="container">
        <h1>📱 Scan QR Code</h1>
        <p>Open WhatsApp on your phone, go to Linked Devices and scan the QR code.</p>
        {% if qr_code %}
            <img src="data:image/png;base64,{{ qr_code }}" alt="WhatsApp QR Code" class="qr-code">
        {% else %}
            <p style="color: red;">❌ Failed to generate QR code. Please try again.</p>
        {% endif %}
        <div><a href="/" class="btn">← Back to Dashboard</a></div>
    </div>
</body>
</html>
"""

PHONE_LINK_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Link with Phone Number</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        .container { max-width: 600px; background: white; border-radius: 15px; padding: 40px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); text-align: center; }
        h1 { color: #4a5568; margin-bottom: 20px; }
        .form-group { margin-bottom: 20px; text-align: left; }
        label { display: block; margin-bottom: 5px; font-weight: 600; color: #4a5568; }
        input { width: 100%; padding: 12px; border: 2px solid #e2e8f0; border-radius: 8px; font-size: 1rem; }
        .btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 25px; border-radius: 8px; cursor: pointer; text-decoration: none; display: inline-block; margin-top: 10px;}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔗 Link with Phone Number</h1>
        <form method="POST" action="/link-with-phone">
            <div class="form-group">
                <label for="phone_number">Enter your full phone number (with country code):</label>
                <input type="text" name="phone_number" id="phone_number" placeholder="+1234567890" required>
            </div>
            <button type="submit" class="btn">Get Code</button>
        </form>
        <div style="margin-top:20px;"><a href="/" class="btn">← Back to Dashboard</a></div>
    </div>
</body>
</html>
"""

SHOW_CODE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Enter Your Code</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        .container { max-width: 600px; background: white; border-radius: 15px; padding: 40px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); text-align: center; }
        h1 { color: #4a5568; margin-bottom: 20px; }
        p { color: #718096; font-size: 1.1rem; }
        .code { font-size: 3rem; font-weight: bold; color: #2d3748; letter-spacing: 10px; margin: 30px 0; background: #f7fafc; padding: 20px; border-radius: 10px; }
        .btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 25px; border-radius: 8px; cursor: pointer; text-decoration: none; display: inline-block; margin-top: 20px;}
    </style>
    <script>
        let checkInterval = setInterval(() => {
            fetch('/check-session-status')
                .then(response => response.json())
                .then(data => {
                    if (data.logged_in) {
                        clearInterval(checkInterval);
                        window.location.href = '/';
                    }
                });
        }, 5000);
        setTimeout(() => clearInterval(checkInterval), 300000);
    </script>
</head>
<body>
    <div class="container">
        <h1>✅ Enter This Code on Your Phone</h1>
        <p>Open WhatsApp on your phone, go to Linked Devices → Link with phone number, and enter the code below.</p>
        <div class="code">{{ code }}</div>
        <p>Waiting for you to enter the code...</p>
        <div><a href="/" class="btn">← Back to Dashboard</a></div>
    </div>
</body>
</html>
"""

DEBUG_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Bot Error</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #fbe9e7; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        .container { max-width: 90%; background: white; border-radius: 15px; padding: 40px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); text-align: center; }
        h1 { color: #c62828; }
        p { color: #444; font-family: monospace; background: #eee; padding: 15px; border-radius: 8px; text-align: left; }
        img { max-width: 100%; border: 2px solid #ddd; margin-top: 20px; }
        .btn { background: #c62828; color: white; border: none; padding: 12px 25px; border-radius: 8px; cursor: pointer; text-decoration: none; display: inline-block; margin-top: 20px;}
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Oops! The Bot Got Stuck.</h1>
        <p><b>Error:</b> {{ error }}</p>
        {% if screenshot_b64 %}
            <h2>Here's what the bot was seeing:</h2>
            <img src="data:image/png;base64,{{ screenshot_b64 }}" alt="Bot Screenshot">
        {% else %}
            <p>No screenshot available.</p>
        {% endif %}
        <div><a href="/" class="btn">← Back to Dashboard</a></div>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    # Check session status for Playwright
    global bot_instance
    if bot_instance and bot_instance.is_running:
        session_status["logged_in"] = True
        session_status["session_valid"] = True
    else:
        session_status["logged_in"] = False
        session_status["session_valid"] = False

    return render_template_string(MAIN_TEMPLATE, session_status=session_status)

@app.route('/generate-session')
def generate_session():
    global bot_instance
    bot_instance = WhatsAppBot(headless=True)
    try:
        bot_instance.start_playwright()
        qr_code = bot_instance.get_qr_code()
        if qr_code:
            threading.Thread(target=bot_instance.wait_for_login_and_extract_session, daemon=True).start()
            return render_template_string(QR_TEMPLATE, qr_code=qr_code)
        else:
            return render_template_string(DEBUG_TEMPLATE, error="Failed to get QR code.", screenshot_b64=None)
    except Exception as e:
        logger.error(f"Error in generate_session: {e}")
        return render_template_string(DEBUG_TEMPLATE, error=str(e), screenshot_b64=None)

@app.route('/check-session-status')
def check_session_status():
    return jsonify(session_status)

@app.route('/link-with-phone', methods=['GET', 'POST'])
def link_with_phone():
    global bot_instance
    if request.method == 'POST':
        phone_number = request.form.get('phone_number')
        bot_instance = WhatsAppBot(headless=True)
        try:
            bot_instance.start_playwright()
            result = bot_instance.get_phone_link_code(phone_number)
            if result["success"]:
                threading.Thread(target=bot_instance.wait_for_login_and_extract_session, daemon=True).start()
                return render_template_string(SHOW_CODE_TEMPLATE, code=result["code"])
            else:
                return render_template_string(DEBUG_TEMPLATE, error=result["error"], screenshot_b64=result["screenshot"])
        except Exception as e:
            logger.error(f"Error in link_with_phone: {e}")
            return render_template_string(DEBUG_TEMPLATE, error=str(e), screenshot_b64=None)
    return render_template_string(PHONE_LINK_TEMPLATE)

@app.route('/load-session-json', methods=['POST'])
def load_session_json():
    global bot_instance
    session_json_str = request.form.get('session_json')
    if not session_json_str:
        flask_session['message'] = "No session JSON provided."
        flask_session['message_type'] = "error"
        return redirect(url_for('dashboard'))

    try:
        session_data = json.loads(session_json_str)
        bot_instance = WhatsAppBot(session_data=session_data, headless=True)
        if bot_instance.start():
            flask_session['message'] = "Session loaded and bot started successfully!"
            flask_session['message_type'] = "success"
        else:
            flask_session['message'] = "Failed to load session or start bot."
            flask_session['message_type'] = "error"
    except json.JSONDecodeError:
        flask_session['message'] = "Invalid JSON format."
        flask_session['message_type'] = "error"
    except Exception as e:
        logger.error(f"Error loading session JSON: {e}")
        flask_session['message'] = f"Error loading session: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/start-bot')
def start_bot_route():
    global bot_instance
    if bot_instance and bot_instance.is_running:
        flask_session['message'] = "Bot is already running."
        flask_session['message_type'] = "info"
        return redirect(url_for('dashboard'))

    bot_instance = WhatsAppBot(headless=True)
    if bot_instance.start():
        flask_session['message'] = "Bot started successfully!"
        flask_session['message_type'] = "success"
    else:
        flask_session['message'] = "Failed to start bot. Please generate or load a valid session."
        flask_session['message_type'] = "error"
    return redirect(url_for('dashboard'))

@app.route('/stop-bot')
def stop_bot_route():
    global bot_instance
    if bot_instance:
        bot_instance.stop()
        flask_session['message'] = "Bot stopped successfully!"
        flask_session['message_type'] = "success"
    else:
        flask_session['message'] = "Bot is not running."
        flask_session['message_type'] = "info"
    return redirect(url_for('dashboard'))

@app.route('/regenerate-session')
def regenerate_session():
    global bot_instance
    if bot_instance:
        bot_instance.stop()
    set_config('session_string', '') # Clear saved session
    session_status["logged_in"] = False
    session_status["session_valid"] = False
    session_status["qr_generated"] = False
    session_status["waiting_for_scan"] = False
    flask_session['message'] = "Session cleared. Please generate a new one."
    flask_session['message_type'] = "info"
    return redirect(url_for('dashboard'))

@app.route('/restart-bot')
def restart_bot_route():
    global bot_instance
    if bot_instance:
        bot_instance.stop()
    time.sleep(2) # Give some time for the browser to close
    start_bot_route()
    return redirect(url_for('dashboard'))

def main():
    print("Starting WhatsApp Web Bot with Web Interface (Playwright version)...")
    print("=" * 50)
    print("🌐 Web Interface: http://localhost:8000")
    print("=" * 50)
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()
