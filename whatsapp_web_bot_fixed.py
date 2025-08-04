#!/usr/bin/env python3
"""
WhatsApp Web Bot with Enhanced Web Interface
Fixed version with login page, improved session generation, and Render optimization
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
import hashlib
import random
import string
import qrcode
import io
from PIL import Image
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session as flask_session, flash
from flask_cors import CORS
import secrets
from webdriver_manager.chrome import ChromeDriverManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
ADMIN_PHONE = os.getenv('ADMIN_PHONE', '+1234567890')
SESSION_STRING = os.getenv('SESSION_STRING', '')
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(32))
LOGIN_PASSWORD = os.getenv('LOGIN_PASSWORD', 'admin123')

# API Configuration
FLUX_API_URL = "https://text2img.hideme.eu.org/image"
NSFW_API_URL = "https://nsfw.hosters.club/"
AI_API_URL = "https://api-chatgpt4.eternalowner06.workers.dev/"
API_BASE_URL = "https://computation-sizes-reasonable-moms.trycloudflare.com/api/search"

# Create directories
os.makedirs("wa-files", exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Global variables
executor = ThreadPoolExecutor(max_workers=50)
bot_instance = None
current_qr_code = None
session_status = {
    "logged_in": False, 
    "session_valid": False, 
    "last_check": 0,
    "qr_generated": False,
    "waiting_for_scan": False,
    "error_message": None
}

# Database setup
DB_FILE = "whatsapp_bot_users.db"

def init_db():
    """Initialize database"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS users
                         (phone_number TEXT PRIMARY KEY, name TEXT, registered_at TIMESTAMP)''')
            c.execute('''CREATE TABLE IF NOT EXISTS bot_config
                         (key TEXT PRIMARY KEY, value TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS web_sessions
                         (session_id TEXT PRIMARY KEY, phone_number TEXT, created_at TIMESTAMP, expires_at TIMESTAMP)''')
            conn.commit()
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

init_db()

def get_config(key, default=None):
    """Get configuration value"""
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
    """Set configuration value"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
    except Exception as e:
        logger.error(f"Error setting config {key}: {e}")

class WhatsAppWebBotFixed:
    def __init__(self, session_string=None, headless=True):
        """Initialize WhatsApp Web Bot with fixes"""
        self.driver = None
        self.session_string = session_string
        self.headless = headless
        self.profile_path = os.path.join(os.getcwd(), "whatsapp_profile")
        self.is_running = False
        self.is_extracting_session = False
        self.current_qr_base64 = None
        self.session_extraction_thread = None
        
    def setup_driver(self):
        """Setup Chrome WebDriver with improved configuration"""
        try:
            chrome_options = Options()
            
            # Create profile directory
            os.makedirs(self.profile_path, exist_ok=True)
            
            # Chrome options optimized for Render
            chrome_options.add_argument(f"--user-data-dir={self.profile_path}")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-plugins")
            chrome_options.add_argument("--disable-background-timer-throttling")
            chrome_options.add_argument("--disable-backgrounding-occluded-windows")
            chrome_options.add_argument("--disable-renderer-backgrounding")
            chrome_options.add_argument("--disable-features=TranslateUI")
            chrome_options.add_argument("--disable-ipc-flooding-protection")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            if self.headless:
                chrome_options.add_argument("--headless=new")
            
            # Disable notifications and popups
            prefs = {
                "profile.default_content_setting_values.notifications": 2,
                "profile.default_content_settings.popups": 0,
                "profile.managed_default_content_settings.images": 1,
                "profile.default_content_setting_values.media_stream_mic": 2,
                "profile.default_content_setting_values.media_stream_camera": 2,
                "profile.default_content_setting_values.geolocation": 2
            }
            chrome_options.add_experimental_option("prefs", prefs)
            
            # Use environment variables for Chrome binary and driver paths
            chrome_bin = os.getenv('GOOGLE_CHROME_BIN')
            if chrome_bin:
                chrome_options.binary_location = chrome_bin
            
            chromedriver_path = os.getenv('CHROMEDRIVER_PATH')
            
            try:
                if chromedriver_path and os.path.exists(chromedriver_path):
                    service = Service(chromedriver_path)
                    self.driver = webdriver.Chrome(service=service, options=chrome_options)
                else:
                    # Use webdriver-manager as fallback
                    service = Service(ChromeDriverManager().install())
                    self.driver = webdriver.Chrome(service=service, options=chrome_options)
            except Exception as e:
                logger.warning(f"Failed to use specified ChromeDriver, trying webdriver-manager: {e}")
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
            
            # Set timeouts
            self.driver.set_page_load_timeout(30)
            self.driver.implicitly_wait(10)
            
            # Execute script to hide webdriver property
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            logger.info("Chrome WebDriver initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error setting up WebDriver: {e}")
            session_status["error_message"] = f"WebDriver setup failed: {str(e)}"
            return False
    
    def get_qr_code(self):
        """Get QR code from WhatsApp Web with improved error handling"""
        try:
            if not self.driver:
                logger.error("Driver not initialized")
                return None
                
            logger.info("Navigating to WhatsApp Web...")
            self.driver.get("https://web.whatsapp.com")
            
            # Wait for page to load
            time.sleep(5)
            
            # Check if already logged in
            try:
                chat_list = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='chat-list']"))
                )
                logger.info("Already logged in to WhatsApp Web")
                session_status["logged_in"] = True
                session_status["session_valid"] = True
                return "already_logged_in"
            except TimeoutException:
                logger.info("Not logged in, looking for QR code...")
            
            # Look for QR code with multiple selectors
            qr_selectors = [
                "[data-ref] canvas",
                "canvas[aria-label*='QR']",
                "div[data-ref] canvas",
                "canvas"
            ]
            
            qr_element = None
            for selector in qr_selectors:
                try:
                    qr_element = WebDriverWait(self.driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    logger.info(f"Found QR code with selector: {selector}")
                    break
                except TimeoutException:
                    continue
            
            if not qr_element:
                logger.error("QR code not found")
                session_status["error_message"] = "QR code not found on page"
                return None
            
            # Get QR code as base64
            try:
                qr_code = self.driver.execute_script("""
                    var canvas = arguments[0];
                    return canvas.toDataURL('image/png').substring(22);
                """, qr_element)
                
                if qr_code and len(qr_code) > 100:  # Valid base64 should be longer
                    self.current_qr_base64 = qr_code
                    session_status["qr_generated"] = True
                    session_status["waiting_for_scan"] = True
                    session_status["error_message"] = None
                    logger.info("QR code extracted successfully")
                    return qr_code
                else:
                    logger.error("Invalid QR code data")
                    return None
                    
            except Exception as e:
                logger.error(f"Error extracting QR code: {e}")
                return None
            
        except Exception as e:
            logger.error(f"Error getting QR code: {e}")
            session_status["error_message"] = f"QR code generation failed: {str(e)}"
            return None
    
    def wait_for_login_and_extract_session(self):
        """Wait for login and extract session data with improved handling"""
        try:
            logger.info("Waiting for QR code scan...")
            session_status["waiting_for_scan"] = True
            
            # Wait for login with longer timeout
            try:
                chat_list = WebDriverWait(self.driver, 300).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='chat-list']"))
                )
                logger.info("Login successful! Extracting session data...")
                session_status["waiting_for_scan"] = False
                
            except TimeoutException:
                logger.error("Login timeout - QR code was not scanned")
                session_status["waiting_for_scan"] = False
                session_status["error_message"] = "Login timeout - QR code was not scanned within 5 minutes"
                return None
            
            # Wait a bit for page to fully load
            time.sleep(5)
            
            # Extract session data
            session_data = self.extract_session_data()
            if session_data:
                # Generate session string
                session_json = json.dumps(session_data, default=str)
                session_string = base64.b64encode(session_json.encode()).decode()
                
                # Save to database
                set_config('session_string', session_string)
                
                # Update global session string
                global SESSION_STRING
                SESSION_STRING = session_string
                
                # Update status
                session_status["logged_in"] = True
                session_status["session_valid"] = True
                session_status["last_check"] = time.time()
                session_status["error_message"] = None
                
                logger.info("Session extracted and saved successfully!")
                return session_string
            else:
                logger.error("Failed to extract session data")
                session_status["error_message"] = "Failed to extract session data"
                return None
            
        except Exception as e:
            logger.error(f"Error during login and session extraction: {e}")
            session_status["error_message"] = f"Session extraction failed: {str(e)}"
            session_status["waiting_for_scan"] = False
            return None
    
    def extract_session_data(self):
        """Extract session data from browser with improved error handling"""
        try:
            logger.info("Extracting session data...")
            
            # Get all cookies
            cookies = self.driver.get_cookies()
            logger.info(f"Extracted {len(cookies)} cookies")
            
            # Get local storage data
            local_storage = {}
            try:
                local_storage = self.driver.execute_script("""
                    var ls = {};
                    try {
                        for (var i = 0; i < localStorage.length; i++) {
                            var key = localStorage.key(i);
                            ls[key] = localStorage.getItem(key);
                        }
                    } catch (e) {
                        console.log('Error accessing localStorage:', e);
                    }
                    return ls;
                """)
                logger.info(f"Extracted {len(local_storage)} localStorage items")
            except Exception as e:
                logger.warning(f"Error extracting localStorage: {e}")
            
            # Get session storage data
            session_storage = {}
            try:
                session_storage = self.driver.execute_script("""
                    var ss = {};
                    try {
                        for (var i = 0; i < sessionStorage.length; i++) {
                            var key = sessionStorage.key(i);
                            ss[key] = sessionStorage.getItem(key);
                        }
                    } catch (e) {
                        console.log('Error accessing sessionStorage:', e);
                    }
                    return ss;
                """)
                logger.info(f"Extracted {len(session_storage)} sessionStorage items")
            except Exception as e:
                logger.warning(f"Error extracting sessionStorage: {e}")
            
            # Get user agent
            user_agent = self.driver.execute_script("return navigator.userAgent;")
            
            session_data = {
                'cookies': cookies,
                'local_storage': local_storage,
                'session_storage': session_storage,
                'user_agent': user_agent,
                'timestamp': time.time(),
                'url': self.driver.current_url
            }
            
            logger.info("Session data extracted successfully")
            return session_data
            
        except Exception as e:
            logger.error(f"Error extracting session data: {e}")
            return None
    
    def load_and_restore_session(self):
        """Load and restore session from session string with improved handling"""
        try:
            if not self.session_string:
                logger.error("No session string provided")
                return False
                
            logger.info("Loading and restoring session...")
            
            # Load session data
            try:
                session_json = base64.b64decode(self.session_string.encode()).decode()
                session_data = json.loads(session_json)
                logger.info("Session data decoded successfully")
            except Exception as e:
                logger.error(f"Error decoding session data: {e}")
                return False
            
            # Navigate to WhatsApp Web
            self.driver.get("https://web.whatsapp.com")
            time.sleep(3)
            
            # Restore cookies
            cookies_restored = 0
            for cookie in session_data.get('cookies', []):
                try:
                    self.driver.add_cookie(cookie)
                    cookies_restored += 1
                except Exception as e:
                    logger.debug(f"Failed to restore cookie {cookie.get('name', 'unknown')}: {e}")
            
            logger.info(f"Restored {cookies_restored} cookies")
            
            # Refresh and restore storage
            self.driver.refresh()
            time.sleep(3)
            
            # Restore local storage
            local_storage_restored = 0
            for key, value in session_data.get('local_storage', {}).items():
                try:
                    self.driver.execute_script(f"localStorage.setItem(arguments[0], arguments[1]);", key, value)
                    local_storage_restored += 1
                except Exception as e:
                    logger.debug(f"Failed to restore localStorage item {key}: {e}")
            
            logger.info(f"Restored {local_storage_restored} localStorage items")
            
            # Restore session storage
            session_storage_restored = 0
            for key, value in session_data.get('session_storage', {}).items():
                try:
                    self.driver.execute_script(f"sessionStorage.setItem(arguments[0], arguments[1]);", key, value)
                    session_storage_restored += 1
                except Exception as e:
                    logger.debug(f"Failed to restore sessionStorage item {key}: {e}")
            
            logger.info(f"Restored {session_storage_restored} sessionStorage items")
            
            # Final refresh
            self.driver.refresh()
            time.sleep(5)
            
            # Check if logged in
            try:
                chat_list = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='chat-list']"))
                )
                logger.info("Session restored successfully - logged in")
                session_status["logged_in"] = True
                session_status["session_valid"] = True
                session_status["last_check"] = time.time()
                return True
            except TimeoutException:
                logger.warning("Session restoration failed - not logged in")
                session_status["logged_in"] = False
                session_status["session_valid"] = False
                return False
            
        except Exception as e:
            logger.error(f"Error loading and restoring session: {e}")
            session_status["error_message"] = f"Session restoration failed: {str(e)}"
            return False
    
    def test_session_string(self, session_string):
        """Test if a session string is valid"""
        try:
            logger.info("Testing session string...")
            
            if not self.setup_driver():
                return False
            
            # Temporarily set session string
            old_session = self.session_string
            self.session_string = session_string
            
            # Try to restore session
            result = self.load_and_restore_session()
            
            # Restore original session string
            self.session_string = old_session
            
            return result
            
        except Exception as e:
            logger.error(f"Error testing session: {e}")
            return False
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
                self.driver = None
    
    def cleanup(self):
        """Cleanup resources"""
        try:
            if self.driver:
                self.driver.quit()
                self.driver = None
            logger.info("Bot cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

# Flask Web Application
app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)

# Login required decorator
def login_required(f):
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in flask_session or not flask_session['logged_in']:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# HTML Templates
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - WhatsApp Web Bot</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #333;
        }
        
        .login-container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            width: 100%;
            max-width: 400px;
            text-align: center;
        }
        
        .logo {
            font-size: 3rem;
            margin-bottom: 10px;
        }
        
        .login-container h1 {
            color: #4a5568;
            margin-bottom: 10px;
            font-size: 1.8rem;
        }
        
        .login-container p {
            color: #718096;
            margin-bottom: 30px;
        }
        
        .form-group {
            margin-bottom: 20px;
            text-align: left;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #4a5568;
        }
        
        .form-group input {
            width: 100%;
            padding: 15px;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 1rem;
            transition: border-color 0.3s ease;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 10px;
            cursor: pointer;
            font-size: 1.1rem;
            font-weight: 600;
            transition: all 0.3s ease;
            width: 100%;
            margin-top: 10px;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(0,0,0,0.2);
        }
        
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }
        
        .alert-error {
            background: #fed7d7;
            color: #c53030;
            border: 1px solid #e53e3e;
        }
        
        .login-options {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #e2e8f0;
        }
        
        .login-options h3 {
            color: #4a5568;
            margin-bottom: 15px;
            font-size: 1.1rem;
        }
        
        .option-btn {
            background: #f7fafc;
            color: #4a5568;
            border: 2px solid #e2e8f0;
            padding: 12px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.3s ease;
            width: 100%;
            margin: 5px 0;
        }
        
        .option-btn:hover {
            background: #edf2f7;
            border-color: #cbd5e0;
        }
        
        .footer {
            margin-top: 30px;
            color: #718096;
            font-size: 0.9rem;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">🚀</div>
        <h1>WhatsApp Web Bot</h1>
        <p>Admin Control Panel</p>
        
        {% if error %}
            <div class="alert alert-error">
                {{ error }}
            </div>
        {% endif %}
        
        <form method="POST">
            <div class="form-group">
                <label for="phone">Admin Phone Number:</label>
                <input type="text" name="phone" id="phone" placeholder="+1234567890" required>
            </div>
            
            <div class="form-group">
                <label for="password">Password:</label>
                <input type="password" name="password" id="password" placeholder="Enter admin password" required>
            </div>
            
            <button type="submit" class="btn">🔐 Login to Dashboard</button>
        </form>
        
        <div class="login-options">
            <h3>Alternative Login Methods</h3>
            <button class="option-btn" onclick="showPhoneCodeLogin()">📱 Login with Phone + Code</button>
            <button class="option-btn" onclick="showQRLogin()">📷 Login with QR Code</button>
        </div>
        
        <div class="footer">
            <p>Secure access to your WhatsApp bot management</p>
        </div>
    </div>
    
    <script>
        function showPhoneCodeLogin() {
            alert('Phone + Code login will be available in the next update!');
        }
        
        function showQRLogin() {
            alert('QR Code login will be available in the next update!');
        }
    </script>
</body>
</html>
"""

MAIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WhatsApp Web Bot - Control Panel</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        
        .navbar {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            padding: 15px 0;
            border-bottom: 1px solid rgba(255,255,255,0.2);
        }
        
        .navbar .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .navbar h1 {
            color: white;
            font-size: 1.5rem;
        }
        
        .navbar .user-info {
            color: white;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            text-align: center;
            color: white;
            margin-bottom: 30px;
        }
        
        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 10px;
        }
        
        .header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }
        
        .card {
            background: white;
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            transition: transform 0.3s ease;
        }
        
        .card:hover {
            transform: translateY(-5px);
        }
        
        .card h2 {
            color: #4a5568;
            margin-bottom: 15px;
            font-size: 1.5rem;
        }
        
        .status-card {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
        }
        
        .status-item {
            text-align: center;
            padding: 20px;
            border-radius: 10px;
            background: #f7fafc;
        }
        
        .status-item.online {
            background: #c6f6d5;
            border: 2px solid #38a169;
        }
        
        .status-item.offline {
            background: #fed7d7;
            border: 2px solid #e53e3e;
        }
        
        .status-item.warning {
            background: #fef5e7;
            border: 2px solid #ed8936;
        }
        
        .status-item h3 {
            margin-bottom: 10px;
            font-size: 1.2rem;
        }
        
        .status-value {
            font-size: 2rem;
            font-weight: bold;
            color: #2d3748;
        }
        
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 25px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1rem;
            transition: all 0.3s ease;
            text-decoration: none;
            display: inline-block;
            margin: 5px;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }
        
        .btn-danger {
            background: linear-gradient(135deg, #e53e3e 0%, #c53030 100%);
        }
        
        .btn-success {
            background: linear-gradient(135deg, #38a169 0%, #2f855a 100%);
        }
        
        .btn-warning {
            background: linear-gradient(135deg, #ed8936 0%, #dd6b20 100%);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-weight: 600;
            color: #4a5568;
        }
        
        .form-group input, .form-group textarea {
            width: 100%;
            padding: 12px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.3s ease;
        }
        
        .form-group input:focus, .form-group textarea:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        
        .alert-success {
            background: #c6f6d5;
            color: #2f855a;
            border: 1px solid #38a169;
        }
        
        .alert-error {
            background: #fed7d7;
            color: #c53030;
            border: 1px solid #e53e3e;
        }
        
        .alert-info {
            background: #bee3f8;
            color: #2b6cb0;
            border: 1px solid #3182ce;
        }
        
        .alert-warning {
            background: #fef5e7;
            color: #c05621;
            border: 1px solid #ed8936;
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }
        
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #f3f3f3;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .footer {
            text-align: center;
            color: white;
            margin-top: 40px;
            opacity: 0.8;
        }
        
        .session-info {
            background: #f7fafc;
            border-radius: 10px;
            padding: 15px;
            margin: 15px 0;
        }
        
        .session-info h4 {
            color: #4a5568;
            margin-bottom: 10px;
        }
        
        .session-info p {
            color: #718096;
            margin: 5px 0;
        }
    </style>
</head>
<body>
    <div class="navbar">
        <div class="container">
            <h1>🚀 WhatsApp Web Bot</h1>
            <div class="user-info">
                <span>Admin: {{ admin_phone }}</span>
                <a href="/logout" class="btn btn-danger">Logout</a>
            </div>
        </div>
    </div>
    
    <div class="container">
        {% if flask_session.get('message') %}
            <div class="alert alert-{{ flask_session.get('message_type', 'info') }}">
                {{ flask_session.pop('message') }}
            </div>
        {% endif %}
        
        {% if session_status.error_message %}
            <div class="alert alert-error">
                ❌ {{ session_status.error_message }}
            </div>
        {% endif %}
        
        <div class="card">
            <h2>📊 Bot Status</h2>
            <div class="status-card">
                <div class="status-item {{ 'online' if session_status.logged_in else 'offline' }}">
                    <h3>Bot Status</h3>
                    <div class="status-value">{{ 'Online' if session_status.logged_in else 'Offline' }}</div>
                </div>
                <div class="status-item {{ 'online' if session_status.session_valid else 'offline' }}">
                    <h3>Session Status</h3>
                    <div class="status-value">{{ 'Valid' if session_status.session_valid else 'Invalid' }}</div>
                </div>
                <div class="status-item {{ 'warning' if session_status.waiting_for_scan else 'online' if session_status.qr_generated else 'offline' }}">
                    <h3>QR Status</h3>
                    <div class="status-value">
                        {% if session_status.waiting_for_scan %}
                            Waiting
                        {% elif session_status.qr_generated %}
                            Ready
                        {% else %}
                            None
                        {% endif %}
                    </div>
                </div>
                <div class="status-item">
                    <h3>Total Users</h3>
                    <div class="status-value">{{ user_count }}</div>
                </div>
            </div>
        </div>
        
        <div class="grid">
            <div class="card">
                <h2>🔐 Session Management</h2>
                {% if session_string %}
                    <div class="alert alert-success">
                        ✅ Session string is configured!
                    </div>
                    <div class="session-info">
                        <h4>Current Session Info:</h4>
                        <p><strong>Length:</strong> {{ session_string|length }} characters</p>
                        <p><strong>Status:</strong> {{ 'Valid' if session_status.session_valid else 'Invalid' }}</p>
                        <p><strong>Last Check:</strong> {{ last_check }}</p>
                    </div>
                    <a href="/test-session" class="btn btn-success">Test Current Session</a>
                    <a href="/regenerate-session" class="btn btn-warning">Regenerate Session</a>
                {% else %}
                    <div class="alert alert-error">
                        ❌ No session string configured. Please generate one to start the bot.
                    </div>
                    <a href="/generate-session" class="btn">Generate Session String</a>
                {% endif %}
                
                <form method="POST" action="/update-session" style="margin-top: 20px;">
                    <div class="form-group">
                        <label for="session_string">Manual Session String Input:</label>
                        <textarea name="session_string" id="session_string" rows="4" placeholder="Paste your session string here...">{{ session_string or '' }}</textarea>
                    </div>
                    <button type="submit" class="btn">Update Session</button>
                </form>
            </div>
            
            <div class="card">
                <h2>⚙️ Bot Configuration</h2>
                <form method="POST" action="/update-config">
                    <div class="form-group">
                        <label for="admin_phone">Admin Phone Number:</label>
                        <input type="text" name="admin_phone" id="admin_phone" value="{{ admin_phone }}" placeholder="+1234567890">
                    </div>
                    <div class="form-group">
                        <label for="login_password">Login Password:</label>
                        <input type="password" name="login_password" id="login_password" placeholder="New login password">
                    </div>
                    <button type="submit" class="btn">Update Configuration</button>
                </form>
                
                <div style="margin-top: 20px;">
                    <a href="/start-bot" class="btn btn-success">Start Bot</a>
                    <a href="/stop-bot" class="btn btn-danger">Stop Bot</a>
                    <a href="/restart-bot" class="btn">Restart Bot</a>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>📱 Quick Actions</h2>
            <div class="grid">
                <div>
                    <h3>🔍 Session Management</h3>
                    <a href="/generate-session" class="btn">Generate New Session</a>
                    <a href="/test-session" class="btn">Test Session</a>
                </div>
                <div>
                    <h3>📊 Monitoring</h3>
                    <a href="/logs" class="btn">View Logs</a>
                    <a href="/users" class="btn">View Users</a>
                </div>
                <div>
                    <h3>🛠️ Utilities</h3>
                    <a href="/clear-data" class="btn btn-danger">Clear User Data</a>
                    <a href="/export-data" class="btn">Export Data</a>
                </div>
            </div>
        </div>
        
        <div class="footer">
            <p>WhatsApp Web Bot v3.0 | Enhanced with Login & Fixed Session Generation</p>
        </div>
    </div>
    
    <script>
        // Auto-refresh status every 30 seconds
        setInterval(function() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    // Update status without full page reload
                    console.log('Status updated:', data);
                })
                .catch(error => console.error('Error updating status:', error));
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Generate Session String - WhatsApp Web Bot</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .container {
            max-width: 700px;
            background: white;
            border-radius: 15px;
            padding: 40px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            text-align: center;
        }
        
        .header {
            margin-bottom: 30px;
        }
        
        .header h1 {
            color: #4a5568;
            margin-bottom: 10px;
        }
        
        .header p {
            color: #718096;
            font-size: 1.1rem;
        }
        
        .qr-container {
            margin: 30px 0;
        }
        
        .qr-code {
            max-width: 300px;
            margin: 20px auto;
            border: 3px solid #e2e8f0;
            border-radius: 10px;
        }
        
        .instructions {
            background: #f7fafc;
            border-radius: 10px;
            padding: 20px;
            margin: 20px 0;
            text-align: left;
        }
        
        .instructions h3 {
            color: #2d3748;
            margin-bottom: 15px;
        }
        
        .instructions ol {
            color: #4a5568;
            line-height: 1.6;
        }
        
        .instructions li {
            margin-bottom: 8px;
        }
        
        .status {
            margin: 20px 0;
            padding: 15px;
            border-radius: 8px;
            font-weight: 600;
        }
        
        .status.waiting {
            background: #bee3f8;
            color: #2b6cb0;
        }
        
        .status.success {
            background: #c6f6d5;
            color: #2f855a;
        }
        
        .status.error {
            background: #fed7d7;
            color: #c53030;
        }
        
        .status.generating {
            background: #fef5e7;
            color: #c05621;
        }
        
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 25px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1rem;
            transition: all 0.3s ease;
            text-decoration: none;
            display: inline-block;
            margin: 10px;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }
        
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #f3f3f3;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-right: 10px;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .progress-bar {
            width: 100%;
            height: 6px;
            background: #e2e8f0;
            border-radius: 3px;
            margin: 20px 0;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            width: 0%;
            transition: width 0.3s ease;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📱 Generate Session String</h1>
            <p>Scan the QR code with your WhatsApp to create a session</p>
        </div>
        
        <div id="qr-section">
            {% if qr_code == 'already_logged_in' %}
                <div class="status success">
                    ✅ Already logged in! Session will be extracted automatically.
                </div>
            {% elif qr_code %}
                <div class="qr-container">
                    <img src="data:image/png;base64,{{ qr_code }}" alt="WhatsApp QR Code" class="qr-code">
                </div>
                
                <div class="instructions">
                    <h3>📋 Instructions:</h3>
                    <ol>
                        <li>Open WhatsApp on your phone</li>
                        <li>Go to <strong>Settings</strong> → <strong>Linked Devices</strong></li>
                        <li>Tap <strong>"Link a Device"</strong></li>
                        <li>Scan the QR code above</li>
                        <li>Wait for the connection to complete</li>
                    </ol>
                </div>
                
                <div class="status waiting" id="status">
                    <div class="loading"></div>
                    Waiting for QR code to be scanned...
                </div>
                
                <div class="progress-bar">
                    <div class="progress-fill" id="progress"></div>
                </div>
            {% else %}
                <div class="status error">
                    ❌ Failed to generate QR code. Please try again.
                </div>
                <div id="error-details">
                    {% if session_status.error_message %}
                        <p><strong>Error:</strong> {{ session_status.error_message }}</p>
                    {% endif %}
                </div>
            {% endif %}
        </div>
        
        <div>
            <a href="/" class="btn">← Back to Dashboard</a>
            <button onclick="location.reload()" class="btn">🔄 Refresh QR Code</button>
        </div>
    </div>
    
    <script>
        let checkInterval;
        let progressInterval;
        let progress = 0;
        
        // Progress bar animation
        function startProgress() {
            progressInterval = setInterval(function() {
                progress += 0.5;
                if (progress > 100) progress = 0;
                document.getElementById('progress').style.width = progress + '%';
            }, 150);
        }
        
        function stopProgress() {
            if (progressInterval) {
                clearInterval(progressInterval);
                document.getElementById('progress').style.width = '100%';
            }
        }
        
        // Start progress animation if waiting
        {% if qr_code and qr_code != 'already_logged_in' %}
            startProgress();
        {% endif %}
        
        // Check session status every 3 seconds
        checkInterval = setInterval(function() {
            fetch('/check-session-status')
                .then(response => response.json())
                .then(data => {
                    const statusDiv = document.getElementById('status');
                    if (data.logged_in) {
                        statusDiv.className = 'status success';
                        statusDiv.innerHTML = '✅ Session created successfully! Redirecting...';
                        stopProgress();
                        clearInterval(checkInterval);
                        setTimeout(() => {
                            window.location.href = '/';
                        }, 2000);
                    } else if (data.error) {
                        statusDiv.className = 'status error';
                        statusDiv.innerHTML = '❌ ' + data.error;
                        stopProgress();
                        clearInterval(checkInterval);
                    } else if (data.waiting_for_scan) {
                        statusDiv.className = 'status waiting';
                        statusDiv.innerHTML = '<div class="loading"></div>Waiting for QR code to be scanned...';
                    }
                })
                .catch(error => {
                    console.error('Error checking session status:', error);
                });
        }, 3000);
        
        // Stop checking after 5 minutes
        setTimeout(() => {
            clearInterval(checkInterval);
            stopProgress();
            const statusDiv = document.getElementById('status');
            if (statusDiv && statusDiv.className.includes('waiting')) {
                statusDiv.className = 'status error';
                statusDiv.innerHTML = '⏰ QR code expired. Please refresh to generate a new one.';
            }
        }, 300000);
    </script>
</body>
</html>
"""

# Web Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '').strip()
        
        # Simple authentication (you can enhance this)
        admin_phone = get_config('admin_phone', ADMIN_PHONE)
        login_password = get_config('login_password', LOGIN_PASSWORD)
        
        if phone == admin_phone and password == login_password:
            flask_session['logged_in'] = True
            flask_session['admin_phone'] = phone
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error="Invalid phone number or password")
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    """Logout"""
    flask_session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    """Main dashboard"""
    current_session = get_config('session_string', SESSION_STRING)
    admin_phone = get_config('admin_phone', ADMIN_PHONE)
    
    # Get user count
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users")
            user_count = c.fetchone()[0]
    except:
        user_count = 0
    
    # Format last check time
    if session_status["last_check"]:
        last_check = time.strftime("%H:%M:%S", time.localtime(session_status["last_check"]))
    else:
        last_check = "Never"
    
    return render_template_string(MAIN_TEMPLATE, 
                                session_string=current_session,
                                admin_phone=admin_phone,
                                session_status=session_status,
                                user_count=user_count,
                                last_check=last_check,
                                flask_session=flask_session)

@app.route('/generate-session')
@login_required
def generate_session():
    """Generate new session string"""
    global bot_instance
    
    try:
        # Reset status
        session_status.update({
            "qr_generated": False,
            "waiting_for_scan": False,
            "error_message": None
        })
        
        # Create bot instance for session extraction
        bot_instance = WhatsAppWebBotFixed(headless=True)
        
        if bot_instance.setup_driver():
            qr_code = bot_instance.get_qr_code()
            
            if qr_code == "already_logged_in":
                # Already logged in, extract session immediately
                def extract_session():
                    session_string = bot_instance.wait_for_login_and_extract_session()
                    if session_string:
                        logger.info("Session string generated successfully!")
                
                threading.Thread(target=extract_session, daemon=True).start()
                return render_template_string(QR_TEMPLATE, qr_code="already_logged_in", session_status=session_status)
                
            elif qr_code:
                # Start session extraction in background
                def extract_session():
                    session_string = bot_instance.wait_for_login_and_extract_session()
                    if session_string:
                        logger.info("Session string generated successfully!")
                    else:
                        logger.error("Failed to generate session string")
                
                bot_instance.session_extraction_thread = threading.Thread(target=extract_session, daemon=True)
                bot_instance.session_extraction_thread.start()
                
                return render_template_string(QR_TEMPLATE, qr_code=qr_code, session_status=session_status)
        
        return render_template_string(QR_TEMPLATE, qr_code=None, session_status=session_status)
        
    except Exception as e:
        logger.error(f"Error generating session: {e}")
        session_status["error_message"] = f"Session generation failed: {str(e)}"
        return render_template_string(QR_TEMPLATE, qr_code=None, session_status=session_status)

@app.route('/check-session-status')
@login_required
def check_session_status():
    """Check if session has been created"""
    return jsonify({
        "logged_in": session_status["logged_in"],
        "session_valid": session_status["session_valid"],
        "waiting_for_scan": session_status["waiting_for_scan"],
        "qr_generated": session_status["qr_generated"],
        "error": session_status.get("error_message")
    })

@app.route('/update-session', methods=['POST'])
@login_required
def update_session():
    """Update session string"""
    try:
        new_session = request.form.get('session_string', '').strip()
        
        if new_session:
            # Test the session string
            test_bot = WhatsAppWebBotFixed(headless=True)
            if test_bot.test_session_string(new_session):
                set_config('session_string', new_session)
                global SESSION_STRING
                SESSION_STRING = new_session
                session_status["session_valid"] = True
                flask_session['message'] = "Session string updated and validated successfully!"
                flask_session['message_type'] = "success"
            else:
                flask_session['message'] = "Invalid session string. Please check and try again."
                flask_session['message_type'] = "error"
        else:
            flask_session['message'] = "Please provide a session string."
            flask_session['message_type'] = "error"
            
    except Exception as e:
        logger.error(f"Error updating session: {e}")
        flask_session['message'] = f"Error updating session: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/update-config', methods=['POST'])
@login_required
def update_config():
    """Update bot configuration"""
    try:
        admin_phone = request.form.get('admin_phone', '').strip()
        login_password = request.form.get('login_password', '').strip()
        
        if admin_phone:
            set_config('admin_phone', admin_phone)
            global ADMIN_PHONE
            ADMIN_PHONE = admin_phone
            flask_session['admin_phone'] = admin_phone
        
        if login_password:
            set_config('login_password', login_password)
            global LOGIN_PASSWORD
            LOGIN_PASSWORD = login_password
        
        if admin_phone or login_password:
            flask_session['message'] = "Configuration updated successfully!"
            flask_session['message_type'] = "success"
        else:
            flask_session['message'] = "Please provide values to update."
            flask_session['message_type'] = "error"
            
    except Exception as e:
        logger.error(f"Error updating config: {e}")
        flask_session['message'] = f"Error updating configuration: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/test-session')
@login_required
def test_session():
    """Test current session"""
    try:
        current_session = get_config('session_string', SESSION_STRING)
        if current_session:
            test_bot = WhatsAppWebBotFixed(headless=True)
            if test_bot.test_session_string(current_session):
                session_status["session_valid"] = True
                session_status["last_check"] = time.time()
                flask_session['message'] = "Session is valid and working!"
                flask_session['message_type'] = "success"
            else:
                session_status["session_valid"] = False
                flask_session['message'] = "Session is invalid or expired. Please regenerate."
                flask_session['message_type'] = "error"
        else:
            flask_session['message'] = "No session string found. Please generate one first."
            flask_session['message_type'] = "error"
            
    except Exception as e:
        logger.error(f"Error testing session: {e}")
        flask_session['message'] = f"Error testing session: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/regenerate-session')
@login_required
def regenerate_session():
    """Regenerate session string"""
    # Clear current session
    set_config('session_string', '')
    global SESSION_STRING
    SESSION_STRING = ''
    session_status.update({
        "session_valid": False,
        "logged_in": False,
        "qr_generated": False,
        "waiting_for_scan": False,
        "error_message": None
    })
    
    return redirect(url_for('generate_session'))

@app.route('/start-bot')
@login_required
def start_bot():
    """Start the bot"""
    try:
        flask_session['message'] = "Bot start functionality will be implemented in the next update!"
        flask_session['message_type'] = "info"
    except Exception as e:
        flask_session['message'] = f"Error starting bot: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/stop-bot')
@login_required
def stop_bot():
    """Stop the bot"""
    try:
        flask_session['message'] = "Bot stop functionality will be implemented in the next update!"
        flask_session['message_type'] = "info"
    except Exception as e:
        flask_session['message'] = f"Error stopping bot: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/restart-bot')
@login_required
def restart_bot():
    """Restart the bot"""
    try:
        flask_session['message'] = "Bot restart functionality will be implemented in the next update!"
        flask_session['message_type'] = "info"
    except Exception as e:
        flask_session['message'] = f"Error restarting bot: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/api/status')
def api_status():
    """API endpoint for bot status"""
    return jsonify({
        "status": "running" if session_status["logged_in"] else "stopped",
        "session_valid": session_status["session_valid"],
        "last_check": session_status["last_check"],
        "qr_generated": session_status["qr_generated"],
        "waiting_for_scan": session_status["waiting_for_scan"]
    })

@app.route('/users')
@login_required
def view_users():
    """View registered users"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT phone_number, name, registered_at FROM users ORDER BY registered_at DESC")
            users = c.fetchall()
        
        users_html = """
        <h2>📱 Registered Users</h2>
        <table style="width: 100%; border-collapse: collapse;">
            <tr style="background: #f7fafc;">
                <th style="padding: 10px; border: 1px solid #e2e8f0;">Phone</th>
                <th style="padding: 10px; border: 1px solid #e2e8f0;">Name</th>
                <th style="padding: 10px; border: 1px solid #e2e8f0;">Registered</th>
            </tr>
        """
        
        for user in users:
            phone, name, registered_at = user
            reg_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(registered_at))
            users_html += f"""
            <tr>
                <td style="padding: 10px; border: 1px solid #e2e8f0;">{phone}</td>
                <td style="padding: 10px; border: 1px solid #e2e8f0;">{name}</td>
                <td style="padding: 10px; border: 1px solid #e2e8f0;">{reg_time}</td>
            </tr>
            """
        
        users_html += "</table><br><a href='/' class='btn'>← Back to Dashboard</a>"
        
        return f"""
        <html>
        <head><title>Users - WhatsApp Bot</title></head>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            {users_html}
        </body>
        </html>
        """
        
    except Exception as e:
        return f"Error loading users: {str(e)}"

@app.route('/logs')
@login_required
def view_logs():
    """View bot logs"""
    try:
        logs_html = f"""
        <h2>📋 Bot Logs</h2>
        <div style="background: #1a202c; color: #e2e8f0; padding: 20px; border-radius: 8px; font-family: monospace;">
            <p>Session Status: {'Valid' if session_status['session_valid'] else 'Invalid'}</p>
            <p>Bot Running: {'Yes' if session_status['logged_in'] else 'No'}</p>
            <p>QR Generated: {'Yes' if session_status['qr_generated'] else 'No'}</p>
            <p>Waiting for Scan: {'Yes' if session_status['waiting_for_scan'] else 'No'}</p>
            <p>Last Check: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session_status['last_check'])) if session_status['last_check'] else 'Never'}</p>
            {f"<p>Error: {session_status['error_message']}</p>" if session_status.get('error_message') else ""}
        </div>
        <br><a href='/' class='btn'>← Back to Dashboard</a>
        """
        
        return f"""
        <html>
        <head><title>Logs - WhatsApp Bot</title></head>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            {logs_html}
        </body>
        </html>
        """
        
    except Exception as e:
        return f"Error loading logs: {str(e)}"

@app.route('/clear-data')
@login_required
def clear_data():
    """Clear user data"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM users")
            conn.commit()
        
        flask_session['message'] = "User data cleared successfully!"
        flask_session['message_type'] = "success"
    except Exception as e:
        flask_session['message'] = f"Error clearing data: {str(e)}"
        flask_session['message_type'] = "error"
    
    return redirect(url_for('dashboard'))

@app.route('/export-data')
@login_required
def export_data():
    """Export user data"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users")
            users = c.fetchall()
        
        # Simple CSV export
        csv_data = "Phone,Name,Registered\n"
        for user in users:
            csv_data += f"{user[0]},{user[1]},{time.strftime('%Y-%m-%d %H:%M', time.localtime(user[2]))}\n"
        
        return csv_data, 200, {
            'Content-Type': 'text/csv',
            'Content-Disposition': 'attachment; filename=whatsapp_bot_users.csv'
        }
        
    except Exception as e:
        return f"Error exporting data: {str(e)}"

def main():
    """Main function"""
    print("🚀 WhatsApp Web Bot with Enhanced Web Interface")
    print("=" * 60)
    print("🌐 Web Interface: http://localhost:8000")
    print("🔐 Login Required: Use admin phone + password")
    print("📱 Session Management: Generate via web interface")
    print("=" * 60)
    
    # Load configuration from database
    global SESSION_STRING, ADMIN_PHONE, LOGIN_PASSWORD
    SESSION_STRING = get_config('session_string', SESSION_STRING)
    ADMIN_PHONE = get_config('admin_phone', ADMIN_PHONE)
    LOGIN_PASSWORD = get_config('login_password', LOGIN_PASSWORD)
    
    # Start Flask app
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()

