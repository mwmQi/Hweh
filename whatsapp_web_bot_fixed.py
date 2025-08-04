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
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session as flask_session
from flask_cors import CORS
import secrets

# Import Green-API client
from green_api import GreenApi

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
ADMIN_PHONE = os.getenv('ADMIN_PHONE', '+1234567890')
SESSION_STRING = os.getenv('SESSION_STRING', '') # Not directly used by Green-API, but kept for consistency
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(32))

# Green-API credentials (from environment variables)
GREEN_API_INSTANCE_ID = os.getenv('GREEN_API_INSTANCE_ID')
GREEN_API_TOKEN = os.getenv('GREEN_API_TOKEN')

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
    def __init__(self, instance_id, api_token):
        self.green_api = GreenApi(instance_id, api_token)
        self.is_running = False
        self.current_qr_base64 = None

    def get_qr_code(self):
        try:
            response = self.green_api.account.getQr()
            if response.code == 200 and response.data.qrCode:
                self.current_qr_base64 = response.data.qrCode
                session_status["qr_generated"] = True
                session_status["waiting_for_scan"] = True
                return response.data.qrCode
            else:
                logger.error(f"Green-API getQr failed: {response.error}")
                return None
        except Exception as e:
            logger.error(f"Error getting QR code from Green-API: {e}")
            return None

    def wait_for_login_and_extract_session(self):
        # For Green-API, session is managed by their service. We just check status.
        try:
            # Poll status until authorized
            max_attempts = 60 # Check for 5 minutes (60 * 5 seconds)
            for _ in range(max_attempts):
                response = self.green_api.account.getStateInstance()
                if response.code == 200 and response.data.stateInstance == 'authorized':
                    session_status["logged_in"] = True
                    session_status["session_valid"] = True
                    session_status["last_check"] = time.time()
                    session_status["waiting_for_scan"] = False
                    logger.info("Green-API instance authorized successfully!")
                    return True
                elif response.code == 200 and response.data.stateInstance == 'notAuthorized':
                    logger.info("Green-API instance not yet authorized. Waiting...")
                else:
                    logger.error(f"Green-API getStateInstance failed: {response.error}")
                time.sleep(5)
            logger.error("Timeout waiting for Green-API instance to be authorized.")
            session_status["waiting_for_scan"] = False
            return False
        except Exception as e:
            logger.error(f"Error waiting for Green-API login: {e}")
            session_status["waiting_for_scan"] = False
            return False

    def get_phone_link_code(self, phone_number):
        try:
            response = self.green_api.account.getLink(phoneNumber=phone_number)
            if response.code == 200 and response.data.linkCode:
                session_status["qr_generated"] = False # Not a QR, but a link code
                session_status["waiting_for_scan"] = True
                return {"success": True, "code": response.data.linkCode}
            else:
                logger.error(f"Green-API getLink failed: {response.error}")
                return {"success": False, "error": response.error, "screenshot": None}
        except Exception as e:
            logger.error(f"Error getting phone link code from Green-API: {e}")
            return {"success": False, "error": str(e), "screenshot": None}

    def start(self):
        # For Green-API, starting means ensuring the instance is authorized
        try:
            response = self.green_api.account.getStateInstance()
            if response.code == 200 and response.data.stateInstance == 'authorized':
                self.is_running = True
                session_status["logged_in"] = True
                session_status["session_valid"] = True
                session_status["last_check"] = time.time()
                logger.info("Green-API instance is authorized and bot is running.")
                return True
            else:
                logger.warning(f"Green-API instance not authorized: {response.data.stateInstance}. Cannot start bot.")
                session_status["logged_in"] = False
                session_status["session_valid"] = False
                return False
        except Exception as e:
            logger.error(f"Error starting bot with Green-API: {e}")
            return False

    def stop(self):
        # For Green-API, stopping means logging out the instance
        try:
            response = self.green_api.account.logout()
            if response.code == 200:
                self.is_running = False
                session_status["logged_in"] = False
                session_status["session_valid"] = False
                logger.info("Green-API instance logged out.")
                return True
            else:
                logger.error(f"Green-API logout failed: {response.error}")
                return False
        except Exception as e:
            logger.error(f"Error stopping bot with Green-API: {e}")
            return False

    # Placeholder for sending messages (to be implemented with Green-API)
    def send_message(self, chat_id, message):
        try:
            response = self.green_api.sending.sendMessage(chatId=chat_id, message=message)
            if response.code == 200:
                logger.info(f"Message sent to {chat_id}: {message}")
                return True
            else:
                logger.error(f"Failed to send message to {chat_id}: {response.error}")
                return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    # Placeholder for receiving messages (to be implemented with Green-API webhooks)
    def start_message_listener(self):
        logger.info("Green-API message listener would typically be handled via webhooks.")
        logger.info("Please configure webhooks in your Green-API account for incoming messages.")
        # For a full implementation, you would set up a webhook endpoint in Flask
        # and process incoming messages from Green-API there.

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
        <p><b>Note:</b> Screenshots are not available with the Green-API. The error above is from the Green-API client.</p>
        <div><a href="/" class="btn">← Back to Dashboard</a></div>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    # For Green-API, we check the instance state directly
    global bot_instance
    if GREEN_API_INSTANCE_ID and GREEN_API_TOKEN:
        bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
        try:
            response = bot_instance.green_api.account.getStateInstance()
            if response.code == 200 and response.data.stateInstance == 'authorized':
                session_status["logged_in"] = True
                session_status["session_valid"] = True
            else:
                session_status["logged_in"] = False
                session_status["session_valid"] = False
        except Exception as e:
            logger.error(f"Error checking Green-API instance state: {e}")
            session_status["logged_in"] = False
            session_status["session_valid"] = False
    else:
        session_status["logged_in"] = False
        session_status["session_valid"] = False

    return render_template_string(MAIN_TEMPLATE, session_status=session_status)

@app.route('/generate-session')
def generate_session():
    global bot_instance
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        return render_template_string(DEBUG_TEMPLATE, error="Green-API credentials not set in environment variables.")

    bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
    qr_code = bot_instance.get_qr_code()
    if qr_code:
        threading.Thread(target=bot_instance.wait_for_login_and_extract_session, daemon=True).start()
        return render_template_string(QR_TEMPLATE, qr_code=qr_code)
    else:
        return render_template_string(DEBUG_TEMPLATE, error="Failed to get QR code from Green-API.")

@app.route('/check-session-status')
def check_session_status():
    return jsonify(session_status)

@app.route('/link-with-phone', methods=['GET', 'POST'])
def link_with_phone():
    global bot_instance
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        return render_template_string(DEBUG_TEMPLATE, error="Green-API credentials not set in environment variables.")

    if request.method == 'POST':
        phone_number = request.form.get('phone_number')
        bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
        result = bot_instance.get_phone_link_code(phone_number)
        if result["success"]:
            threading.Thread(target=bot_instance.wait_for_login_and_extract_session, daemon=True).start()
            return render_template_string(SHOW_CODE_TEMPLATE, code=result["code"])
        else:
            return render_template_string(DEBUG_TEMPLATE, error=result["error"], screenshot_b64=result["screenshot"])
    return render_template_string(PHONE_LINK_TEMPLATE)

@app.route('/start-bot')
def start_bot_route():
    global bot_instance
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        flask_session['message'] = "Green-API credentials not set. Cannot start bot."
        flask_session['message_type'] = "error"
        return redirect(url_for('dashboard'))

    bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
    if bot_instance.start():
        flask_session['message'] = "Bot started successfully!"
        flask_session['message_type'] = "success"
    else:
        flask_session['message'] = "Failed to start bot. Check Green-API instance status."
        flask_session['message_type'] = "error"
    return redirect(url_for('dashboard'))

@app.route('/stop-bot')
def stop_bot_route():
    global bot_instance
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        flask_session['message'] = "Green-API credentials not set. Cannot stop bot."
        flask_session['message_type'] = "error"
        return redirect(url_for('dashboard'))

    bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
    if bot_instance.stop():
        flask_session['message'] = "Bot stopped successfully!"
        flask_session['message_type'] = "success"
    else:
        flask_session['message'] = "Failed to stop bot."
        flask_session['message_type'] = "error"
    return redirect(url_for('dashboard'))

@app.route('/regenerate-session')
def regenerate_session():
    # For Green-API, this means logging out the current session
    global bot_instance
    if GREEN_API_INSTANCE_ID and GREEN_API_TOKEN:
        bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
        bot_instance.stop() # This logs out the instance
    
    session_status["logged_in"] = False
    session_status["session_valid"] = False
    session_status["qr_generated"] = False
    session_status["waiting_for_scan"] = False
    flask_session['message'] = "Session logged out. Please generate a new one."
    flask_session['message_type'] = "info"
    return redirect(url_for('dashboard'))

@app.route('/restart-bot')
def restart_bot_route():
    global bot_instance
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        flask_session['message'] = "Green-API credentials not set. Cannot restart bot."
        flask_session['message_type'] = "error"
        return redirect(url_for('dashboard'))

    bot_instance = WhatsAppBot(GREEN_API_INSTANCE_ID, GREEN_API_TOKEN)
    bot_instance.stop()
    time.sleep(2) # Give some time for the instance to log out
    if bot_instance.start():
        flask_session['message'] = "Bot restarted successfully!"
        flask_session['message_type'] = "success"
    else:
        flask_session['message'] = "Failed to restart bot. Check Green-API instance status."
        flask_session['message_type'] = "error"
    return redirect(url_for('dashboard'))

def main():
    print("Starting WhatsApp Web Bot with Web Interface (Green-API version)...")
    print("=" * 50)
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        print("WARNING: GREEN_API_INSTANCE_ID or GREEN_API_TOKEN not set.")
        print("Please set these environment variables for the bot to function.")
    print("🌐 Web Interface: http://localhost:8000")
    print("=" * 50)
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()