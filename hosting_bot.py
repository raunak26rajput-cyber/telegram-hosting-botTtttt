import os
import json
import sqlite3
import subprocess
import sys
import shutil
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
import threading
import secrets
import string
import re
import signal
import platform
import traceback
import hashlib
import base64
import tempfile
import random

# ========== CONFIGURATION ==========
# Get configuration from environment variables (SECURE)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8994638094:AAHpwefTyuwgnAfJsoicDbEWVUoWk0exTEo")
if not BOT_TOKEN:
    for _alias in ("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN", "API_TOKEN", "TG_BOT_TOKEN"):
        BOT_TOKEN = os.environ.get(_alias, "")
        if BOT_TOKEN:
            os.environ["BOT_TOKEN"] = BOT_TOKEN
            print(f"ℹ️  BOT_TOKEN set from {_alias}")
            break
if not BOT_TOKEN:
    print("⚠️  WARNING: No bot token found. Set BOT_TOKEN env var before deploying.")
    BOT_TOKEN = "MISSING_TOKEN"

# Get admin IDs from environment (comma-separated, robust parsing)
admin_ids_str = os.environ.get("ADMIN_IDS", "8033024853")
ADMIN_IDS = set()
for _x in admin_ids_str.replace(";", ",").split(","):
    try:
        if _x.strip(): ADMIN_IDS.add(int(_x.strip()))
    except ValueError:
        pass
if not ADMIN_IDS:
    ADMIN_IDS = {8033024853}
    print("⚠️  ADMIN_IDS not set or invalid — using default admin ID")

def is_admin(user_id) -> bool:
    """Return True if user_id is a configured admin. Tolerant of str/int input."""
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False

# Channel verification settings (configure via env)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/zvxay")

# Platform detection
IS_RENDER = os.environ.get("RENDER") == "true"
IS_HEROKU = os.environ.get("HEROKU") == "true"
IS_CHOREO = os.environ.get("CHOREO") == "true"
IS_ANDROID = 'pydroid' in sys.executable.lower() or 'termux' in sys.executable.lower()

# Set base directory based on platform
# ── Persistent disk override ──────────────────────────────────────────
# The paths below (e.g. /opt/render/project/src/...) live INSIDE the app's
# own source checkout. On Render (and most PaaS), that directory is rebuilt
# from git on every deploy and is not guaranteed to survive even a plain
# restart — only an explicitly attached persistent Disk, mounted at a fixed
# path, actually survives redeploys. If PERSISTENT_DISK_PATH is set (point
# it at your Render Disk's mount path, e.g. /var/data), we use that instead
# so deployment files, packages and the database genuinely persist.
_persistent_override = os.environ.get("PERSISTENT_DISK_PATH", "").strip()
if _persistent_override:
    BASE_DIR = Path(_persistent_override) / "bot_hosting_data"
elif IS_RENDER:
    BASE_DIR = Path("/opt/render/project/src/bot_hosting_data")
elif IS_HEROKU:
    BASE_DIR = Path("/app/bot_hosting_data")
elif IS_CHOREO:
    BASE_DIR = Path("/choreo/app/bot_hosting_data")
elif IS_ANDROID:
    BASE_DIR = Path("/storage/emulated/0/bot_hosting_data")
else:
    BASE_DIR = Path("./bot_hosting_data")

USING_PERSISTENT_DISK = bool(_persistent_override)

# Always make this absolute. A relative BASE_DIR (the "./bot_hosting_data"
# local/default case) breaks subprocess.run([str(script)], cwd=str(folder)):
# POSIX resolves a *relative* executable path against the CHILD's new cwd,
# not the parent's, so the same relative path ends up looked up twice-nested
# and every deploy/restart launch fails with a misleading FileNotFoundError.
BASE_DIR = BASE_DIR.resolve()

DEPLOYMENTS_DIR = BASE_DIR / "deployments"
DATABASE_FILE = BASE_DIR / "hosting_bot.db"
USER_FILES_DIR = BASE_DIR / "user_files"
LOGS_DIR = BASE_DIR / "logs"
PIP_CACHE_DIR = BASE_DIR / "pip_cache"

# Create directories
for dir_path in [BASE_DIR, DEPLOYMENTS_DIR, USER_FILES_DIR, LOGS_DIR, PIP_CACHE_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# File size limit
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", 50))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Premium Subscription Pricing
PRICE_MONTHLY_STARS = int(os.environ.get("PRICE_MONTHLY_STARS", 50))
PRICE_YEARLY_STARS = int(os.environ.get("PRICE_YEARLY_STARS", 500))
PRICE_MONTHLY_COINS = PRICE_MONTHLY_STARS * 10
PRICE_YEARLY_COINS = PRICE_YEARLY_STARS * 10

# Free tier settings
FREE_USER_MAX_DEPLOYMENTS = int(os.environ.get("FREE_USER_MAX_DEPLOYMENTS", 3))
FREE_DEPLOYMENT_DURATION_HOURS = int(os.environ.get("FREE_DEPLOYMENT_DURATION_HOURS", 24))

# Exchange rate
STARS_PER_COIN = int(os.environ.get("STARS_PER_COIN", 10))

# Referral reward
REFERRAL_REWARD_COINS = int(os.environ.get("REFERRAL_REWARD_COINS", 50))

# ── GitHub Backup ─────────────────────────────────────────────────────────────
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO_OWNER   = os.environ.get("GITHUB_REPO_OWNER", "")
GITHUB_REPO_NAME    = os.environ.get("GITHUB_REPO_NAME", "")
GITHUB_BACKUP_BRANCH= os.environ.get("GITHUB_BACKUP_BRANCH", "main")
GITHUB_BACKUP_PATH  = os.environ.get("GITHUB_BACKUP_PATH", "backups/hosting_bot.db")
GITHUB_ENABLED      = bool(GITHUB_TOKEN and GITHUB_REPO_OWNER and GITHUB_REPO_NAME)

# Timeout settings
PIP_INSTALL_TIMEOUT = int(os.environ.get("PIP_INSTALL_TIMEOUT", 600))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
LAST_UPDATE_ID = 0

# Server status
server_running = True
active_deployments = {}
deployment_lock = threading.Lock()

# Webhook server
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")

print("=" * 70)
print("╔═════════════════════════════════════════════════════════════════════╗")
print("║         UNIVERSAL BOT HOSTING PLATFORM - ENTERPRISE EDITION         ║")
print("╚═════════════════════════════════════════════════════════════════════╝")
print("=" * 70)
print(f"📍 Platform: {'Render' if IS_RENDER else 'Heroku' if IS_HEROKU else 'Choreo' if IS_CHOREO else 'Android' if IS_ANDROID else 'Local'}")
print(f"📁 Data Directory: {BASE_DIR}")
print(f"💰 Monthly: {PRICE_MONTHLY_STARS}⭐ / {PRICE_MONTHLY_COINS}🪙")
print(f"💰 Yearly: {PRICE_YEARLY_STARS}⭐ / {PRICE_YEARLY_COINS}🪙")
print(f"🆓 Free Tier: {FREE_USER_MAX_DEPLOYMENTS} x {FREE_DEPLOYMENT_DURATION_HOURS}h")
print(f"📦 Max File Size: {MAX_FILE_SIZE_MB}MB")
print(f"🐍 Python Version: {platform.python_version()}")
print("=" * 70)
print("✨ ENHANCED FEATURES:")
print("   ✓ Supports ANY Python bot (Telegram, Discord, Flask, FastAPI, etc.)")
print("   ✓ ALL dependency types (PyPI, Git, Mercurial, Subversion, wheel, egg)")
print("   ✓ Auto-dependency detection from imports")
print("   ✓ Progress bars for installations")
print("   ✓ Environment variable injection with .env support")
print("   ✓ Premium/Free tier with Stars/Coins")
print("   ✓ Persistent storage with user file management")
print("   ✓ Framework detection and optimized launcher")
print("=" * 70)

# ========== DATABASE SETUP ==========
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        join_date TEXT,
        last_active TEXT,
        coins_balance INTEGER DEFAULT 0,
        stars_balance INTEGER DEFAULT 0,
        total_coins_earned INTEGER DEFAULT 0,
        total_coins_spent INTEGER DEFAULT 0,
        total_stars_earned INTEGER DEFAULT 0,
        total_stars_spent INTEGER DEFAULT 0,
        joined_channel INTEGER DEFAULT 0,
        free_deployment_count INTEGER DEFAULT 0,
        is_premium INTEGER DEFAULT 0,
        premium_expires TEXT,
        premium_plan TEXT,
        step TEXT,
        temp_file TEXT,
        requirements TEXT,
        env_vars TEXT,
        plan TEXT,
        payment_method TEXT,
        duration INTEGER,
        cost_coins INTEGER,
        cost_stars INTEGER,
        waiting_for_env INTEGER DEFAULT 0,
        waiting_for_reqs INTEGER DEFAULT 0,
        waiting_for_redeem INTEGER DEFAULT 0,
        temp_target_user TEXT,
        temp_coins_amount INTEGER,
        temp_stars_amount INTEGER,
        temp_expiry INTEGER,
        temp_reward_type TEXT,
        pending_payment_payload TEXT,
        last_expiry_notification TEXT,
        referral_code TEXT UNIQUE,
        referred_by INTEGER DEFAULT NULL,
        total_referrals INTEGER DEFAULT 0,
        pending_json TEXT DEFAULT '{}'
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan TEXT,
        amount_stars INTEGER,
        amount_coins INTEGER,
        start_date TEXT,
        end_date TEXT,
        status TEXT,
        renewal_count INTEGER DEFAULT 0,
        admin_notified INTEGER DEFAULT 0,
        payment_payload TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS deployments (
        deployment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        file_name TEXT,
        file_size INTEGER,
        requirements TEXT,
        env_vars TEXT,
        plan TEXT,
        payment_method TEXT,
        cost_coins INTEGER,
        cost_stars INTEGER,
        start_time TEXT,
        expire_time TEXT,
        status TEXT,
        proc_pid INTEGER,
        install_log TEXT,
        deploy_log TEXT,
        error_log TEXT,
        is_free INTEGER DEFAULT 0,
        is_paused INTEGER DEFAULT 0,
        last_expiry_notification TEXT,
        bot_type TEXT DEFAULT 'python_app',
        framework TEXT DEFAULT 'unknown',
        dependencies_installed TEXT,
        folder_name TEXT,
        source_type TEXT DEFAULT 'upload',
        github_repo TEXT,
        github_branch TEXT DEFAULT 'main'
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS coin_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        transaction_type TEXT,
        source TEXT,
        reference_id TEXT,
        timestamp TEXT,
        status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS star_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        transaction_type TEXT,
        source TEXT,
        reference_id TEXT,
        timestamp TEXT,
        status TEXT,
        payload TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
        code_id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        coins_amount INTEGER,
        stars_amount INTEGER,
        created_by INTEGER,
        created_at TEXT,
        expires_at TEXT,
        expiry_days INTEGER,
        max_uses INTEGER,
        used_count INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        used_by TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_premium_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        plan TEXT,
        duration_days INTEGER,
        cost_stars INTEGER,
        cost_coins INTEGER,
        payload TEXT,
        created_at TEXT,
        status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        temp_file TEXT,
        requirements TEXT,
        env_vars TEXT,
        plan TEXT,
        duration INTEGER,
        cost_coins INTEGER,
        cost_stars INTEGER,
        payment_method TEXT,
        payload TEXT,
        created_at TEXT,
        status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS system_stats (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        total_users INTEGER DEFAULT 0,
        total_deployments INTEGER DEFAULT 0,
        total_active_deployments INTEGER DEFAULT 0,
        total_paused_deployments INTEGER DEFAULT 0,
        total_free_deployments INTEGER DEFAULT 0,
        total_coins_created INTEGER DEFAULT 0,
        total_stars_created INTEGER DEFAULT 0,
        total_revenue_stars INTEGER DEFAULT 0,
        total_revenue_usd REAL DEFAULT 0,
        premium_users INTEGER DEFAULT 0,
        server_start_time TEXT,
        last_updated TEXT
    )''')
    
    c.execute('INSERT OR IGNORE INTO system_stats (id, server_start_time, last_updated) VALUES (1, ?, ?)', 
              (datetime.now().isoformat(), datetime.now().isoformat()))
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_deployments_user ON deployments(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deployments_expire ON deployments(expire_time)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_expire ON subscriptions(end_date)')
    c.execute('''CREATE TABLE IF NOT EXISTS bug_reports (
        report_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        message TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        admin_reply TEXT,
        replied_by INTEGER,
        created_at TEXT,
        replied_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL UNIQUE,
        created_at TEXT,
        reward_coins INTEGER DEFAULT 0,
        reward_given INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS security_reviews (
        review_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        chat_id       INTEGER NOT NULL,
        file_name     TEXT NOT NULL,
        file_path     TEXT,
        critical_json TEXT,
        warnings_json TEXT,
        report_text   TEXT,
        source        TEXT DEFAULT 'upload',
        status        TEXT DEFAULT 'pending',
        reviewed_by   INTEGER,
        created_at    TEXT,
        reviewed_at   TEXT,
        user_step_json TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sec_reviews_status ON security_reviews(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sec_reviews_user   ON security_reviews(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_bug_reports_user   ON bug_reports(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)')
    
    # Migrations for existing databases
    for _col, _type in [
        ("folder_name",    "TEXT"),
        ("referral_code",  "TEXT"),
        ("referred_by",    "INTEGER"),
        ("total_referrals","INTEGER DEFAULT 0"),
        ("pending_json",   "TEXT DEFAULT '{}'"),
    ]:
        try:
            c.execute(f"ALTER TABLE deployments ADD COLUMN {_col} {_type}" if _col == "folder_name"
                      else f"ALTER TABLE users ADD COLUMN {_col} {_type}")
        except Exception:
            pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN tos_accepted INTEGER DEFAULT 0")
    except Exception:
        pass

    for _col, _type in [("source_type","TEXT DEFAULT 'upload'"),
                        ("github_repo", "TEXT"),
                        ("github_branch","TEXT DEFAULT 'main'"),
                        ("crash_restart_count", "INTEGER DEFAULT 0"),
                        ("last_crash_restart", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE deployments ADD COLUMN {_col} {_type}")
        except Exception:
            pass

    # security_reviews — backfill user_step_json for older DBs
    try:
        c.execute("ALTER TABLE security_reviews ADD COLUMN user_step_json TEXT")
    except Exception:
        pass
    
    conn.commit()
    conn.close()
    print("✅ Database initialized with enhanced schema")

# ========== DEPLOY FOLDER HELPER ==========
def kill_deployment_process(pid, sig=None):
    """
    Terminate a deployment's process AND anything it spawned as children.

    A deployment's launcher (run.py) often runs the actual bot as a
    subprocess (Method 2 execution — the fallback when the launcher can't
    find a recognizable entry point to import directly, which is common).
    That child process shares the launcher's process group by default.
    Killing only the launcher's own PID (os.kill) leaves that child running
    forever, completely orphaned and untracked — silently consuming RAM/CPU
    with no record of what it even is. Killing the whole process GROUP
    instead takes the launcher and everything it spawned down together.

    Critical safety check: a target's process group must NEVER be killed if
    it matches OUR OWN group. Every deployment spawned via the current
    (setsid-based) start.sh gets its own isolated group, so this should
    never normally happen for a live deployment — but pre-existing orphans
    from before that fix was in place were spawned WITHOUT setsid, meaning
    they very likely still share the hosting bot's own inherited group.
    Group-killing those would take the whole hosting bot down as collateral
    damage. When target and caller share a group, fall back to a plain
    single-PID kill instead — safe either way, just less thorough about
    orphaned grandchildren in that specific legacy case.
    """
    if sig is None:
        sig = signal.SIGTERM
    if not pid:
        return
    try:
        target_pgid = os.getpgid(pid)
        own_pgid = os.getpgid(os.getpid())
        if target_pgid == own_pgid:
            os.kill(pid, sig)
            return
        os.killpg(target_pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            os.kill(pid, sig)
        except Exception:
            pass


def get_deploy_folder(user_id, deployment_id):
    """Return the correct deploy folder path using folder_name from DB (fixes timestamp vs DB-id mismatch)."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT folder_name, user_id FROM deployments WHERE deployment_id = ?", (deployment_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            owner_id = row[1] if row[1] else user_id
            return DEPLOYMENTS_DIR / str(owner_id) / str(row[0])
    except Exception:
        pass
    # Fallback: old behaviour (folder named by DB id) for records without folder_name
    return DEPLOYMENTS_DIR / str(user_id) / str(deployment_id)


# ==================== SECURITY SCANNER ====================

class SecurityScanner:
    """
    6-Layer Security Scanner for uploaded bot files.

    Layer 1  – AI pattern-based analysis
    Layer 2  – AST-level Python code analysis
    Layer 3  – Encoded / hidden content detection (base64, hex, url-enc)
    Layer 4  – Secure sandbox extraction for archives
    Layer 5  – Permission / path traversal / symlink / data-protection scan
    Layer 6  – Archive bomb & nested extraction protection

    For a hosting platform, patterns are calibrated in two tiers:
    CRITICAL  → deployment blocked entirely
    WARNING   → deployment proceeds with a notification to user and admin
    """

    MAX_FILES        = 1_000
    MAX_TOTAL_SIZE   = 100 * 1024 * 1024   # 100 MB extracted
    MAX_RECURSION    = 4
    B64_MIN_LEN      = 60   # ignore short base64 strings to cut false positives

    # ── CRITICAL patterns (block deployment) ──────────────────────────
    CRITICAL_PYTHON = [
        r'eval\s*\(\s*base64\.b64decode',            # obfuscated eval
        r'exec\s*\(\s*base64\.b64decode',            # obfuscated exec
        r'exec\s*\(\s*__import__\s*\(',              # import+exec combo
        r'stratum\+tcp',                              # crypto miner
        r'xmrig|minergate|nicehash|coinhive',         # crypto miner names
        r'socket\.connect\([^)]+\).*exec\(',          # reverse shell
        r'\.connect\(\(\s*["\'][0-9]+\.[0-9]+',      # hardcoded IP socket
        r"os\.system\s*\(\s*['\"]rm\s+-rf\s+/",      # rm -rf /
        r"shutil\.rmtree\s*\(\s*['\"][/\\]",         # rmtree on root
        r'urllib\.request\.urlopen.*base64\.b64decode',  # fetch+decode
    ]
    CRITICAL_JS = [
        r'stratum\+tcp',
        r'xmrig|minergate|coinhive',
        r'child_process.*exec.*base64',
        r'eval\s*\(\s*Buffer\.from',
        r'exec\s*\(\s*require\s*\(\s*["\']child_process',
    ]
    CRITICAL_PKG_HOOKS = [
        r'curl\s+http',
        r'wget\s+http',
        r'\|\s*bash',
        r'\|\s*sh\b',
        r'python\s+-c\s+["\']import',
        r'node\s+-e\s+',
        r'base64\s+--decode',
        r'chmod\s+\+x',
    ]

    # ── WARNING patterns (proceed with alert) ────────────────────────
    WARN_PYTHON = [
        r'\beval\s*\(',
        r'\bexec\s*\(',
        r'subprocess\.(Popen|call|check_output|run)\s*\(',
        r'os\.(system|popen)\s*\(',
        r'os\.(remove|unlink)\s*\(',
        r'shutil\.rmtree\s*\(',
        r'pickle\.loads\s*\(',
        r'marshal\.loads\s*\(',
        r'__import__\s*\(',
    ]
    WARN_JS = [
        r'\beval\s*\(',
        r'new\s+Function\s*\(',
        r'child_process\.(exec|spawn|execSync|spawnSync)\s*\(',
        r'fs\.(unlink|rmdir|rmdirSync|unlinkSync)\s*\(',
        r'require\s*\(\s*["\']child_process["\']\s*\)',
    ]

    # ── File types → scanner map ──────────────────────────────────────
    SUPPORTED_BOT_EXTS = {
        '.py':  'python',
        '.js':  'javascript',
        '.mjs': 'javascript',
        '.cjs': 'javascript',
        '.ts':  'typescript',
        '.tsx': 'typescript',
        '.jsx': 'javascript',
    }
    ARCHIVE_EXTS = {'.zip', '.tar', '.gz', '.tgz', '.7z', '.rar'}

    def __init__(self):
        self._reset()

    def _reset(self):
        self.critical = []
        self.warnings = []

    # ── Public API ────────────────────────────────────────────────────
    def scan(self, file_bytes: bytes, filename: str) -> tuple[bool, list, list]:
        """
        Returns (blocked, critical_list, warning_list).
        blocked=True means the file should NOT be deployed.
        """
        self._reset()
        filename = filename or 'unknown'
        ext = os.path.splitext(filename)[1].lower()
        if ext in self.ARCHIVE_EXTS:
            self._scan_archive(file_bytes, filename)
        else:
            c, w = self._scan_file(file_bytes, filename)
            self.critical.extend(c)
            self.warnings.extend(w)
        blocked = bool(self.critical)
        return blocked, self.critical[:], self.warnings[:]

    # ── Archive handling ──────────────────────────────────────────────
    def _scan_archive(self, data: bytes, filename: str):
        import tempfile, shutil, zipfile, tarfile, io as _io
        ext      = os.path.splitext(filename)[1].lower()
        tmp_dir  = tempfile.mkdtemp(prefix='sec_scan_')
        try:
            if ext == '.zip':
                with zipfile.ZipFile(_io.BytesIO(data)) as zf:
                    self._check_zip(zf)
                    zf.extractall(tmp_dir)
            elif ext in ('.tar', '.gz', '.tgz'):
                with tarfile.open(fileobj=_io.BytesIO(data), mode='r:*') as tf:
                    self._check_tar(tf)
                    tf.extractall(tmp_dir)
            elif ext == '.rar':
                try:
                    import rarfile
                    with rarfile.RarFile(_io.BytesIO(data)) as rf:
                        self._check_rar(rf)
                        rf.extractall(tmp_dir)
                except ImportError:
                    self.warnings.append("RAR archive: cannot deep-scan (rarfile not installed)")
                    return
            elif ext == '.7z':
                try:
                    import py7zr
                    with py7zr.SevenZipFile(_io.BytesIO(data)) as sz:
                        sz.extractall(tmp_dir)
                except ImportError:
                    self.warnings.append("7z archive: cannot deep-scan (py7zr not installed)")
                    return

            for root, _dirs, files in os.walk(tmp_dir):
                for fname in files:
                    fpath    = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, tmp_dir)
                    if os.path.islink(fpath):
                        self.critical.append(f"Symlink in archive: `{rel_path}`")
                        continue
                    if '..' in rel_path or rel_path.startswith('/'):
                        self.critical.append(f"Path traversal in archive: `{rel_path}`")
                        continue
                    try:
                        with open(fpath, 'rb') as fp:
                            content = fp.read()
                        c, w = self._scan_file(content, fname, rel_path)
                        self.critical.extend(c)
                        self.warnings.extend(w)
                    except Exception as e:
                        self.warnings.append(f"Could not scan `{rel_path}`: {e}")
        except Exception as e:
            self.critical.append(f"Archive error: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Single file dispatcher ────────────────────────────────────────
    def _scan_file(self, data: bytes, filename: str, rel: str = None) -> tuple[list, list]:
        critical, warnings = [], []
        ext  = os.path.splitext(filename)[1].lower()
        path = rel or filename

        # Path traversal guard
        if rel and ('..' in rel or rel.startswith('/')):
            critical.append(f"Path traversal: `{rel}`")
            return critical, warnings

        try:
            text = data.decode('utf-8', errors='ignore')
        except Exception:
            text = ''

        # Layer 3: encoded content
        ec, ew = self._check_encoded(text, path)
        critical.extend(ec); warnings.extend(ew)

        # Layer 1+2: language-specific
        lang = self.SUPPORTED_BOT_EXTS.get(ext, '')
        if lang == 'python':
            c, w = self._scan_python(data, text, path)
            critical.extend(c); warnings.extend(w)
        elif lang in ('javascript', 'typescript'):
            c, w = self._scan_js(text, path)
            critical.extend(c); warnings.extend(w)
        elif filename.lower() == 'package.json':
            c, w = self._scan_package_json(text)
            critical.extend(c); warnings.extend(w)
        elif text:
            c, w = self._scan_generic(text, path)
            critical.extend(c); warnings.extend(w)

        # Layer 5: sensitive file names
        base = os.path.basename(filename).lower()
        if base in ('credentials.json', 'service_account.json', 'gcp_key.json'):
            warnings.append(f"Sensitive credential file: `{path}`")

        return critical, warnings

    # ── Python scanner (Layer 1 + 2) ─────────────────────────────────
    def _scan_python(self, data: bytes, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []

        # Regex critical
        for pat in self.CRITICAL_PYTHON:
            if re.search(pat, text, re.IGNORECASE | re.DOTALL):
                critical.append(f"[PY-CRITICAL] `{pat}` in `{path}`")

        # Regex warnings
        for pat in self.WARN_PYTHON:
            if re.search(pat, text, re.IGNORECASE):
                warnings.append(f"[PY-WARN] `{pat}` in `{path}`")

        # AST analysis
        try:
            import ast as _ast
            tree = _ast.parse(text, mode='exec')
            for node in _ast.walk(tree):
                if not isinstance(node, _ast.Call):
                    continue
                func = node.func

                # Name calls: eval(), exec(), __import__()
                if isinstance(func, _ast.Name):
                    name = func.id
                    if name in ('eval', 'exec'):
                        # Check if argument is base64 (obfuscation)
                        if node.args and isinstance(node.args[0], _ast.Call):
                            critical.append(f"[AST] Obfuscated `{name}()` call in `{path}`")
                        else:
                            warnings.append(f"[AST] `{name}()` call in `{path}`")

                # Attribute calls: os.system(), subprocess.Popen() etc.
                elif isinstance(func, _ast.Attribute):
                    attr     = func.attr
                    # Safely get the object name
                    obj_name = ''
                    if isinstance(func.value, _ast.Name):
                        obj_name = func.value.id

                    # Destructive OS ops
                    if attr in ('remove', 'unlink', 'rmdir') and obj_name == 'os':
                        warnings.append(f"[AST] `os.{attr}()` in `{path}`")
                    elif attr == 'rmtree' and obj_name == 'shutil':
                        warnings.append(f"[AST] `shutil.rmtree()` in `{path}`")
                    elif attr in ('Popen', 'call', 'check_output', 'run') and obj_name == 'subprocess':
                        warnings.append(f"[AST] `subprocess.{attr}()` in `{path}`")
                    elif attr in ('system', 'popen') and obj_name == 'os':
                        warnings.append(f"[AST] `os.{attr}()` in `{path}`")

                # Dangerous imports
                if isinstance(node, _ast.ImportFrom):
                    if node.module in ('subprocess', 'os'):
                        for alias in (node.names or []):
                            if alias.name in ('system', 'popen', 'remove', 'unlink',
                                              'Popen', 'call', 'check_output', 'rmtree'):
                                warnings.append(f"[AST] Dangerous import: `from {node.module} import {alias.name}` in `{path}`")

        except SyntaxError as e:
            warnings.append(f"[PY] Syntax error in `{path}`: {e}")
        except Exception:
            pass   # AST parse may fail on dynamic code; regex already covered it

        return critical, warnings

    # ── JavaScript / TypeScript / Node.js scanner ─────────────────────
    def _scan_js(self, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []
        if not text:
            return critical, warnings

        for pat in self.CRITICAL_JS:
            if re.search(pat, text, re.IGNORECASE | re.DOTALL):
                critical.append(f"[JS-CRITICAL] `{pat}` in `{path}`")

        for pat in self.WARN_JS:
            if re.search(pat, text, re.IGNORECASE):
                warnings.append(f"[JS-WARN] `{pat}` in `{path}`")

        # TypeScript-specific: dynamic import with obfuscated string
        if re.search(r'import\s*\(\s*atob\s*\(', text):
            critical.append(f"[TS-CRITICAL] Obfuscated dynamic import in `{path}`")

        # Crypto miner in JS
        if re.search(r'(stratum\+tcp|coinhive|cryptonight)', text, re.IGNORECASE):
            critical.append(f"[JS-CRITICAL] Crypto miner pattern in `{path}`")

        # Hardcoded token/secret
        if re.search(r'(?i)(token|api_key|secret|password)\s*=\s*["\'][A-Za-z0-9_\-]{8,}', text):
            warnings.append(f"[JS] Possible hardcoded credential in `{path}`")

        return critical, warnings

    # ── package.json scanner ──────────────────────────────────────────
    def _scan_package_json(self, text: str) -> tuple[list, list]:
        critical, warnings = [], []
        try:
            pkg = json.loads(text)
        except Exception:
            return critical, warnings

        scripts = pkg.get('scripts', {})
        dangerous_hooks = ('preinstall', 'postinstall', 'prepare',
                           'preuninstall', 'postuninstall')
        for hook in dangerous_hooks:
            cmd = scripts.get(hook, '')
            if not cmd:
                continue
            for pat in self.CRITICAL_PKG_HOOKS:
                if re.search(pat, cmd, re.IGNORECASE):
                    critical.append(
                        f"[NPM-CRITICAL] Dangerous `{hook}` hook: `{cmd[:80]}`")
                    break
            else:
                if cmd.strip():
                    warnings.append(f"[NPM] `{hook}` hook detected: `{cmd[:80]}`")

        return critical, warnings

    # ── Generic scanner ───────────────────────────────────────────────
    def _scan_generic(self, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []
        generic_critical = [
            r'stratum\+tcp', r'xmrig', r'minergate',
            r'rm\s+-rf\s+/',
        ]
        generic_warn = [
            r'\beval\s*\(',
            r'\bexec\s*\(',
            r'child_process',
        ]
        for pat in generic_critical:
            if re.search(pat, text, re.IGNORECASE):
                critical.append(f"[GENERIC-CRITICAL] `{pat}` in `{path}`")
        for pat in generic_warn:
            if re.search(pat, text, re.IGNORECASE):
                warnings.append(f"[GENERIC-WARN] `{pat}` in `{path}`")
        return critical, warnings

    # ── Encoded content detection (Layer 3) ───────────────────────────
    def _check_encoded(self, text: str, path: str) -> tuple[list, list]:
        critical, warnings = [], []

        # Base64: only scan long strings to avoid noise
        b64_pat = r'[A-Za-z0-9+/]{' + str(self.B64_MIN_LEN) + r',}={0,2}'
        for match in re.findall(b64_pat, text):
            try:
                import base64 as _b64
                decoded = _b64.b64decode(match + '==')
                decoded_text = decoded.decode('utf-8', errors='ignore')
                for pat in self.CRITICAL_PYTHON + self.CRITICAL_JS:
                    if re.search(pat, decoded_text, re.IGNORECASE):
                        critical.append(
                            f"[ENCODED-CRITICAL] Dangerous content hidden in base64 in `{path}`")
                        break
            except Exception:
                pass

        # Hex strings (long)
        hex_pat = r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{80,}(?![0-9A-Fa-f])'
        for match in re.findall(hex_pat, text):
            try:
                decoded_text = bytes.fromhex(match).decode('utf-8', errors='ignore')
                for pat in self.CRITICAL_PYTHON:
                    if re.search(pat, decoded_text, re.IGNORECASE):
                        critical.append(
                            f"[ENCODED-CRITICAL] Dangerous content in hex string in `{path}`")
                        break
            except Exception:
                pass

        # URL-encoded blob (≥3 consecutive %xx tokens = suspicious)
        if re.search(r'(?:%[0-9A-Fa-f]{2}){4,}', text):
            warnings.append(f"[ENCODED-WARN] URL-encoded block detected in `{path}` (possible obfuscation)")

        return critical, warnings

    # ── Archive bomb protection (Layer 6) ────────────────────────────
    def _check_zip(self, zf):
        import zipfile
        total, n = 0, 0
        for info in zf.infolist():
            n += 1
            if n > self.MAX_FILES:
                raise Exception(f"ZIP contains >{self.MAX_FILES} files (archive bomb?)")
            total += info.file_size
            if total > self.MAX_TOTAL_SIZE:
                raise Exception("ZIP extracted size too large (archive bomb?)")
            name = info.filename
            if '..' in name or name.startswith('/') or name.startswith('\\'):
                raise Exception(f"Path traversal in ZIP: {name}")

    def _check_tar(self, tf):
        import tarfile
        total, n = 0, 0
        for m in tf:
            if m.isreg():
                n += 1
                if n > self.MAX_FILES:
                    raise Exception(f"TAR contains >{self.MAX_FILES} files (archive bomb?)")
                total += m.size
                if total > self.MAX_TOTAL_SIZE:
                    raise Exception("TAR extracted size too large (archive bomb?)")
            if m.issym():
                raise Exception(f"Symlink in TAR: {m.name}")
            if '..' in m.name or m.name.startswith('/'):
                raise Exception(f"Path traversal in TAR: {m.name}")

    def _check_rar(self, rf):
        total, n = 0, 0
        for info in rf.infolist():
            if not info.isdir():
                n += 1
                if n > self.MAX_FILES:
                    raise Exception(f"RAR contains >{self.MAX_FILES} files (archive bomb?)")
                total += info.file_size
                if total > self.MAX_TOTAL_SIZE:
                    raise Exception("RAR extracted size too large (archive bomb?)")
            if '..' in info.filename or info.filename.startswith('/'):
                raise Exception(f"Path traversal in RAR: {info.filename}")


SECURITY_SCANNER = SecurityScanner()


def run_security_scan(file_bytes: bytes, filename: str) -> tuple[bool, list, list, str]:
    """
    Wrapper around SecurityScanner.scan().
    Returns (blocked, critical_issues, warnings, report_text).
    `blocked` is True when critical issues are found and deployment must stop.
    """
    try:
        blocked, critical, warnings = SECURITY_SCANNER.scan(file_bytes, filename)
    except Exception as e:
        # Scanner itself crashed — log and let deployment proceed with a warning
        return False, [], [f"Scanner error: {e}"], f"⚠️ Security scan encountered an error: {e}"

    lines = [f"🔒 Security Scan — `{display_filename(filename)}`\n"]
    if critical:
        lines.append(f"🔴 *{len(critical)} CRITICAL issue(s) — BLOCKED:*")
        for i in critical[:10]:
            lines.append(f"  • {i}")
        if len(critical) > 10:
            lines.append(f"  … and {len(critical)-10} more")
    if warnings:
        lines.append(f"\n🟡 *{len(warnings)} warning(s):*")
        for w in warnings[:8]:
            lines.append(f"  • {w}")
        if len(warnings) > 8:
            lines.append(f"  … and {len(warnings)-8} more")
    if not critical and not warnings:
        lines.append("✅ No issues found — file is clean")

    return blocked, critical, warnings, '\n'.join(lines)


# ==================== GITHUB BACKUP SYSTEM ====================

_github_push_lock   = threading.Lock()
_last_backup_time   = 0.0
_MIN_BACKUP_INTERVAL = 30          # minimum seconds between consecutive backups

def _gh_api(method, path, payload=None):
    """Raw GitHub Contents API call. Returns (http_status, response_dict)."""
    url     = (f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/"
               f"{GITHUB_REPO_NAME}/contents/{path}")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read())
        except: return e.code, {}
    except Exception as ex:
        return 0, {"error": str(ex)}

def _db_has_data():
    """Return True only when the database contains real rows (not just schema)."""
    if not DATABASE_FILE.exists():
        return False
    if DATABASE_FILE.stat().st_size < 8192:   # < 8 KB → almost certainly empty
        return False
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM deployments")
        deps  = c.fetchone()[0]
        conn.close()
        return users > 0 or deps > 0
    except Exception:
        return False

def github_restore_db():
    """
    Download the database from GitHub and write it to DATABASE_FILE.
    Must be called BEFORE init_db() so existing data is never overwritten by
    a fresh schema.  Safe to call even when GitHub is not configured.
    """
    if not GITHUB_ENABLED:
        print("ℹ️  GitHub backup not configured — skipping restore")
        return False

    print(f"🔄 Restoring from GitHub → "
          f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BACKUP_PATH} "
          f"[branch: {GITHUB_BACKUP_BRANCH}]")

    status, resp = _gh_api("GET", GITHUB_BACKUP_PATH)

    if status == 404:
        print("ℹ️  No backup found on GitHub — starting fresh")
        return False
    if status != 200:
        print(f"⚠️  GitHub restore HTTP {status}: {resp.get('message', resp)}")
        return False

    try:
        raw_b64  = resp.get("content", "").replace("\n", "")
        db_bytes = base64.b64decode(raw_b64)

        if len(db_bytes) < 1024:
            print("⚠️  GitHub backup is too small — skipping restore (corrupt?)")
            return False

        # Validate it is really a SQLite database before touching the live one
        if not db_bytes.startswith(b"SQLite format 3\x00"):
            print("⚠️  GitHub backup is not a valid SQLite file — skipping restore")
            return False

        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file first and run an integrity check on it
        _tmp_restore = DATABASE_FILE.parent / (DATABASE_FILE.name + ".restore_tmp")
        with open(_tmp_restore, "wb") as f:
            f.write(db_bytes)
        try:
            _chk = sqlite3.connect(_tmp_restore)
            _ok = _chk.execute("PRAGMA integrity_check").fetchone()
            _chk.close()
            if not _ok or _ok[0] != "ok":
                print(f"⚠️  GitHub backup failed integrity check ({_ok}) — skipping restore")
                _tmp_restore.unlink(missing_ok=True)
                return False
        except Exception as _ie:
            print(f"⚠️  GitHub backup unreadable ({_ie}) — skipping restore")
            _tmp_restore.unlink(missing_ok=True)
            return False

        # Keep a safety copy of any existing live database before overwriting
        if DATABASE_FILE.exists() and DATABASE_FILE.stat().st_size > 0:
            try:
                _safety = DATABASE_FILE.parent / (
                    f"pre_github_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
                shutil.copy2(DATABASE_FILE, _safety)
            except Exception as _se:
                print(f"⚠️  Could not create safety copy before restore: {_se}")

        os.replace(_tmp_restore, DATABASE_FILE)

        size_kb = len(db_bytes) / 1024
        print(f"✅ Database restored from GitHub ({size_kb:.1f} KB)")
        return True
    except Exception as e:
        print(f"❌ GitHub restore error: {e}")
        return False

def github_backup_db(reason: str = "auto", force: bool = False):
    """
    Upload DATABASE_FILE to GitHub.  Thread-safe via push lock.
    Silently skips when:
      • GitHub is not configured
      • The database is empty / has no real rows
      • Another backup is already in flight (force=True waits briefly instead)
      • The previous backup was less than _MIN_BACKUP_INTERVAL seconds ago
        (force=True bypasses this — for explicit manual/admin-triggered backups)
    """
    global _last_backup_time

    if not GITHUB_ENABLED:
        return False
    if not _db_has_data():
        print(f"⏭️  Backup skipped ({reason}): database has no data")
        return False

    now = datetime.now().timestamp()
    if not force and now - _last_backup_time < _MIN_BACKUP_INTERVAL:
        return False   # too soon — silent

    if force:
        acquired = _github_push_lock.acquire(blocking=True, timeout=15)
    else:
        acquired = _github_push_lock.acquire(blocking=False)
    if not acquired:
        return False   # another backup already running

    try:
        with open(DATABASE_FILE, "rb") as f:
            db_bytes = f.read()

        if len(db_bytes) < 1024:
            return False

        content_b64 = base64.b64encode(db_bytes).decode()

        # We need the current file's SHA to update it (GitHub API requirement)
        sha    = None
        status, resp = _gh_api("GET", GITHUB_BACKUP_PATH)
        if status == 200:
            sha = resp.get("sha")
        elif status not in (200, 404):
            print(f"⚠️  GitHub SHA lookup failed (HTTP {status}) — backup aborted")
            return False

        commit = {
            "message": f"backup: {reason} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "content": content_b64,
            "branch":  GITHUB_BACKUP_BRANCH,
        }
        if sha:
            commit["sha"] = sha

        status, resp = _gh_api("PUT", GITHUB_BACKUP_PATH, commit)

        if status in (200, 201):
            _last_backup_time = datetime.now().timestamp()
            print(f"✅ DB backed up to GitHub ({len(db_bytes)/1024:.1f} KB) — {reason}")
            return True
        else:
            print(f"⚠️  GitHub backup HTTP {status}: {resp.get('message', resp)}")
            return False

    except Exception as e:
        print(f"❌ GitHub backup error: {e}")
        return False
    finally:
        _github_push_lock.release()

def async_backup(reason: str = "auto"):
    """Fire-and-forget backup — never blocks the bot's response path."""
    if GITHUB_ENABLED:
        threading.Thread(
            target=github_backup_db, args=(reason,),
            daemon=True, name="GitHubBackup"
        ).start()

def _periodic_backup_thread():
    """Safety-net: flush a backup every 30 minutes regardless of other triggers."""
    sleep(300)   # wait 5 min before first periodic attempt
    while True:
        try:
            github_backup_db("periodic")
        except Exception as e:
            print(f"⚠️  Periodic backup error: {e}")
        sleep(1800)   # 30 minutes


def _find_available_port(preferred: int = 20000) -> int:
    """Return an available TCP port, starting from preferred and sampling nearby."""
    import socket, random
    candidates = [preferred] + random.sample(
        range(max(20000, preferred - 500), min(39999, preferred + 500)), 40
    )
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('', port))
                return port
            except OSError:
                continue
    return preferred + 1  # best-effort fallback

# ========== PROGRESS BAR FUNCTION ==========
def create_progress_bar(percentage: float, width: int = 30, filled_char: str = "█", empty_char: str = "░") -> str:
    filled = int(width * percentage / 100)
    empty = width - filled
    bar = filled_char * filled + empty_char * empty
    return f"[{bar}] {percentage:.1f}%"

# ========== HELPER FUNCTIONS ==========
def db_execute(query, params=(), fetch='none', retries=5, delay=0.2):
    """
    Thread-safe SQLite helper with automatic retry on SQLITE_BUSY/LOCKED.
    fetch='none' → no return, 'one' → fetchone(), 'all' → fetchall()
    """
    for attempt in range(retries):
        conn = None
        try:
            conn = sqlite3.connect(DATABASE_FILE, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            if fetch == 'one':
                return c.fetchone()
            if fetch == 'all':
                return c.fetchall()
            return True
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() or 'busy' in str(e).lower():
                if attempt < retries - 1:
                    sleep(delay * (attempt + 1))
                    continue
            print(f"❌ DB OperationalError: {e} | Query: {query[:80]}")
            return None
        except sqlite3.IntegrityError as e:
            print(f"⚠️ DB IntegrityError (ignored): {e}")
            return None
        except Exception as e:
            print(f"❌ DB error: {e}")
            return None
        finally:
            if conn:
                try: conn.close()
                except Exception: pass
    return None

def get_user_info(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return {'first_name': row[0] if row else 'User'}

def update_system_stats():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments")
        total_deployments = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments WHERE status='active'")
        active_deployments_count = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments WHERE status='paused'")
        paused_deployments = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM deployments WHERE is_free=1")
        free_deployments = c.fetchone()[0] or 0
        c.execute("SELECT SUM(coins_amount) FROM redeem_codes")
        coins_created = c.fetchone()[0] or 0
        c.execute("SELECT SUM(stars_amount) FROM redeem_codes")
        stars_created = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM star_transactions WHERE transaction_type='subscription' AND status='completed'")
        revenue_stars = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM users WHERE is_premium=1")
        premium_users = c.fetchone()[0] or 0
        
        revenue_usd = revenue_stars * 0.01
        
        c.execute('''UPDATE system_stats SET 
            total_users=?, total_deployments=?, total_active_deployments=?,
            total_paused_deployments=?, total_free_deployments=?, total_coins_created=?,
            total_stars_created=?, total_revenue_stars=?, total_revenue_usd=?,
            premium_users=?, last_updated=?
            WHERE id=1''',
            (total_users, total_deployments, active_deployments_count, paused_deployments,
             free_deployments, coins_created, stars_created, revenue_stars, revenue_usd,
             premium_users, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Update system stats error: {e}")

def get_system_stats():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM system_stats WHERE id = 1')
        row = c.fetchone()
        conn.close()
        if row:
            return {
                'total_users': row[1], 'total_deployments': row[2],
                'active_deployments': row[3], 'paused_deployments': row[4],
                'free_deployments': row[5], 'coins_created': row[6],
                'stars_created': row[7], 'revenue_stars': row[8],
                'revenue_usd': row[9], 'premium_users': row[10],
                'server_start_time': row[11], 'last_updated': row[12]
            }
        return {}
    except Exception as e:
        print(f"❌ Get system stats error: {e}")
        return {}

try:
    import psutil as _psutil
except ImportError:
    _psutil = None


def _read_proc_meminfo():
    """Total/available RAM in MB, straight from /proc (no dependency needed)."""
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                key, val = line.split(':', 1)
                info[key.strip()] = int(val.strip().split()[0])  # kB
        total_mb = info.get('MemTotal', 0) / 1024
        avail_mb = info.get('MemAvailable', info.get('MemFree', 0)) / 1024
        used_mb = total_mb - avail_mb
        return total_mb, used_mb, avail_mb
    except Exception:
        return 0, 0, 0


def _read_proc_cpu_times():
    """Cumulative CPU jiffies since boot, for computing a delta-based %."""
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        return total, idle
    except Exception:
        return None, None


def get_system_resources(sample_seconds=0.3):
    """
    Host-wide RAM/CPU usage. Uses psutil if available (no blocking sample
    needed for RAM; still samples briefly for an accurate CPU%), otherwise
    falls back to reading /proc directly — pure stdlib, works on any Linux
    container (Render included) with zero extra dependencies.
    """
    result = {'ram_total_mb': 0, 'ram_used_mb': 0, 'ram_percent': 0,
              'cpu_percent': 0, 'cpu_count': os.cpu_count() or 1,
              'load_avg': (0.0, 0.0, 0.0), 'source': 'proc'}

    if _psutil:
        try:
            vm = _psutil.virtual_memory()
            result['ram_total_mb'] = vm.total / (1024 * 1024)
            result['ram_used_mb'] = (vm.total - vm.available) / (1024 * 1024)
            result['ram_percent'] = vm.percent
            result['cpu_percent'] = _psutil.cpu_percent(interval=sample_seconds)
            result['cpu_count'] = _psutil.cpu_count() or result['cpu_count']
            result['source'] = 'psutil'
        except Exception:
            pass

    if result['source'] == 'proc':
        total_mb, used_mb, _ = _read_proc_meminfo()
        result['ram_total_mb'] = total_mb
        result['ram_used_mb'] = used_mb
        result['ram_percent'] = (used_mb / total_mb * 100) if total_mb else 0

        t0, idle0 = _read_proc_cpu_times()
        if t0 is not None:
            sleep(sample_seconds)
            t1, idle1 = _read_proc_cpu_times()
            if t1 and t1 > t0:
                busy_delta = (t1 - idle1) - (t0 - idle0)
                total_delta = t1 - t0
                result['cpu_percent'] = max(0, min(100, busy_delta / total_delta * 100)) if total_delta else 0

    try:
        result['load_avg'] = os.getloadavg()
    except (OSError, AttributeError):
        pass

    return result


def get_deployment_resource_usage(proc_pid):
    """RAM (MB) and CPU% for one deployment's process, best-effort."""
    if not proc_pid:
        return None
    if _psutil:
        try:
            p = _psutil.Process(proc_pid)
            return {
                'ram_mb': p.memory_info().rss / (1024 * 1024),
                'cpu_percent': p.cpu_percent(interval=0.1),
            }
        except Exception:
            return None
    # /proc fallback: RSS from status, skip CPU% (needs sampling per-process,
    # too slow to do for every deployment in a list — RAM alone is still useful)
    try:
        with open(f'/proc/{proc_pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return {'ram_mb': int(line.split()[1]) / 1024, 'cpu_percent': None}
    except Exception:
        pass
    return None


# ==================== PER-USER RESOURCE QUOTAS ====================
# The host's total resource pool is split equally between all registered
# (non-admin) users: e.g. 30 GB pool / 10 users = 3 GB each. Admins are not
# limited and do not count towards the divisor.
#   RESOURCE_POOL_GB   total pool in GB (default: 90% of the host's RAM)
#   QUOTA_ENFORCE      1 = stop a user's biggest bot after sustained overuse
#   QUOTA_STRIKES      consecutive 60 s checks over quota before stopping (3)
RESOURCE_POOL_GB = os.environ.get("RESOURCE_POOL_GB", "").strip()
QUOTA_ENFORCE    = os.environ.get("QUOTA_ENFORCE", "1") == "1"
QUOTA_STRIKES    = int(os.environ.get("QUOTA_STRIKES", 3))
_quota_strikes   = {}


def get_resource_pool_mb() -> float:
    """Total shareable pool in MB (RESOURCE_POOL_GB, else 90% of host RAM)."""
    if RESOURCE_POOL_GB:
        try:
            return max(64.0, float(RESOURCE_POOL_GB) * 1024)
        except ValueError:
            pass
    total_mb, _, _ = _read_proc_meminfo()
    if not total_mb and _psutil:
        try:
            total_mb = _psutil.virtual_memory().total / (1024 * 1024)
        except Exception:
            total_mb = 0
    return max(256.0, total_mb * 0.9) if total_mb else 1024.0


def count_quota_users() -> int:
    """Number of users the pool is divided between (non-admins, minimum 1)."""
    try:
        admin_list = list(ADMIN_IDS)
        if admin_list:
            q = ("SELECT COUNT(*) FROM users WHERE user_id NOT IN (%s)"
                 % ",".join("?" * len(admin_list)))
            row = db_execute(q, tuple(admin_list), fetch='one')
        else:
            row = db_execute("SELECT COUNT(*) FROM users", (), fetch='one')
        return max(1, int(row[0]) if row and row[0] else 1)
    except Exception:
        return 1


def get_user_quota_mb(user_id) -> float:
    """This user's share of the pool in MB (admins get the whole pool)."""
    pool = get_resource_pool_mb()
    if is_admin(user_id):
        return pool
    return pool / count_quota_users()


def format_resource(mb: float) -> str:
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def get_process_tree_ram_mb(pid) -> float:
    """RSS (MB) of a deployment's launcher AND everything it spawned."""
    if not pid:
        return 0.0
    if _psutil:
        try:
            p = _psutil.Process(pid)
            total = p.memory_info().rss
            for ch in p.children(recursive=True):
                try:
                    total += ch.memory_info().rss
                except Exception:
                    pass
            return total / (1024 * 1024)
        except Exception:
            return 0.0
    # /proc fallback: deployments run under setsid, so pgid == launcher pid
    try:
        pgid = os.getpgid(pid)
        own_pgid = os.getpgid(os.getpid())
    except Exception:
        return 0.0
    only_self = (pgid == own_pgid)   # legacy un-isolated process: just itself
    total_kb = 0
    try:
        candidates = [d for d in os.listdir('/proc') if d.isdigit()]
    except Exception:
        return 0.0
    for d in candidates:
        cpid = int(d)
        if only_self and cpid != pid:
            continue
        try:
            if not only_self and os.getpgid(cpid) != pgid:
                continue
            with open(f'/proc/{cpid}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        total_kb += int(line.split()[1])
                        break
        except Exception:
            continue
    return total_kb / 1024


def get_user_resource_usage_mb(user_id) -> float:
    rows = db_execute("SELECT proc_pid FROM deployments WHERE user_id = ? "
                      "AND status = 'active' AND proc_pid IS NOT NULL",
                      (user_id,), fetch='all') or []
    return sum(get_process_tree_ram_mb(r[0]) for r in rows)


def check_user_quota(user_id):
    """Returns (ok, used_mb, quota_mb). ok=False when the user has no headroom."""
    quota = get_user_quota_mb(user_id)
    if is_admin(user_id):
        return True, get_user_resource_usage_mb(user_id), quota
    used = get_user_resource_usage_mb(user_id)
    return used < quota, used, quota


def quota_exceeded_text(used_mb, quota_mb) -> str:
    return (f"⚠️ *Resource limit reached*\n\n"
            f"You are using `{format_resource(used_mb)}` of your "
            f"`{format_resource(quota_mb)}` share.\n"
            f"The host's resources are split equally between all users, so your "
            f"share shrinks as more people join.\n\n"
            f"Stop or delete a bot to free up resources, then try again.")


def quota_monitor():
    """Every 60 s: warn users over their share, stop their biggest bot if sustained."""
    sleep(90)
    print("✅ Resource quota monitor started")
    while True:
        try:
            rows = db_execute("SELECT DISTINCT user_id FROM deployments "
                              "WHERE status = 'active' AND proc_pid IS NOT NULL",
                              (), fetch='all') or []
            for (uid,) in rows:
                if is_admin(uid):
                    continue
                quota = get_user_quota_mb(uid)
                used = get_user_resource_usage_mb(uid)
                if used <= quota:
                    _quota_strikes.pop(uid, None)
                    continue

                strikes = _quota_strikes.get(uid, 0) + 1
                _quota_strikes[uid] = strikes
                if strikes == 1:
                    try:
                        send_message(uid,
                            f"⚠️ *Over your resource share*\n\n"
                            f"Using `{format_resource(used)}` of `{format_resource(quota)}`.\n"
                            f"Reduce usage or your largest bot will be stopped in "
                            f"~{QUOTA_STRIKES} min.")
                    except Exception:
                        pass
                if QUOTA_ENFORCE and strikes >= QUOTA_STRIKES:
                    dep_rows = db_execute("SELECT deployment_id, proc_pid FROM deployments "
                                          "WHERE user_id = ? AND status = 'active' "
                                          "AND proc_pid IS NOT NULL",
                                          (uid,), fetch='all') or []
                    if dep_rows:
                        biggest = max(dep_rows, key=lambda r: get_process_tree_ram_mb(r[1]))
                        stop_deployment(biggest[0])
                        print(f"🛑 Quota: stopped deployment {biggest[0]} (user {uid})")
                        try:
                            send_message(uid,
                                f"🛑 *Bot #{biggest[0]} stopped*\n\n"
                                f"You stayed over your `{format_resource(quota)}` share.",
                                {"inline_keyboard": [[{"text": "📦 My Deployments",
                                                       "callback_data": "my_deployments"}]]})
                        except Exception:
                            pass
                    _quota_strikes[uid] = 0
        except Exception as e:
            print(f"❌ Quota monitor error: {e}")
        sleep(60)


def get_user_balances(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT coins_balance, stars_balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return {'coins': row[0] if row else 0, 'stars': row[1] if row else 0}

def update_user_coins(user_id, delta, transaction_type="balance_update", source="system"):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute("UPDATE users SET coins_balance = coins_balance + ? WHERE user_id = ?", (delta, user_id))
        if delta > 0:
            c.execute("UPDATE users SET total_coins_earned = total_coins_earned + ? WHERE user_id = ?", (delta, user_id))
        else:
            c.execute("UPDATE users SET total_coins_spent = total_coins_spent + ? WHERE user_id = ?", (abs(delta), user_id))
        c.execute('''INSERT INTO coin_transactions 
            (user_id, amount, transaction_type, source, reference_id, timestamp, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, delta, transaction_type, source, None, datetime.now().isoformat(), 'completed'))
        conn.commit()
    except Exception as e:
        print(f"❌ update_user_coins error: {e}")
        conn.rollback()
    finally:
        conn.close()
    update_system_stats()
    return True

def update_user_stars(user_id, delta, transaction_type="balance_update", source="system", payload=None):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute("UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?", (delta, user_id))
        if delta > 0:
            c.execute("UPDATE users SET total_stars_earned = total_stars_earned + ? WHERE user_id = ?", (delta, user_id))
        else:
            c.execute("UPDATE users SET total_stars_spent = total_stars_spent + ? WHERE user_id = ?", (abs(delta), user_id))
        c.execute('''INSERT INTO star_transactions 
            (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, delta, transaction_type, source, None, datetime.now().isoformat(), 'completed', payload))
        conn.commit()
    except Exception as e:
        print(f"❌ update_user_stars error: {e}")
        conn.rollback()
    finally:
        conn.close()
    update_system_stats()
    return True

def is_user_premium(user_id):
    if is_admin(user_id):
        return True
    
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''SELECT is_premium, premium_expires FROM users WHERE user_id = ?''', (user_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0] == 1 and row[1]:
            expires = datetime.fromisoformat(row[1])
            if expires > datetime.now():
                return True
        return False
    except Exception:
        return False

def _wait_for_launch(deploy_folder, timeout=15, settle=3):
    """
    Poll pid.txt instead of sleeping a fixed time. Returns once the process
    named in pid.txt has been alive for `settle` seconds (so an immediate
    crash is still caught), or when `timeout` seconds have passed. Slow hosts
    that need >5s to start Python no longer produce false "failed" results.
    """
    pid_file = Path(deploy_folder) / "pid.txt"
    deadline = datetime.now().timestamp() + timeout
    alive_since = None
    while datetime.now().timestamp() < deadline:
        pid = None
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except Exception:
                pid = None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        if alive:
            if alive_since is None:
                alive_since = datetime.now().timestamp()
            elif datetime.now().timestamp() - alive_since >= settle:
                return True
        else:
            alive_since = None
        sleep(0.5)
    return False

def premium_covers_plan(user_id, plan) -> bool:
    """
    Whether the user's CURRENT premium tier includes a free deployment of
    `plan`. A monthly premium subscription covers monthly deployments only;
    a yearly subscription covers monthly and yearly. Admins cover everything.
    """
    if is_admin(user_id):
        return True
    if not is_user_premium(user_id):
        return False
    if plan in ("free", None):
        return True
    if plan == "lifetime":
        return False  # admin-only
    try:
        row = db_execute("SELECT premium_plan FROM users WHERE user_id = ?",
                         (user_id,), fetch='one')
        tier = (row[0] if row and row[0] else "monthly").lower()
    except Exception:
        tier = "monthly"
    if tier == "yearly":
        return plan in ("monthly", "yearly")
    return plan == "monthly"

def get_free_deployment_used_count(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM deployments WHERE user_id = ? AND is_free = 1 AND status = 'active'", (user_id,))
    count = c.fetchone()[0] or 0
    conn.close()
    return count

def can_use_free_deployment(user_id):
    if is_admin(user_id):
        return True, "Admin - Unlimited"
    
    if is_user_premium(user_id):
        return True, "Premium - Unlimited"
    
    used_count = get_free_deployment_used_count(user_id)
    remaining = FREE_USER_MAX_DEPLOYMENTS - used_count
    
    if remaining > 0:
        return True, f"Free tier - {remaining} remaining (used {used_count}/{FREE_USER_MAX_DEPLOYMENTS})"
    else:
        return False, f"Free tier limit reached ({used_count}/{FREE_USER_MAX_DEPLOYMENTS})"

def stop_free_deployments_for_user(user_id):
    """
    Stop all active/paused free-tier (is_free=1) deployments for a user.
    Called when premium is activated to eliminate duplicates.
    Returns the count of bots stopped.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT deployment_id, proc_pid FROM deployments
                 WHERE user_id = ? AND is_free = 1 AND status IN ('active','paused')""",
              (user_id,))
    rows = c.fetchall()
    stopped = 0
    for dep_id, proc_pid in rows:
        if proc_pid:
            try:
                kill_deployment_process(proc_pid)
            except Exception:
                pass
        c.execute("""UPDATE deployments SET status='stopped', proc_pid=NULL, is_paused=0
                     WHERE deployment_id=?""", (dep_id,))
        with deployment_lock:
            active_deployments.pop(dep_id, None)
        stopped += 1
    conn.commit()
    conn.close()
    return stopped


def resume_premium_deployment(user_id, duration_days):
    """
    Find the most recent stopped/paused premium deployment for this user,
    restart it with the new expiry window, and return (dep_id, success).
    This is called right after a new premium subscription is activated so the
    user's bot picks up exactly where it left off — database fully intact.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT deployment_id, file_name, env_vars, folder_name, proc_pid
                 FROM deployments
                 WHERE user_id = ? AND is_free = 0
                       AND status IN ('stopped','paused','failed')
                 ORDER BY start_time DESC LIMIT 1""", (user_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return None, False

    dep_id, file_name, env_vars_json, folder_name_val, old_pid = row

    # Kill stale process if any
    if old_pid:
        try:
            kill_deployment_process(old_pid)
        except Exception:
            pass

    deploy_folder = get_deploy_folder(user_id, dep_id)
    start_script  = deploy_folder / "start.sh"
    launcher      = deploy_folder / "run.py"

    if not start_script.exists() or not launcher.exists():
        return dep_id, False

    # Re-run the bot
    result = subprocess.run([str(start_script)], cwd=str(deploy_folder),
                            capture_output=True, text=True)
    _wait_for_launch(deploy_folder, timeout=15)

    pid_file  = deploy_folder / "pid.txt"
    new_pid   = None
    if pid_file.exists():
        try:
            new_pid = int(pid_file.read_text().strip())
        except Exception:
            pass

    running = False
    if new_pid:
        try:
            os.kill(new_pid, 0)
            running = True
        except Exception:
            pass

    if running:
        new_expire = datetime.now() + timedelta(days=duration_days)
        conn2 = sqlite3.connect(DATABASE_FILE)
        conn2.execute("""UPDATE deployments
                         SET status='active', proc_pid=?, is_paused=0,
                             expire_time=?, start_time=?
                         WHERE deployment_id=?""",
                      (new_pid, new_expire.isoformat(), datetime.now().isoformat(), dep_id))
        conn2.commit()
        conn2.close()
        with deployment_lock:
            active_deployments[dep_id] = new_pid
        async_backup(f"premium_resume_{dep_id}")
        return dep_id, True

    return dep_id, False


def continue_deployment_as_free(deployment_id, user_id, chat_id):
    """
    Downgrade a stopped/expired premium deployment to a fresh 24-hr free slot.
    The deploy folder and database are untouched — the bot resumes from the
    exact state it was in when premium expired.
    """
    deploy_folder = get_deploy_folder(user_id, deployment_id)
    start_script  = deploy_folder / "start.sh"
    launcher      = deploy_folder / "run.py"

    if not start_script.exists() or not launcher.exists():
        send_message(chat_id,
            "❌ Deployment files not found — please deploy a new bot.",
            {"inline_keyboard": [[{"text": "🚀 Deploy New Bot", "callback_data": "deploy_new"}]]})
        return False

    # Kill stale PID first
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE deployment_id=?", (deployment_id,))
    r = c.fetchone()
    conn.close()
    if r and r[0]:
        try:
            kill_deployment_process(r[0])
        except Exception:
            pass

    subprocess.run([str(start_script)], cwd=str(deploy_folder), capture_output=True)
    _wait_for_launch(deploy_folder, timeout=15)

    pid_file = deploy_folder / "pid.txt"
    new_pid  = None
    if pid_file.exists():
        try:
            new_pid = int(pid_file.read_text().strip())
        except Exception:
            pass

    running = False
    if new_pid:
        try:
            os.kill(new_pid, 0)
            running = True
        except Exception:
            pass

    if running:
        new_expire = datetime.now() + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
        conn2 = sqlite3.connect(DATABASE_FILE)
        conn2.execute("""UPDATE deployments
                         SET status='active', proc_pid=?, is_paused=0,
                             is_free=1, expire_time=?, start_time=?
                         WHERE deployment_id=?""",
                      (new_pid, new_expire.isoformat(),
                       datetime.now().isoformat(), deployment_id))
        conn2.commit()
        conn2.close()
        with deployment_lock:
            active_deployments[deployment_id] = new_pid

        async_backup(f"continue_as_free_{deployment_id}")
        send_message(chat_id,
            f"✅ *Bot Resumed (Free 24h)*\n\n"
            f"Deployment `#{deployment_id}` is running again.\n"
            f"Your database and settings are intact.\n"
            f"Expires: `{new_expire.strftime('%Y-%m-%d %H:%M')}`",
            {"inline_keyboard": [
                [{"text": "📄 View Logs",       "callback_data": f"view_runtime_logs_{deployment_id}"}],
                [{"text": "⭐ Get Premium",      "callback_data": "subscribe_premium"}],
                [{"text": "🏠 Menu",            "callback_data": "main_menu"}],
            ]})
        return True
    else:
        log_tail = ""
        lf = deploy_folder / "output.log"
        if lf.exists():
            log_tail = lf.read_text(errors='replace')[-800:].strip()
        send_message(chat_id,
            f"❌ *Failed to resume bot*\n\n"
            f"```\n{log_tail[-500:]}\n```\n\n"
            "The process exited immediately. Check your env vars.",
            {"inline_keyboard": [
                [{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}],
                [{"text": "🚀 Deploy New Bot", "callback_data": "deploy_new"}],
            ]})
        return False


def count_github_deployments(user_id):
    """Count active/paused GitHub-sourced deployments for a user."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT COUNT(*) FROM deployments
                 WHERE user_id=? AND source_type='github'
                 AND status IN ('active','paused')""", (user_id,))
    n = c.fetchone()[0]
    conn.close()
    return n


def activate_premium(user_id, plan, amount_stars, amount_coins, duration_days):
    try:
        end_date = datetime.now() + timedelta(days=duration_days)

        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium=1, premium_expires=?, premium_plan=? WHERE user_id=?",
                  (end_date.isoformat(), plan, user_id))
        c.execute("""INSERT INTO subscriptions
                     (user_id, plan, amount_stars, amount_coins, start_date, end_date, status)
                     VALUES (?,?,?,?,?,?,?)""",
                  (user_id, plan, amount_stars, amount_coins,
                   datetime.now().isoformat(), end_date.isoformat(), 'active'))
        conn.commit()
        conn.close()

        # 1. Stop all free-tier bots (no duplicates while premium runs)
        stopped_free = stop_free_deployments_for_user(user_id)

        # 2. Resume the most recent stopped/paused premium deployment
        resumed_dep_id, premium_resumed = resume_premium_deployment(user_id, duration_days)

        # 3. If no prior premium deployment exists, fall back to resuming paused ones
        paused_resumed = 0
        if not premium_resumed:
            paused_resumed = resume_paused_deployments(user_id)

        update_system_stats()

        user_info   = get_user_info(user_id)
        notify_admin(
            f"🎉 NEW PREMIUM!\n"
            f"User: {user_info.get('first_name','?')} ({user_id})\n"
            f"Plan: {plan.upper()} | Amount: {amount_stars}⭐\n"
            f"Premium dep resumed: #{resumed_dep_id} ({premium_resumed})\n"
            f"Free bots stopped: {stopped_free}")

        async_backup(f"premium_{plan}_{user_id}")
        total_resumed = (1 if premium_resumed else paused_resumed)
        return True, total_resumed

    except Exception as e:
        print(f"❌ Activate premium error: {e}")
        return False, 0

def resume_paused_deployments(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute('''SELECT deployment_id, file_name, env_vars, folder_name
                     FROM deployments WHERE user_id = ? AND status = 'paused' AND is_paused = 1''', (user_id,))
        paused = c.fetchall()
        
        resumed_count = 0
        for dep_id, file_name, env_vars_json, folder_name_val in paused:
            # Resolve correct folder using stored folder_name
            if folder_name_val:
                deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(folder_name_val)
            else:
                deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(dep_id)
            dest_script = deploy_folder / file_name
            
            if dest_script.exists():
                env_vars = json.loads(env_vars_json) if env_vars_json else {}
                env = os.environ.copy()
                for k, v in env_vars.items():
                    env[k] = v
                
                launcher_script = deploy_folder / "run.py"
                log_file_path = deploy_folder / "output.log"
                with open(log_file_path, "a") as log_f:
                    if launcher_script.exists():
                        proc = subprocess.Popen(
                            [sys.executable, str(launcher_script)],
                            cwd=str(deploy_folder),
                            env=env,
                            stdout=log_f,
                            stderr=subprocess.STDOUT
                        )
                    else:
                        proc = subprocess.Popen(
                            [sys.executable, str(dest_script)],
                            cwd=str(deploy_folder),
                            env=env,
                            stdout=log_f,
                            stderr=subprocess.STDOUT
                        )
                
                c.execute('''UPDATE deployments SET status = 'active', is_paused = 0, proc_pid = ? 
                             WHERE deployment_id = ?''', (proc.pid, dep_id))  # FIX: store proc.pid not proc
                resumed_count += 1
                
                with deployment_lock:
                    active_deployments[dep_id] = proc.pid  # FIX: store PID integer
        
        conn.commit()
        conn.close()
        return resumed_count
    except Exception as e:
        print(f"❌ Resume paused deployments error: {e}")
        return 0

def notify_admin(message):
    for admin_id in ADMIN_IDS:
        try:
            send_message(admin_id, message)
        except Exception as e:
            print(f"Failed to notify admin {admin_id}: {e}")

# Records the last Telegram HTTP error code seen by send_message() on this
# thread, so callers (e.g. broadcast) can tell a 429 rate-limit (worth
# retrying with backoff) from a 403 "bot was blocked" (not worth retrying).
_send_err = threading.local()

def send_message(chat_id, text, keyboard=None, parse_mode="Markdown"):
    _send_err.code = None
    url  = f"{TELEGRAM_API}/sendMessage"
    text = str(text or '')[:4096]
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    for _attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                return result
        except urllib.error.HTTPError as e:
            _send_err.code = e.code
            if e.code == 429:          # rate limit
                sleep(2 * (_attempt + 1))
                continue
            body = ''
            try: body = e.read().decode()[:200]
            except Exception: pass
            print(f"Send HTTP {e.code}: {body}")
            # ── Fallback: bad/unbalanced Markdown must never mean "no reply" ──
            # Telegram rejects the WHOLE message if entity parsing fails (e.g. a
            # stray "_" or "*" in a username/first_name breaks bold/italic
            # pairing). Retry once as plain text so the user always gets a
            # response instead of silence.
            if parse_mode and "can't parse entities" in body.lower():
                data_plain = {k: v for k, v in data.items() if k != "parse_mode"}
                try:
                    req2 = urllib.request.Request(
                        url, data=json.dumps(data_plain).encode('utf-8'),
                        headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req2, timeout=30) as resp2:
                        print("⚠️  Sent as plain text after Markdown parse failure")
                        return json.loads(resp2.read().decode('utf-8'))
                except Exception as e2:
                    print(f"Plain-text fallback also failed: {e2}")
            return None
        except Exception as e:
            print(f"Send error (attempt {_attempt+1}): {e}")
            if _attempt < 2:
                sleep(1)
    return None

def edit_message(chat_id, message_id, text, keyboard=None):
    if not message_id:          # Error 31/10: guard against None message_id
        send_message(chat_id, str(text or '')[:4096], keyboard)
        return None
    url  = f"{TELEGRAM_API}/editMessageText"
    data = {"chat_id": chat_id, "message_id": message_id,
            "text": str(text or '')[:4096], "parse_mode": "Markdown"}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    for _attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                sleep(2 * (_attempt + 1))
                continue
            if e.code == 400:
                body = ''
                try: body = e.read().decode()[:200]
                except Exception: pass
                # "can't parse entities" = broken Markdown (e.g. special chars
                # in user-supplied text). Retry as plain text instead of
                # silently dropping the edit. Other 400s (message not
                # modified / too old) are still ignored.
                if "can't parse entities" in body.lower():
                    data_plain = {k: v for k, v in data.items() if k != "parse_mode"}
                    try:
                        req2 = urllib.request.Request(
                            url, data=json.dumps(data_plain).encode('utf-8'),
                            headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req2, timeout=30) as resp2:
                            print("⚠️  Edited as plain text after Markdown parse failure")
                            return json.loads(resp2.read().decode('utf-8'))
                    except Exception as e2:
                        print(f"Plain-text edit fallback also failed: {e2}")
                return None
            print(f"Edit HTTP {e.code}")
            return None
        except Exception as e:
            print(f"Edit error (attempt {_attempt+1}): {e}")
            if _attempt < 2:
                sleep(1)
    return None

def answer_callback(callback_id, text=None, show_alert=False):
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    if show_alert:
        data["show_alert"] = True
    try:
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Answer callback error: {e}")

def send_document(chat_id, file_path, caption=None, filename=None):
    """
    Upload a local file to Telegram via sendDocument. Pure urllib multipart
    encoding — no extra dependencies (mirrors send_message's style).
    """
    file_path = Path(file_path)
    if not file_path.exists():
        print(f"❌ send_document: file not found: {file_path}")
        return None

    filename = filename or file_path.name
    file_bytes = file_path.read_bytes()

    def _build_body(use_markdown):
        boundary = f"----HostingBotBoundary{secrets.token_hex(16)}"

        def _field(name, value):
            return (f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f'{value}\r\n').encode('utf-8')

        body = b""
        body += _field("chat_id", chat_id)
        if caption:
            body += _field("caption", str(caption)[:1024])
            if use_markdown:
                body += _field("parse_mode", "Markdown")

        body += (f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
        body += file_bytes
        body += f'\r\n--{boundary}--\r\n'.encode('utf-8')
        return boundary, body

    url = f"{TELEGRAM_API}/sendDocument"

    def _attempt_send(use_markdown):
        boundary, body = _build_body(use_markdown)
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))

    for _attempt in range(3):
        try:
            return _attempt_send(use_markdown=True)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                sleep(2 * (_attempt + 1))
                continue
            try:
                err_body = e.read().decode()[:300]
            except Exception:
                err_body = ''
            print(f"❌ send_document HTTP {e.code}: {err_body}")
            # Bad/unbalanced caption Markdown must never mean total silence —
            # retry once as plain text (same pattern as send_message).
            if caption and "can't parse entities" in err_body.lower():
                try:
                    print("⚠️  Retrying send_document as plain-text caption")
                    return _attempt_send(use_markdown=False)
                except Exception as e2:
                    print(f"❌ send_document plain-text retry also failed: {e2}")
            try:
                return json.loads(err_body)  # {"ok": False, "description": "..."}
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code}"}
        except Exception as e:
            print(f"❌ send_document error (attempt {_attempt+1}): {e}")
            if _attempt < 2:
                sleep(1)
    return {"ok": False, "description": "request failed after 3 attempts (see server logs)"}

def http_get(url, params=None):
    try:
        # The socket timeout must be longer than any long-poll "timeout"
        # param we send Telegram, or the connection can time out right as
        # Telegram's response arrives at the edge of its own wait window,
        # silently dropping that poll (getUpdates uses timeout=30).
        socket_timeout = 30
        if params and 'timeout' in params:
            socket_timeout = int(params['timeout']) + 10
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=socket_timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"HTTP error: {e}")
        return None

_FILENAME_PREFIX_RE = re.compile(r'^\d{8}_\d{6}_temp_\d+_')

def display_filename(system_filename: str) -> str:
    """
    Deployment files on disk are named like
    "20260720_211725_temp_7713987088_index.js" (timestamp + temp + user id
    prefix). That's fine for storage, but showing it raw to the user is both
    ugly AND risky: legacy Telegram Markdown's handling of underscores inside
    backtick-wrapped text is notoriously unreliable (well-documented — even
    content inside backticks isn't always treated as truly literal the way
    it should be), and this prefix always contains multiple underscores.
    Strip it for display; fall back to the full name if it doesn't match
    (e.g. GitHub deployments, which aren't named this way).
    """
    if not system_filename:
        return system_filename
    return _FILENAME_PREFIX_RE.sub('', system_filename)


def escape_md_v1(text: str) -> str:
    """
    Escape legacy-Markdown special characters in text that will appear
    OUTSIDE of backticks/code spans — per Telegram's own documented rule,
    prefix _, *, `, [ with a backslash. Do NOT use this on text that's
    already inside a backtick span (code spans have different, stricter
    rules — escaping inside them shows a literal backslash instead of
    hiding it).
    """
    if not text:
        return text
    return re.sub(r'([_*`\[])', r'\\\1', str(text))


def safe_md(text) -> str:
    """
    Make arbitrary text (file names, scanner findings) safe to embed in a
    Telegram legacy-Markdown message, including inside `backtick` spans.
    Backticks and backslashes are the only characters that can break out of
    (or be mangled inside) a code span, so they are neutralised; any other
    stray markup is still covered by send_message()/edit_message()'s
    plain-text fallback on "can't parse entities".
    """
    if text is None:
        return ''
    s = str(text).replace('\\', '/').replace('`', "'")
    return s


def sanitize_upload_filename(name: str, default: str = "upload") -> str:
    """
    Reduce a user-supplied file name to a safe basename: no directories,
    no '..', no control characters, only [A-Za-z0-9._-]. Prevents path
    traversal when the name is used to build a path under BASE_DIR.
    """
    name = os.path.basename(str(name or '').replace('\\', '/'))
    name = re.sub(r'[^A-Za-z0-9._-]', '_', name).lstrip('.')
    return name[:120] or default


def format_file_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    
    return f"{size_bytes:.1f} {size_names[i]}"

def format_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

# ==================== CHANNEL VERIFICATION ====================

def check_channel_membership(user_id) -> bool:
    """
    Permanent fix: check whether user_id is a member of REQUIRED_CHANNEL.

    Rules:
    - If REQUIRED_CHANNEL is not configured → always pass (no gate)
    - Uses GET with query-string params (works regardless of Content-Type)
    - Retries up to 3 times on network errors
    - If Telegram returns a bot-permission error (bot not admin), auto-pass
      so a misconfigured channel never permanently blocks users
    """
    channel = (REQUIRED_CHANNEL or '').strip()
    if not channel or channel in ('', 'None', 'false', '0'):
        return True          # no channel gate configured — everyone passes

    # Ensure channel starts with @ or is a numeric id
    if not channel.startswith('@') and not channel.lstrip('-').isdigit():
        channel = '@' + channel

    url = (f"{TELEGRAM_API}/getChatMember"
           f"?chat_id={urllib.parse.quote(channel)}&user_id={user_id}")

    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode('utf-8'))
                if data.get('ok'):
                    status = (data.get('result') or {}).get('status', '')
                    return status in ('member', 'administrator', 'creator', 'restricted')
                # Telegram error — check if it's a permission issue
                desc = (data.get('description') or '').lower()
                if any(kw in desc for kw in ('bot is not', 'not a member',
                                              'need admin', 'rights', 'forbidden')):
                    print(f"⚠️  getChatMember: {data.get('description')} — auto-passing")
                    return True     # bot isn't admin → don't punish users
                return False        # user genuinely not in channel
        except urllib.error.HTTPError as e:
            body = ''
            try: body = e.read().decode()[:200]
            except Exception: pass
            print(f"⚠️  getChatMember HTTP {e.code}: {body}")
            if e.code in (400, 403):
                # Bad request or forbidden — likely config issue, don't block users
                return True
            if attempt < 2:
                sleep(1)
        except Exception as e:
            print(f"⚠️  getChatMember attempt {attempt+1}: {e}")
            if attempt < 2:
                sleep(1)

    # All 3 attempts failed (network issue) → fail CLOSED. A transient outage
    # must not let everyone through the channel gate; the user can simply
    # press VERIFY again. (Bot-permission/config errors are handled above.)
    print(f"⚠️  getChatMember failed after 3 attempts for {user_id} — denying (retry later)")
    return False


def mark_channel_joined(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute('UPDATE users SET joined_channel = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ mark_channel_joined: {e}")
        return False


def is_user_verified(user_id) -> bool:
    """
    Returns True if user has verified (or no channel is required).
    Admins are always considered verified.
    """
    if is_admin(user_id):
        return True
    channel = (REQUIRED_CHANNEL or '').strip()
    if not channel or channel in ('', 'None', 'false', '0'):
        return True
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute('SELECT joined_channel FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def send_verification_required(chat_id, user_id, first_name, message_id=None):
    channel = (REQUIRED_CHANNEL or '').strip() or 'our channel'
    link    = (CHANNEL_LINK or '').strip()

    text = (
        f"*🔐 VERIFICATION REQUIRED*\n\n"
        f"👋 Hi {first_name or 'there'}!\n\n"
        f"To use this hosting platform you must join our channel:\n"
        f"📢 *{channel}*\n\n"
        f"*Steps:*\n"
        f"1️⃣ Click *JOIN CHANNEL* below\n"
        f"2️⃣ Join the channel\n"
        f"3️⃣ Come back and click *✅ VERIFY*"
    )
    buttons = [{"text": "✅ VERIFY", "callback_data": "verify_channel"}]
    if link:
        buttons.insert(0, {"text": "📢 JOIN CHANNEL", "url": link})

    keyboard = {"inline_keyboard": [buttons]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def has_accepted_tos(user_id) -> bool:
    """Admins are exempt, same as channel verification."""
    if is_admin(user_id):
        return True
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute('SELECT tos_accepted FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:
        return False


def mark_tos_accepted(user_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute('UPDATE users SET tos_accepted = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ mark_tos_accepted: {e}")
        return False


def show_tos_prompt(chat_id, user_id, message_id=None):
    text = (
        f"*📜 TERMS OF SERVICE*\n\n"
        f"Before you can use this hosting platform, please read and accept:\n\n"
        f"1️⃣ This service provides infrastructure to run code you upload or "
        f"link from GitHub — we do not review, monitor, or endorse the "
        f"content or purpose of anything you host.\n\n"
        f"2️⃣ *You are solely responsible for anything you deploy.* "
        f"You confirm you have the right to host it and that it does not "
        f"violate any applicable law.\n\n"
        f"3️⃣ *We are not responsible for any illegal file, bot, or content "
        f"hosted through this platform — responsibility lies entirely with "
        f"the user who uploaded or deployed it.*\n\n"
        f"4️⃣ We reserve the right to remove any deployment and suspend any "
        f"account found to violate these terms, without notice.\n\n"
        f"5️⃣ Continued use of this bot after clicking Agree constitutes "
        f"acceptance of these terms.\n\n"
        f"👇 You must agree to continue."
    )
    keyboard = {"inline_keyboard": [[{"text": "✅ I Agree", "callback_data": "tos_agree"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

# ========== REDEEM CODES ==========
def generate_redeem_code(length=12):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def create_redeem_code(admin_id, coins_amount=0, stars_amount=0, expiry_days=30, max_uses=1):
    code = generate_redeem_code()
    expires_at = datetime.now() + timedelta(days=expiry_days)
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO redeem_codes 
        (code, coins_amount, stars_amount, created_by, created_at, expires_at, expiry_days, max_uses, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (code, coins_amount, stars_amount, admin_id, datetime.now().isoformat(), 
         expires_at.isoformat(), expiry_days, max_uses, 1))
    conn.commit()
    conn.close()
    update_system_stats()
    return code

def redeem_code(user_id, code):
    if not is_user_verified(user_id):
        return False, f"❌ You must join {REQUIRED_CHANNEL} first!"
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT code_id, coins_amount, stars_amount, max_uses, used_count, expires_at, is_active 
                 FROM redeem_codes WHERE code = ?''', (code,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return False, "❌ Invalid redeem code!"
    
    code_id, coins_amount, stars_amount, max_uses, used_count, expires_at, is_active = row
    
    if datetime.fromisoformat(expires_at) < datetime.now():
        conn.close()
        return False, "❌ This redeem code has expired!"
    
    if not is_active:
        conn.close()
        return False, "❌ This redeem code is no longer active!"
    
    if max_uses > 0 and used_count >= max_uses:
        conn.close()
        return False, f"❌ This redeem code has reached its usage limit ({max_uses}/{max_uses})!"
    
    used_by = c.execute("SELECT used_by FROM redeem_codes WHERE code = ?", (code,)).fetchone()[0]
    if used_by and str(user_id) in used_by.split(','):
        conn.close()
        return False, "❌ You have already used this redeem code!"
    
    reward_msg = []
    if coins_amount > 0:
        update_user_coins(user_id, coins_amount, "redeem", f"code_{code}")
        reward_msg.append(f"{coins_amount} 🪙")
    if stars_amount > 0:
        update_user_stars(user_id, stars_amount, "redeem", f"code_{code}")
        reward_msg.append(f"{stars_amount} ⭐")
    
    new_used_count = used_count + 1
    new_used_by = f"{used_by},{user_id}" if used_by else str(user_id)
    c.execute('''UPDATE redeem_codes SET used_count = ?, used_by = ? WHERE code_id = ?''',
              (new_used_count, new_used_by, code_id))
    
    conn.commit()
    conn.close()
    update_system_stats()
    async_backup(f"redeem_{user_id}")
    return True, f"✅ Redeemed: {', '.join(reward_msg)}!"

# ==================== REFERRAL SYSTEM ====================

def get_or_create_referral_code(user_id):
    """Return (and lazily create) a user's unique referral code."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    code = row[0] if row and row[0] else None
    if not code:
        code = f"ref{user_id}"
        c.execute("UPDATE users SET referral_code = ? WHERE user_id = ?", (code, user_id))
        conn.commit()
    conn.close()
    return code

def get_bot_username():
    """Fetch the bot's @username for building referral links."""
    try:
        with urllib.request.urlopen(f"{TELEGRAM_API}/getMe", timeout=10) as r:
            data = json.loads(r.read())
            return data.get("result", {}).get("username", "")
    except Exception:
        return ""

def process_referral(referrer_id, new_user_id):
    """Credit referrer when a new user joins via their link. Safe to call multiple times."""
    if referrer_id == new_user_id:
        return False
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    # Ensure the referrer exists
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
    if not c.fetchone():
        conn.close()
        return False
    # One referral per new user
    try:
        c.execute(
            "INSERT INTO referrals (referrer_id, referred_id, created_at, reward_coins, reward_given) "
            "VALUES (?, ?, ?, ?, 0)",
            (referrer_id, new_user_id, datetime.now().isoformat(), REFERRAL_REWARD_COINS))
        c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, new_user_id))
        c.execute("UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id = ?", (referrer_id,))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        conn.close()
        return False  # user already referred
    # Give coins to referrer
    update_user_coins(referrer_id, REFERRAL_REWARD_COINS, "referral_reward", f"referred_{new_user_id}")
    # Mark reward as given
    conn2 = sqlite3.connect(DATABASE_FILE)
    conn2.execute("UPDATE referrals SET reward_given = 1 WHERE referred_id = ?", (new_user_id,))
    conn2.commit()
    conn2.close()
    async_backup(f"referral_{referrer_id}")
    return True

def show_referral_menu(chat_id, user_id, message_id=None):
    code    = get_or_create_referral_code(user_id)
    bot_username = get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start={code}" if bot_username else f"Your code: `{code}`"

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(reward_coins),0) FROM referrals WHERE referrer_id = ? AND reward_given = 1", (user_id,))
    row = c.fetchone()
    total_refs   = row[0] or 0
    total_earned = row[1] or 0
    c.execute("SELECT referred_id, created_at FROM referrals WHERE referrer_id = ? ORDER BY created_at DESC LIMIT 5", (user_id,))
    recent = c.fetchall()
    conn.close()

    recent_text = ""
    for rid, rat in recent:
        dt = datetime.fromisoformat(rat).strftime("%d %b")
        recent_text += f"  • User `{rid}` — {dt}\n"

    text = (
        f"*👥 REFERRAL PROGRAMME*\n\n"
        f"Invite friends and earn *{REFERRAL_REWARD_COINS} 🪙* for every person who joins!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 *Your referral link:*\n`{ref_link}`\n\n"
        f"📊 *Your stats:*\n"
        f"  👥 Total referrals: `{total_refs}`\n"
        f"  🪙 Total earned:    `{total_earned}🪙`\n\n"
        + (f"🕐 *Recent referrals:*\n{recent_text}\n" if recent_text else "")
        + f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Share your link — every new user who starts the bot through it earns you coins!"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "📤 Share Link", "switch_inline_query": f"Join this bot hosting platform! {ref_link}"}],
        [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
    ]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

# ==================== BUG REPORT SYSTEM ====================

def submit_bug_report(user_id, username, first_name, message_text):
    """Save a bug report and broadcast it to all admins."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO bug_reports (user_id, username, first_name, message, status, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (user_id, username, first_name, message_text, datetime.now().isoformat()))
    report_id = c.lastrowid
    conn.commit()
    conn.close()

    # Notify all admins
    user_link = f"@{username}" if username else f"User `{user_id}`"
    admin_text = (
        f"*🐛 NEW BUG REPORT #{report_id}*\n\n"
        f"From: {user_link} (`{user_id}`)\n"
        f"Time: `{datetime.now().strftime('%Y-%m-%d %H:%M')}`\n\n"
        f"*Message:*\n{message_text}"
    )
    admin_kb = {"inline_keyboard": [
        [{"text": f"✉️ Reply to #{report_id}", "callback_data": f"bug_reply_{report_id}"}],
        [{"text": f"✅ Close #{report_id}",    "callback_data": f"bug_close_{report_id}"}]
    ]}
    for admin_id in ADMIN_IDS:
        send_message(admin_id, admin_text, admin_kb)

    async_backup(f"bug_report_{report_id}")
    return report_id

def reply_to_bug_report(report_id, admin_id, reply_text):
    """Save admin reply, mark report replied, and notify the user."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM bug_reports WHERE report_id = ?", (report_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, "Report not found"
    target_user_id, username, first_name = row
    c.execute(
        "UPDATE bug_reports SET status='replied', admin_reply=?, replied_by=?, replied_at=? "
        "WHERE report_id = ?",
        (reply_text, admin_id, datetime.now().isoformat(), report_id))
    conn.commit()
    conn.close()

    # Notify the user
    send_message(target_user_id,
        f"*✉️ REPLY TO YOUR BUG REPORT #{report_id}*\n\n"
        f"An admin has replied to your report:\n\n"
        f"_{reply_text}_\n\n"
        f"Thank you for helping us improve the service! 🙏",
        {"inline_keyboard": [[{"text": "🐛 Submit Another Report", "callback_data": "report_bug"}],
                              [{"text": "🏠 Main Menu",            "callback_data": "main_menu"}]]})
    return True, target_user_id

def show_admin_bug_reports(chat_id, admin_id, message_id=None):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT report_id, user_id, username, first_name, message, status, created_at "
              "FROM bug_reports ORDER BY created_at DESC LIMIT 20")
    reports = c.fetchall()
    c.execute("SELECT COUNT(*) FROM bug_reports WHERE status = 'open'")
    open_count = c.fetchone()[0]
    conn.close()

    if not reports:
        text = "*📋 BUG REPORTS*\n\nNo reports yet."
        kb   = {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]}
        if message_id: edit_message(chat_id, message_id, text, kb)
        else:          send_message(chat_id, text, kb)
        return

    text = f"*📋 BUG REPORTS* ({open_count} open)\n\n"
    buttons = []
    for rep_id, uid, uname, fname, msg, status, created in reports:
        icon   = "🔴" if status == "open" else "✅" if status == "replied" else "⚫"
        who    = f"@{uname}" if uname else f"#{uid}"
        dt     = datetime.fromisoformat(created).strftime("%d/%m %H:%M")
        preview = msg[:40].replace("\n", " ") + ("…" if len(msg) > 40 else "")
        text  += f"{icon} *#{rep_id}* {who} — {dt}\n   _{preview}_\n\n"
        row_btns = [{"text": f"✉️ #{rep_id}", "callback_data": f"bug_reply_{rep_id}"},
                    {"text": f"✅ Close",      "callback_data": f"bug_close_{rep_id}"}]
        buttons.append(row_btns)

    buttons.append([{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}])
    kb = {"inline_keyboard": buttons}
    if message_id: edit_message(chat_id, message_id, text, kb)
    else:          send_message(chat_id, text, kb)
def create_premium_invoice(chat_id, user_id, plan, duration_days, cost_stars):
    try:
        payload = f"premium_{plan}_{user_id}_{int(datetime.now().timestamp())}"
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO pending_premium_purchases 
            (user_id, chat_id, plan, duration_days, cost_stars, cost_coins, payload, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, chat_id, plan, duration_days, cost_stars, 
             cost_stars * STARS_PER_COIN, payload, datetime.now().isoformat(), 'pending'))
        conn.commit()
        conn.close()
        
        title = f"Premium {plan.capitalize()} Subscription"
        description = f"Get premium status for {duration_days} days"
        
        url = f"{TELEGRAM_API}/sendInvoice"
        prices = [{"label": title, "amount": cost_stars}]
        
        invoice_data = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "currency": "XTR",
            "prices": prices,
            "need_name": False,
            "need_phone_number": False,
            "need_email": False,
            "need_shipping_address": False,
            "is_flexible": False
        }
        
        data_bytes = json.dumps(invoice_data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('ok'):
                return True, None
            else:
                return False, result.get('description', 'Unknown error')
    except Exception as e:
        return False, str(e)

def show_premium_menu(chat_id, user_id, message_id=None):
    is_premium = is_user_premium(user_id)
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM deployments WHERE user_id = ? AND status = 'paused'", (user_id,))
    paused_count = c.fetchone()[0] or 0
    conn.close()
    
    if is_premium:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT premium_expires, premium_plan FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        
        expires = datetime.fromisoformat(row[0]) if row and row[0] else None
        plan = row[1] if row else "monthly"
        
        if expires:
            days_left = (expires - datetime.now()).days
            text = (
                f"*⭐ PREMIUM MEMBER*\n\n"
                f"✅ Plan: `{plan.upper()}`\n"
                f"📅 Expires: `{expires.strftime('%Y-%m-%d')}`\n"
                f"⏰ Days left: `{days_left}`\n"
                f"⏸️ Paused: `{paused_count}`\n\n"
                f"✨ Benefits active!\n\n"
                f"💰 Monthly/Yearly deployments are *FREE* for you!"
            )
            keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]]}
        else:
            text = "❌ Premium expired. Renew now!"
            keyboard = {"inline_keyboard": [[{"text": "💰 Renew Premium", "callback_data": "subscribe_premium"}]]}
    else:
        text = (
            f"*⭐ PREMIUM SUBSCRIPTION*\n\n"
            f"✨ *Benefits:*\n"
            f"• ✅ Unlimited free deployments (24h)\n"
            f"• ✅ FREE Monthly/Yearly deployments\n"
            f"• ✅ Priority support\n"
            f"• ✅ Auto-resume paused bots\n\n"
            f"⏸️ Paused: `{paused_count}`\n\n"
            f"💰 *Pricing:*\n"
            f"📅 Monthly: `{PRICE_MONTHLY_STARS}⭐` / `{PRICE_MONTHLY_COINS}🪙`\n"
            f"🌟 Yearly: `{PRICE_YEARLY_STARS}⭐` / `{PRICE_YEARLY_COINS}🪙`\n\n"
            f"Choose payment method:"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": f"⭐ Monthly ({PRICE_MONTHLY_STARS}⭐)", "callback_data": "premium_monthly_stars"},
                 {"text": f"🪙 Monthly ({PRICE_MONTHLY_COINS}🪙)", "callback_data": "premium_monthly_coins"}],
                [{"text": f"⭐ Yearly ({PRICE_YEARLY_STARS}⭐)", "callback_data": "premium_yearly_stars"},
                 {"text": f"🪙 Yearly ({PRICE_YEARLY_COINS}🪙)", "callback_data": "premium_yearly_coins"}],
                [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
            ]
        }
    
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def purchase_premium_stars(chat_id, user_id, plan, duration_days, cost_stars):
    success, error = create_premium_invoice(chat_id, user_id, plan, duration_days, cost_stars)
    if success:
        send_message(chat_id, f"⭐ *Stars Payment Required*\n\nPlan: {plan.upper()}\nCost: {cost_stars}⭐\n\nPlease complete the payment using the invoice above.\n\nYour premium will activate automatically after payment.")
    else:
        send_message(chat_id, f"❌ Failed to create invoice: {error}")

def purchase_premium_coins(chat_id, user_id, plan, duration_days, cost_coins):
    balances = get_user_balances(user_id)
    
    if balances['coins'] >= cost_coins:
        update_user_coins(user_id, -cost_coins, "premium_subscription", f"plan_{plan}")
        success, resumed = activate_premium(user_id, plan, cost_coins // STARS_PER_COIN, cost_coins, duration_days)
        
        if success:
            resume_msg = f"\n\n✅ Resumed {resumed} paused deployment(s)!" if resumed > 0 else ""
            send_message(chat_id,
                f"✅ *PREMIUM ACTIVATED!*{resume_msg}\n\n"
                f"Plan: `{plan.upper()}`\n"
                f"Duration: `{duration_days}` days\n"
                f"Paid: `{cost_coins}🪙`\n\n"
                f"Thank you! 🙏",
                {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})
        else:
            send_message(chat_id, "❌ Failed to activate premium.")
    else:
        send_message(chat_id,
            f"❌ *INSUFFICIENT COINS*\n\nRequired: `{cost_coins}🪙`\nYour balance: `{balances['coins']}🪙`\n\nUse redeem codes or pay with Stars!",
            {"inline_keyboard": [[{"text": "⭐ Pay with Stars", "callback_data": f"premium_{plan}_stars"},
                                  {"text": "🎫 Redeem Code", "callback_data": "redeem_code"}]]})

# ========== PERMANENT USER FILE STORAGE ==========
def save_user_file(user_id, temp_file_path, original_filename):
    user_dir = USER_FILES_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved_filename = f"{timestamp}_{original_filename}"
    saved_path = user_dir / saved_filename
    
    shutil.copy2(temp_file_path, saved_path)
    
    latest_path = user_dir / "latest.py"
    shutil.copy2(temp_file_path, latest_path)
    
    return saved_path, saved_filename

def get_user_files(user_id):
    user_dir = USER_FILES_DIR / str(user_id)
    if not user_dir.exists():
        return []
    
    files = []
    for file_path in user_dir.glob("*.py"):
        stat = file_path.stat()
        files.append({
            'filename': file_path.name,
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime),
            'path': str(file_path)
        })
    
    files.sort(key=lambda x: x['modified'], reverse=True)
    return files

def delete_user_file(user_id, filename):
    user_dir = USER_FILES_DIR / str(user_id)
    file_path = user_dir / filename
    if file_path.exists():
        file_path.unlink()
        return True
    return False

# ========== UNIVERSAL DEPENDENCY INSTALLER ==========
class UniversalDependencyInstaller:
    """Supports ALL Python dependency types: PyPI, Git, Mercurial, Subversion, local, wheel, egg, etc."""
    
    DEPENDENCY_PATTERNS = {
        r'^([a-zA-Z0-9_\-\.]+)$': 'pypi',
        r'^([a-zA-Z0-9_\-\.]+)[=<>~!]+': 'pypi',
        r'^git\+https?://': 'git',
        r'^git\+ssh://': 'git',
        r'^git@': 'git',
        r'\.git(@|#|$|/| )': 'git',
        r'^hg\+https?://': 'mercurial',
        r'^hg\+ssh://': 'mercurial',
        r'^svn\+https?://': 'subversion',
        r'^svn\+ssh://': 'subversion',
        r'^\./': 'local',
        r'^\.\./': 'local',
        r'^/': 'local',
        r'^[A-Za-z]:\\': 'local',
        r'\.whl$': 'wheel',
        r'\.egg$': 'egg',
        r'\.(tar\.gz|tgz|tar\.bz2|zip)$': 'archive',
    }
    
    @classmethod
    def detect_dependency_type(cls, dependency: str) -> str:
        for pattern, dep_type in cls.DEPENDENCY_PATTERNS.items():
            if re.search(pattern, dependency, re.IGNORECASE):
                return dep_type
        return 'pypi'
    
    @classmethod
    def install_dependency(cls, dependency: str, update_logs: callable, retries: int = 3) -> tuple:
        dep_type = cls.detect_dependency_type(dependency)
        update_logs(f"   📦 Type: {dep_type.upper()} - {dependency[:60]}...")
        
        for attempt in range(retries):
            try:
                if dep_type == 'git':
                    return cls._install_git_dependency(dependency, update_logs)
                elif dep_type in ['mercurial', 'subversion']:
                    return cls._install_vcs_dependency(dependency, update_logs)
                elif dep_type in ['wheel', 'egg', 'archive', 'local']:
                    return cls._install_file_dependency(dependency, update_logs)
                else:
                    return cls._install_pypi_dependency(dependency, update_logs, attempt)
            except subprocess.TimeoutExpired:
                update_logs(f"   ⏰ Attempt {attempt + 1} timeout, retrying...")
                sleep(5)
            except Exception as e:
                update_logs(f"   ⚠️ Attempt {attempt + 1} failed: {str(e)[:80]}")
                if attempt < retries - 1:
                    sleep(3)
                else:
                    return False, str(e)
        return False, "Max retries exceeded"
    
    @classmethod
    def _install_pypi_dependency(cls, dependency: str, update_logs: callable, attempt: int) -> tuple:
        match = re.match(r'^([a-zA-Z0-9_\-\.]+)([=<>~!].*)?$', dependency)
        if match:
            package = match.group(1)
            version_spec = match.group(2) or ""
            full_package = package + version_spec
        else:
            full_package = dependency
        
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--cache-dir", str(PIP_CACHE_DIR), 
               "--timeout", str(PIP_INSTALL_TIMEOUT), full_package]
        
        # Only add --pre when the version spec explicitly requests a pre-release (e.g. ==1.0a1, ==2.0b3, ==1.0rc1)
        if re.search(r'[=<>!]=?\s*\d[\d.]*\s*(a\d|b\d|rc\d|\.dev|\.post)', dependency.lower()):
            cmd.insert(4, "--pre")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        
        if result.returncode == 0:
            version_match = re.search(r'Successfully installed .*? ([\d\.]+)', result.stdout)
            version = version_match.group(1) if version_match else "unknown"
            return True, version
        else:
            error = result.stderr[:200] if result.stderr else "Unknown error"
            return False, error
    
    @classmethod
    def _install_git_dependency(cls, dependency: str, update_logs: callable) -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", dependency]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        return result.returncode == 0, result.stderr[:200] if result.stderr else ""
    
    @classmethod
    def _install_vcs_dependency(cls, dependency: str, update_logs: callable) -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", dependency]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        return result.returncode == 0, result.stderr[:200] if result.stderr else ""
    
    @classmethod
    def _install_file_dependency(cls, dependency: str, update_logs: callable) -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", dependency]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
        return result.returncode == 0, result.stderr[:200] if result.stderr else ""

    IMPORT_MAPPING = {
        'flask': 'flask', 'django': 'django', 'fastapi': 'fastapi',
        'aiohttp': 'aiohttp', 'tornado': 'tornado', 'sanic': 'sanic',
        # Pinned floor, not just a bare name: versions before 21.7 have a
        # confirmed __slots__/__dict__ AttributeError on modern Python
        # (github.com/python-telegram-bot/python-telegram-bot/issues/4127).
        # Auto-detection has no way to know which version pip would
        # otherwise pick, so pin a known-safe minimum explicitly.
        'telegram': 'python-telegram-bot>=22.0', 'aiogram': 'aiogram',
        'pyrogram': 'pyrogram', 'telethon': 'telethon',
        'discord': 'discord.py', 'nextcord': 'nextcord',
        'sqlalchemy': 'sqlalchemy', 'psycopg2': 'psycopg2-binary',
        'pymysql': 'pymysql', 'pymongo': 'pymongo', 'redis': 'redis',
        'requests': 'requests', 'httpx': 'httpx',
        'numpy': 'numpy', 'pandas': 'pandas', 'scipy': 'scipy',
        'PIL': 'Pillow', 'cv2': 'opencv-python',
        'bs4': 'beautifulsoup4', 'selenium': 'selenium',
        'dotenv': 'python-dotenv', 'click': 'click',
        'cryptography': 'cryptography', 'jwt': 'pyjwt',
        'yaml': 'pyyaml', 'toml': 'toml',
        'boto3': 'boto3', 'psutil': 'psutil', 'loguru': 'loguru',
        'rich': 'rich', 'tqdm': 'tqdm', 'uvicorn': 'uvicorn',
        'gunicorn': 'gunicorn', 'celery': 'celery',
    }

    @classmethod
    def scan_imports(cls, code_content: str, update_logs: callable) -> list:
        """Automatically detect required dependencies by scanning code"""
        detected = set()
        lines = code_content.split('\n')
        
        for line in lines:
            line = line.strip()
            match = re.match(r'^(?:from|import)\s+([a-zA-Z0-9_\.]+)', line)
            if match:
                module = match.group(1).split('.')[0]
                if module in cls.IMPORT_MAPPING:
                    package = cls.IMPORT_MAPPING[module]
                    if package and package not in detected:
                        detected.add(package)
                        update_logs(f"   🔍 Detected: {module} → {package}")
                elif module not in ['os', 'sys', 'time', 'datetime', 'json', 're', 'math', 'random', 
                                     'string', 'collections', 'itertools', 'functools', 'typing', 'pathlib',
                                     'tempfile', 'subprocess', 'threading', 'multiprocessing', 'socket',
                                     'ssl', 'hashlib', 'base64', 'zipfile', 'tarfile', 'shutil', 'glob',
                                     'io', 'abc', 'copy', 'enum', 'struct', 'queue', 'weakref',
                                     'contextlib', 'dataclasses', 'inspect', 'logging', 'warnings',
                                     'argparse', 'configparser', 'csv', 'html', 'http', 'urllib',
                                     'uuid', 'decimal', 'fractions', 'statistics', 'operator',
                                     'concurrent', 'asyncio', 'signal', 'platform', 'traceback',
                                     'pprint', 'textwrap', 'binascii', 'hmac', 'secrets']:
                    # Only add if it looks like a real installable package (not a relative import or internal module)
                    if module and not module.startswith('_') and len(module) > 1:
                        guessed = module.replace('_', '-')
                        if module == 'bs4':
                            guessed = 'beautifulsoup4'
                        elif module == 'cv2':
                            guessed = 'opencv-python'
                        elif module == 'PIL':
                            guessed = 'Pillow'
                        elif module == 'sklearn':
                            guessed = 'scikit-learn'
                        elif module == 'wx':
                            guessed = 'wxPython'
                        # Only guess if it's plausibly a PyPI package name
                        if guessed and re.match(r'^[a-zA-Z][a-zA-Z0-9\-\.]+$', guessed):
                            detected.add(guessed)
                            update_logs(f"   🔍 Guessed: {module} → {guessed}")
        
        return list(detected)

    # Known cases where the PyPI package name doesn't match what people
    # naturally write for `import X` — most critically "telegram", which is
    # a real but completely unrelated PyPI package. Installing it alongside
    # python-telegram-bot corrupts the telegram/ module (mixed files from
    # two different packages) and produces cryptic __slots__/__dict__
    # AttributeErrors deep inside PTB's Updater — a well-known trap for
    # anyone who writes "telegram" in requirements.txt expecting PTB.
    _WRONG_PACKAGE_FIXES = {
        'telegram': 'python-telegram-bot>=22.0',
        'discord': 'discord.py',
        'cv2': 'opencv-python',
        'bs4': 'beautifulsoup4',
        'PIL': 'Pillow',
        'sklearn': 'scikit-learn',
    }

    @classmethod
    def scan_requirements_file(cls, content: str, update_logs: callable) -> list:
        requirements = []
        for line in content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('-r'):
                if ';' in line:
                    line = line.split(';')[0].strip()

                # Correct known wrong package names, preserving any version pin
                _base = re.split(r'[=<>!~\[]', line, 1)[0].strip()
                if _base in cls._WRONG_PACKAGE_FIXES:
                    _correct = cls._WRONG_PACKAGE_FIXES[_base]
                    update_logs(f"   ⚠️ '{_base}' is the wrong package for this import — "
                                f"using '{_correct}' instead")
                    line = _correct

                requirements.append(line)
                update_logs(f"   📄 From requirements: {line[:60]}")
        return requirements

# ==================== NODE.JS DEPENDENCY DETECTION ====================
# Mirrors UniversalDependencyInstaller.scan_imports, but for JavaScript's
# require()/import syntax instead of Python's import/from — the upload
# deploy path previously had zero Node.js dependency support at all: no way
# to submit a package.json, no detection of what a .js file actually needs,
# and nothing ever wrote package.json to disk for npm to find.
_JS_BUILTIN_MODULES = {
    'fs', 'path', 'http', 'https', 'os', 'crypto', 'util', 'events',
    'stream', 'url', 'querystring', 'child_process', 'assert', 'buffer',
    'net', 'dns', 'tls', 'zlib', 'readline', 'process', 'timers',
    'cluster', 'worker_threads', 'perf_hooks', 'v8', 'vm', 'module', 'dgram',
}

def scan_js_requires(code_content: str) -> list:
    """Detect npm packages actually referenced by a Node.js script."""
    found = set()
    # require('pkg') / require("pkg")
    for m in re.finditer(r'require\(\s*[\'"]([^\'"./][^\'"]*)[\'"]\s*\)', code_content):
        found.add(m.group(1))
    # ES module: import ... from 'pkg'
    for m in re.finditer(r'''\bfrom\s+['"]([^'"./][^'"]*)['"]''', code_content):
        found.add(m.group(1))
    # import 'pkg'; (side-effect only import)
    for m in re.finditer(r'''^\s*import\s+['"]([^'"./][^'"]*)['"]''', code_content, re.M):
        found.add(m.group(1))

    packages = set()
    for name in found:
        # Scoped packages (@org/pkg) or subpaths (pkg/sub) — keep only the
        # installable package root, not the specific file within it.
        if name.startswith('@'):
            parts = name.split('/')
            pkg = '/'.join(parts[:2]) if len(parts) >= 2 else name
        else:
            pkg = name.split('/')[0]
        if pkg in _JS_BUILTIN_MODULES or pkg.startswith('node:'):
            continue
        packages.add(pkg)
    return sorted(packages)


def build_package_json(deploy_folder: Path, packages: list, main_file_name: str, update_logs=None) -> Path:
    """Write a minimal package.json for auto-detected dependencies."""
    pkg_json = {
        "name": "hosted-bot",
        "version": "1.0.0",
        "private": True,
        "main": main_file_name,
        "dependencies": {pkg: "*" for pkg in packages}
    }
    path = deploy_folder / "package.json"
    path.write_text(json.dumps(pkg_json, indent=2))
    if update_logs:
        update_logs(f"📦 Generated package.json with {len(packages)} package(s): {', '.join(packages)}")
    return path


# ==================== VPS DEPENDENCY ENGINE ====================
# Supports every package manager a real hosting server would handle.

import shutil as _shutil

# ── helpers ──────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, timeout=300, env_extra=None):
    """Run a shell command, return (ok, stdout+stderr text)."""
    import os as _os
    env = _os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=cwd, env=env)
        out = (r.stdout or '') + (r.stderr or '')
        return r.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except Exception as e:
        return False, str(e)

def _cmd_exists(name):
    return _shutil.which(name) is not None

def _log(update_logs, msg):
    if update_logs:
        update_logs(msg)
    else:
        print(msg)


def _normalize_pkg_name(name):
    return re.sub(r'[-_.]+', '-', name).strip().lower()


def _clean_stale_package(packages_dir, pkg_spec):
    """
    Remove any existing installed files for a package before (re)installing it.

    `pip install --target` does not properly clean up on upgrade the way a
    normal site-packages install does — if a package's file layout changes
    between versions, files removed in the new version can be left behind
    from the old one, producing a package with files mixed across two
    versions. This is a well-known, well-documented cause of cryptic
    AttributeErrors deep inside libraries with __slots__-based classes (e.g.
    python-telegram-bot's Updater) that have nothing to do with the user's
    own code. Best-effort and silent — never blocks the actual install.
    """
    try:
        packages_dir = Path(packages_dir)
        if not packages_dir.exists():
            return
        base = re.split(r'[=<>!~\[\s]', pkg_spec.strip(), 1)[0].strip()
        if not base or base.startswith(('git+', 'hg+', 'svn+', 'http://', 'https://', '.', '/')):
            return  # not a simple named package spec — nothing to clean by name
        target_norm = _normalize_pkg_name(base)

        for item in list(packages_dir.iterdir()):
            name = item.name
            if not (name.endswith('.dist-info') or name.endswith('.egg-info')):
                continue
            dist_base = re.sub(r'-[^-]+\.(dist-info|egg-info)$', '', name)
            if _normalize_pkg_name(dist_base) != target_norm:
                continue

            record = item / 'RECORD'
            if record.exists():
                try:
                    for line in record.read_text(errors='ignore').splitlines():
                        rel = line.split(',')[0].strip()
                        if not rel or rel.startswith('..'):
                            continue
                        f = packages_dir / rel
                        try:
                            if f.is_file() or f.is_symlink():
                                f.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass
            shutil.rmtree(item, ignore_errors=True)
    except Exception:
        pass


def _pip_base(packages_dir=None):
    cmd = [sys.executable, '-m', 'pip', 'install',
           '--quiet', '--no-warn-script-location',
           '--disable-pip-version-check', '--no-color']
    if packages_dir:
        cmd += ['--target', str(packages_dir)]
    return cmd


# ── per-package-manager installers ───────────────────────────────────────────

def _install_pip(packages_dir, deploy_folder, update_logs):
    """Install Python packages from every requirements variant."""
    installed = 0

    # Upgrade pip first (once, silently)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--upgrade', 'pip',
                    '--disable-pip-version-check'], capture_output=True, timeout=60)

    # All requirements file variants, in priority order
    req_variants = [
        'requirements.txt', 'requirements-dev.txt', 'requirements-prod.txt',
        'requirements_dev.txt', 'requirements_prod.txt',
        'requirements/base.txt', 'requirements/main.txt',
        'requirements/prod.txt',  'requirements/dev.txt',
    ]

    for fname in req_variants:
        rp = deploy_folder / fname
        if not rp.exists():
            continue
        _log(update_logs, f"   📄 {fname}")
        raw = rp.read_text(errors='ignore').strip()
        pkgs = [ln.split(';')[0].strip() for ln in raw.splitlines()
                if ln.strip() and not ln.startswith(('#', '-r ', '-c ', '--'))]
        if not pkgs:
            continue

        _log(update_logs, f"   📦 {len(pkgs)} package(s) from {fname}")
        for i, pkg in enumerate(pkgs):
            _log(update_logs, f"   ⬇️  [{i+1}/{len(pkgs)}] {pkg[:70]}")
            _clean_stale_package(packages_dir, pkg)
            # Try with --upgrade first, then a clean forced reinstall on failure
            # (never trust partially-written files from a failed first attempt)
            ok, out = _run(_pip_base(packages_dir) + ['--upgrade', pkg], timeout=180)
            if not ok:
                _clean_stale_package(packages_dir, pkg)
                ok, out = _run(_pip_base(packages_dir) + ['--upgrade', '--force-reinstall', pkg], timeout=180)
            if ok:
                installed += 1
            else:
                last = out.strip().split('\n')[-1][:100]
                _log(update_logs, f"   ⚠️  {pkg[:50]} → {last}")
        _log(update_logs, f"   ✅ {fname} done")

    # pyproject.toml
    pp = deploy_folder / 'pyproject.toml'
    if pp.exists():
        _log(update_logs, "   📄 pyproject.toml")
        # Try pip install . first (works for PEP 517 projects)
        ok, out = _run(_pip_base(packages_dir) + ['.'], cwd=str(deploy_folder), timeout=300)
        if ok:
            _log(update_logs, "   ✅ pyproject.toml (pip install .)")
            installed += 1
        else:
            # Manual parse as fallback
            text = pp.read_text(errors='ignore')
            deps = re.findall(r'"([A-Za-z0-9_\-]+)(?:[>=<!\[^~][^"]*)?"\s*,', text)
            deps += re.findall(r"^([A-Za-z0-9_\-]+)\s*=\s*['\"\^~]", text, re.M)
            skip = {'python','pip','setuptools','wheel','flit','hatchling','poetry'}
            deps = list(dict.fromkeys(d for d in deps if d.lower() not in skip))
            if deps:
                _log(update_logs, f"   📦 {len(deps)} deps parsed from pyproject.toml")
                for pkg in deps:
                    ok2, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
                    if ok2:
                        installed += 1

    # Pipfile
    pf = deploy_folder / 'Pipfile'
    if pf.exists():
        _log(update_logs, "   📄 Pipfile")
        text = pf.read_text(errors='ignore')
        in_pkgs = False
        pkgs = []
        for line in text.splitlines():
            if line.strip() in ('[packages]', '[dev-packages]'):
                in_pkgs = True
            elif line.startswith('['):
                in_pkgs = False
            elif in_pkgs:
                m = re.match(r'^([A-Za-z0-9_\-]+)\s*=', line)
                if m and m.group(1) not in ('python',):
                    pkgs.append(m.group(1))
        if pkgs:
            _log(update_logs, f"   📦 {len(pkgs)} packages from Pipfile")
            for pkg in pkgs:
                ok, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
                if ok:
                    installed += 1

    # conda environment.yml
    for env_file in ['environment.yml', 'environment.yaml', 'conda.yml']:
        ef = deploy_folder / env_file
        if not ef.exists():
            continue
        if _cmd_exists('conda'):
            _log(update_logs, f"   📄 {env_file} (conda)")
            ok, out = _run(['conda', 'env', 'update', '--file', str(ef),
                            '--prune', '-q'], timeout=300)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} conda env update: {out[-100:]}")
            installed += int(ok)
        else:
            # Parse pip dependencies from environment.yml and install them
            _log(update_logs, f"   📄 {env_file} (conda not found — installing pip deps)")
            text = ef.read_text(errors='ignore')
            in_pip = False
            pip_pkgs = []
            for line in text.splitlines():
                if '- pip:' in line:
                    in_pip = True
                    continue
                if line.startswith('  - ') and in_pip:
                    pkg = line.strip().lstrip('- ').strip()
                    if pkg and not pkg.startswith('#'):
                        pip_pkgs.append(pkg)
                elif not line.startswith(' ') and line.strip():
                    in_pip = False
            for pkg in pip_pkgs:
                ok2, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
                if ok2:
                    installed += 1

    # setup.py / setup.cfg
    for sf in ['setup.py', 'setup.cfg']:
        if (deploy_folder / sf).exists():
            _log(update_logs, f"   📄 {sf}")
            ok, _ = _run(_pip_base(packages_dir) + ['-e', '.'],
                         cwd=str(deploy_folder), timeout=300)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} {sf}")
            installed += int(ok)
            break

    return installed


def _install_npm(deploy_folder, update_logs):
    """Install Node.js dependencies from package.json using npm/yarn/pnpm/bun."""
    pkg_json = deploy_folder / 'package.json'
    if not pkg_json.exists():
        return 0

    # Detect which lock file / manager to use
    if (deploy_folder / 'bun.lockb').exists() and _cmd_exists('bun'):
        mgr = 'bun'
        cmd = ['bun', 'install', '--frozen-lockfile']
    elif (deploy_folder / 'pnpm-lock.yaml').exists() and _cmd_exists('pnpm'):
        mgr = 'pnpm'
        cmd = ['pnpm', 'install', '--frozen-lockfile', '--prefer-offline']
    elif (deploy_folder / 'yarn.lock').exists() and _cmd_exists('yarn'):
        mgr = 'yarn'
        cmd = ['yarn', 'install', '--frozen-lockfile', '--non-interactive']
    elif _cmd_exists('npm'):
        mgr = 'npm'
        # Use ci if lock file exists, else install
        lockfile = deploy_folder / 'package-lock.json'
        cmd = ['npm', 'ci'] if lockfile.exists() else ['npm', 'install', '--no-audit', '--no-fund']
    else:
        _log(update_logs, "   ⚠️ No Node.js package manager found (npm/yarn/pnpm/bun)")
        return 0

    _log(update_logs, f"   📦 npm install via {mgr}...")
    ok, out = _run(cmd, cwd=str(deploy_folder), timeout=300)
    if ok:
        _log(update_logs, f"   ✅ {mgr} install complete")
    else:
        _log(update_logs, f"   ⚠️ {mgr} install failed — {out[-200:]}")
    return int(ok)


def _install_cargo(deploy_folder, update_logs):
    """Build Rust project from Cargo.toml."""
    if not (deploy_folder / 'Cargo.toml').exists():
        return 0
    if not _cmd_exists('cargo'):
        _log(update_logs, "   ⚠️ Cargo not installed — skipping Rust build")
        return 0
    _log(update_logs, "   🦀 Building Rust project (cargo build --release)...")
    ok, out = _run(['cargo', 'build', '--release', '--quiet'],
                   cwd=str(deploy_folder), timeout=600)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} cargo build: {out[-150:]}")
    return int(ok)


def _install_go(deploy_folder, update_logs):
    """Download Go modules and build."""
    if not (deploy_folder / 'go.mod').exists():
        return 0
    if not _cmd_exists('go'):
        _log(update_logs, "   ⚠️ Go not installed — skipping Go modules")
        return 0
    _log(update_logs, "   🐹 Downloading Go modules...")
    ok, out = _run(['go', 'mod', 'download'], cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} go mod download: {out[-100:]}")
    if ok:
        ok2, _ = _run(['go', 'build', './...'], cwd=str(deploy_folder), timeout=300)
        _log(update_logs, f"   {'✅' if ok2 else '⚠️'} go build")
    return int(ok)


def _install_gem(deploy_folder, update_logs):
    """Install Ruby gems from Gemfile."""
    if not (deploy_folder / 'Gemfile').exists():
        return 0
    if not _cmd_exists('bundle'):
        if _cmd_exists('gem'):
            _log(update_logs, "   💎 Installing bundler...")
            _run(['gem', 'install', 'bundler', '--no-document'], timeout=120)
        else:
            _log(update_logs, "   ⚠️ Ruby/gem not installed — skipping Gemfile")
            return 0
    _log(update_logs, "   💎 Bundle install (Ruby gems)...")
    ok, out = _run(['bundle', 'install', '--jobs', '4', '--retry', '3'],
                   cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} bundle install: {out[-120:]}")
    return int(ok)


def _install_composer(deploy_folder, update_logs):
    """Install PHP packages from composer.json."""
    if not (deploy_folder / 'composer.json').exists():
        return 0
    composer = _cmd_exists('composer') and 'composer' or _cmd_exists('composer.phar') and 'composer.phar'
    if not composer:
        _log(update_logs, "   ⚠️ Composer not installed — skipping PHP deps")
        return 0
    _log(update_logs, "   🐘 Composer install (PHP packages)...")
    ok, out = _run([composer, 'install', '--no-dev', '--optimize-autoloader', '--no-interaction'],
                   cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} composer install: {out[-120:]}")
    return int(ok)


def _install_gradle(deploy_folder, update_logs):
    """Build Java project with Gradle or Maven."""
    # Gradle
    if (deploy_folder / 'build.gradle').exists() or (deploy_folder / 'build.gradle.kts').exists():
        gw = str(deploy_folder / 'gradlew')
        gradle = gw if Path(gw).exists() else ('gradle' if _cmd_exists('gradle') else None)
        if gradle:
            _log(update_logs, "   ☕ Gradle build (Java)...")
            ok, out = _run([gradle, 'dependencies', '--no-daemon', '-q'],
                           cwd=str(deploy_folder), timeout=600)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} gradle deps: {out[-100:]}")
            return int(ok)
        else:
            _log(update_logs, "   ⚠️ Gradle not installed — skipping")

    # Maven
    if (deploy_folder / 'pom.xml').exists():
        mvn = 'mvn' if _cmd_exists('mvn') else ('mvnw' if (deploy_folder / 'mvnw').exists() else None)
        if mvn:
            _log(update_logs, "   ☕ Maven dependency:resolve (Java)...")
            ok, out = _run([mvn, 'dependency:resolve', '-q', '--no-transfer-progress'],
                           cwd=str(deploy_folder), timeout=600)
            _log(update_logs, f"   {'✅' if ok else '⚠️'} mvn resolve: {out[-100:]}")
            return int(ok)
        else:
            _log(update_logs, "   ⚠️ Maven not installed — skipping")
    return 0


def _install_mix(deploy_folder, update_logs):
    """Install Elixir dependencies."""
    if not (deploy_folder / 'mix.exs').exists():
        return 0
    if not _cmd_exists('mix'):
        _log(update_logs, "   ⚠️ Elixir/mix not installed — skipping")
        return 0
    _log(update_logs, "   💜 Mix deps.get (Elixir)...")
    ok, out = _run(['mix', 'deps.get'], cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} mix deps.get: {out[-100:]}")
    return int(ok)


def _install_pub(deploy_folder, update_logs):
    """Install Dart/Flutter dependencies."""
    pubspec = deploy_folder / 'pubspec.yaml'
    if not pubspec.exists():
        return 0
    runner = 'flutter' if _cmd_exists('flutter') else ('dart' if _cmd_exists('dart') else None)
    if not runner:
        _log(update_logs, "   ⚠️ Dart/Flutter not installed — skipping pubspec.yaml")
        return 0
    cmd = [runner, 'pub', 'get'] if runner == 'dart' else [runner, 'pub', 'get']
    _log(update_logs, f"   🎯 {runner} pub get (Dart/Flutter)...")
    ok, out = _run(cmd, cwd=str(deploy_folder), timeout=300)
    _log(update_logs, f"   {'✅' if ok else '⚠️'} pub get: {out[-100:]}")
    return int(ok)


def _auto_detect_and_install_pip(code_file, packages_dir, update_logs):
    """
    Scan a Python/JS file for imports and auto-install any missing packages.
    Only installs what isn't already importable — avoids redundant reinstalls.
    """
    IMPORT_MAP = {
        'telebot':     'pyTelegramBotAPI',
        'telegram':    'python-telegram-bot',
        'aiogram':     'aiogram',
        'pyrogram':    'pyrogram',
        'telethon':    'telethon',
        'discord':     'discord.py',
        'nextcord':    'nextcord',
        'disnake':     'disnake',
        'flask':       'flask',
        'fastapi':     'fastapi',
        'uvicorn':     'uvicorn',
        'aiohttp':     'aiohttp',
        'requests':    'requests',
        'httpx':       'httpx',
        'dotenv':      'python-dotenv',
        'sqlalchemy':  'SQLAlchemy',
        'pymongo':     'pymongo',
        'motor':       'motor',
        'redis':       'redis',
        'celery':      'celery',
        'bs4':         'beautifulsoup4',
        'PIL':         'Pillow',
        'cv2':         'opencv-python',
        'sklearn':     'scikit-learn',
        'tweepy':      'tweepy',
        'slack_sdk':   'slack-sdk',
        'slack_bolt':  'slack-bolt',
        'linebot':     'line-bot-sdk',
        'yaml':        'pyyaml',
        'toml':        'toml',
        'boto3':       'boto3',
        'psutil':      'psutil',
        'loguru':      'loguru',
        'rich':        'rich',
        'pydantic':    'pydantic',
        'cryptography':'cryptography',
        'jwt':         'pyjwt',
        'aiosqlite':   'aiosqlite',
        'tortoise':    'tortoise-orm',
        'pywa':        'pywa',
        'viberbot':    'viberbot',
        'nio':         'matrix-nio',
        'pytz':        'pytz',
        'arrow':       'arrow',
        'pendulum':    'pendulum',
        'click':       'click',
        'typer':       'typer',
        'tqdm':        'tqdm',
        'matplotlib':  'matplotlib',
        'numpy':       'numpy',
        'pandas':      'pandas',
        'scipy':       'scipy',
        'openai':      'openai',
        'anthropic':   'anthropic',
        'transformers':'transformers',
        'torch':       'torch',
        'tensorflow':  'tensorflow',
        'paramiko':    'paramiko',
        'fabric':      'fabric',
        'invoke':      'invoke',
        'pyserial':    'pyserial',
        'serial':      'pyserial',
    }

    try:
        code = Path(code_file).read_text(errors='ignore') if isinstance(code_file, (str, Path)) else ''
    except Exception:
        code = ''

    if not code:
        return 0

    # Extract top-level module names
    imported = set()
    for m in re.findall(r'^import\s+([\w]+)', code, re.M):
        imported.add(m)
    for m in re.findall(r'^from\s+([\w]+)', code, re.M):
        imported.add(m)

    to_install = []
    for mod in imported:
        pkg = IMPORT_MAP.get(mod)
        if pkg:
            # Check if already importable (skip install if yes)
            try:
                import importlib.util as _ilu
                if _ilu.find_spec(mod) is None:
                    to_install.append(pkg)
            except Exception:
                to_install.append(pkg)

    installed = 0
    if to_install:
        _log(update_logs, f"   🔍 Auto-detected {len(to_install)} missing package(s)")
        for pkg in to_install:
            _log(update_logs, f"   ⬇️  {pkg}")
            ok, _ = _run(_pip_base(packages_dir) + [pkg], timeout=180)
            if ok:
                installed += 1
                _log(update_logs, f"   ✅ {pkg}")
            else:
                _log(update_logs, f"   ⚠️ {pkg} failed")
    return installed


# ── main entry point ──────────────────────────────────────────────────────────

def install_all_dependencies(deploy_folder: Path, update_logs=None,
                              packages_dir=None, code_file=None) -> dict:
    """
    VPS-grade universal dependency installer.
    Scans the deploy folder for ALL known dependency manifests and installs them.
    Returns a summary dict: {manager: count_installed}
    """
    deploy_folder = Path(deploy_folder)
    if packages_dir:
        packages_dir = Path(packages_dir)
        packages_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    _log(update_logs, "═" * 50)
    _log(update_logs, "📦 VPS DEPENDENCY ENGINE — STARTING")
    _log(update_logs, "═" * 50)

    # ── Python ────────────────────────────────────────────────────────
    has_python = any([
        (deploy_folder / f).exists()
        for f in ['requirements.txt', 'pyproject.toml', 'Pipfile', 'setup.py',
                  'setup.cfg', 'environment.yml', 'environment.yaml']
    ])
    if has_python or code_file:
        _log(update_logs, "🐍 Python packages:")
        n = _install_pip(packages_dir, deploy_folder, update_logs)
        if code_file:
            n += _auto_detect_and_install_pip(code_file, packages_dir, update_logs)
        summary['pip'] = n
        _log(update_logs, f"   → {n} Python package(s) installed")

    # ── Node.js ───────────────────────────────────────────────────────
    if (deploy_folder / 'package.json').exists():
        _log(update_logs, "🟨 Node.js packages:")
        n = _install_npm(deploy_folder, update_logs)
        summary['npm'] = n

    # ── Rust ──────────────────────────────────────────────────────────
    if (deploy_folder / 'Cargo.toml').exists():
        _log(update_logs, "🦀 Rust (cargo):")
        n = _install_cargo(deploy_folder, update_logs)
        summary['cargo'] = n

    # ── Go ────────────────────────────────────────────────────────────
    if (deploy_folder / 'go.mod').exists():
        _log(update_logs, "🐹 Go modules:")
        n = _install_go(deploy_folder, update_logs)
        summary['go'] = n

    # ── Ruby ──────────────────────────────────────────────────────────
    if (deploy_folder / 'Gemfile').exists():
        _log(update_logs, "💎 Ruby gems:")
        n = _install_gem(deploy_folder, update_logs)
        summary['gem'] = n

    # ── PHP ───────────────────────────────────────────────────────────
    if (deploy_folder / 'composer.json').exists():
        _log(update_logs, "🐘 PHP (composer):")
        n = _install_composer(deploy_folder, update_logs)
        summary['composer'] = n

    # ── Java ──────────────────────────────────────────────────────────
    has_java = any((deploy_folder / f).exists()
                   for f in ['build.gradle', 'build.gradle.kts', 'pom.xml'])
    if has_java:
        _log(update_logs, "☕ Java (gradle/maven):")
        n = _install_gradle(deploy_folder, update_logs)
        summary['java'] = n

    # ── Elixir ───────────────────────────────────────────────────────
    if (deploy_folder / 'mix.exs').exists():
        _log(update_logs, "💜 Elixir (mix):")
        n = _install_mix(deploy_folder, update_logs)
        summary['mix'] = n

    # ── Dart / Flutter ────────────────────────────────────────────────
    if (deploy_folder / 'pubspec.yaml').exists():
        _log(update_logs, "🎯 Dart/Flutter (pub):")
        n = _install_pub(deploy_folder, update_logs)
        summary['pub'] = n

    total = sum(summary.values())
    _log(update_logs, "═" * 50)
    parts = ', '.join(f"{k}: {v}" for k, v in summary.items() if v)
    _log(update_logs, f"✅ DEPENDENCIES DONE — {total} item(s) installed ({parts or 'none detected'})")
    _log(update_logs, "═" * 50)
    return summary


# ========== ENHANCED DEPENDENCY INSTALLATION ==========
def install_dependencies_enhanced(reqs_file, update_logs, packages_dir=None):
    """
    VPS-grade installer.
    - Installs into packages_dir (per-deployment isolation) when provided
    - Falls back to system-wide install
    - Handles requirements.txt, pyproject.toml, Pipfile, setup.py/cfg
    - Upgrades pip first, retries individual failures
    """
    if not reqs_file.exists():
        update_logs("✅ No requirements file found")
        return True, []

    try:
        with open(reqs_file, 'r') as f:
            raw = f.read().strip()
        if not raw:
            update_logs("⚠️ requirements.txt is empty")
            return True, []

        packages = [p.strip() for p in raw.split('\n')
                    if p.strip() and not p.startswith('#') and not p.startswith('-')]
        update_logs(f"📦 {len(packages)} package(s) to install")

        # Upgrade pip silently first
        update_logs("🔄 Upgrading pip...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
                       capture_output=True, timeout=60)

        # Build base pip command
        def _pip_cmd(pkg_or_flag, extra_flags=None):
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
                   "--no-warn-script-location", "--quiet"]
            if packages_dir:
                cmd += ["--target", str(packages_dir)]
            if extra_flags:
                cmd += extra_flags
            cmd.append(pkg_or_flag)
            return cmd

        success_count = 0
        failed_packages = []

        for i, package in enumerate(packages):
            pct = int((i / len(packages)) * 100)
            update_logs(create_progress_bar(pct, 25))
            update_logs(f"   ⬇️  {package[:60]}")

            if packages_dir:
                _clean_stale_package(packages_dir, package)

            # Determine install type
            pkg_lower = package.lower()
            if pkg_lower.startswith('git+') or pkg_lower.startswith('hg+') or pkg_lower.startswith('svn+'):
                cmd = _pip_cmd(package)
            elif pkg_lower.endswith('.whl') or pkg_lower.endswith('.tar.gz'):
                cmd = _pip_cmd(package)
            elif '.git' in package or 'github.com' in package:
                cmd = _pip_cmd(f"git+{package}" if not package.startswith('git+') else package)
            else:
                cmd = _pip_cmd(package)

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode == 0:
                success_count += 1
                update_logs(f"   ✅ {package[:60]}")
            else:
                # Never trust whatever partial files the failed attempt above
                # may have left behind — clean before the forced retry, or a
                # package can end up with files mixed across two versions
                # (a well-known cause of cryptic __slots__ AttributeErrors
                # deep inside libraries like python-telegram-bot).
                if packages_dir:
                    _clean_stale_package(packages_dir, package)
                res2 = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", "--force-reinstall",
                     *(["--target", str(packages_dir)] if packages_dir else []),
                     package],
                    capture_output=True, text=True, timeout=180)
                if res2.returncode == 0:
                    success_count += 1
                    update_logs(f"   ✅ {package[:60]} (retry OK)")
                else:
                    failed_packages.append(package)
                    err_line = (res.stderr or res.stdout or "")[-200:].strip().split('\n')[-1]
                    update_logs(f"   ❌ {package[:50]}: {err_line[:80]}")

        update_logs(create_progress_bar(100, 25))
        if failed_packages:
            update_logs(f"⚠️ Failed ({len(failed_packages)}): {', '.join(p.split('==')[0][:20] for p in failed_packages[:5])}")
        update_logs(f"✅ {success_count}/{len(packages)} packages installed")

        return (success_count / len(packages)) >= 0.7 if packages else True, failed_packages

    except Exception as e:
        update_logs(f"❌ Installer error: {e}")
        return False, []


def install_from_repo_requirements(deploy_folder: Path, update_logs) -> list:
    """
    Scan a cloned repo directory for ALL supported requirement sources and
    install everything.  Returns a flat list of packages installed.
    """
    packages_dir = deploy_folder / 'packages'
    packages_dir.mkdir(exist_ok=True)

    installed = []

    # ── requirements.txt (and variants) ──────────────────────────────
    for fname in ['requirements.txt', 'requirements-dev.txt', 'requirements_prod.txt',
                  'requirements/base.txt', 'requirements/main.txt']:
        req_path = deploy_folder / fname
        if req_path.exists():
            update_logs(f"📋 Found {fname}")
            ok, failed = install_dependencies_enhanced(req_path, update_logs, packages_dir)
            installed.extend([l.strip() for l in req_path.read_text().splitlines()
                              if l.strip() and not l.startswith('#')])

    # ── pyproject.toml (PEP 517/518) ─────────────────────────────────
    pyproject = deploy_folder / 'pyproject.toml'
    if pyproject.exists():
        update_logs("📋 Found pyproject.toml — installing with pip...")
        res = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--quiet',
             '--no-warn-script-location', '--target', str(packages_dir), '.'],
            cwd=str(deploy_folder), capture_output=True, text=True, timeout=300)
        if res.returncode == 0:
            update_logs("✅ pyproject.toml dependencies installed")
        else:
            # Extract [tool.poetry.dependencies] or [project.dependencies] manually
            try:
                import re as _re
                text = pyproject.read_text()
                # PEP 621 style
                deps = _re.findall(r'^\s*"([a-zA-Z0-9_\-]+)[>=<!\[\]"]*"', text, _re.M)
                # Poetry style
                deps += _re.findall(r'^\s*([a-zA-Z0-9_\-]+)\s*=\s*["\^~]', text, _re.M)
                skip = {'python', 'pip', 'setuptools', 'wheel'}
                deps = [d for d in dict.fromkeys(deps) if d.lower() not in skip]
                if deps:
                    update_logs(f"📦 Parsed {len(deps)} deps from pyproject.toml")
                    tmp = deploy_folder / '_pyproject_reqs.txt'
                    tmp.write_text('\n'.join(deps))
                    install_dependencies_enhanced(tmp, update_logs, packages_dir)
                    tmp.unlink(missing_ok=True)
                    installed.extend(deps)
            except Exception as pe:
                update_logs(f"⚠️ pyproject.toml parse error: {pe}")

    # ── Pipfile ───────────────────────────────────────────────────────
    pipfile = deploy_folder / 'Pipfile'
    if pipfile.exists():
        update_logs("📋 Found Pipfile — extracting packages...")
        try:
            import re as _re
            text = pipfile.read_text()
            in_packages = False
            pkgs = []
            for line in text.splitlines():
                if line.strip() in ('[packages]', '[dev-packages]'):
                    in_packages = True
                elif line.startswith('['):
                    in_packages = False
                elif in_packages:
                    m = _re.match(r'^([a-zA-Z0-9_\-]+)\s*=', line)
                    if m:
                        pkgs.append(m.group(1))
            if pkgs:
                update_logs(f"📦 Parsed {len(pkgs)} deps from Pipfile")
                tmp = deploy_folder / '_pipfile_reqs.txt'
                tmp.write_text('\n'.join(pkgs))
                install_dependencies_enhanced(tmp, update_logs, packages_dir)
                tmp.unlink(missing_ok=True)
                installed.extend(pkgs)
        except Exception as pfe:
            update_logs(f"⚠️ Pipfile parse error: {pfe}")

    # ── setup.py / setup.cfg ──────────────────────────────────────────
    for setup_file in ['setup.py', 'setup.cfg']:
        sf = deploy_folder / setup_file
        if sf.exists():
            update_logs(f"📋 Found {setup_file} — installing package...")
            res = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--quiet',
                 '--no-warn-script-location', '--target', str(packages_dir), '-e', '.'],
                cwd=str(deploy_folder), capture_output=True, text=True, timeout=300)
            if res.returncode == 0:
                update_logs(f"✅ {setup_file} installed")
            else:
                update_logs(f"⚠️ {setup_file} install failed (check output log)")
            break

    return installed

# ========== FRAMEWORK DETECTION ==========
# ── Platform detection patterns ──────────────────────────────────────────────
_PLATFORM_PATTERNS = {
    # Telegram
    'telegram_telebot':  ['import telebot', 'from telebot', 'TeleBot('],
    'telegram_aiogram':  ['import aiogram', 'from aiogram', 'Dispatcher(', 'Router()'],
    'telegram_ptb':      ['from telegram', 'from telegram.ext', 'Application.builder', 'ApplicationBuilder'],
    'telegram_pyrogram': ['import pyrogram', 'from pyrogram', 'Client('],
    'telegram_telethon': ['import telethon', 'from telethon', 'TelegramClient('],
    # WhatsApp
    'whatsapp_pywa':     ['import pywa', 'from pywa', 'WhatsApp('],
    'whatsapp_heyoo':    ['import heyoo', 'from heyoo', 'WhatsApp('],
    'whatsapp_twilio':   ['from twilio', 'import twilio', 'MessagingResponse('],
    'whatsapp_cloud':    ['WHATSAPP_TOKEN', 'PHONE_NUMBER_ID', 'graph.facebook.com', 'whatsapp/messages'],
    # Discord
    'discord_py':        ['import discord', 'from discord', 'discord.Client(', 'commands.Bot('],
    'discord_nextcord':  ['import nextcord', 'from nextcord'],
    'discord_disnake':   ['import disnake', 'from disnake'],
    'discord_pycord':    ['import py_cord', 'from py_cord'],
    # Slack
    'slack':             ['from slack_sdk', 'import slack_sdk', 'from slack_bolt', 'import slack_bolt', 'WebClient('],
    # Twitter / X
    'twitter':           ['import tweepy', 'from tweepy', 'tweepy.Client', 'tweepy.API'],
    # Line
    'line':              ['from linebot', 'import linebot', 'LineBotApi(', 'WebhookHandler('],
    # Viber
    'viber':             ['from viberbot', 'import viberbot', 'ViberApi('],
    # Matrix
    'matrix':            ['from nio import', 'import nio', 'matrix_client', 'AsyncClient('],
    # IRC
    'irc':               ['import irc', 'from irc.bot', 'SingleServerIRCBot('],
    # Web frameworks (keep last — bot frameworks above take priority)
    'flask':             ['from flask import', 'import flask', 'Flask(__name__)'],
    'fastapi':           ['from fastapi', 'import fastapi', 'FastAPI()'],
    'django':            ['import django', 'from django', 'DJANGO_SETTINGS'],
    'aiohttp':           ['import aiohttp', 'from aiohttp'],
    'starlette':         ['from starlette', 'import starlette'],
}

# Human-readable labels for display
_PLATFORM_LABELS = {
    'telegram_telebot':  '📱 Telegram (pyTelegramBotAPI)',
    'telegram_aiogram':  '📱 Telegram (aiogram)',
    'telegram_ptb':      '📱 Telegram (python-telegram-bot)',
    'telegram_pyrogram': '📱 Telegram (Pyrogram)',
    'telegram_telethon': '📱 Telegram (Telethon)',
    'whatsapp_pywa':     '💬 WhatsApp (PyWA)',
    'whatsapp_heyoo':    '💬 WhatsApp (heyoo)',
    'whatsapp_twilio':   '💬 WhatsApp (Twilio)',
    'whatsapp_cloud':    '💬 WhatsApp (Cloud API)',
    'discord_py':        '🎮 Discord (discord.py)',
    'discord_nextcord':  '🎮 Discord (nextcord)',
    'discord_disnake':   '🎮 Discord (disnake)',
    'discord_pycord':    '🎮 Discord (py-cord)',
    'slack':             '💼 Slack',
    'twitter':           '🐦 Twitter/X (Tweepy)',
    'line':              '💚 Line',
    'viber':             '💜 Viber',
    'matrix':            '🔷 Matrix',
    'irc':               '📡 IRC',
    'flask':             '🌐 Flask',
    'fastapi':           '🌐 FastAPI',
    'django':            '🌐 Django',
    'aiohttp':           '🌐 aiohttp',
    'starlette':         '🌐 Starlette',
    # Node.js (from create_node_launcher_script's own detection — separate
    # from the Python-oriented _PLATFORM_PATTERNS above). Every one of these
    # previously fell through get_platform_label()'s fallback and displayed
    # as its raw internal key — several contain underscores, which breaks
    # legacy Markdown parsing outright when shown unprotected by backticks.
    'typescript':        '🟦 TypeScript',
    'node_esm':          '🟩 Node.js (ESM)',
    'node':              '🟩 Node.js',
    'discord_js':        '🎮 Discord (discord.js)',
    'telegram_node':     '📱 Telegram (node-telegram-bot-api)',
    'whatsapp_node':     '💬 WhatsApp (Node.js)',
    'web_server':        '🌐 Web Server',
}

# Required env vars hints per platform
PLATFORM_ENV_HINTS = {
    'telegram_telebot':  '`BOT_TOKEN` — your Telegram bot token from @BotFather',
    'telegram_aiogram':  '`BOT_TOKEN` — your Telegram bot token from @BotFather',
    'telegram_ptb':      '`BOT_TOKEN` — your Telegram bot token from @BotFather',
    'telegram_pyrogram': '`API_ID`, `API_HASH`, `BOT_TOKEN` — from my.telegram.org + @BotFather',
    'telegram_telethon': '`API_ID`, `API_HASH` — from my.telegram.org',
    'whatsapp_pywa':     '`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` — from Meta Developer Portal',
    'whatsapp_heyoo':    '`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` — from Meta Developer Portal',
    'whatsapp_twilio':   '`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER` — from Twilio Console',
    'whatsapp_cloud':    '`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN` — from Meta Developer Portal',
    'discord_py':        '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'discord_nextcord':  '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'discord_disnake':   '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'discord_pycord':    '`DISCORD_TOKEN` — from Discord Developer Portal → Bot → Token',
    'slack':             '`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` — from api.slack.com',
    'twitter':           '`TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_BEARER_TOKEN` — from developer.twitter.com',
    'line':              '`LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` — from Line Developer Console',
    'viber':             '`VIBER_AUTH_TOKEN` — from Viber Admin Panel',
    'matrix':            '`MATRIX_HOMESERVER`, `MATRIX_USER`, `MATRIX_PASSWORD` — from your Matrix server',
    'irc':               '`IRC_SERVER`, `IRC_PORT`, `IRC_NICK`, `IRC_CHANNEL`',
    'flask':             '`PORT` (optional, default 5000)',
    'fastapi':           '`PORT` (optional, default 8000)',
    'django':            '`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`',
}

def detect_bot_framework(code_content: str) -> list:
    """Detect all bot frameworks/platforms used in the code."""
    detected = []
    code_lower = code_content.lower()

    for platform, patterns in _PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in code_lower:
                detected.append(platform)
                break   # one match per platform is enough

    # Deduplicate while preserving order
    seen = set()
    result = []
    for p in detected:
        if p not in seen:
            seen.add(p)
            result.append(p)

    return result if result else ['generic']


def get_platform_env_hint(frameworks: list) -> str:
    """Return env var hints for the detected platforms."""
    hints = []
    for fw in frameworks:
        hint = PLATFORM_ENV_HINTS.get(fw)
        if hint and hint not in hints:
            label = _PLATFORM_LABELS.get(fw, fw)
            hints.append(f"*{label}:*\n{hint}")
    return '\n\n'.join(hints) if hints else ''


def get_platform_label(frameworks: list) -> str:
    """Return a human-readable platform summary."""
    # Defense in depth: even for a framework key with no _PLATFORM_LABELS
    # entry (future additions, typos, etc.), never expose the raw internal
    # key verbatim — it's very likely to contain an underscore, which
    # breaks legacy Markdown parsing outright when unprotected by backticks.
    labels = [_PLATFORM_LABELS.get(fw, fw.replace('_', ' ').title())
              for fw in frameworks if fw != 'generic']
    return ', '.join(labels) if labels else '🤖 Generic Python Bot'

# ========== ENHANCED LAUNCHER SCRIPT ==========
def create_node_launcher_script(deploy_folder: Path, dest_script: Path,
                                env_vars_dict: dict, update_logs) -> tuple:
    """
    Create start.sh for Node.js / TypeScript bots.
    Installs npm packages, picks the right runtime (node / npx ts-node / npx tsx).
    Returns (start_script_path, detected_framework_list).
    """
    ext = dest_script.suffix.lower()

    if ext in ('.ts', '.tsx'):
        # Prefer tsx (faster) then ts-node
        # setsid gives this its own process group, separate from the
        # hosting bot's own — without it, stopping/restarting this
        # deployment could only ever kill THIS PID, leaving the actual
        # node process (a child, once npx/ts-node execs or forks) running
        # forever as an orphan, or worse, a bare killpg here would risk
        # taking down unrelated processes that share the inherited group.
        run_cmd = (
            f'if command -v npx &>/dev/null; then\n'
            f'    setsid npx --yes tsx "{dest_script}" >> output.log 2>&1 &\n'
            f'elif command -v ts-node &>/dev/null; then\n'
            f'    setsid ts-node "{dest_script}" >> output.log 2>&1 &\n'
            f'else\n'
            f'    echo "❌ No TypeScript runner found (install tsx or ts-node)" >> output.log\n'
            f'    exit 1\n'
            f'fi'
        )
        framework = ['typescript']
    elif ext in ('.mjs',):
        run_cmd = f'setsid node --input-type=module "{dest_script}" >> output.log 2>&1 &'
        framework = ['node_esm']
    else:
        run_cmd = f'setsid node "{dest_script}" >> output.log 2>&1 &'
        framework = ['node']

    # Detect framework from content
    try:
        code = dest_script.read_text(errors='ignore')
        if 'discord' in code.lower():
            framework.append('discord_js')
        if 'telegraf' in code.lower() or 'node-telegram' in code.lower():
            framework.append('telegram_node')
        if 'whatsapp' in code.lower() or 'baileys' in code.lower():
            framework.append('whatsapp_node')
        if 'express' in code.lower() or 'fastify' in code.lower():
            framework.append('web_server')
    except Exception:
        pass

    # Write .env
    env_file = deploy_folder / '.env'
    env_file.write_text('\n'.join(f'{k}={v}' for k, v in env_vars_dict.items()) + '\n')

    # Write start.sh
    # Single-quote every value so bash treats it as fully literal — no `$`
    # variable expansion, no backtick/`$()` command substitution, no
    # quote-breaking. This is the standard POSIX-safe way to embed arbitrary
    # values in a shell script: replace each ' with '\'' and wrap in ' '.
    # Without this, a value containing $ (extremely common in real DB
    # connection strings and API keys) is silently corrupted with no error
    # anywhere, and a value containing backticks/`$()` is a shell injection risk.
    def _sh_quote(value):
        return "'" + str(value).replace("'", "'\\''") + "'"

    env_lines = '\n'.join(f'export {k}={_sh_quote(v)}' for k, v in env_vars_dict.items())
    npm_install = (
        'if [ -f "package.json" ]; then\n'
        '    echo "📦 Installing npm packages..." >> output.log\n'
        '    npm install --no-audit --no-fund >> output.log 2>&1\n'
        'fi'
    )
    start_content = (
        f'#!/bin/bash\n'
        f'cd "{deploy_folder}"\n'
        f'export PYTHONUNBUFFERED=1\n'
        f'{env_lines}\n\n'
        f'{npm_install}\n\n'
        f'{run_cmd}\n'
        f'echo $! > pid.txt\n'
    )
    start_script = deploy_folder / 'start.sh'
    start_script.write_text(start_content)
    start_script.chmod(0o755)

    update_logs(f"🟨 Node.js launcher created ({', '.join(framework)})")
    return start_script, framework


def create_enhanced_launcher_script(deploy_folder, dest_script, env_vars_dict, code_content, update_logs, packages_dir=None):
    frameworks = detect_bot_framework(code_content)
    framework_str = ', '.join(frameworks)
    update_logs(f"🔧 Framework detection: {framework_str}")
    
    launcher_script = deploy_folder / "run.py"
    
    env_set_code = []
    for k, v in env_vars_dict.items():
        # repr() is the correct, complete way to generate a valid Python
        # string literal from arbitrary content — handles embedded quotes,
        # backslashes, AND actual newlines (a hand-rolled quote/backslash
        # escape misses real newline characters, which breaks the entire
        # generated run.py with a syntax error for any multi-line value
        # such as a PEM private key or a JSON credentials blob).
        env_set_code.append(f'os.environ[{k!r}] = {v!r}')
    
    env_set_str = "\n".join(env_set_code) if env_set_code else "# No custom environment variables"
    
    # Build packages_dir injection line for launcher
    packages_dir_str = str(packages_dir) if packages_dir else str(deploy_folder / 'packages')
    
    with open(launcher_script, 'w', encoding='utf-8') as f:
        f.write(f'''#!/usr/bin/env python3
"""
UNIVERSAL BOT LAUNCHER - Auto-generated by Hosting Platform
Detected frameworks: {framework_str}
"""

import os
import sys
import time
import json
import signal
import threading
import traceback
from pathlib import Path

# ── Per-deployment packages directory (VPS isolation) ────────────────────────
# Packages installed with --target land here and take priority over system libs
_pkg_dir = r"{packages_dir_str}"
if os.path.isdir(_pkg_dir) and _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

# Change to deployment directory
os.chdir(r"{deploy_folder}")

# ========== ENVIRONMENT ISOLATION ==========
# This process was spawned by the hosting bot itself and inherits its FULL
# environment — including the hosting bot's OWN BOT_TOKEN, GITHUB_TOKEN,
# ADMIN_IDS, DATABASE_FILE, etc. For any of these the deployment doesn't
# explicitly configure itself, that leaked value would otherwise silently
# persist into the deployed bot's process — at best confusing (a deployed
# bot inheriting the hosting platform's admin list), at worst a real
# credential leak (a deployed bot's code having access to the platform's
# own GitHub backup token). Clear them all before applying the user's own
# configured values, so an unset key is genuinely unset, not leaked.
_HOSTING_BOT_KEYS = {{
    'BOT_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN',
    'TOKEN', 'API_TOKEN', 'TG_BOT_TOKEN', 'TGBOT_TOKEN',
    'DISCORD_TOKEN', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN',
    'GITHUB_TOKEN', 'GITHUB_REPO_OWNER', 'GITHUB_REPO_NAME',
    'GITHUB_BACKUP_BRANCH', 'GITHUB_BACKUP_PATH',
    'DATABASE_URL', 'DATABASE_FILE',
    'ADMIN_IDS', 'REQUIRED_CHANNEL', 'CHANNEL_LINK',
}}
for _hk in _HOSTING_BOT_KEYS:
    os.environ.pop(_hk, None)

# ========== SET ENVIRONMENT VARIABLES ==========
# The deployment's own configured values — applied after clearing inherited
# ones above, so they start from a genuinely clean slate.
{env_set_str}

# Try to load .env file (lowest priority — never overrides an explicitly
# configured deployment variable, only fills in what's still unset)
try:
    from dotenv import load_dotenv
    env_file = Path(r"{deploy_folder}") / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
        print("✅ Loaded .env file (non-overriding)")
except ImportError:
    pass
except Exception as e:
    print(f"⚠️ Could not load .env: {{e}}")

# ========== HEARTBEAT THREAD ==========
heartbeat_running = True

def heartbeat():
    heartbeat_file = Path(r"{deploy_folder}") / ".heartbeat"
    while heartbeat_running:
        try:
            with open(heartbeat_file, 'w') as f:
                f.write(str(time.time()))
            time.sleep(30)
        except:
            pass

heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
heartbeat_thread.start()

# ========== VERIFY ENVIRONMENT ==========
print("=" * 50)
print("🔧 LAUNCHER READY")
print("=" * 50)
print(f"Python: {{sys.version.split()[0]}}")
print(f"Dir: {{os.getcwd()}}")
sys.stdout.flush()

# Skip noisy cloud/system env vars — only show what the user actually set
_SKIP_PREFIXES = ('KUBERNETES_', 'RENDER_', 'UV_', 'PIPENV_', 'NPM_', 'NODE_',
                   'BUN_', 'GUNICORN_', 'YARN_', 'DEFAULT_', 'PYTHON_')
_SKIP_EXACT = frozenset([
    'PATH', 'PYTHONPATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM',
    'LANG', 'LC_ALL', 'PWD', 'HOSTNAME', 'RENDER', 'PORT',
    'IS_PULL_REQUEST', 'RENDER_ROOT', 'RENDER_ENV_IS_DOCKER',
    'RENDER_SERVICE_ID', 'RENDER_SERVICE_NS', 'RENDER_SERVICE_CONTEXT_ROOT',
    'RENDER_EXTERNAL_HOSTNAME', 'RENDER_GIT_REPO_SLUG',
    'RENDER_NODE_VERSION_DETECTED', 'RENDER_NODE_INSTALLED',
    'RENDER_PRE_RUN_COMMAND', 'RENDER_CPU_COUNT', 'USER_RUN_COMMAND',
    'BLACK', 'BLUE', 'CYAN', 'YELLOW', 'RESET', 'ENTER_STANDOUT',
    'PYTHONUNBUFFERED', 'UV_COMPILE_BYTECODE', 'ENTER_BOLD',
])

_shown_keys = []
for _k, _v in os.environ.items():
    if _k in _SKIP_EXACT:
        continue
    _skip = False
    for _pfx in _SKIP_PREFIXES:
        if _k.startswith(_pfx):
            _skip = True
            break
    if _skip:
        continue
    _shown_keys.append(_k)
    if any(_s in _k.upper() for _s in ['TOKEN', 'SECRET', 'KEY', 'PASSWORD', 'HASH']):
        _disp = (_v[:8] + '...') if len(_v) > 8 else '***'
    else:
        _disp = (_v[:60] + '...') if len(_v) > 60 else _v
    print(f"  ✅ {{_k}} = {{_disp}}")

if not _shown_keys:
    print("  ℹ️  No custom env vars (set KEY=VALUE when deploying)")

# Token check
_token_found = False
for _tname in ['BOT_TOKEN', 'TOKEN', 'API_TOKEN', 'DISCORD_TOKEN', 'TELEGRAM_TOKEN']:
    if os.environ.get(_tname):
        print(f"✅ {{_tname}} is set")
        _token_found = True
        break
if not _token_found:
    print("⚠️  WARNING: No bot token detected in env vars!")
    print("   Set BOT_TOKEN (or TOKEN) via env vars when deploying.")

sys.stdout.flush()
print("=" * 50)
print("🚀 STARTING BOT")
print("=" * 50)
sys.stdout.flush()

# ── Token aliasing ────────────────────────────────────────────────────────
# Bridge all common token env var names so the bot finds its token regardless
# of which variable name it reads.
_TOKEN_ALIASES = ['BOT_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN',
                  'TOKEN', 'API_TOKEN', 'TG_BOT_TOKEN', 'TGBOT_TOKEN',
                  'DISCORD_TOKEN']
_found_token = None
for _alias in _TOKEN_ALIASES:
    _t = os.environ.get(_alias, '')
    if _t:
        _found_token = _t
        break
if _found_token:
    for _alias in _TOKEN_ALIASES:
        if not os.environ.get(_alias):
            os.environ[_alias] = _found_token
    print("✅ Token env vars bridged")
else:
    print("⚠️  No bot token found — set BOT_TOKEN or TELEGRAM_TOKEN in env vars")

# ── Heartbeat file ───────────────────────────────────────────────────────
# Written every 30s by a background thread. A hung/deadlocked process still
# passes a simple "is the PID alive" check — this gives the host a way to
# eventually detect "alive but stuck" too, not just "dead", by checking
# whether this file's mtime is stale.
_heartbeat_running = True
def _heartbeat():
    _heartbeat_file = Path(r"{deploy_folder}") / ".heartbeat"
    while _heartbeat_running:
        try:
            with open(_heartbeat_file, 'w') as _hf:
                _hf.write(str(time.time()))
        except Exception:
            pass
        time.sleep(30)
threading.Thread(target=_heartbeat, daemon=True, name='Heartbeat').start()

# ── Universal webhook pre-clear (raw HTTP, framework-agnostic) ───────────
# Runs unconditionally before anything else, using only the raw bot token —
# independent of which framework the bot uses or whether entry-point
# detection below finds a recognizable bot object at all. A bot can't
# receive updates via polling while Telegram still has a webhook registered
# for it, so any bot that was ever previously run in webhook mode elsewhere
# would otherwise silently receive nothing here, with no error anywhere.
_wh_token = os.environ.get('BOT_TOKEN', '').strip()
if _wh_token and ':' in _wh_token:
    try:
        import urllib.request as _ur, json as _jr
        _wh_info_url = f"https://api.telegram.org/bot{{_wh_token}}/getWebhookInfo"
        with _ur.urlopen(_ur.Request(_wh_info_url), timeout=10) as _r:
            _wh_data = _jr.loads(_r.read().decode('utf-8'))
        _active_wh = (_wh_data.get('result') or {{}}).get('url', '')
        if _active_wh:
            print(f"⚠️  Active webhook detected: {{_active_wh[:60]}}")
            _del_url = (f"https://api.telegram.org/bot{{_wh_token}}"
                        f"/deleteWebhook?drop_pending_updates=true")
            _ur.urlopen(_ur.Request(_del_url), timeout=10).read()
            time.sleep(1)
            print("✅ Webhook deleted — polling mode active")
        else:
            print("✅ No active webhook — polling ready")
    except Exception as _we:
        print(f"⚠️  Webhook pre-check skipped: {{_we}}")

# ========== METHOD 1: Import and run ==========
try:
    print("📌 Method 1: Importing as module...")
    sys.stdout.flush()
    sys.path.insert(0, r"{deploy_folder}")

    import importlib.util, inspect as _inspect, socket as _socket, threading as _thr

    spec = importlib.util.spec_from_file_location("user_bot", r"{dest_script}")
    if spec is None:
        raise ImportError(f"Cannot load module from {dest_script}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["user_bot"] = module

    # ----------------------------------------------------------------
    # Intercept bot.set_webhook() calls that fire at module level.
    # Many webhook-mode bots call set_webhook() during initialisation;
    # if we let that run it will point Telegram at the old server and
    # polling will receive nothing.  We monkey-patch the telebot module
    # BEFORE exec_module so any set_webhook() becomes a silent no-op.
    # We restore the real method immediately after import.
    # ----------------------------------------------------------------
    _webhook_patches = []   # list of (obj, attr, original_value)
    try:
        import telebot as _tb_mod
        _orig_sw = _tb_mod.TeleBot.set_webhook
        def _noop_set_webhook(self, *a, **kw):
            print("  ℹ️  [launcher] set_webhook() suppressed during import (will use polling)")
        _tb_mod.TeleBot.set_webhook = _noop_set_webhook
        _webhook_patches.append((_tb_mod.TeleBot, 'set_webhook', _orig_sw))
    except ImportError:
        pass   # telebot not installed — nothing to patch

    try:
        spec.loader.exec_module(module)
    finally:
        # Always restore original set_webhook regardless of import success
        for _obj, _attr, _orig in _webhook_patches:
            setattr(_obj, _attr, _orig)

    print("✅ Module loaded successfully")
    sys.stdout.flush()

    # ----------------------------------------------------------------
    # Helper: find a random free TCP port (so hosted bots never clash
    # with the hosting bot's PORT=10000 health server).
    # ----------------------------------------------------------------
    def _free_port():
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
            _s.bind(('', 0))
            return _s.getsockname()[1]

    # ----------------------------------------------------------------
    # Helper: True if the module contains any Telegram bot object
    # (telebot, aiogram, PTB, or anything else with a polling-style
    # method) — used to decide whether Flask should run as a keep-alive
    # thread rather than the main entry point. Scans ALL module-level
    # objects rather than a fixed name list: a real bot object under an
    # unexpected variable name (very common — "telegram_bot", "tb",
    # anything not literally "bot"/"dp"/"application"/etc.) would
    # otherwise go undetected, causing a co-located Flask app (used for
    # keep-alive/health-checks) to be wrongly treated as the ONLY thing
    # to run — the process looks healthy forever while never actually
    # polling Telegram.
    # ----------------------------------------------------------------
    def _module_has_telegram_bot():
        _poll_methods = ('polling', 'run_polling', 'start_polling', 'infinity_polling')
        try:
            _names = list(vars(module).keys())
        except Exception:
            _names = []
        for _n in _names:
            if _n.startswith('__'):
                continue
            try:
                _obj = getattr(module, _n, None)
            except Exception:
                continue
            if _obj is None or _obj is module:
                continue
            # Flask's context-local proxies (request, g, session,
            # current_app — near-universal in any Flask app's imports) raise
            # RuntimeError, not AttributeError, when ANY attribute is
            # touched outside an active request. getattr(..., default) only
            # catches AttributeError, so probing these unconditionally
            # crashes the whole scan. Skip them by type name, and wrap the
            # actual probe too as a second layer of defense for anything
            # else with similarly surprising __getattr__ behavior.
            if type(_obj).__name__ == 'LocalProxy':
                continue
            try:
                if any(callable(getattr(_obj, _m, None)) for _m in _poll_methods):
                    return True
            except Exception:
                continue
        return False

    # ----------------------------------------------------------------
    # _clear_webhook: remove any active Telegram webhook so polling
    # can receive updates.  Bots deployed on webhook platforms (Choreo,
    # Railway, Heroku, etc.) will have a live webhook that silently
    # swallows all updates — polling never sees anything until it's gone.
    # ----------------------------------------------------------------
    def _clear_webhook(bot_obj, label='bot'):
        import time as _time
        import asyncio as _aio
        import inspect as _inspect

        def _resolve(value):
            # telebot's methods are plain sync calls — value is already the
            # real result. PTB/aiogram methods are async — calling them
            # without awaiting returns an unresolved coroutine object (which
            # silently has no attributes at all, not an error), so any
            # existing webhook would never actually be detected or removed.
            # No event loop is running yet at this point (run_polling()
            # creates its own later), so a fresh one here is safe.
            if _inspect.iscoroutine(value):
                return _aio.run(value)
            return value

        try:
            get_info = getattr(bot_obj, 'get_webhook_info', None)
            if callable(get_info):
                _wh = _resolve(get_info())
                _url = getattr(_wh, 'url', '') or ''
                if _url:
                    print(f"  ⚠️ Active webhook on {{label}}: {{_url[:60]}}...")
                    print(f"  🔧 Removing webhook — switching to polling mode...")
                    # telebot names this remove_webhook(); PTB/aiogram use
                    # delete_webhook() (matching the Bot API method name).
                    remove_fn = (getattr(bot_obj, 'remove_webhook', None) or
                                 getattr(bot_obj, 'delete_webhook', None))
                    if callable(remove_fn):
                        _resolve(remove_fn())
                    _time.sleep(1)   # give Telegram a moment to propagate
                    print(f"  ✅ Webhook removed — polling mode active")
                else:
                    print(f"  ✅ No webhook set on {{label}} — polling ready")
            # PTB / aiogram: bot object lives at .bot
            elif hasattr(bot_obj, 'bot') and callable(getattr(bot_obj.bot, 'get_webhook_info', None)):
                _clear_webhook(bot_obj.bot, label + '.bot')
        except Exception as _we:
            print(f"  ⚠️ Could not check/remove webhook ({{_we}}) — trying polling anyway")

    # ----------------------------------------------------------------
    # _make_runner: return a zero-arg callable that starts the object
    # correctly, or None if it should be called as a plain function.
    # ----------------------------------------------------------------
    def _make_runner(name, obj):
        _cls     = type(obj).__name__.lower()
        _cls_mod = (getattr(type(obj), '__module__', '') or '').lower()

        # ── pyTelegramBotAPI (telebot) ────────────────────────────────
        if callable(getattr(obj, 'polling', None)) and callable(getattr(obj, 'stop_polling', None)):
            print(f"  📱 {{name}} → Telegram (pyTelegramBotAPI) → polling")
            def _run_telebot(_b=obj, _n=name):
                _clear_webhook(_b, _n)
                _b.polling(non_stop=True, timeout=60, long_polling_timeout=60)
            return _run_telebot

        # ── aiogram Application ───────────────────────────────────────
        if hasattr(obj, 'run_polling') and hasattr(obj, 'run_webhook'):
            print(f"  📱 {{name}} → Telegram (aiogram) → run_polling")
            def _run_aiogram(_a=obj, _n=name):
                _clear_webhook(_a, _n)
                _a.run_polling()
            return _run_aiogram

        # ── python-telegram-bot Application ──────────────────────────
        if hasattr(obj, 'run_polling') and hasattr(obj, 'initialize'):
            print(f"  📱 {{name}} → Telegram (PTB) → run_polling")
            def _run_ptb(_a=obj, _n=name):
                _clear_webhook(_a, _n)
                _a.run_polling()
            return _run_ptb

        # ── Generic run_polling ───────────────────────────────────────
        if callable(getattr(obj, 'run_polling', None)):
            print(f"  📱 {{name}} → has run_polling()")
            def _run_genpoll(_a=obj, _n=name):
                _clear_webhook(_a, _n)
                _a.run_polling()
            return _run_genpoll

        # ── Pyrogram / Telethon client ────────────────────────────────
        if _cls in ('client',) and 'pyrogram' in _cls_mod:
            print(f"  📱 {{name}} → Telegram (Pyrogram) → run")
            return lambda: obj.run()
        if 'telethon' in _cls_mod and hasattr(obj, 'run_until_disconnected'):
            print(f"  📱 {{name}} → Telegram (Telethon)")
            return lambda: obj.run_until_disconnected()

        # ── PyWA (WhatsApp Cloud API) ─────────────────────────────────
        if 'pywa' in _cls_mod or (hasattr(obj, 'run_forever') and 'whatsapp' in str(type(obj)).lower()):
            print(f"  💬 {{name}} → WhatsApp (PyWA) → run_forever")
            return lambda: obj.run_forever()

        # ── Discord.py / nextcord / disnake / py-cord ────────────────
        _is_discord = ('discord' in _cls_mod or 'nextcord' in _cls_mod
                       or 'disnake' in _cls_mod or 'py_cord' in _cls_mod
                       or 'pycord' in _cls_mod)
        if _is_discord and callable(getattr(obj, 'run', None)):
            _tok = (os.environ.get('DISCORD_TOKEN')
                    or os.environ.get('TOKEN')
                    or os.environ.get('BOT_TOKEN', ''))
            if _tok:
                print(f"  🎮 {{name}} → Discord → .run(token)")
                return lambda: obj.run(_tok)
            print(f"  ⚠️ Discord object found but no DISCORD_TOKEN env var set")

        # ── Slack Bolt App ────────────────────────────────────────────
        if 'slack' in _cls_mod and callable(getattr(obj, 'start', None)):
            _port = int(os.environ.get('PORT', _free_port()))
            print(f"  💼 {{name}} → Slack Bolt → .start(port={{_port}})")
            return lambda: obj.start(port=_port)

        # ── Tweepy StreamingClient / Stream ──────────────────────────
        if 'tweepy' in _cls_mod:
            if callable(getattr(obj, 'filter', None)):
                print(f"  🐦 {{name}} → Twitter/X (Tweepy Stream) → .filter()")
                return lambda: obj.filter()
            if callable(getattr(obj, 'sample', None)):
                print(f"  🐦 {{name}} → Twitter/X (Tweepy) → .sample()")
                return lambda: obj.sample()

        # ── Line SDK ─────────────────────────────────────────────────
        # Line bots use Flask/ASGI webhooks — Flask detection below handles them

        # ── Matrix (matrix-nio) ───────────────────────────────────────
        if 'nio' in _cls_mod and callable(getattr(obj, 'sync_forever', None)):
            print(f"  🔷 {{name}} → Matrix (nio) → sync_forever")
            return lambda: obj.sync_forever()

        # ── IRC (irc.bot) ─────────────────────────────────────────────
        if 'irc' in _cls_mod and callable(getattr(obj, 'start', None)):
            print(f"  📡 {{name}} → IRC → .start()")
            return lambda: obj.start()

        # ── Flask / Quart ─────────────────────────────────────────────
        if _cls in ('flask', 'quart') or 'flask' in _cls_mod or 'quart' in _cls_mod:
            if _module_has_telegram_bot():
                # Pure keep-alive/health-check endpoint running alongside real
                # polling — a random internal port is fine here since it's
                # never meant to receive real external traffic.
                _port = _free_port()
                print(f"  🌐 {{name}} → Flask keep-alive thread (port {{_port}})")
                def _flask_bg(_app=obj, _p=_port):
                    try:
                        _app.run(host='0.0.0.0', port=_p, debug=False, use_reloader=False)
                    except Exception as _fe:
                        print(f"  ⚠️ Flask keep-alive: {{_fe}}")
                _thr.Thread(target=_flask_bg, daemon=True, name='FlaskKeepAlive').start()
                return 'BACKGROUND'
            else:
                # This Flask app IS the whole program (e.g. a real webhook
                # receiver) — it must bind the platform-assigned PORT, or
                # nothing external can ever reach it, random port or not.
                _port = int(os.environ.get('PORT', os.environ.get('BOT_PORT', 0)) or _free_port())
                print(f"  🌐 {{name}} → Flask/Quart → .run(port={{_port}})")
                return lambda: obj.run(host='0.0.0.0', port=_port, debug=False, use_reloader=False)

        # ── FastAPI / Starlette ───────────────────────────────────────
        if _cls in ('fastapi', 'starlette') or 'fastapi' in _cls_mod or 'starlette' in _cls_mod:
            _port = int(os.environ.get('PORT', _free_port()))
            print(f"  🌐 {{name}} → FastAPI/Starlette → uvicorn (port={{_port}})")
            def _run_asgi():
                try:
                    import uvicorn
                    uvicorn.run(obj, host='0.0.0.0', port=_port, log_level='info')
                except ImportError:
                    try:
                        import asyncio, hypercorn.asyncio as _ha, hypercorn.config as _hc
                        _cfg = _hc.Config(); _cfg.bind = [f'0.0.0.0:{{_port}}']
                        asyncio.run(_ha.serve(obj, _cfg))
                    except ImportError:
                        print("❌ uvicorn/hypercorn not found — add uvicorn to requirements.txt")
                        sys.exit(1)
            return _run_asgi

        # ── Django WSGI/ASGI ──────────────────────────────────────────
        if 'django' in _cls_mod:
            _port = int(os.environ.get('PORT', _free_port()))
            print(f"  🌐 {{name}} → Django → runserver port {{_port}}")
            import subprocess as _sp
            return lambda: _sp.run([sys.executable, 'manage.py', 'runserver',
                                     f'0.0.0.0:{{_port}}', '--noreload'],
                                    cwd=str(os.getcwd()))

        # ── Async coroutine function ──────────────────────────────────
        if _inspect.iscoroutinefunction(obj):
            import asyncio as _aio
            print(f"  ⚡ {{name}} → async function → asyncio.run")
            return lambda: _aio.run(obj())

        # ── Plain sync function / method ──────────────────────────────
        if _inspect.isfunction(obj) or _inspect.isbuiltin(obj) or _inspect.ismethod(obj):
            return None   # caller does obj()

        # ── Generic .run() ────────────────────────────────────────────
        if callable(getattr(obj, 'run', None)):
            print(f"  ▶️  {{name}} → has .run()")
            return lambda: obj.run()

        # ── Fallback: callable ────────────────────────────────────────
        if callable(obj):
            return None

        return None

    # ----------------------------------------------------------------
    # Entry point search.
    # Telegram bot objects (bot, dp) come BEFORE web app objects (app)
    # so a bot that has both a TeleBot and a Flask keep-alive doesn't
    # accidentally start the web server as the main loop.
    # ----------------------------------------------------------------
    entry_points = ['main', 'run', 'start', 'bot', 'dp', 'dispatcher',
                    'updater', 'setup', 'client', 'app', 'application']

    for entry in entry_points:
        if not hasattr(module, entry):
            continue
        attr = getattr(module, entry)
        if not callable(attr):
            continue

        print(f"✅ Entry point: {{entry}}")
        sys.stdout.flush()
        runner = _make_runner(entry, attr)

        if runner == 'BACKGROUND':
            # Flask started as daemon thread; keep searching for the real bot
            continue

        try:
            if runner is not None:
                runner()
            else:
                # Plain function — call it, then handle any returned app
                result = attr()
                if result is not None:
                    sub = _make_runner(f"{{entry}}() result", result)
                    if sub and sub != 'BACKGROUND':
                        sub()
                    elif callable(getattr(result, 'polling', None)):
                        result.polling(non_stop=True, timeout=60)
                    elif callable(getattr(result, 'run_polling', None)):
                        result.run_polling()
                    elif callable(getattr(result, 'run', None)):
                        result.run()
                    elif callable(getattr(result, 'run_forever', None)):
                        result.run_forever()
                    elif callable(getattr(result, 'serve_forever', None)):
                        result.serve_forever()
        except KeyboardInterrupt:
            print("\\n🛑 Bot stopped by user")
        except SystemExit as _se:
            sys.exit(_se.code)
        except Exception as _err:
            print(f"❌ Error in {{entry}}: {{type(_err).__name__}}: {{_err}}")
            traceback.print_exc()
            sys.stdout.flush()
            heartbeat_running = False
            sys.exit(1)

        heartbeat_running = False
        sys.exit(0)

    print("⚠️ No recognised entry point — falling through to Method 2...")
    sys.stdout.flush()

except Exception as e:
    print(f"⚠️ Import method failed: {{type(e).__name__}}: {{e}}")
    traceback.print_exc()
    sys.stdout.flush()

# ========== METHOD 2: Subprocess execution ==========
print("📌 Method 2: Running as subprocess...")
sys.stdout.flush()
import subprocess

try:
    _child_env = os.environ.copy()
    # sys.path.insert(0, _pkg_dir) above only affected THIS process's import
    # path — a subprocess gets a fresh interpreter with its own default
    # sys.path, so packages installed into the per-deployment packages/ dir
    # (via `pip install --target`) would otherwise be invisible to it.
    # Propagate via PYTHONPATH so the child can find them too.
    if os.path.isdir(_pkg_dir):
        _existing_pp = _child_env.get('PYTHONPATH', '')
        _child_env['PYTHONPATH'] = _pkg_dir + (os.pathsep + _existing_pp if _existing_pp else '')
    result = subprocess.run([sys.executable, r"{dest_script}"], env=_child_env)
    sys.exit(result.returncode)
except KeyboardInterrupt:
    print("\\n🛑 Stopped by user")
    sys.exit(0)
except Exception as e:
    print(f"❌ Subprocess failed: {{e}}")
    sys.exit(1)
''')
    
    launcher_script.chmod(0o755)
    return launcher_script, frameworks

# ========== ENHANCED DEPLOYMENT FUNCTION ==========
# ==================== GITHUB REPO DEPLOYMENT ====================

def parse_github_url(raw: str):
    """
    Parse any GitHub URL/shorthand into (owner, repo, branch).
    Supports:
      github.com/owner/repo
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/branch
      owner/repo
      owner/repo@branch
    Returns (None,None,None) on failure.
    """
    import re as _re
    raw = raw.strip().rstrip('/')

    # Pull branch from @branch suffix
    branch = None
    if '@' in raw and 'github.com' not in raw.split('@')[0]:
        raw, branch = raw.rsplit('@', 1)

    raw = _re.sub(r'^https?://', '', raw)
    raw = _re.sub(r'^github\.com/', '', raw)
    raw = raw.rstrip('/')

    parts = raw.split('/')
    if len(parts) < 2:
        return None, None, None

    owner = parts[0]
    repo  = parts[1].removesuffix('.git')

    if not branch:
        if len(parts) >= 4 and parts[2] == 'tree':
            branch = '/'.join(parts[3:])
        else:
            branch = None   # will be resolved from API

    return owner, repo, branch


def resolve_default_branch(owner: str, repo: str, token: str = None) -> str:
    """Ask GitHub API for the repo's default branch."""
    headers = {"User-Agent": "BotHostingPlatform/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data.get("default_branch", "main")
    except Exception:
        return "main"


def get_repo_info(owner: str, repo: str, token: str = None) -> dict:
    """Fetch basic repo metadata for display."""
    headers = {"User-Agent": "BotHostingPlatform/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def download_github_repo(owner: str, repo: str, branch: str,
                         dest_folder: Path, token: str = None,
                         update_logs=None) -> bool:
    """
    Download a GitHub repo as a tarball and extract it to dest_folder.
    Works for both public and private repos (private requires a valid token).
    Does NOT require git to be installed.
    """
    import tarfile, io as _io

    def _log(msg):
        if update_logs:
            update_logs(msg)
        else:
            print(msg)

    url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{branch}"
    headers = {"User-Agent": "BotHostingPlatform/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    _log(f"⬇️  Downloading {owner}/{repo}@{branch}...")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            tarball = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _log("❌ GitHub auth failed — check your token")
        elif e.code == 404:
            _log("❌ Repo not found — is it private? Provide a token")
        else:
            _log(f"❌ GitHub download HTTP {e.code}")
        return False
    except Exception as e:
        _log(f"❌ Download error: {e}")
        return False

    size_kb = len(tarball) / 1024
    _log(f"✅ Downloaded {size_kb:.0f} KB — extracting...")

    try:
        with tarfile.open(fileobj=_io.BytesIO(tarball), mode='r:gz') as tar:
            members = tar.getmembers()
            # GitHub tarballs contain a single top-level dir like "owner-repo-sha/"
            # We strip it so files land directly in dest_folder
            if members:
                prefix = members[0].name.split('/')[0] + '/'
            else:
                prefix = ''

            for member in members:
                rel = member.name
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                if not rel:
                    continue
                member.name = rel
                # Safety: no absolute paths or path traversal
                if rel.startswith('/') or '..' in rel:
                    continue
                try:
                    tar.extract(member, dest_folder)
                except Exception:
                    pass

        _log(f"✅ Repo extracted to {dest_folder.name}/")
        return True

    except Exception as e:
        _log(f"❌ Extraction error: {e}")
        return False


# Priority order for auto-detecting the main bot file
_MAIN_FILE_CANDIDATES = [
    'main.py', 'bot.py', 'app.py', 'run.py', 'start.py',
    'index.py', 'server.py', 'handler.py', '__main__.py',
]

def find_main_file(folder: Path) -> list[Path]:
    """
    Return a ranked list of Python file candidates for the bot entry point.
    Priority: known names → files with if __name__ == '__main__' → other .py
    """
    found = []

    # 1. Known entry-point names first
    for name in _MAIN_FILE_CANDIDATES:
        p = folder / name
        if p.exists():
            found.append(p)

    # 2. Any .py file with an if __name__ == '__main__' block
    for p in sorted(folder.rglob('*.py')):
        if p in found:
            continue
        try:
            txt = p.read_text(errors='ignore')
            if "__name__" in txt and "__main__" in txt:
                found.append(p)
        except Exception:
            pass

    # 3. Any remaining top-level .py files
    for p in sorted(folder.glob('*.py')):
        if p not in found:
            found.append(p)

    return found


def deploy_from_github(chat_id, user_id, owner, repo, branch, token,
                       env_vars_dict, plan, duration,
                       cost_coins, cost_stars, payment_method,
                       is_free=False, main_file_name=None,
                       skip_security=False):
    """
    Full deployment flow for a GitHub repo:
    download → detect requirements → install → launch.
    """
    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False

    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        send_message(chat_id, quota_exceeded_text(_q_used, _q_total),
                     {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        return False

    status_msg = send_message(chat_id, "```\n🐙 GITHUB DEPLOYMENT STARTING\n```", None)
    status_message_id = status_msg.get('result', {}).get('message_id') if status_msg else None
    logs = []

    def update_logs(msg):
        logs.append(msg)
        if status_message_id:
            try:
                edit_message(chat_id, status_message_id,
                             f"```\n🐙 GITHUB DEPLOY\n\n{chr(10).join(logs[-25:])[-3000:]}\n```", None)
            except Exception:
                pass

    try:
        # ── Free-tier GitHub deployment limit: 1 concurrent slot ────
        # Applies only to FREE-plan deployments; a paid monthly/yearly plan
        # (or premium) is not limited to one GitHub bot.
        if is_free and not is_user_premium(user_id) and not is_admin(user_id):
            active_gh = count_github_deployments(user_id)
            if active_gh >= 1:
                send_message(chat_id,
                    "⚠️ *GitHub Deployment Limit Reached*\n\n"
                    "Free users can have *1 active GitHub deployment* at a time (24hrs).\n\n"
                    "Stop or wait for your existing GitHub deployment to expire, "
                    "or upgrade to Premium for unlimited deployments.",
                    {"inline_keyboard": [
                        [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                        [{"text": "⭐ Get Premium",     "callback_data": "subscribe_premium"}],
                    ]})
                return False

        deploy_id     = int(datetime.now().timestamp())
        deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(deploy_id)
        deploy_folder.mkdir(parents=True, exist_ok=True)
        packages_dir  = deploy_folder / 'packages'
        packages_dir.mkdir(exist_ok=True)

        # ── Download repo ────────────────────────────────────────────
        if not download_github_repo(owner, repo, branch, deploy_folder, token, update_logs):
            edit_message(chat_id, status_message_id,
                "❌ *GitHub download failed.*\n\n"
                "• For private repos: ensure the token has `repo` scope\n"
                "• Check the repo URL and branch name",
                {"inline_keyboard": [[{"text": "🔄 Try Again", "callback_data": "github_deploy"},
                                      {"text": "🏠 Menu",       "callback_data": "main_menu"}]]})
            return False

        # ── Install all requirement sources ──────────────────────────
        update_logs("📦 Installing all dependencies (VPS mode)...")
        _installed_from_manifests = install_from_repo_requirements(deploy_folder, update_logs)

        # ── Detect main file ─────────────────────────────────────────
        if main_file_name:
            dest_script = deploy_folder / main_file_name
        else:
            candidates = find_main_file(deploy_folder)
            dest_script = candidates[0] if candidates else None

        if not dest_script or not dest_script.exists():
            update_logs("❌ No main Python file found — cannot launch")
            return False

        update_logs(f"🎯 Entry point: {dest_script.relative_to(deploy_folder)}")

        # ── Security scan of the main file ───────────────────────────
        if skip_security:
            update_logs("⏭️  Security scan skipped (admin-approved deployment)")
        else:
          try:
            scan_bytes = dest_script.read_bytes()
            blocked, crit, scan_w, scan_report = run_security_scan(scan_bytes, dest_script.name)
            if blocked:
                update_logs(f"🚫 SECURITY BLOCK: {len(crit)} critical issue(s)")
                for c in crit[:5]:
                    update_logs(f"  • {c}")

                # Persist blocked file for admin review
                try:
                    pending_path = BASE_DIR / f"pending_review_{user_id}_{sanitize_upload_filename(dest_script.name)}"
                    pending_path.write_bytes(dest_script.read_bytes())
                except Exception:
                    pending_path = None

                gh_step_json = json.dumps({
                    "source": "github", "owner": owner, "repo": repo,
                    "branch": branch, "token": token or "",
                    "env_vars": env_vars_dict, "plan": plan,
                    "duration": duration, "cost_coins": cost_coins,
                    "cost_stars": cost_stars, "payment_method": payment_method,
                    "is_free": is_free,
                })
                conn_r = sqlite3.connect(DATABASE_FILE, timeout=10)
                c_r = conn_r.cursor()
                c_r.execute("""INSERT INTO security_reviews
                    (user_id, chat_id, file_name, file_path, critical_json,
                     warnings_json, report_text, source, status, created_at, user_step_json)
                    VALUES (?,?,?,?,?,?,'github_deploy_blocked','github','pending',?,?)""",
                    (user_id, chat_id, dest_script.name,
                     str(pending_path) if pending_path else '',
                     json.dumps(crit), json.dumps(scan_w),
                     datetime.now().isoformat(), gh_step_json))
                review_id = c_r.lastrowid
                conn_r.commit()
                conn_r.close()

                edit_message(chat_id, status_message_id,
                    f"🔒 *Security Review Required*\n\n"
                    f"Repo `{owner}/{repo}` triggered security checks.\n"
                    f"An admin will review and approve or reject deployment.\n"
                    f"🆔 Review ID: `{review_id}`",
                    {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})

                admin_text = (
                    f"🚨 *SECURITY REVIEW #{review_id}* (GitHub)\n\n"
                    f"👤 User: `{user_id}`\n"
                    f"🐙 Repo: `{owner}/{repo}@{branch}`\n"
                    f"📁 File: `{safe_md(dest_script.name)}`\n"
                    f"⚠️ Critical: `{len(crit)}`\n\n"
                    + '\n'.join(f"• {safe_md(c[:120])}" for c in crit[:8])
                    + (f"\n_...and {len(crit)-8} more_" if len(crit) > 8 else "")
                )
                admin_kb = {"inline_keyboard": [
                    [{"text": "✅ Approve & Deploy",
                      "callback_data": f"sec_approve_{review_id}"},
                     {"text": "⚠️ Approve (warn user)",
                      "callback_data": f"sec_approve_warn_{review_id}"}],
                    [{"text": "❌ Reject",
                      "callback_data": f"sec_reject_{review_id}"}],
                    [{"text": "📋 View Full Report",
                      "callback_data": f"sec_report_{review_id}"}],
                ]}
                for admin_id in ADMIN_IDS:
                    try:
                        send_message(admin_id, admin_text, admin_kb)
                    except Exception:
                        pass
                return False
            elif scan_w:
                update_logs(f"⚠️ Security scan: {len(scan_w)} warning(s) — proceeding")
          except Exception as se:
              update_logs(f"⚠️ Security scan error: {se} — proceeding anyway")

        # ── Read code for framework detection ────────────────────────
        try:
            code_content = dest_script.read_text(errors='ignore')
        except Exception:
            code_content = ""

        # ── Fill any gaps between what's actually imported and what the
        # repo's manifest files declared ──────────────────────────────
        # A repo's requirements.txt (or lack of one) is very often
        # incomplete — e.g. `from dotenv import load_dotenv` with no
        # matching line anywhere, because it "just works" locally from
        # whatever's already installed globally. install_from_repo_requirements
        # only reads explicit manifest files; it has no equivalent of the
        # upload path's import-scanning fallback. Add one here too.
        try:
            _gh_already = {re.split(r'[=<>!~\[\s]', str(r).strip(), 1)[0].lower()
                           for r in (_installed_from_manifests or [])}
            _gh_auto = UniversalDependencyInstaller.scan_imports(code_content, update_logs)
            _gh_gap = [a for a in _gh_auto
                       if re.split(r'[=<>!~\[\s]', a.strip(), 1)[0].lower() not in _gh_already]
            if _gh_gap:
                update_logs(f"📦 Filling {len(_gh_gap)} import(s) missing from repo manifests: "
                            f"{', '.join(_gh_gap)}")
                _gh_packages_dir = deploy_folder / 'packages'
                _gh_tmp_reqs = deploy_folder / '_autodetect_gap_reqs.txt'
                _gh_tmp_reqs.write_text('\n'.join(_gh_gap))
                install_dependencies_enhanced(_gh_tmp_reqs, update_logs, _gh_packages_dir)
                _gh_tmp_reqs.unlink(missing_ok=True)
        except Exception as _gh_gap_err:
            update_logs(f"⚠️ Gap-fill dependency scan error: {_gh_gap_err}")

        file_size = dest_script.stat().st_size

        # ── Write .env ───────────────────────────────────────────────
        env_file = deploy_folder / ".env"
        # Assign unique port
        _hosting_port = int(os.environ.get('PORT', 10000))
        if int(env_vars_dict.get('PORT', 0) or 0) in (0, _hosting_port):
            _deploy_port = _find_available_port(20000 + (deploy_id % 9000))
            env_vars_dict['PORT'] = str(_deploy_port)
            env_vars_dict['BOT_PORT'] = str(_deploy_port)
        with open(env_file, 'w') as f:
            for k, v in env_vars_dict.items():
                f.write(f"{k}={v}\n")
        update_logs(f"📝 .env written ({len(env_vars_dict)} vars)")

        # ── Create launcher ──────────────────────────────────────────
        update_logs("🔧 Creating launcher...")
        launcher_script, frameworks = create_enhanced_launcher_script(
            deploy_folder, dest_script, env_vars_dict, code_content, update_logs,
            packages_dir=packages_dir)

        # ── Start script ─────────────────────────────────────────────
        start_script = deploy_folder / "start.sh"
        start_script.write_text(
            f"#!/bin/bash\ncd \"{deploy_folder}\"\nexport PYTHONUNBUFFERED=1\n"
            f"setsid nohup {sys.executable} \"{launcher_script}\" > output.log 2>&1 &\necho $! > pid.txt\n")
        start_script.chmod(0o755)

        update_logs("🚀 Starting bot...")
        subprocess.run([str(start_script)], cwd=str(deploy_folder),
                       capture_output=True, text=True)

        update_logs("⏳ Waiting for initialization (12s)...")
        sleep(12)

        # ── Check alive ──────────────────────────────────────────────
        pid_file  = deploy_folder / "pid.txt"
        proc_pid  = None
        log_file  = deploy_folder / "output.log"
        if pid_file.exists():
            try:
                proc_pid = int(pid_file.read_text().strip())
            except Exception:
                pass

        is_running = False
        if proc_pid:
            try:
                os.kill(proc_pid, 0)
                is_running = True
            except Exception:
                pass

        # ── Show last log lines ──────────────────────────────────────
        if log_file.exists() and log_file.stat().st_size > 0:
            lines = [l for l in log_file.read_text(errors='replace').split('\n') if l.strip()]
            for line in lines[-20:]:
                update_logs(f"   {line[:120]}")

        if is_running:
            start_time  = datetime.now()
            is_lifetime = (plan == "lifetime")
            if is_lifetime:
                expire_time = None
            elif is_free:
                expire_time = start_time + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
            else:
                expire_time = start_time + timedelta(hours=duration * 24)

            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method,
                 cost_coins, cost_stars, start_time, expire_time, status, proc_pid,
                 install_log, deploy_log, is_free, is_paused, framework,
                 dependencies_installed, folder_name, source_type, github_repo, github_branch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (user_id, str(dest_script.relative_to(deploy_folder)), file_size, "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 start_time.isoformat(), expire_time.isoformat() if expire_time else None, "active", proc_pid,
                 "\n".join(logs[-100:]), "GitHub bot running",
                 1 if is_free else 0, 0, ', '.join(frameworks),
                 json.dumps([]), str(deploy_id),
                 "github", f"{owner}/{repo}", branch))
            deployment_db_id = c.lastrowid
            conn.commit()
            conn.close()

            set_user_step(user_id, None)
            with deployment_lock:
                active_deployments[deployment_db_id] = proc_pid

            expire_str = "Never (lifetime)" if is_lifetime else expire_time.strftime('%Y-%m-%d %H:%M')
            edit_message(chat_id, status_message_id,
                f"*🎉 GITHUB DEPLOYMENT SUCCESSFUL!*\n\n"
                f"🐙 *Repo:* `{owner}/{repo}`\n"
                f"🌿 *Branch:* `{branch}`\n"
                f"🎯 *Entry:* `{display_filename(dest_script.name)}`\n"
                f"🔧 *Framework:* {get_platform_label(frameworks)}\n"
                f"📋 *Plan:* {plan.upper()}\n"
                f"📅 *Expires:* `{expire_str}`\n"
                f"🆔 *ID:* `{deployment_db_id}`",
                {"inline_keyboard": [
                    [{"text": "📄 Runtime Logs",  "callback_data": f"view_runtime_logs_{deployment_db_id}"}],
                    [{"text": "🔄 Restart",        "callback_data": f"restart_deploy_{deployment_db_id}"}],
                    [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                    [{"text": "🏠 Menu",           "callback_data": "main_menu"}],
                ]})

            async_backup(f"github_deploy_{deployment_db_id}")
            return True
        else:
            err_tail = ""
            if log_file.exists():
                err_tail = log_file.read_text(errors='replace')[-2500:].strip()
            edit_message(chat_id, status_message_id,
                f"❌ *GITHUB DEPLOYMENT FAILED*\n\n"
                f"Repo: `{owner}/{repo}@{branch}`\n\n"
                f"*Last output:*\n```\n{err_tail[-1500:]}\n```\n\n"
                "Common fixes:\n"
                "• Set `BOT_TOKEN` in env vars\n"
                "• Check the main file name\n"
                "• Verify requirements are correct",
                {"inline_keyboard": [[{"text": "🔄 Retry", "callback_data": "github_deploy"},
                                      {"text": "🏠 Menu",  "callback_data": "main_menu"}]]})
            return False

    except Exception as e:
        print(f"❌ deploy_from_github error: {e}")
        import traceback; traceback.print_exc()
        return False


def deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars, 
                     plan, duration, cost_coins, cost_stars, payment_method, is_free=False):
    # Defense in depth: lifetime deployments are admin-only no matter how this
    # function got called — never trust the caller alone for this check.
    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False

    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        send_message(chat_id, quota_exceeded_text(_q_used, _q_total),
                     {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        Path(temp_file).unlink(missing_ok=True)
        return False

    status_msg = send_message(chat_id, "```\n🚀 STARTING DEPLOYMENT\n```", None)
    if not status_msg:
        return False
    status_message_id = status_msg.get('result', {}).get('message_id')
    
    logs = []
    
    def update_logs(new_log):
        logs.append(new_log)
        display_logs = logs[-25:]
        log_text = "\n".join(display_logs)
        if status_message_id:
            try:
                edit_message(chat_id, status_message_id, 
                            f"```\n🚀 DEPLOYMENT IN PROGRESS\n\n{log_text[-3000:]}\n```", None)
            except:
                pass
    
    try:
        update_logs("📁 Creating deployment folder...")
        
        deploy_id = int(datetime.now().timestamp())
        deploy_folder = DEPLOYMENTS_DIR / str(user_id) / str(deploy_id)
        deploy_folder.mkdir(parents=True, exist_ok=True)
        packages_dir = deploy_folder / 'packages'
        packages_dir.mkdir(exist_ok=True)
        
        saved_path, saved_filename = save_user_file(user_id, temp_file, Path(temp_file).name)
        file_size = saved_path.stat().st_size
        
        dest_script = deploy_folder / saved_filename
        shutil.copy2(saved_path, dest_script)
        update_logs(f"📄 File saved: {saved_filename} ({format_file_size(file_size)})")

        _ext = dest_script.suffix.lower()
        _is_node = _ext in ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')
        
        # Parse environment variables
        env_vars_dict = {}
        if env_vars:
            if isinstance(env_vars, dict):
                env_vars_dict = env_vars
            elif isinstance(env_vars, str):
                for line in env_vars.strip().split('\n'):
                    line = line.strip()
                    if '=' in line:
                        first_eq = line.find('=')
                        key = line[:first_eq].strip()
                        value = line[first_eq+1:].strip()
                        if key:
                            env_vars_dict[key] = value
        
        # Read code content for analysis
        with open(dest_script, 'r', encoding='utf-8') as f:
            code_content = f.read()
        
        # Detect dependencies
        update_logs("🔍 Scanning for dependencies...")
        requirements_list = []

        if _is_node:
            # ── Node.js: package.json + npm, not requirements.txt/pip ──
            if requirements_text and requirements_text.strip().startswith('{'):
                # User uploaded an actual package.json
                try:
                    _pkg_data = json.loads(requirements_text)
                    (deploy_folder / "package.json").write_text(json.dumps(_pkg_data, indent=2))
                    _dep_count = len(_pkg_data.get('dependencies', {})) + len(_pkg_data.get('devDependencies', {}))
                    update_logs(f"📦 Using uploaded package.json ({_dep_count} package(s))")
                except Exception as _pje:
                    update_logs(f"⚠️ Invalid package.json ({_pje}) — falling back to auto-detect")
                    requirements_text = None
            if not (requirements_text and requirements_text.strip().startswith('{')):
                _js_packages = scan_js_requires(code_content)
                if _js_packages:
                    build_package_json(deploy_folder, _js_packages, dest_script.name, update_logs)
                else:
                    update_logs("ℹ️ No external npm packages detected")
            deps_success, failed = True, []
        else:
            if requirements_text and requirements_text.strip():
                requirements_list = UniversalDependencyInstaller.scan_requirements_file(requirements_text, update_logs)
                # A user-provided requirements.txt is very often incomplete —
                # e.g. `from dotenv import load_dotenv` at the top of a script
                # with no matching line in requirements.txt, because it "just
                # works" locally from whatever's already installed globally.
                # Auto-detect too and fill in anything actually imported but
                # missing, without touching what the user explicitly listed.
                _already = {re.split(r'[=<>!~\[\s]', r.strip(), 1)[0].lower() for r in requirements_list}
                _auto = UniversalDependencyInstaller.scan_imports(code_content, update_logs)
                _gap_filled = [a for a in _auto
                               if re.split(r'[=<>!~\[\s]', a.strip(), 1)[0].lower() not in _already]
                if _gap_filled:
                    requirements_list.extend(_gap_filled)
                    update_logs(f"📦 Added {len(_gap_filled)} import(s) missing from requirements.txt: "
                                f"{', '.join(_gap_filled)}")
            else:
                auto_detected = UniversalDependencyInstaller.scan_imports(code_content, update_logs)
                if auto_detected:
                    requirements_list.extend(auto_detected)
                    update_logs(f"📦 Auto-detected {len(auto_detected)} package(s)")

            # Install dependencies
            deps_success, failed = True, []
            if requirements_list:
                reqs_file = deploy_folder / "requirements.txt"
                with open(reqs_file, 'w') as f:
                    f.write('\n'.join(requirements_list))
                deps_success, failed = install_dependencies_enhanced(reqs_file, update_logs, packages_dir)

        if not deps_success and requirements_list:
            update_logs("⚠️ Some dependencies failed to install - continuing anyway")
        
        # Create .env file
        env_file = deploy_folder / ".env"
        with open(env_file, 'w') as f:
            for k, v in env_vars_dict.items():
                f.write(f"{k}={v}\n")
        update_logs(f"📝 Created .env with {len(env_vars_dict)} variables")
        
        # ---- Unique port assignment ----
        # The hosting bot already holds the platform PORT (e.g. 10000 on Render).
        # Give each deployed bot its own port so Flask keep-alive servers don't collide.
        _hosting_port = int(os.environ.get('PORT', 10000))
        _user_port    = int(env_vars_dict.get('PORT', 0) or 0)
        if _user_port == 0 or _user_port == _hosting_port:
            _deploy_port = _find_available_port(20000 + (deploy_id % 9000))
            env_vars_dict['PORT']     = str(_deploy_port)
            env_vars_dict['BOT_PORT'] = str(_deploy_port)
            # Re-write .env with the updated PORT
            with open(env_file, 'w') as f:
                for k, v in env_vars_dict.items():
                    f.write(f"{k}={v}\n")
            update_logs(f"🔌 Assigned port {_deploy_port} (hosting bot uses {_hosting_port})")
        
        # Create launcher — Python or Node.js depending on file type
        update_logs("🚀 Creating launcher...")

        if _is_node:
            start_script, frameworks = create_node_launcher_script(
                deploy_folder, dest_script, env_vars_dict, update_logs)
            launcher_script = start_script   # start.sh is the entry for Node
        else:
            launcher_script, frameworks = create_enhanced_launcher_script(
                deploy_folder, dest_script, env_vars_dict, code_content, update_logs,
                packages_dir=packages_dir)
            # Python start.sh wraps the Python launcher
            start_script = deploy_folder / "start.sh"
            start_script.write_text(
                f'#!/bin/bash\ncd "{deploy_folder}"\nexport PYTHONUNBUFFERED=1\n'
                f'setsid nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &\necho $! > pid.txt\n')
            start_script.chmod(0o755)
        
        # Start the bot
        update_logs("🚀 Starting bot process...")
        subprocess.run([str(start_script)], cwd=str(deploy_folder), capture_output=True, text=True)
        
        # Give the process time to start (or fail fast on syntax errors)
        update_logs("⏳ Waiting for bot to initialize (10s)...")
        sleep(10)
        
        # Check if running
        pid_file = deploy_folder / "pid.txt"
        proc_pid = None
        if pid_file.exists():
            with open(pid_file, 'r') as f:
                try:
                    proc_pid = int(f.read().strip())
                except:
                    pass
        
        is_running = False
        if proc_pid:
            try:
                os.kill(proc_pid, 0)
                is_running = True
            except:
                pass
        
        # Show the TAIL of the log so the actual error/startup line is visible
        log_file = deploy_folder / "output.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            with open(log_file, 'r', errors='replace') as f:
                raw = f.read()
            all_lines = [l for l in raw.split('\n') if l.strip()]
            # Show last 20 meaningful lines — these contain the actual error/success
            tail_lines = all_lines[-20:]
            update_logs("📋 Bot output (last lines):")
            for line in tail_lines:
                update_logs(f"   {line[:120]}")
        
        if is_running:
            start_time = datetime.now()
            is_lifetime = (plan == "lifetime")
            if is_lifetime:
                expire_time = None
            elif is_free:
                expire_time = start_time + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
            else:
                expire_time = start_time + timedelta(days=duration)
            
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments 
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method, cost_coins, cost_stars, 
                 start_time, expire_time, status, proc_pid, install_log, deploy_log, is_free, is_paused, framework, dependencies_installed, folder_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, dest_script.name, file_size, requirements_text or "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 start_time.isoformat(), expire_time.isoformat() if expire_time else None, "active", proc_pid,
                 "\n".join(logs[-100:]), "Bot running", 1 if is_free else 0, 0,
                 ', '.join(frameworks), json.dumps(requirements_list), str(deploy_id)))
            deployment_db_id = c.lastrowid
            conn.commit()
            conn.close()
            
            Path(temp_file).unlink(missing_ok=True)
            set_user_step(user_id, None)
            
            success_keyboard = {
                "inline_keyboard": [
                    [{"text": "📋 View Logs", "callback_data": f"view_install_logs_{deployment_db_id}"}],
                    [{"text": "📄 Runtime Logs", "callback_data": f"view_runtime_logs_{deployment_db_id}"}],
                    [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{deployment_db_id}"}],
                    [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{deployment_db_id}"}],
                    [{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                    [{"text": "🏠 Menu", "callback_data": "main_menu"}]
                ]
            }
            
            expiry_line = "📅 *Expires:* `Never (lifetime)`" if is_lifetime \
                else f"📅 *Expires:* {expire_time.strftime('%Y-%m-%d %H:%M:%S')}"
            duration_line = "⏱️ *Duration:* Lifetime — never expires" if is_lifetime \
                else f"⏱️ *Duration:* {duration if not is_free else FREE_DEPLOYMENT_DURATION_HOURS} {'days' if not is_free else 'hours'}"

            success_text = (
                f"*🎉 DEPLOYMENT SUCCESSFUL!* 🎉\n\n"
                f"📁 *File:* `{display_filename(dest_script.name)}`\n"
                f"🤖 *Platform:* {get_platform_label(frameworks)}\n"
                f"📋 *Plan:* {plan.upper()}\n"
                f"{duration_line}\n"
                f"{expiry_line}\n"
                f"📦 *Dependencies:* {len(requirements_list)} package(s)\n"
                f"🔧 *Env Vars:* {len(env_vars_dict)}\n"
                f"🆔 *ID:* `{deployment_db_id}`"
            )
            
            if failed:
                success_text += f"\n\n⚠️ *Partial Success:* {len(failed)} package(s) failed to install"
            
            edit_message(chat_id, status_message_id, success_text, success_keyboard)
            
            if proc_pid:
                with deployment_lock:
                    active_deployments[deployment_db_id] = proc_pid

            async_backup(f"deployment_{deployment_db_id}")
            return True
        else:
            # Read the TAIL of the log — that's where the error actually is
            error_tail = ""
            if log_file.exists() and log_file.stat().st_size > 0:
                with open(log_file, 'r', errors='replace') as f:
                    raw = f.read()
                # Last 3000 chars contains the crash traceback
                error_tail = raw[-3000:].strip()
            
            # Try to detect common failure causes from the log
            diagnosis = ""
            if error_tail:
                low = error_tail.lower()
                if 'no bot token' in low or 'bot_token' in low and 'not' in low:
                    diagnosis = "\n\n💡 *Tip:* Your bot needs `BOT_TOKEN` — set it in env vars when deploying."
                elif 'modulenotfounderror' in low or 'importerror' in low:
                    import re as _re
                    m = _re.search(r"No module named '([^']+)'", error_tail)
                    pkg = m.group(1) if m else "unknown"
                    diagnosis = f"\n\n💡 *Tip:* Missing package `{pkg}` — add it to `requirements.txt` and redeploy."
                elif 'syntaxerror' in low:
                    diagnosis = "\n\n💡 *Tip:* Your bot has a Python syntax error — test it locally first."
                elif 'telegramapiexception' in low or 'unauthorized' in low:
                    diagnosis = "\n\n💡 *Tip:* Invalid bot token — check your `BOT_TOKEN` env var."
                elif 'address already in use' in low or 'port' in low and 'bind' in low:
                    diagnosis = "\n\n💡 *Tip:* Port conflict — another process is using the same port."
                elif 'permissionerror' in low:
                    diagnosis = "\n\n💡 *Tip:* File permission error — contact support."
                else:
                    diagnosis = "\n\n💡 *Tip:* Check the error above. Common fixes:\n• Add `BOT_TOKEN` env var\n• Add a `requirements.txt`\n• Make sure your bot has no syntax errors"
            
            error_msg = f"❌ *DEPLOYMENT FAILED*\n\n"
            if error_tail:
                error_msg += f"*Last output (tail):*\n```\n{error_tail}\n```{diagnosis}"
            else:
                error_msg += (
                    "No output captured. Common issues:\n"
                    "• Missing required env vars (e.g. `BOT_TOKEN`)\n"
                    "• Syntax error in your code\n"
                    "• Missing packages not in requirements.txt\n\n"
                    "*Tip:* Test your bot locally before deploying."
                )
            
            edit_message(chat_id, status_message_id, error_msg[:4000])
            
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute('''INSERT INTO deployments 
                (user_id, file_name, file_size, requirements, env_vars, plan, payment_method, cost_coins, cost_stars, 
                 start_time, expire_time, status, install_log, deploy_log, error_log, is_free, framework, folder_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, dest_script.name, file_size, requirements_text or "", json.dumps(env_vars_dict),
                 plan, payment_method, cost_coins, cost_stars,
                 datetime.now().isoformat(), datetime.now().isoformat(), "failed",
                 "\n".join(logs[-50:]), "Bot failed to start", error_msg[:2000], 1 if is_free else 0,
                 ', '.join(frameworks), str(deploy_id)))
            conn.commit()
            conn.close()
            
            Path(temp_file).unlink(missing_ok=True)
            return False
            
    except Exception as e:
        error_msg = f"❌ *DEPLOYMENT FAILED*\n\nException: {str(e)}\n{traceback.format_exc()}"
        edit_message(chat_id, status_message_id, error_msg[:4000])
        Path(temp_file).unlink(missing_ok=True)
        return False

# ========== WRAPPER FUNCTIONS ==========
def deploy_free_bot_with_logs(chat_id, user_id, temp_file, requirements_text, env_vars):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", None)
        return False
    
    can_deploy, reason = can_use_free_deployment(user_id)
    if not can_deploy:
        send_message(chat_id, f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited deployments!",
                    {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        return False
    
    return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                           "free", FREE_DEPLOYMENT_DURATION_HOURS, 0, 0, "none", is_free=True)

def deploy_paid_bot(chat_id, user_id, temp_file, requirements_text, env_vars, plan, duration, cost_coins, cost_stars, payment_method):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", None)
        return False
    
    # Central guard: lifetime is admin-only on EVERY entry point (stars, coins,
    # premium-free), and only known plans are accepted.
    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False
    if plan not in ("monthly", "yearly", "lifetime"):
        send_message(chat_id, "❌ Invalid deployment plan.")
        return False

    if premium_covers_plan(user_id, plan):
        send_message(chat_id,
            f"*✨ PREMIUM BENEFIT ACTIVE!*\n\n"
            f"As a premium user, your {plan.upper()} deployment is *FREE*!\n\n"
            f"Proceeding with deployment...")
        
        return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                               plan, duration, 0, 0, "premium_free", is_free=False)
    else:
        return deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements_text, env_vars,
                               plan, duration, cost_coins, cost_stars, payment_method, is_free=False)

def get_payment_keyboard(plan, cost_stars, cost_coins):
    return {
        "inline_keyboard": [
            [{"text": f"⭐ Pay {cost_stars} Stars", "callback_data": f"pay_stars_{plan}"}],
            [{"text": f"🪙 Pay {cost_coins} Coins", "callback_data": f"pay_coins_{plan}"}],
            [{"text": "🔙 Back", "callback_data": "deploy_new"}]
        ]
    }

# ========== DEPLOYMENT MANAGEMENT ==========
def delete_deployment(deployment_id, user_id, chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        
        c.execute("SELECT proc_pid, file_name, user_id, is_free, plan FROM deployments WHERE deployment_id = ?", (deployment_id,))
        row = c.fetchone()
        
        if not row:
            send_message(chat_id, "❌ Deployment not found")
            return False
        
        proc_pid, file_name, owner_id, is_free, plan = row
        
        if owner_id != user_id and not is_admin(user_id):
            send_message(chat_id, "❌ Permission denied")
            return False
        
        if proc_pid:
            try:
                kill_deployment_process(proc_pid)
                sleep(1)
            except:
                pass
        
        deploy_folder = get_deploy_folder(owner_id, deployment_id)
        if deploy_folder.exists():
            shutil.rmtree(deploy_folder)
        
        c.execute("DELETE FROM deployments WHERE deployment_id = ?", (deployment_id,))
        conn.commit()
        conn.close()
        
        if is_free:
            used_count = get_free_deployment_used_count(user_id)
            remaining = FREE_USER_MAX_DEPLOYMENTS - used_count
            send_message(chat_id, 
                f"✅ Deployment `{deployment_id}` deleted!\n\n"
                f"📁 File: `{display_filename(file_name)}`\n"
                f"🆓 Free slots left: `{remaining}/{FREE_USER_MAX_DEPLOYMENTS}`")
        else:
            send_message(chat_id, f"✅ Deployment `{deployment_id}` deleted!\n\n📁 File: `{display_filename(file_name)}`")
        
        with deployment_lock:
            if deployment_id in active_deployments:
                del active_deployments[deployment_id]
        
        update_system_stats()
        return True
        
    except Exception as e:
        send_message(chat_id, f"❌ Error deleting deployment: {str(e)}")
        return False

def restart_deployment(deployment_id, user_id, chat_id, force_free_downgrade=False):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("""SELECT proc_pid, file_name, user_id, is_paused, env_vars,
                            status, start_time, expire_time, is_free, plan
                     FROM deployments WHERE deployment_id=?""", (deployment_id,))
        row = c.fetchone()

        if not row:
            conn.close()
            send_message(chat_id, "❌ Deployment not found")
            return False

        proc_pid, file_name, owner_id, is_paused, env_vars_json, \
            status, start_time_str, expire_time_str, is_free, plan = row

        if owner_id != user_id and not is_admin(user_id):
            conn.close()
            send_message(chat_id, "❌ Permission denied")
            return False

        # Paused premium bots require premium renewal, not a simple restart
        # (lifetime deployments never expire, so they never legitimately hit
        # this state — but guard it anyway rather than trust that invariant)
        if is_paused and not is_free and plan != "lifetime":
            conn.close()
            send_message(chat_id,
                "⏸️ This premium deployment is paused.\n\n"
                "Purchase or renew premium to resume it — your database stays intact.",
                {"inline_keyboard": [[{"text": "⭐ Renew Premium", "callback_data": "subscribe_premium"}]]})
            return False

        # ── Re-verify premium status before auto-extending a premium plan ──
        # A deployment's `plan` reflects what it was created under, not the
        # owner's CURRENT subscription — someone could restart a long-expired
        # "monthly" deployment and silently get another free extension of the
        # original duration even if their premium lapsed ages ago. Re-check
        # live status and let the user explicitly choose how to proceed
        # instead of assuming.
        needs_expiry_extension = False
        if expire_time_str and not is_free and plan != "lifetime":
            current_expire = datetime.fromisoformat(expire_time_str)
            needs_expiry_extension = status in ('stopped', 'failed') or current_expire < datetime.now()

        if needs_expiry_extension and not force_free_downgrade:
            conn.close()
            if not is_user_premium(owner_id):
                send_message(chat_id,
                    f"⭐ *This bot's plan (`{plan}`) has expired, and your premium "
                    f"subscription is no longer active.*\n\n"
                    f"Your database and files are safe. How would you like to restart it?",
                    {"inline_keyboard": [
                        [{"text": "⭐ Renew Premium", "callback_data": "subscribe_premium"}],
                        [{"text": "🆓 Restart as Free (24h)", "callback_data": f"restart_free_{deployment_id}"}],
                        [{"text": "❌ Cancel", "callback_data": f"view_deploy_{deployment_id}"}],
                    ]})
                return False
            # Still genuinely premium — reopen the connection and proceed
            # exactly as before.
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()

        deploy_folder = get_deploy_folder(owner_id, deployment_id)
        dest_script   = deploy_folder / file_name

        if not deploy_folder.exists():
            conn.close()
            send_message(chat_id, "❌ Deployment folder missing — please create a new deployment.")
            return False
        if not dest_script.exists():
            conn.close()
            send_message(chat_id,
                f"❌ Bot file `{file_name}` missing.\n\nPlease delete this deployment and create a new one.")
            return False

        # A bot that is NOT currently running needs fresh headroom to start
        if status != 'active':
            _q_ok, _q_used, _q_total = check_user_quota(owner_id)
            if not _q_ok:
                conn.close()
                send_message(chat_id, quota_exceeded_text(_q_used, _q_total),
                             {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
                return False

        # Kill any stale process
        if proc_pid:
            try:
                kill_deployment_process(proc_pid)
                sleep(1)
            except Exception:
                pass

        env_vars = json.loads(env_vars_json) if env_vars_json else {}

        try:
            code_content = dest_script.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            code_content = ""

        launcher_script, _ = create_enhanced_launcher_script(
            deploy_folder, dest_script, env_vars, code_content, lambda x: None)

        # Rewrite .env and start.sh to ensure they're fresh
        env_file = deploy_folder / ".env"
        env_file.write_text('\n'.join(f"{k}={v}" for k, v in env_vars.items()) + '\n')

        start_script = deploy_folder / "start.sh"
        start_script.write_text(
            f'#!/bin/bash\ncd "{deploy_folder}"\nexport PYTHONUNBUFFERED=1\n'
            f'setsid nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &\necho $! > pid.txt\n')
        start_script.chmod(0o755)

        subprocess.run([str(start_script)], cwd=str(deploy_folder), capture_output=True)
        sleep(4)

        pid_file = deploy_folder / "pid.txt"
        new_pid  = None
        if pid_file.exists():
            try:
                new_pid = int(pid_file.read_text().strip())
            except Exception:
                pass

        is_running = False
        if new_pid:
            try:
                os.kill(new_pid, 0)
                is_running = True
            except Exception:
                pass

        if is_running:
            if expire_time_str is None:
                # Lifetime deployment — never had an expiry and never gets one.
                c.execute("UPDATE deployments SET proc_pid=?, status='active', is_paused=0 WHERE deployment_id=?",
                          (new_pid, deployment_id))
            elif force_free_downgrade:
                # User explicitly chose to continue on the free tier rather
                # than renew — actually downgrade the plan, not just extend
                # the expiry, so it's consistently treated as free from now on
                # (future restarts, expiry monitor, resource accounting, etc.)
                new_expire = datetime.now() + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
                c.execute("""UPDATE deployments
                             SET proc_pid=?, status='active', is_paused=0, expire_time=?,
                                 start_time=?, is_free=1, plan='free'
                             WHERE deployment_id=?""",
                          (new_pid, new_expire.isoformat(), datetime.now().isoformat(), deployment_id))
            else:
                # For stopped/expired bots: extend expiry from now
                current_expire = datetime.fromisoformat(expire_time_str)
                if status in ('stopped', 'failed') or current_expire < datetime.now():
                    if is_free:
                        new_expire = datetime.now() + timedelta(hours=FREE_DEPLOYMENT_DURATION_HOURS)
                    else:
                        # Premium restart: owner's active status was already
                        # re-verified above — keep the original duration window
                        original_hours = max(1, (current_expire - datetime.fromisoformat(start_time_str)).total_seconds() / 3600)
                        new_expire = datetime.now() + timedelta(hours=original_hours)
                    c.execute("""UPDATE deployments
                                 SET proc_pid=?, status='active', is_paused=0, expire_time=?, start_time=?
                                 WHERE deployment_id=?""",
                              (new_pid, new_expire.isoformat(), datetime.now().isoformat(), deployment_id))
                else:
                    c.execute("UPDATE deployments SET proc_pid=?, status='active', is_paused=0 WHERE deployment_id=?",
                              (new_pid, deployment_id))

            conn.commit()
            conn.close()
            with deployment_lock:
                active_deployments[deployment_id] = new_pid

            async_backup(f"restart_{deployment_id}")
            send_message(chat_id,
                f"✅ *Bot #{deployment_id} Restarted*\n\n"
                f"Your database and settings are preserved.\n"
                f"Bot is running from the same deployment folder.",
                {"inline_keyboard": [
                    [{"text": "📄 View Logs",       "callback_data": f"view_runtime_logs_{deployment_id}"}],
                    [{"text": "📦 My Deployments",  "callback_data": "my_deployments"}],
                ]})
            return True
        else:
            log_tail = ""
            lf = deploy_folder / "output.log"
            if lf.exists():
                log_tail = lf.read_text(errors='replace')[-600:].strip()
            c.execute("UPDATE deployments SET status='failed' WHERE deployment_id=?", (deployment_id,))
            conn.commit()
            conn.close()
            send_message(chat_id,
                f"❌ *Bot #{deployment_id} failed to start*\n\n"
                f"```\n{log_tail[-400:]}\n```\n\n"
                "Check your env vars and try again.",
                {"inline_keyboard": [
                    [{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}],
                    [{"text": "🔄 Retry",     "callback_data": f"restart_deploy_{deployment_id}"}],
                ]})
            return False

    except Exception as e:
        print(f"❌ restart_deployment error: {e}")
        send_message(chat_id, f"❌ Error restarting: {e}")
        return False

def stop_deployment(deployment_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE deployment_id = ?", (deployment_id,))
    row = c.fetchone()
    if row and row[0]:
        try:
            kill_deployment_process(row[0])
        except:
            pass
    c.execute("UPDATE deployments SET status = 'stopped', proc_pid = NULL WHERE deployment_id = ?", (deployment_id,))
    conn.commit()
    conn.close()
    
    with deployment_lock:
        if deployment_id in active_deployments:
            del active_deployments[deployment_id]
    
    update_system_stats()

# ========== LOG VIEWING FUNCTIONS ==========
def view_install_logs(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT install_log, error_log, file_name FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ No logs found", 
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    
    install_log, error_log, file_name = row
    log_text = install_log if install_log else "No installation logs available"
    if error_log:
        log_text += f"\n\n❌ ERROR:\n{error_log}"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "📄 View Runtime Logs", "callback_data": f"view_runtime_logs_{dep_id}"}],
            [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{dep_id}"}],
            [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{dep_id}"}],
            [{"text": "🔙 Back", "callback_data": f"view_deploy_{dep_id}"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"```\n📋 INSTALLATION LOGS\n📁 {display_filename(file_name)}\n\n{log_text[-3000:]}\n```",
        keyboard)

def view_runtime_logs(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deploy_log, file_name, status, error_log, is_paused FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ Not found", 
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    
    deploy_log, file_name, status, error_log, is_paused = row
    log_file = get_deploy_folder(user_id, dep_id) / "output.log"
    
    log_text = deploy_log if deploy_log else ""
    
    if log_file.exists():
        with open(log_file, 'r') as f:
            runtime_log = f.read()
            if runtime_log:
                log_text += f"\n\n📌 RUNTIME OUTPUT:\n{runtime_log[-3000:]}"
    
    if error_log:
        log_text += f"\n\n❌ ERROR:\n{error_log}"
    
    if status == "active":
        log_text += "\n\n✅ RUNNING"
    elif status == "paused":
        log_text += "\n\n⏸️ PAUSED"
    elif status == "stopped":
        log_text += "\n\n🛑 STOPPED"
    elif status == "failed":
        log_text += "\n\n❌ FAILED"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "🔄 Refresh", "callback_data": f"view_runtime_logs_{dep_id}"}],
            [{"text": "📋 Install Logs", "callback_data": f"view_install_logs_{dep_id}"}],
            [{"text": "🔄 Restart", "callback_data": f"restart_deploy_{dep_id}"}],
            [{"text": "🗑️ Delete", "callback_data": f"delete_deploy_{dep_id}"}],
            [{"text": "🔙 Back", "callback_data": f"view_deploy_{dep_id}"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"```\n📄 RUNTIME LOGS\n📁 {display_filename(file_name)} (Status: {status.upper()})\n\n{log_text[-3000:]}\n```",
        keyboard)

def view_deployment(chat_id, message_id, user_id, dep_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT file_name, file_size, plan, payment_method, cost_coins, cost_stars, start_time, expire_time, status, is_free, requirements, env_vars, error_log, is_paused, framework FROM deployments WHERE deployment_id = ? AND user_id = ?", (dep_id, user_id))
    row = c.fetchone()
    conn.close()
    
    if not row:
        edit_message(chat_id, message_id, "❌ Not found", {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "my_deployments"}]]})
        return
    
    fname, fsize, plan, payment, cost_coins, cost_stars, start_str, expire_str, status, is_free, requirements, env_vars_json, error_log, is_paused, framework = row
    start_time = datetime.fromisoformat(start_str)
    is_lifetime = (expire_str is None)
    expire_time = datetime.fromisoformat(expire_str) if expire_str else None
    env_vars = json.loads(env_vars_json) if env_vars_json else {}
    
    if is_lifetime:
        cost_text      = "FREE (Lifetime)"
        remaining_text = "Never expires"
    elif is_free:
        remaining_secs = max(0, (expire_time - datetime.now()).total_seconds())
        remaining_h    = int(remaining_secs / 3600)
        remaining_m    = int((remaining_secs % 3600) / 60)
        cost_text      = "FREE"
        remaining_text = f"{remaining_h}h {remaining_m}m" if remaining_secs > 0 else "Expired"
    else:
        remaining_secs = max(0, (expire_time - datetime.now()).total_seconds())
        remaining_days = int(remaining_secs / 86400)
        if payment == "stars":
            cost_text = f"{cost_stars}⭐"
        elif payment == "coins":
            cost_text = f"{cost_coins}🪙"
        elif payment == "premium_free":
            cost_text = "FREE (Premium)"
        else:
            cost_text = f"{cost_coins}🪙"
        remaining_text = f"{remaining_days}d" if remaining_secs > 0 else "Expired"
    
    if status == "active":
        status_emoji = "🟢 ACTIVE"
    elif status == "paused":
        status_emoji = "⏸️ PAUSED"
    elif status == "stopped":
        status_emoji = "🔴 STOPPED"
    elif status == "failed":
        status_emoji = "❌ FAILED"
    else:
        status_emoji = status.upper()
    
    size_str = format_file_size(fsize) if fsize else "Unknown"
    
    env_text = "\n".join([f"• `{k}`" for k in list(env_vars.keys())[:10]]) if env_vars else "None"
    if len(env_vars) > 10:
        env_text += f"\n• ... and {len(env_vars) - 10} more"
    
    reqs_text = requirements if requirements else "None"
    if len(reqs_text) > 200:
        reqs_text = reqs_text[:200] + "..."
    
    keyboard = {"inline_keyboard": []}

    if status == "active":
        keyboard["inline_keyboard"].append(
            [{"text": "🛑 Stop Bot",    "callback_data": f"stop_deploy_{dep_id}"},
             {"text": "🔄 Restart",     "callback_data": f"restart_deploy_{dep_id}"}])

    elif status == "paused":
        if is_free:
            keyboard["inline_keyboard"].append(
                [{"text": "🔄 Restart (Free 24h)", "callback_data": f"restart_deploy_{dep_id}"}])
        else:
            keyboard["inline_keyboard"].append(
                [{"text": "⭐ Reactivate Premium",   "callback_data": f"reactivate_premium_{dep_id}"},
                 {"text": "🆓 Continue Free (24h)", "callback_data": f"continue_as_free_{dep_id}"}])

    elif status in ("stopped", "failed"):
        if is_free:
            keyboard["inline_keyboard"].append(
                [{"text": "🔄 Restart Bot (Free 24h)", "callback_data": f"restart_deploy_{dep_id}"}])
        else:
            # Premium deployment stopped — offer both paths
            keyboard["inline_keyboard"].append(
                [{"text": "⭐ Reactivate Premium",   "callback_data": f"reactivate_premium_{dep_id}"}])
            keyboard["inline_keyboard"].append(
                [{"text": "🆓 Continue Free (24h)", "callback_data": f"continue_as_free_{dep_id}"}])
    
    keyboard["inline_keyboard"].append([{"text": "📋 View Install Logs", "callback_data": f"view_install_logs_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "📄 View Runtime Logs", "callback_data": f"view_runtime_logs_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🗑️ Delete Deployment", "callback_data": f"delete_deploy_{dep_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "my_deployments"}])
    keyboard["inline_keyboard"].append([{"text": "🏠 Menu", "callback_data": "main_menu"}])
    
    error_text = f"\n\n*❌ Error:*\n`{error_log[:500]}`" if error_log else ""
    
    if is_free and not is_user_premium(user_id) and not is_admin(user_id):
        used = get_free_deployment_used_count(user_id)
        remaining_slots = FREE_USER_MAX_DEPLOYMENTS - used
        free_status_text = f"\n\n*🆓 Free Slots Left:* `{remaining_slots}/{FREE_USER_MAX_DEPLOYMENTS}`"
    else:
        free_status_text = ""
    
    text = (
        f"*📄 DEPLOYMENT #{dep_id}*\n\n"
        f"📁 File: `{display_filename(fname)}` ({size_str})\n"
        f"🔧 Framework: `{framework}`\n"
        f"📋 Plan: `{plan.upper()}`\n"
        f"💰 Cost: {cost_text}\n"
        f"📅 Started: `{start_time.strftime('%Y-%m-%d %H:%M')}`\n"
        f"⏰ Expires: `{'Never (lifetime)' if is_lifetime else expire_time.strftime('%Y-%m-%d %H:%M')}`\n"
        f"📊 Remaining: `{remaining_text}`\n"
        f"🔘 Status: {status_emoji}\n"
        f"📦 Requirements:\n`{reqs_text}`\n\n"
        f"🔧 Environment Variables: {len(env_vars)}\n{env_text}{error_text}{free_status_text}"
    )
    edit_message(chat_id, message_id, text, keyboard)

# ========== HANDLERS ==========
def build_main_menu_text(user_id, first_name=None) -> str:
    """Default main-menu header: name, plan, resources, coins, projects."""
    row = db_execute("SELECT username, first_name, premium_plan, premium_expires "
                     "FROM users WHERE user_id = ?", (user_id,), fetch='one')
    username, db_first, premium_plan, premium_expires = row if row else (None, None, None, None)
    display = f"@{username}" if username else (first_name or db_first or "User")
    display = escape_md_v1(display)

    # Plan
    if is_admin(user_id):
        plan_line = "👑 ADMIN (unlimited)"
    elif is_user_premium(user_id):
        days_left = ""
        try:
            d = (datetime.fromisoformat(premium_expires) - datetime.now()).days
            days_left = f" · {max(0, d)}d left"
        except Exception:
            pass
        plan_line = f"⭐ PREMIUM ({(premium_plan or 'monthly').capitalize()}){days_left}"
    else:
        used_free = get_free_deployment_used_count(user_id)
        plan_line = f"🆓 FREE ({used_free}/{FREE_USER_MAX_DEPLOYMENTS} slots)"

    # Resources
    used_mb = get_user_resource_usage_mb(user_id)
    quota_mb = get_user_quota_mb(user_id)
    pct = min(100.0, (used_mb / quota_mb * 100) if quota_mb else 0)
    filled = int(pct / 100 * 10)
    bar = "█" * filled + "░" * (10 - filled)
    n_users = count_quota_users()
    share_note = ("whole pool" if is_admin(user_id)
                  else f"pool {format_resource(get_resource_pool_mb())} ÷ {n_users} user(s)")

    # Coins & projects
    bal = get_user_balances(user_id)
    counts = {}
    for status, n in (db_execute("SELECT status, COUNT(*) FROM deployments "
                                 "WHERE user_id = ? GROUP BY status",
                                 (user_id,), fetch='all') or []):
        counts[status] = n
    total_proj = sum(counts.values())
    active = counts.get('active', 0)
    other = total_proj - active

    return (
        f"*🤖 BOT HOSTING*\n\n"
        f"👤 *{display}*\n"
        f"🎫 Plan: {plan_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💾 *Resources:* `{format_resource(used_mb)} / {format_resource(quota_mb)}`\n"
        f"`{bar}` {pct:.0f}%\n"
        f"_{share_note}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Coins: `{bal['coins']}`   ⭐ Stars: `{bal['stars']}`\n"
        f"📦 Projects: `{total_proj}` (🟢 `{active}` running · ⏸️ `{other}` stopped/paused)\n\n"
        f"Choose an option:"
    )


def handle_start(chat_id, user_id, username, first_name, start_param=""):
    is_new = False
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    is_new = c.fetchone() is None
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) VALUES (?, ?, ?, ?)",
              (user_id, username, first_name, datetime.now().isoformat()))
    c.execute("UPDATE users SET last_active = ?, username = ?, first_name = ? WHERE user_id = ?",
              (datetime.now().isoformat(), username, first_name, user_id))
    conn.commit()
    conn.close()

    # ── Referral processing ──────────────────────────────────────────
    if is_new and start_param and start_param.startswith("ref"):
        try:
            referrer_id = int(start_param[3:])  # "ref7713987088" → 7713987088
            credited = process_referral(referrer_id, user_id)
            if credited:
                # Notify referrer
                send_message(referrer_id,
                    f"🎉 *Referral Bonus!*\n\n"
                    f"Someone joined using your referral link!\n"
                    f"You earned *{REFERRAL_REWARD_COINS} 🪙* coins.\n\n"
                    f"Keep sharing to earn more!",
                    {"inline_keyboard": [[{"text": "👥 My Referrals", "callback_data": "my_referral"}]]})
        except Exception as _re:
            print(f"⚠️ Referral processing error: {_re}")
    # ─────────────────────────────────────────────────────────────────

    if is_new:
        async_backup(f"new_user_{user_id}")
    # ─────────────────────────────────────────────────────────────────

    update_system_stats()
    get_or_create_referral_code(user_id)   # eagerly create code so it's ready

    if is_user_verified(user_id):
        if not has_accepted_tos(user_id):
            show_tos_prompt(chat_id, user_id)
            return

        welcome = build_main_menu_text(user_id, first_name)
        send_message(chat_id, welcome, get_main_menu(user_id))
    else:
        send_verification_required(chat_id, user_id, first_name)

def handle_balance(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    balances = get_user_balances(user_id)
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT total_coins_earned, total_coins_spent, total_stars_earned, total_stars_spent FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    is_premium = is_user_premium(user_id)
    used_free = get_free_deployment_used_count(user_id)
    free_remaining = FREE_USER_MAX_DEPLOYMENTS - used_free
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "main_menu"}]]}
    premium_badge = "⭐ PREMIUM ⭐" if is_premium else "🆓 FREE"
    
    text = (
        f"*💰 YOUR BALANCE*\n\n"
        f"🪙 Coins: `{balances['coins']}`\n"
        f"⭐ Stars: `{balances['stars']}`\n"
        f"🎫 Status: {premium_badge}\n"
        f"🆓 Free Slots: `{free_remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n"
        f"🪙 Earned: `{row[0] if row else 0}`\n"
        f"🪙 Spent: `{row[1] if row else 0}`\n"
        f"⭐ Earned: `{row[2] if row else 0}`\n"
        f"⭐ Spent: `{row[3] if row else 0}`\n\n"
        f"*Premium Pricing:*\n"
        f"📅 Monthly: `{PRICE_MONTHLY_STARS}⭐` / `{PRICE_MONTHLY_COINS}🪙`\n"
        f"🌟 Yearly: `{PRICE_YEARLY_STARS}⭐` / `{PRICE_YEARLY_COINS}🪙`"
    )
    
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def handle_redeem(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    set_user_step(user_id, 'awaiting_redeem', waiting_for_redeem=1)
    send_message(chat_id,
        f"*🎫 REDEEM CODE*\n\nSend your code:",
        {"inline_keyboard": [[{"text": "🔙 Cancel", "callback_data": "main_menu"}]]})

def process_redeem(chat_id, user_id, code):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User")
        return
    
    success, message = redeem_code(user_id, code.upper().strip())
    if success:
        balances = get_user_balances(user_id)
        message += f"\n\n🪙 `{balances['coins']}` | ⭐ `{balances['stars']}`"
    
    send_message(chat_id, message, {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
    set_user_step(user_id, None, waiting_for_redeem=0)

def handle_free_deployment(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    can_deploy, reason = can_use_free_deployment(user_id)
    
    if not can_deploy:
        if message_id:
            edit_message(chat_id, message_id,
                f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited free deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        else:
            send_message(chat_id,
                f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited free deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
        return
    
    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        _qt = quota_exceeded_text(_q_used, _q_total)
        _qkb = {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]}
        if message_id:
            edit_message(chat_id, message_id, _qt, _qkb)
        else:
            send_message(chat_id, _qt, _qkb)
        return

    set_user_step(user_id, 'awaiting_file', plan='free', duration=FREE_DEPLOYMENT_DURATION_HOURS,
                  cost_coins=0, cost_stars=0, payment_method='none')
    
    text = (
        f"*🆓 FREE DEPLOYMENT*\n\n"
        f"⏱️ Duration: `{FREE_DEPLOYMENT_DURATION_HOURS}` hours\n"
        f"💰 Cost: FREE\n"
        f"📊 Free slots left: {reason}\n"
        f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
        f"📤 *Send your Python file (.py)*\n\n"
        f"After sending the file, send:\n"
        f"• requirements.txt file (optional)\n"
        f"• Environment variables (KEY=VALUE, one per line)"
    )
    
    if message_id:
        edit_message(chat_id, message_id, text,
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
    else:
        send_message(chat_id, text,
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})

def handle_paid_deployment(chat_id, user_id, message_id, plan, duration, cost_coins, cost_stars):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return

    if plan == "lifetime" and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return
    
    _q_ok, _q_used, _q_total = check_user_quota(user_id)
    if not _q_ok:
        edit_message(chat_id, message_id, quota_exceeded_text(_q_used, _q_total),
                     {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        return

    set_user_step(user_id, 'awaiting_file', plan=plan, duration=duration,
                  cost_coins=cost_coins, cost_stars=cost_stars, payment_method=None)
    
    if plan == "lifetime":
        text = (
            f"*♾️ LIFETIME DEPLOYMENT (ADMIN)*\n\n"
            f"⏱️ Duration: `Never expires`\n"
            f"💰 Cost: FREE\n\n"
            f"📤 *Send your file (.py, .js, etc.)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt / package.json (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"*Note:* All environment variables will be available via os.environ.get('KEY')"
        )
    elif premium_covers_plan(user_id, plan):
        text = (
            f"*✨ PREMIUM BENEFIT!*\n\n"
            f"Your {plan.upper()} deployment is *FREE* as a premium member!\n\n"
            f"📤 *Send your Python file (.py)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt file (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"*Note:* All environment variables will be available via os.environ.get('KEY')"
        )
    else:
        text = (
            f"*💰 {plan.capitalize()} DEPLOYMENT*\n\n"
            f"⏱️ Duration: `{duration}` days\n"
            f"⭐ Cost: `{cost_stars}⭐`\n"
            f"🪙 Cost: `{cost_coins}🪙`\n\n"
            f"📤 *Send your Python file (.py)*\n"
            f"📦 Max size: `{MAX_FILE_SIZE_MB}MB`\n\n"
            f"After sending the file, send:\n"
            f"• requirements.txt file (optional)\n"
            f"• Environment variables (KEY=VALUE, one per line)\n\n"
            f"*Note:* All environment variables will be available via os.environ.get('KEY')"
        )
    
    edit_message(chat_id, message_id, text,
                {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "deploy_new"}]]})

def handle_deployments_list(chat_id, user_id, message_id=None):
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deployment_id, file_name, file_size, plan, expire_time, status, is_free, payment_method, is_paused, framework FROM deployments WHERE user_id = ? ORDER BY deployment_id DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        keyboard = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "main_menu"}]]}
        if message_id:
            edit_message(chat_id, message_id, "📭 *No Deployments*", keyboard)
        else:
            send_message(chat_id, "📭 *No Deployments*", keyboard)
        return
    
    if not is_user_premium(user_id) and not is_admin(user_id):
        used = get_free_deployment_used_count(user_id)
        remaining = FREE_USER_MAX_DEPLOYMENTS - used
        header = f"📦 *Your Deployments*\n\n🆓 Free slots: `{remaining}/{FREE_USER_MAX_DEPLOYMENTS}`\n\n"
    else:
        header = "📦 *Your Deployments*\n\n"
    
    keyboard = {"inline_keyboard": []}
    for dep_id, fname, fsize, plan, exp_str, status, is_free, payment, is_paused, framework in rows:
        if exp_str is None:
            remaining_text = "♾️"
        else:
            exp_time = datetime.fromisoformat(exp_str)
            if is_free:
                remaining = (exp_time - datetime.now()).total_seconds() / 3600
                remaining_text = f"{int(remaining)}h"
            else:
                remaining = (exp_time - datetime.now()).days
                remaining_text = f"{remaining}d"
        
        if status == "active":
            status_icon = "✅"
        elif status == "paused":
            status_icon = "⏸️"
        elif status == "stopped":
            status_icon = "🛑"
        elif status == "failed":
            status_icon = "❌"
        else:
            status_icon = "❓"
        
        icon = "🆓" if is_free else ("⭐" if payment == "stars" else "🪙")
        if payment == "premium_free":
            icon = "✨"
        if is_paused:
            icon = "⏸️"
        
        # Framework icon
        if 'telegram' in framework:
            fw_icon = "🤖"
        elif 'discord' in framework:
            fw_icon = "🎮"
        elif 'flask' in framework or 'fastapi' in framework:
            fw_icon = "🌐"
        else:
            fw_icon = "🐍"
        
        size_str = format_file_size(fsize) if fsize else "Unknown"
        clean_fname = display_filename(fname)
        keyboard["inline_keyboard"].append([{"text": f"{icon}{status_icon}{fw_icon} ID:{dep_id} - {clean_fname[:20]} ({size_str}) [{remaining_text}]", 
                         "callback_data": f"view_deploy_{dep_id}"}])
    
    keyboard["inline_keyboard"].append([{"text": "🔙 Back", "callback_data": "main_menu"}])
    
    if message_id:
        edit_message(chat_id, message_id, header, keyboard)
    else:
        send_message(chat_id, header, keyboard)

# ==================== MENU FUNCTIONS ====================
def get_main_menu(user_id):
    balances  = get_user_balances(user_id)
    is_verified = is_user_verified(user_id)
    is_premium  = is_user_premium(user_id)
    verified_badge = "✅" if is_verified else "🔐"
    premium_badge  = "⭐" if is_premium  else "🆓"

    # Count open bug reports for admin badge
    open_reports = 0
    if is_admin(user_id):
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            open_reports = conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE status='open'").fetchone()[0]
            conn.close()
        except Exception:
            pass

    keyboard = {
        "inline_keyboard": [
            [{"text": f"{verified_badge} Join Channel",         "callback_data": "check_verification"}],
            [{"text": "📤 Deploy New Bot",                       "callback_data": "deploy_new"}],
            [{"text": "🐙 Deploy from GitHub",                   "callback_data": "github_deploy"}],
            [{"text": f"{premium_badge} Free Deployment (24h)", "callback_data": "free_deployment"}],
            [{"text": "📦 My Deployments",                      "callback_data": "my_deployments"}],
            [{"text": f"💰 {balances['coins']}🪙 | {balances['stars']}⭐", "callback_data": "my_balance"}],
            [{"text": "🎫 Redeem Code",                         "callback_data": "redeem_code"},
             {"text": "👥 Referral",                             "callback_data": "my_referral"}],
            [{"text": "⭐ Premium Subscription",                "callback_data": "subscribe_premium"},
             {"text": "🐛 Report Bug",                          "callback_data": "report_bug"}],
        ]
    }

    if is_admin(user_id):
        badge = f" ({open_reports} open)" if open_reports else ""
        keyboard["inline_keyboard"].append(
            [{"text": f"🔧 Admin Panel{badge}", "callback_data": "admin_panel"}])

    return keyboard

def get_deploy_menu(user_id):
    is_premium = is_user_premium(user_id)
    covers_monthly = premium_covers_plan(user_id, "monthly")
    covers_yearly  = premium_covers_plan(user_id, "yearly")

    if covers_monthly or covers_yearly:
        monthly_label = ("📅 Monthly (30 days) - FREE for Premium" if covers_monthly
                         else f"📅 Monthly (30 days) - {PRICE_MONTHLY_STARS}⭐ / {PRICE_MONTHLY_COINS}🪙")
        yearly_label  = ("🌟 Yearly (365 days) - FREE for Premium" if covers_yearly
                         else f"🌟 Yearly (365 days) - {PRICE_YEARLY_STARS}⭐ / {PRICE_YEARLY_COINS}🪙")
        rows = [
            [{"text": monthly_label, "callback_data": "plan_monthly"}],
            [{"text": yearly_label,  "callback_data": "plan_yearly"}],
            [{"text": "🆓 Free Deployment (24h)", "callback_data": "free_deployment"}],
        ]
        if not covers_yearly:
            rows.append([{"text": "⭐ Upgrade to Yearly Premium", "callback_data": "subscribe_premium"}])
        rows.append([{"text": "🔙 Back to Menu", "callback_data": "main_menu"}])
        keyboard = {"inline_keyboard": rows}
    else:
        keyboard = {
            "inline_keyboard": [
                [{"text": f"📅 Monthly (30 days) - {PRICE_MONTHLY_STARS}⭐ / {PRICE_MONTHLY_COINS}🪙", "callback_data": "plan_monthly"}],
                [{"text": f"🌟 Yearly (365 days) - {PRICE_YEARLY_STARS}⭐ / {PRICE_YEARLY_COINS}🪙", "callback_data": "plan_yearly"}],
                [{"text": "🆓 Free Deployment (24h)", "callback_data": "free_deployment"}],
                [{"text": "⭐ Get Premium for FREE Monthly/Yearly", "callback_data": "subscribe_premium"}],
                [{"text": "🔙 Back to Menu", "callback_data": "main_menu"}]
            ]
        }

    # ── Lifetime deployment: admins only ────────────────────────────
    if is_admin(user_id):
        keyboard["inline_keyboard"].insert(
            -1, [{"text": "♾️ Lifetime (never expires) — ADMIN", "callback_data": "plan_lifetime"}])

    return keyboard

def get_reqs_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📦 Send requirements.txt", "callback_data": "reqs_yes"}],
            [{"text": "⚡ Auto-detect & Skip", "callback_data": "reqs_no"}],
            [{"text": "❌ Cancel Deployment", "callback_data": "cancel_deploy"}]
        ]
    }

def get_env_keyboard(env_count):
    return {
        "inline_keyboard": [
            [{"text": f"📝 View Variables ({env_count})", "callback_data": "view_env"}],
            [{"text": "📤 Send KEY=VALUE (one per line)", "callback_data": "env_send"}],
            [{"text": "⏭️ SKIP - No Environment Variables", "callback_data": "env_skip"}],
            [{"text": "✅ DEPLOY NOW", "callback_data": "env_done"}],
            [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]
        ]
    }

# ==================== ADMIN PANEL ====================
def scan_orphaned_processes():
    """
    Find processes that clearly belong to this hosting platform (their
    command line references a path under DEPLOYMENTS_DIR) but aren't the
    currently-tracked proc_pid for any active deployment. These are almost
    always leftovers from before process-group isolation was added — a
    stopped/restarted/redeployed bot's actual process, orphaned because
    only its launcher's single PID was ever killed, not the whole tree.
    Deliberately conservative: only matches on the DEPLOYMENTS_DIR path
    appearing in the process's own command line, never touches anything
    else, and always excludes this very process.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT proc_pid FROM deployments WHERE proc_pid IS NOT NULL")
    tracked_pids = {row[0] for row in c.fetchall()}
    conn.close()

    own_pid = os.getpid()
    orphans = []
    deployments_dir_str = str(DEPLOYMENTS_DIR)

    try:
        pid_dirs = [d for d in os.listdir('/proc') if d.isdigit()]
    except Exception:
        return []

    for pid_str in pid_dirs:
        pid = int(pid_str)
        if pid in tracked_pids or pid == own_pid:
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\x00', b' ').decode(errors='ignore').strip()
        except Exception:
            continue
        if deployments_dir_str not in cmdline:
            continue

        ram_mb = 0
        try:
            with open(f'/proc/{pid}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        ram_mb = int(line.split()[1]) / 1024
                        break
        except Exception:
            pass

        orphans.append({'pid': pid, 'cmdline': cmdline[:120], 'ram_mb': ram_mb})

    return orphans


def admin_scan_orphans_view(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    edit_message(chat_id, message_id, "🧹 Scanning for orphaned processes...")

    orphans = scan_orphaned_processes()

    if not orphans:
        edit_message(chat_id, message_id,
            "✅ No orphaned processes found — nothing to clean up.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_resources"}]]})
        return

    total_ram = sum(o['ram_mb'] for o in orphans)
    text = (f"🧹 *Found {len(orphans)} orphaned process(es)* using "
            f"~`{total_ram:.0f}MB` RAM combined:\n\n")
    for o in orphans[:15]:
        text += f"• PID `{o['pid']}` — `{o['ram_mb']:.0f}MB`\n  `{o['cmdline'][:70]}`\n"
    if len(orphans) > 15:
        text += f"\n… and {len(orphans) - 15} more"
    text += ("\n\nThese are process trees left behind by stop/restart/redeploy actions "
             "from before process-group isolation was in place. Safe to kill — they're "
             "not attached to any currently-tracked deployment.")

    edit_message(chat_id, message_id, text,
        {"inline_keyboard": [
            [{"text": f"🗑️ Kill All {len(orphans)} Orphans", "callback_data": "admin_kill_orphans"}],
            [{"text": "🔙 Back", "callback_data": "admin_resources"}]]})


def admin_kill_orphans_action(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    orphans = scan_orphaned_processes()
    killed = 0
    for o in orphans:
        try:
            kill_deployment_process(o['pid'], signal.SIGKILL)
            killed += 1
        except Exception:
            pass

    edit_message(chat_id, message_id,
        f"✅ Killed {killed}/{len(orphans)} orphaned process(es).\n\n"
        f"Give it a few seconds, then check *📊 Resource Usage* again.",
        {"inline_keyboard": [[{"text": "📊 Resource Usage", "callback_data": "admin_resources"}]]})


def admin_resources_view(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return

    edit_message(chat_id, message_id, "📊 Measuring resource usage...")

    sys_res = get_system_resources(sample_seconds=0.5)

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deployment_id, user_id, file_name, proc_pid FROM deployments "
              "WHERE status = 'active' AND proc_pid IS NOT NULL")
    active = c.fetchall()
    conn.close()

    per_deploy = []
    for dep_id, owner_id, fname, pid in active:
        usage = get_deployment_resource_usage(pid)
        if usage:
            per_deploy.append((dep_id, owner_id, fname, usage['ram_mb'], usage['cpu_percent']))

    # Heaviest RAM users first — the ones most likely to matter if something's
    # starving other bots for resources.
    per_deploy.sort(key=lambda x: x[3], reverse=True)

    bar_len = 20
    def _bar(pct):
        filled = int(pct / 100 * bar_len)
        return '█' * filled + '░' * (bar_len - filled)

    text = (
        f"*📊 SYSTEM RESOURCE USAGE*\n\n"
        f"🖥️ CPU: `{sys_res['cpu_percent']:.1f}%` ({sys_res['cpu_count']} core(s))\n"
        f"`{_bar(sys_res['cpu_percent'])}`\n\n"
        f"💾 RAM: `{sys_res['ram_used_mb']:.0f} / {sys_res['ram_total_mb']:.0f} MB` "
        f"(`{sys_res['ram_percent']:.1f}%`)\n"
        f"`{_bar(sys_res['ram_percent'])}`\n\n"
        f"⚖️ Load average: `{sys_res['load_avg'][0]:.2f}, {sys_res['load_avg'][1]:.2f}, "
        f"{sys_res['load_avg'][2]:.2f}`\n"
        f"🤖 Active deployments: `{len(active)}`\n"
    )

    if per_deploy:
        text += f"\n*Top RAM usage by deployment:*\n"
        for dep_id, owner_id, fname, ram_mb, cpu_pct in per_deploy[:10]:
            cpu_str = f"{cpu_pct:.1f}%" if cpu_pct is not None else "n/a"
            text += f"• #{dep_id} `{display_filename(fname)[:25]}` — `{ram_mb:.0f}MB` / CPU `{cpu_str}`\n"

    if sys_res['source'] == 'proc':
        text += f"\n_ℹ️ Per-deployment CPU% unavailable without psutil (RAM still accurate)_"

    edit_message(chat_id, message_id, text,
        {"inline_keyboard": [
            [{"text": "🧹 Scan for Orphaned Processes", "callback_data": "admin_scan_orphans"}],
            [{"text": "🔄 Refresh", "callback_data": "admin_resources"}],
            [{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})


def admin_database_menu(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    try:
        size_kb = DATABASE_FILE.stat().st_size / 1024 if DATABASE_FILE.exists() else 0
    except Exception:
        size_kb = 0
    gh_status = "✅ Configured" if GITHUB_ENABLED else "❌ Not configured"
    last_backup = (f"{int(datetime.now().timestamp() - _last_backup_time)}s ago"
                   if _last_backup_time else "never (this run)")
    text = (
        f"*💾 DATABASE BACKUP*\n\n"
        f"📦 Current size: `{size_kb:.1f} KB`\n"
        f"☁️ GitHub backup: {gh_status}\n"
        f"🕐 Last GitHub push: `{last_backup}`\n\n"
        f"• *Backup Now* — pushes the current database to GitHub immediately.\n"
        f"• *Download* — sends you the raw `.db` file here in chat.\n"
        f"• *Restore* — replaces the live database with a `.db` file you "
        f"upload. A safety copy of the current database is made first."
    )
    keyboard = {"inline_keyboard": [
        [{"text": "🔄 Backup to GitHub Now", "callback_data": "admin_db_backup_now"}],
        [{"text": "⬇️ Download DB File",      "callback_data": "admin_db_download"}],
        [{"text": "⬆️ Restore from File",     "callback_data": "admin_db_restore_start"}],
        [{"text": "🔙 Back to Admin Panel",   "callback_data": "admin_panel"}],
    ]}
    edit_message(chat_id, message_id, text, keyboard)


def admin_db_backup_now(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    if not GITHUB_ENABLED:
        edit_message(chat_id, message_id,
            "❌ GitHub backup isn't configured.\n\n"
            "Set `GITHUB_TOKEN`, `GITHUB_REPO_OWNER`, and `GITHUB_REPO_NAME` to enable it.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
        return
    edit_message(chat_id, message_id, "🔄 Backing up to GitHub...")
    ok = github_backup_db(reason=f"manual_admin_{user_id}", force=True)
    if ok:
        edit_message(chat_id, message_id,
            "✅ *Backup complete!*\n\nThe database was pushed to GitHub successfully.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
    else:
        edit_message(chat_id, message_id,
            "❌ *Backup failed.*\n\nCheck the server logs (invalid token, repo not "
            "found, or the database has no data yet are the usual causes).",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})


def admin_db_download(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    if not DATABASE_FILE.exists():
        edit_message(chat_id, message_id, "❌ No database file found.",
                     {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
        return

    size_mb = DATABASE_FILE.stat().st_size / (1024 * 1024)
    if size_mb > 50:
        # Telegram's bot API hard-caps sendDocument at 50MB regardless of
        # this platform's own MAX_FILE_SIZE_MB setting — without this check
        # a too-large DB just fails silently with a generic error and no
        # way to tell what actually went wrong.
        edit_message(chat_id, message_id,
            f"❌ *Database is too large to send via Telegram* ({size_mb:.1f} MB).\n\n"
            f"Telegram's bot API caps file uploads at 50 MB. Use "
            f"*🔄 Backup to GitHub Now* instead, or download it directly "
            f"from the server's disk.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
        return

    edit_message(chat_id, message_id, "⬇️ Preparing your database file...")
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    ts_display = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # no underscores — safe for Markdown captions
    result = send_document(chat_id, DATABASE_FILE,
                            caption=f"💾 Database backup — {ts_display}",
                            filename=f"hosting_bot_backup_{ts}.db")
    if result and result.get('ok'):
        edit_message(chat_id, message_id, "✅ Database file sent above.",
                     {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})
    else:
        err_detail = ""
        if isinstance(result, dict):
            err_detail = f"\n\nTelegram said: `{result.get('description', 'unknown error')}`"
        edit_message(chat_id, message_id,
            f"❌ Failed to send the database file ({size_mb:.1f} MB).{err_detail}\n\n"
            f"Check the server console for a `send_document` error line.",
            {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_database"}]]})


def admin_db_restore_start(chat_id, message_id, user_id):
    if not is_admin(user_id):
        return
    set_user_step(user_id, 'awaiting_db_restore')
    edit_message(chat_id, message_id,
        "*⬆️ RESTORE DATABASE*\n\n"
        "Send the `.db` file to restore.\n\n"
        "⚠️ This replaces ALL current data (users, deployments, coins, "
        "everything) with the contents of the file you send. A safety copy "
        "of the current database is made automatically before the swap.\n\n"
        "Send the file now, or cancel below.",
        {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_database"}]]})


def handle_db_restore_upload(message, user_id, chat_id):
    """Admin uploaded a .db file in response to admin_db_restore_start."""
    if not is_admin(user_id):
        return

    doc = message['document']
    file_size = doc.get('file_size', 0)
    if file_size > 200 * 1024 * 1024:  # sanity cap — a real DB shouldn't be this big
        send_message(chat_id, "❌ File too large to be a plausible database restore (>200MB).")
        return

    file_id = doc['file_id']
    file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
    if not (file_info and file_info.get('ok')):
        send_message(chat_id, "❌ Could not download the file from Telegram.")
        return

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
    try:
        with urllib.request.urlopen(file_url, timeout=60) as resp:
            new_db_bytes = resp.read()
    except Exception as e:
        send_message(chat_id, f"❌ Download failed: {e}")
        return

    # Validate it's actually a SQLite database before touching anything live.
    if not new_db_bytes.startswith(b"SQLite format 3\x00"):
        send_message(chat_id,
            "❌ That file doesn't look like a valid SQLite database (missing "
            "the SQLite file header). Restore aborted — nothing was changed.")
        return
    if len(new_db_bytes) < 1024:
        send_message(chat_id, "❌ That file is suspiciously small to be a real database. Restore aborted.")
        return

    set_user_step(user_id, None)

    # Safety net: snapshot the CURRENT database before overwriting it — both
    # locally and (if configured) to GitHub — so a bad restore is recoverable
    # and no in-flight information gets lost without a way back.
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    safety_path = DATABASE_FILE.parent / f"pre_restore_backup_{ts}.db"
    try:
        if DATABASE_FILE.exists():
            shutil.copy2(DATABASE_FILE, safety_path)
    except Exception as e:
        send_message(chat_id, f"⚠️ Could not create a local safety copy ({e}) — continuing anyway.")

    if GITHUB_ENABLED:
        github_backup_db(reason=f"pre_restore_safety_{user_id}", force=True)

    try:
        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DATABASE_FILE, "wb") as f:
            f.write(new_db_bytes)
    except Exception as e:
        send_message(chat_id, f"❌ Restore failed while writing the file: {e}")
        return

    # Re-run migrations in case the uploaded DB predates newer columns.
    try:
        init_db()
    except Exception as e:
        send_message(chat_id, f"⚠️ Database restored, but schema migration hit an error: {e}")

    try:
        conn = sqlite3.connect(DATABASE_FILE)
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        deploys_count = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        conn.close()
        send_message(chat_id,
            f"✅ *Database restored successfully!*\n\n"
            f"👥 Users: `{users_count}`\n"
            f"📦 Deployments: `{deploys_count}`\n\n"
            f"A safety copy of the previous database was kept as "
            f"`{safety_path.name}` on disk"
            + (" and pushed to GitHub." if GITHUB_ENABLED else "."),
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    except Exception as e:
        send_message(chat_id,
            f"⚠️ Database file was replaced, but it doesn't look like a valid "
            f"hosting-bot database (schema check failed: {e}). "
            f"Your previous database is saved at `{safety_path.name}`.",
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})


def show_admin_panel(chat_id, message_id):
    stats = get_system_stats()

    # Bug report counts
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        open_bugs    = conn.execute("SELECT COUNT(*) FROM bug_reports WHERE status='open'").fetchone()[0]
        total_bugs   = conn.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
        total_refs   = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        top_referrer = conn.execute(
            "SELECT referrer_id, COUNT(*) c FROM referrals GROUP BY referrer_id ORDER BY c DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception:
        open_bugs = total_bugs = total_refs = 0
        top_referrer = None

    stats_text = (
        f"*📊 System Stats*\n\n"
        f"👥 Users: `{stats.get('total_users', 0)}`\n"
        f"📦 Deployments: `{stats.get('total_deployments', 0)}`\n"
        f"🟢 Active: `{stats.get('active_deployments', 0)}`\n"
        f"⏸️ Paused: `{stats.get('paused_deployments', 0)}`\n"
        f"🆓 Free: `{stats.get('free_deployments', 0)}`\n"
        f"⭐ Premium: `{stats.get('premium_users', 0)}`\n"
        f"💰 Revenue: `${stats.get('revenue_usd', 0):.2f}`\n"
        f"🪙 Coins Created: `{stats.get('coins_created', 0)}`\n"
        f"🐛 Bug Reports: `{open_bugs}` open / `{total_bugs}` total\n"
        f"👥 Referrals: `{total_refs}` total\n"
        + (f"🏆 Top Referrer: `{top_referrer[0]}` ({top_referrer[1]} refs)\n" if top_referrer else "")
        + f"\n━━━━━━━━━━━━━━━━━━━━━━"
    )

    open_badge = f" ({open_bugs})" if open_bugs else ""

    # Count pending security reviews
    sec_pending = 0
    try:
        conn_sp = sqlite3.connect(DATABASE_FILE, timeout=5)
        sec_pending = conn_sp.execute(
            "SELECT COUNT(*) FROM security_reviews WHERE status='pending'").fetchone()[0]
        conn_sp.close()
    except Exception:
        pass
    sec_badge = f" ({sec_pending})" if sec_pending else ""

    keyboard = {
        "inline_keyboard": [
            [{"text": f"🔒 Security Reviews{sec_badge}", "callback_data": "admin_sec_reviews"},
             {"text": f"🐛 Bug Reports{open_badge}",     "callback_data": "admin_bug_reports"}],
            [{"text": "👥 Referral Stats",               "callback_data": "admin_referral_stats"},
             {"text": "📢 Broadcast",                    "callback_data": "admin_broadcast"}],
            [{"text": "🎫 Create Redeem Code",           "callback_data": "admin_create_code"}],
            [{"text": "🪙 Add Coins",                    "callback_data": "admin_add_coins"}],
            [{"text": "📋 List Redeem Codes",            "callback_data": "admin_list_codes"}],
            [{"text": "👥 List Users",                   "callback_data": "admin_list_users"}],
            [{"text": "📊 View Subscriptions",           "callback_data": "admin_subscriptions"}],
            [{"text": "💾 Database Backup",              "callback_data": "admin_database"}],
            [{"text": "📊 Resource Usage",               "callback_data": "admin_resources"}],
            [{"text": "🔙 Back to Main Menu",            "callback_data": "main_menu"}]
        ]
    }

    edit_message(chat_id, message_id, f"*🔧 ADMIN PANEL*\n\n{stats_text}", keyboard)

def admin_list_codes(chat_id, message_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT code, coins_amount, used_count, max_uses, expires_at, is_active, created_at
                 FROM redeem_codes ORDER BY created_at DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        text = "📭 No redeem codes found."
    else:
        text = "*🎫 REDEEM CODES*\n\n"
        for code, coins, used, max_uses, expires, active, created in rows:
            status = "✅" if active else "❌"
            max_text = "∞" if max_uses == 0 else str(max_uses)
            expires_short = expires[:10] if expires else "Never"
            created_short = created[:10] if created else "Unknown"
            text += f"• `{code}` → {coins}🪙 | {used}/{max_text} | {status}\n"
            text += f"  Created: {created_short} | Expires: {expires_short}\n\n"
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)

def admin_list_users(chat_id, message_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT user_id, username, first_name, coins_balance, stars_balance, is_premium, join_date
                 FROM users ORDER BY join_date DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        text = "👥 No users found."
    else:
        text = "*👥 USERS*\n\n"
        for uid, username, first, coins, stars, premium, join_date in rows:
            premium_icon = "⭐" if premium else "🆓"
            join_short = join_date[:10] if join_date else "Unknown"
            text += f"• `{uid}` - {first or username or 'Unknown'}\n"
            text += f"  🪙{coins} ⭐{stars} | {premium_icon} | Joined: {join_short}\n\n"
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)

def admin_subscriptions(chat_id, message_id):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''SELECT s.subscription_id, s.user_id, u.first_name, s.plan, s.amount_stars, 
                        s.start_date, s.end_date, s.status
                 FROM subscriptions s
                 JOIN users u ON s.user_id = u.user_id
                 ORDER BY s.end_date DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        text = "📭 No subscriptions found."
    else:
        text = "*📋 SUBSCRIPTIONS*\n\n"
        for sub_id, uid, name, plan, stars, start_date, end_date, status in rows:
            start_short = start_date[:10] if start_date else "Unknown"
            end_short = end_date[:10] if end_date else "Unknown"
            status_icon = "✅" if status == "active" else "❌"
            text += f"• #{sub_id} - {name or uid}\n"
            text += f"  Plan: {plan.upper()} ({stars}⭐) | {status_icon}\n"
            text += f"  {start_short} → {end_short}\n\n"
    
    keyboard = {"inline_keyboard": [[{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]]}
    edit_message(chat_id, message_id, text, keyboard)

def admin_create_code(chat_id, message_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "100 🪙", "callback_data": "create_code_100"},
             {"text": "500 🪙", "callback_data": "create_code_500"}],
            [{"text": "1000 🪙", "callback_data": "create_code_1000"},
             {"text": "5000 🪙", "callback_data": "create_code_5000"}],
            [{"text": "10000 🪙", "callback_data": "create_code_10000"},
             {"text": "50000 🪙", "callback_data": "create_code_50000"}],
            [{"text": "🔙 Back to Admin", "callback_data": "admin_panel"}]
        ]
    }
    edit_message(chat_id, message_id, "*🎫 CREATE REDEEM CODE*\n\nSelect amount:", keyboard)

def ask_code_max_uses(admin_id, amount, chat_id, message_id):
    set_user_step(admin_id, 'awaiting_code_uses', temp_code_amount=amount)
    keyboard = {
        "inline_keyboard": [
            [{"text": "1 use",   "callback_data": "code_uses_1"},
             {"text": "5 uses",  "callback_data": "code_uses_5"}],
            [{"text": "10 uses", "callback_data": "code_uses_10"},
             {"text": "50 uses", "callback_data": "code_uses_50"}],
            [{"text": "100 uses", "callback_data": "code_uses_100"},
             {"text": "♾️ Unlimited", "callback_data": "code_uses_0"}],
            [{"text": "✍️ Custom number", "callback_data": "code_uses_custom"}],
            [{"text": "🔙 Back", "callback_data": "admin_create_code"}]
        ]
    }
    text = f"*🎫 CREATE REDEEM CODE*\n\n💰 Amount: `{amount}🪙`\n\nHow many times can this code be used in total?"
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def ask_code_expiry(admin_id, max_uses, chat_id, message_id):
    set_user_step(admin_id, 'awaiting_code_expiry', temp_code_max_uses=max_uses)
    keyboard = {
        "inline_keyboard": [
            [{"text": "1 day",   "callback_data": "code_expiry_1"},
             {"text": "7 days",  "callback_data": "code_expiry_7"}],
            [{"text": "30 days", "callback_data": "code_expiry_30"},
             {"text": "90 days", "callback_data": "code_expiry_90"}],
            [{"text": "365 days", "callback_data": "code_expiry_365"},
             {"text": "♾️ Never expires", "callback_data": "code_expiry_36500"}],
            [{"text": "✍️ Custom days", "callback_data": "code_expiry_custom"}],
            [{"text": "🔙 Back", "callback_data": "admin_create_code"}]
        ]
    }
    uses_label = "Unlimited" if max_uses == 0 else str(max_uses)
    text = f"*🎫 CREATE REDEEM CODE*\n\n🔢 Max uses: `{uses_label}`\n\nHow many days until this code expires?"
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def finalize_create_code(admin_id, expiry_days, chat_id, message_id):
    user_step = get_user_step(admin_id)
    amount = user_step.get('temp_code_amount')
    max_uses = user_step.get('temp_code_max_uses')

    if not amount or max_uses is None:
        err_text = "❌ Session expired. Please start over."
        err_kb = {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_create_code"}]]}
        if message_id:
            edit_message(chat_id, message_id, err_text, err_kb)
        else:
            send_message(chat_id, err_text, err_kb)
        return

    set_user_step(admin_id, None)
    code = create_redeem_code(admin_id, amount, 0, expiry_days, max_uses)

    uses_label = "Unlimited" if max_uses == 0 else f"{max_uses} use(s)"
    expiry_label = "Never expires" if expiry_days >= 36500 else f"{expiry_days} day(s)"

    text = (f"✅ *CODE CREATED!*\n\n"
            f"🎫 `{code}`\n"
            f"💰 {amount}🪙\n"
            f"📅 {expiry_label}\n"
            f"🔢 {uses_label}\n\n"
            f"Share with users!")
    keyboard = {"inline_keyboard": [[{"text": "🎫 Create Another", "callback_data": "admin_create_code"},
                                     {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def process_create_code(admin_id, amount, chat_id, message_id):
    ask_code_max_uses(admin_id, amount, chat_id, message_id)

# ==================== ADMIN ADD COINS ====================
def admin_add_coins_start(admin_id, chat_id, message_id):
    set_user_step(admin_id, 'awaiting_coins_target')
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "target_digit_1"}, {"text": "2", "callback_data": "target_digit_2"}, {"text": "3", "callback_data": "target_digit_3"}],
            [{"text": "4", "callback_data": "target_digit_4"}, {"text": "5", "callback_data": "target_digit_5"}, {"text": "6", "callback_data": "target_digit_6"}],
            [{"text": "7", "callback_data": "target_digit_7"}, {"text": "8", "callback_data": "target_digit_8"}, {"text": "9", "callback_data": "target_digit_9"}],
            [{"text": "0", "callback_data": "target_digit_0"}, {"text": "⌫", "callback_data": "target_backspace"}, {"text": "✅", "callback_data": "target_confirm"}],
            [{"text": "📋 Use My ID", "callback_data": "use_my_id_target"}],
            [{"text": "❌ Cancel", "callback_data": "admin_panel"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"*🪙 ADD COINS*\n\n"
        f"Enter the *User ID* using the number pad below:\n\n"
        f"*User ID:* ` `\n\n"
        f"Example: `123456789`",
        keyboard)

def update_target_id_display(admin_id, digit, chat_id, message_id):
    user_step = get_user_step(admin_id)
    current_target = user_step.get('temp_target_user') or ""
    
    if digit == 'backspace':
        new_target = current_target[:-1]
    else:
        new_target = current_target + str(digit)
    
    set_user_step(admin_id, 'awaiting_coins_target', temp_target_user=new_target)
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "target_digit_1"}, {"text": "2", "callback_data": "target_digit_2"}, {"text": "3", "callback_data": "target_digit_3"}],
            [{"text": "4", "callback_data": "target_digit_4"}, {"text": "5", "callback_data": "target_digit_5"}, {"text": "6", "callback_data": "target_digit_6"}],
            [{"text": "7", "callback_data": "target_digit_7"}, {"text": "8", "callback_data": "target_digit_8"}, {"text": "9", "callback_data": "target_digit_9"}],
            [{"text": "0", "callback_data": "target_digit_0"}, {"text": "⌫", "callback_data": "target_backspace"}, {"text": "✅", "callback_data": "target_confirm"}],
            [{"text": "📋 Use My ID", "callback_data": "use_my_id_target"}],
            [{"text": "❌ Cancel", "callback_data": "admin_panel"}]
        ]
    }
    
    display_target = new_target if new_target else " "
    edit_message(chat_id, message_id,
        f"*🪙 ADD COINS*\n\n"
        f"Enter the *User ID* using the number pad below:\n\n"
        f"*User ID:* `{display_target}`\n\n"
        f"Click ✅ when done.",
        keyboard)

def confirm_target_user(admin_id, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    
    if not target_user_id or not target_user_id.strip():
        edit_message(chat_id, message_id,
            f"❌ *No User ID entered!*\n\nPlease enter a user ID using the number pad.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    try:
        target_user_id = int(target_user_id)
    except ValueError:
        edit_message(chat_id, message_id,
            f"❌ *Invalid User ID!*\n\nPlease enter a valid numeric user ID.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (target_user_id,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        edit_message(chat_id, message_id,
            f"❌ *User not found!*\n\nUser ID `{target_user_id}` does not exist.\n\nPlease check and try again.",
            {"inline_keyboard": [[{"text": "🔙 Try Again", "callback_data": "admin_add_coins"}]]})
        return
    
    first_name = user[0] or "User"
    
    set_user_step(admin_id, 'awaiting_coins_amount', temp_target_user=str(target_user_id), temp_coins_amount=0)
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "coin_digit_1"}, {"text": "2", "callback_data": "coin_digit_2"}, {"text": "3", "callback_data": "coin_digit_3"}],
            [{"text": "4", "callback_data": "coin_digit_4"}, {"text": "5", "callback_data": "coin_digit_5"}, {"text": "6", "callback_data": "coin_digit_6"}],
            [{"text": "7", "callback_data": "coin_digit_7"}, {"text": "8", "callback_data": "coin_digit_8"}, {"text": "9", "callback_data": "coin_digit_9"}],
            [{"text": "0", "callback_data": "coin_digit_0"}, {"text": "⌫", "callback_data": "coin_backspace"}, {"text": "✅", "callback_data": "coin_confirm"}],
            [{"text": "100", "callback_data": "coin_preset_100"}, {"text": "500", "callback_data": "coin_preset_500"}],
            [{"text": "1000", "callback_data": "coin_preset_1000"}, {"text": "5000", "callback_data": "coin_preset_5000"}],
            [{"text": "10000", "callback_data": "coin_preset_10000"}, {"text": "50000", "callback_data": "coin_preset_50000"}],
            [{"text": "🔙 Back", "callback_data": "admin_add_coins"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"*🪙 ADD COINS*\n\n"
        f"Target user: `{target_user_id}` ({first_name})\n"
        f"Current balance: `{get_user_balances(target_user_id)['coins']}🪙`\n\n"
        f"Enter the amount of coins to add using the number pad below:\n\n"
        f"Amount: `0` 🪙",
        keyboard)

def update_coin_amount_display(admin_id, digit, chat_id, message_id):
    user_step = get_user_step(admin_id)
    current_amount = user_step.get('temp_coins_amount') or 0
    target_user_id = user_step.get('temp_target_user')
    
    if digit == 'backspace':
        new_amount = current_amount // 10
    else:
        new_amount = current_amount * 10 + digit
    
    set_user_step(admin_id, 'awaiting_coins_amount', 
                  temp_target_user=target_user_id, 
                  temp_coins_amount=new_amount)
    
    targets = _parse_coin_targets(target_user_id)
    if len(targets) == 1:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT first_name FROM users WHERE user_id = ?", (targets[0],))
        user = c.fetchone()
        conn.close()
        first_name = user[0] if user else "User"
        target_line = f"Target user: `{target_user_id}` ({first_name})\n"
        balance_line = f"Current balance: `{get_user_balances(targets[0])['coins']}🪙`\n\n"
    else:
        target_line = f"Target: *{len(targets)} users*\n"
        balance_line = ""

    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "coin_digit_1"}, {"text": "2", "callback_data": "coin_digit_2"}, {"text": "3", "callback_data": "coin_digit_3"}],
            [{"text": "4", "callback_data": "coin_digit_4"}, {"text": "5", "callback_data": "coin_digit_5"}, {"text": "6", "callback_data": "coin_digit_6"}],
            [{"text": "7", "callback_data": "coin_digit_7"}, {"text": "8", "callback_data": "coin_digit_8"}, {"text": "9", "callback_data": "coin_digit_9"}],
            [{"text": "0", "callback_data": "coin_digit_0"}, {"text": "⌫", "callback_data": "coin_backspace"}, {"text": "✅", "callback_data": "coin_confirm"}],
            [{"text": "100", "callback_data": "coin_preset_100"}, {"text": "500", "callback_data": "coin_preset_500"}],
            [{"text": "1000", "callback_data": "coin_preset_1000"}, {"text": "5000", "callback_data": "coin_preset_5000"}],
            [{"text": "10000", "callback_data": "coin_preset_10000"}, {"text": "50000", "callback_data": "coin_preset_50000"}],
            [{"text": "🔙 Back", "callback_data": "admin_add_coins"}]
        ]
    }
    
    edit_message(chat_id, message_id,
        f"*🪙 ADD COINS*\n\n"
        f"{target_line}"
        f"{balance_line}"
        f"Enter the amount of coins to add using the number pad below:\n\n"
        f"Amount: `{new_amount}` 🪙",
        keyboard)

def _parse_coin_targets(target_str):
    """
    temp_target_user holds either a single numeric ID (legacy/keypad flow)
    or a JSON list of IDs (multi-user text flow). Returns a list of ints
    either way, so every caller handles both formats identically instead
    of each re-implementing (and risking a crash on) the distinction.
    """
    if not target_str:
        return []
    try:
        parsed = json.loads(target_str)
        if isinstance(parsed, list):
            return [int(x) for x in parsed]
        return [int(parsed)]
    except (json.JSONDecodeError, ValueError, TypeError):
        try:
            return [int(target_str)]
        except (ValueError, TypeError):
            return []


def process_coin_preset(admin_id, preset_amount, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    
    if not target_user_id:
        edit_message(chat_id, message_id, "❌ Session expired. Please start over.",
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_add_coins"}]]})
        return
    
    set_user_step(admin_id, 'awaiting_coins_amount', 
                  temp_target_user=target_user_id, 
                  temp_coins_amount=preset_amount)
    
    targets = _parse_coin_targets(target_user_id)
    if len(targets) == 1:
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT first_name FROM users WHERE user_id = ?", (targets[0],))
        user = c.fetchone()
        conn.close()
        first_name = user[0] if user else "User"
    else:
        first_name = f"{len(targets)} users"
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "coin_digit_1"}, {"text": "2", "callback_data": "coin_digit_2"}, {"text": "3", "callback_data": "coin_digit_3"}],
            [{"text": "4", "callback_data": "coin_digit_4"}, {"text": "5", "callback_data": "coin_digit_5"}, {"text": "6", "callback_data": "coin_digit_6"}],
            [{"text": "7", "callback_data": "coin_digit_7"}, {"text": "8", "callback_data": "coin_digit_8"}, {"text": "9", "callback_data": "coin_digit_9"}],
            [{"text": "0", "callback_data": "coin_digit_0"}, {"text": "⌫", "callback_data": "coin_backspace"}, {"text": "✅", "callback_data": "coin_confirm"}],
            [{"text": "100", "callback_data": "coin_preset_100"}, {"text": "500", "callback_data": "coin_preset_500"}],
            [{"text": "1000", "callback_data": "coin_preset_1000"}, {"text": "5000", "callback_data": "coin_preset_5000"}],
            [{"text": "10000", "callback_data": "coin_preset_10000"}, {"text": "50000", "callback_data": "coin_preset_50000"}],
            [{"text": "🔙 Back", "callback_data": "admin_add_coins"}]
        ]
    }
    
    if len(targets) == 1:
        balance_line = f"Current balance: `{get_user_balances(targets[0])['coins']}🪙`\n\n"
        target_line = f"Target user: `{target_user_id}` ({first_name})\n"
    else:
        balance_line = ""
        target_line = f"Target: *{first_name}*\n"

    edit_message(chat_id, message_id,
        f"*🪙 ADD COINS*\n\n"
        f"{target_line}"
        f"{balance_line}"
        f"Amount preset: `{preset_amount}` 🪙\n\n"
        f"Click ✅ to confirm or continue entering digits:",
        keyboard)

def confirm_add_coins(admin_id, chat_id, message_id):
    user_step = get_user_step(admin_id)
    target_user_id = user_step.get('temp_target_user')
    amount = user_step.get('temp_coins_amount')
    
    if not target_user_id or not amount or amount <= 0:
        edit_message(chat_id, message_id, "❌ Invalid amount. Please try again.",
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_add_coins"}]]})
        return

    targets = _parse_coin_targets(target_user_id)
    if not targets:
        edit_message(chat_id, message_id, "❌ No valid target user(s). Please try again.",
                    {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_add_coins"}]]})
        return

    set_user_step(admin_id, None, temp_target_user=None, temp_coins_amount=None)

    succeeded, failed = [], []
    for uid in targets:
        try:
            update_user_coins(uid, amount, "admin_add", f"by_admin_{admin_id}")
            new_balance = get_user_balances(uid)['coins']
            succeeded.append((uid, new_balance))
        except Exception as e:
            failed.append((uid, str(e)))
            continue

        # Best-effort notification — a user who blocked the bot shouldn't
        # stop the rest of the batch from being credited.
        try:
            send_message(uid,
                f"🎉 *You received {amount} Coins!* 🎉\n\n"
                f"Your new balance: `{new_balance}🪙`\n\n"
                f"Use your coins to purchase premium subscription!",
                {"inline_keyboard": [[{"text": "⭐ Get Premium", "callback_data": "subscribe_premium"}]]})
        except Exception:
            pass

    async_backup(f"admin_coins_{admin_id}")

    if len(targets) == 1:
        if succeeded:
            uid, new_balance = succeeded[0]
            edit_message(chat_id, message_id,
                f"✅ *COINS ADDED SUCCESSFULLY!*\n\n"
                f"User: `{uid}`\n"
                f"Added: `+{amount}🪙`\n"
                f"New balance: `{new_balance}🪙`",
                {"inline_keyboard": [[{"text": "➕ Add More", "callback_data": "admin_add_coins"},
                                      {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        else:
            edit_message(chat_id, message_id,
                f"❌ Failed to add coins to `{targets[0]}`: {failed[0][1]}",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    else:
        summary = f"✅ *COINS ADDED TO {len(succeeded)}/{len(targets)} USERS!*\n\n" \
                  f"Amount each: `+{amount}🪙`\n\n"
        summary += "\n".join(f"• `{uid}` → `{bal}🪙`" for uid, bal in succeeded[:20])
        if len(succeeded) > 20:
            summary += f"\n… and {len(succeeded) - 20} more"
        if failed:
            summary += f"\n\n⚠️ Failed: " + ", ".join(f"`{uid}`" for uid, _ in failed[:10])
        edit_message(chat_id, message_id, summary,
            {"inline_keyboard": [[{"text": "➕ Add More", "callback_data": "admin_add_coins"},
                                  {"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})

# ==================== GITHUB DEPLOY HELPERS ====================

def _start_github_deploy(chat_id, user_id, message_id, step, token=None):
    """
    After we have owner/repo/branch (and optionally token):
    fetch repo info, resolve branch, ask for env vars.
    """
    owner  = step.get('temp_github_owner', '')
    repo   = step.get('temp_github_repo', '')
    branch = step.get('temp_github_branch', '') or None

    if not owner or not repo:
        send_message(chat_id, "❌ Missing repo info. Start again.",
                     {"inline_keyboard": [[{"text": "🐙 GitHub Deploy", "callback_data": "github_deploy"}]]})
        return

    # Resolve branch
    if not branch:
        send_message(chat_id, f"🔄 Fetching repo info for `{owner}/{repo}`...")
        info = get_repo_info(owner, repo, token)
        branch = info.get('default_branch', 'main')

    description = ""
    try:
        info = get_repo_info(owner, repo, token)
        description = info.get('description', '') or ''
        lang = info.get('language', '') or ''
        stars = info.get('stargazers_count', 0)
        description = f"_{description[:80]}_\n" if description else ''
    except Exception:
        lang = ''; stars = 0

    summary = (
        f"Repo:   `{owner}/{repo}`\n"
        f"Branch: `{branch}`\n"
        + (f"Lang:   `{lang}` | ⭐ {stars}\n" if lang else "")
        + description
    )

    # Save state and let the user pick a plan (same choices as a file upload)
    set_user_step(user_id, 'awaiting_github_plan',
                  temp_github_owner=owner,
                  temp_github_repo=repo,
                  temp_github_branch=branch,
                  temp_github_token=token or '',
                  temp_source='github',
                  temp_gh_summary=summary)
    _show_github_plan_menu(chat_id, user_id, message_id, summary)


def _gh_plan_prices(plan):
    """Returns (duration_days_or_0, cost_coins, cost_stars) for a paid plan."""
    if plan == 'monthly':
        return 30, PRICE_MONTHLY_COINS, PRICE_MONTHLY_STARS
    if plan == 'yearly':
        return 365, PRICE_YEARLY_COINS, PRICE_YEARLY_STARS
    return 0, 0, 0


def _show_github_plan_menu(chat_id, user_id, message_id, summary):
    """Plan picker for GitHub deployments: 24h free / monthly / yearly / lifetime (admin)."""
    def _label(plan, title, days):
        if premium_covers_plan(user_id, plan):
            return f"{title} ({days}) - FREE for Premium"
        _, coins, stars = _gh_plan_prices(plan)
        return f"{title} ({days}) - {stars}⭐ / {coins}🪙"

    rows = [
        [{"text": f"🆓 Free ({FREE_DEPLOYMENT_DURATION_HOURS}h)", "callback_data": "gh_plan_free"}],
        [{"text": _label('monthly', '📅 Monthly', '30 days'), "callback_data": "gh_plan_monthly"}],
        [{"text": _label('yearly',  '🌟 Yearly',  '365 days'), "callback_data": "gh_plan_yearly"}],
    ]
    if is_admin(user_id):
        rows.append([{"text": "♾️ Lifetime (never expires) — ADMIN", "callback_data": "gh_plan_lifetime"}])
    rows.append([{"text": "❌ Cancel", "callback_data": "main_menu"}])

    msg = f"*🐙 REPO CONFIRMED*\n\n{summary}\n*💰 Choose a deployment plan:*"
    kb = {"inline_keyboard": rows}
    if message_id:
        edit_message(chat_id, message_id, msg, kb)
    else:
        send_message(chat_id, msg, kb)


def _ask_github_env_vars(chat_id, message_id, summary, plan):
    plan_label = {'free': f"Free ({FREE_DEPLOYMENT_DURATION_HOURS}h)", 'monthly': "Monthly (30 days)",
                  'yearly': "Yearly (365 days)", 'lifetime': "Lifetime (never expires)"}.get(plan, plan)
    msg = (
        f"*🐙 REPO CONFIRMED*\n\n{summary}"
        f"📋 Plan: *{plan_label}*\n\n"
        f"✅ Now send environment variables (one per line):\n"
        f"`BOT_TOKEN=xxxx`\n`OTHER_VAR=value`\n\n"
        f"Or send a dot `.` to skip:"
    )
    kb = {"inline_keyboard": [[{"text": "⏭️ Skip (no env vars)", "callback_data": "github_skip_env"}],
                               [{"text": "❌ Cancel", "callback_data": "main_menu"}]]}
    if message_id:
        edit_message(chat_id, message_id, msg, kb)
    else:
        send_message(chat_id, msg, kb)


def _launch_github_deploy(chat_id, user_id, step, main_file_name=None, skip_security=False,
                          paid_method=None):
    """Final step: kick off the actual GitHub deployment."""
    owner  = step.get('temp_github_owner', '')
    repo   = step.get('temp_github_repo', '')
    branch = step.get('temp_github_branch', 'main')
    token  = step.get('temp_github_token') or None
    env_raw = step.get('temp_env_vars', '')

    env_vars_dict = {}
    if env_raw and env_raw.strip() != '.':
        for line in env_raw.strip().splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                k = k.strip(); v = v.strip()
                if k:
                    env_vars_dict[k] = v
    if token:
        env_vars_dict['GITHUB_DEPLOY_TOKEN'] = token

    # Plan chosen by the user in the plan picker (default: free)
    plan = step.get('temp_plan') or 'free'
    if plan not in ('free', 'monthly', 'yearly', 'lifetime'):
        plan = 'free'

    if plan == 'lifetime' and not is_admin(user_id):
        send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
        return False

    cost_coins = cost_stars = 0
    if plan == 'free':
        can_deploy, reason = can_use_free_deployment(user_id)
        if not can_deploy:
            send_message(chat_id,
                f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nUpgrade to Premium for unlimited deployments!",
                {"inline_keyboard": [[{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
            return False
        duration, payment_method, is_free = FREE_DEPLOYMENT_DURATION_HOURS, 'free', True
    else:
        duration, price_coins, price_stars = _gh_plan_prices(plan)
        is_free = False
        if plan == 'lifetime' or premium_covers_plan(user_id, plan):
            payment_method = 'premium_free'
        elif paid_method in ('coins', 'stars'):
            payment_method = paid_method
            cost_coins = price_coins if paid_method == 'coins' else 0
            cost_stars = price_stars if paid_method == 'stars' else 0
        else:
            # Needs payment first — persist everything collected so far and ask.
            set_user_step(user_id, 'awaiting_github_payment',
                          temp_env_vars=env_raw,
                          temp_main_file=main_file_name or '',
                          temp_plan=plan)
            send_message(chat_id,
                f"*💰 Choose Payment Method*\n\n"
                f"Repo: `{owner}/{repo}`\n"
                f"Plan: *{plan.upper()}* ({duration} days)\n\n"
                f"Cost: `{price_stars}⭐` or `{price_coins}🪙`",
                {"inline_keyboard": [
                    [{"text": f"⭐ Pay {price_stars} Stars", "callback_data": "gh_pay_stars"}],
                    [{"text": f"🪙 Pay {price_coins} Coins", "callback_data": "gh_pay_coins"}],
                    [{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
            return False

    set_user_step(user_id, None)
    ok = deploy_from_github(
        chat_id, user_id,
        owner, repo, branch, token,
        env_vars_dict,
        plan=plan, duration=duration,
        cost_coins=cost_coins, cost_stars=cost_stars,
        payment_method=payment_method,
        is_free=is_free, main_file_name=main_file_name,
        skip_security=skip_security)

    if not ok and paid_method == 'coins' and cost_coins:
        update_user_coins(user_id, cost_coins, "deployment_refund", f"github_{plan}")
        send_message(chat_id, f"↩️ Deployment didn't complete — `{cost_coins}🪙` refunded.")
    return ok


# ==================== BROADCAST SYSTEM ====================

def _send_broadcast_preview(chat_id, btype, file_id, caption, buttons):
    """Send a live preview of the broadcast to the admin before confirming."""
    if not isinstance(buttons, list):
        buttons = []

    confirm_kb = {"inline_keyboard": (buttons if buttons else []) + [
        [{"text": "✅ Send to All Users", "callback_data": "broadcast_confirm"}],
        [{"text": "✏️ Edit Caption",       "callback_data": "broadcast_edit_caption"}],
        [{"text": "❌ Cancel",              "callback_data": "admin_panel"}],
    ]}

    send_message(chat_id, "*👁 PREVIEW — exactly what users will see:*", None)
    try:
        if btype == 'photo' and file_id:
            _tg_send_media(chat_id, 'photo', file_id, caption or '', confirm_kb)
        elif btype == 'video' and file_id:
            _tg_send_media(chat_id, 'video', file_id, caption or '', confirm_kb)
        else:
            msg_text = caption or '⚠️ (no caption / empty message)'
            send_message(chat_id, msg_text, confirm_kb)
    except Exception as e:
        send_message(chat_id,
            f"⚠️ Preview error: `{e}`\n\n*Caption:*\n{caption or '(empty)'}",
            confirm_kb)


def _tg_send_media(chat_id, media_type, file_id, caption, keyboard=None):
    """Send a photo or video — with retry on 429 and proper UTF-8 decoding."""
    endpoint = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    field    = 'photo'    if media_type == 'photo' else 'video'

    if not file_id:
        print(f"❌ _tg_send_media: file_id is empty for {media_type}")
        return None

    _send_err.code = None
    payload = {"chat_id": chat_id, field: file_id, "parse_mode": "Markdown"}
    if caption:
        payload["caption"] = str(caption)[:1024]
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"{TELEGRAM_API}/{endpoint}",
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            _send_err.code = e.code
            if e.code == 429:
                sleep(3 * (attempt + 1))
                continue
            body = ''
            try: body = e.read().decode()[:300]
            except Exception: pass
            print(f"❌ _tg_send_media HTTP {e.code}: {body}")
            return None
        except Exception as e:
            print(f"❌ _tg_send_media error (attempt {attempt+1}): {e}")
            if attempt < 3:
                sleep(1)
    return None


def do_broadcast(admin_id, btype, file_id, caption, buttons_json):
    """
    Send a message/photo/video to all users.
    Returns (success_count, fail_count, skipped_count).
    """
    # Parse inline buttons
    try:
        buttons = json.loads(buttons_json) if buttons_json else []
        if not isinstance(buttons, list):
            buttons = []
    except Exception:
        buttons = []

    kb = {"inline_keyboard": buttons} if buttons else None

    # Validate text content before starting
    if btype == 'text' and not (caption or '').strip():
        print("⚠️ do_broadcast: text broadcast has empty caption — aborting")
        return 0, 0, 0

    if btype in ('photo', 'video') and not file_id:
        print(f"⚠️ do_broadcast: {btype} broadcast has no file_id — aborting")
        return 0, 0, 0

    # Fetch ALL users (not just verified — we want everyone who ever used the bot)
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        c = conn.cursor()
        # Get every user that has interacted (has a join_date or last_active)
        c.execute("""SELECT DISTINCT user_id FROM users
                     WHERE user_id IS NOT NULL AND user_id != 0
                     ORDER BY user_id""")
        all_users = [r[0] for r in c.fetchall()]
        conn.close()
    except Exception as e:
        print(f"❌ do_broadcast user fetch error: {e}")
        return 0, 0, 0

    total    = len(all_users)
    success  = fail = skipped = 0
    print(f"📢 Starting broadcast to {total} users | type={btype} | file_id={bool(file_id)}")

    for i, uid in enumerate(all_users):
        # Skip the admin who triggered the broadcast
        if int(uid) in ADMIN_IDS:
            skipped += 1
            continue

        try:
            text_to_send = (caption or '').strip()
            if btype == 'text' and not text_to_send:
                skipped += 1
                continue

            def _send_once():
                if btype == 'photo' and file_id:
                    return _tg_send_media(uid, 'photo', file_id, caption, kb)
                if btype == 'video' and file_id:
                    return _tg_send_media(uid, 'video', file_id, caption, kb)
                return send_message(uid, text_to_send, kb)

            # Exponential backoff with jitter, but only when the failure was
            # a 429 rate-limit / transient error — never for blocked users.
            result = None
            for _try in range(5):
                result = _send_once()
                if isinstance(result, dict) and result.get('ok'):
                    break
                _code = getattr(_send_err, 'code', None)
                if result is None and (_code == 429 or _code is None) and _try < 4:
                    sleep(min(60, (2 ** _try)) + random.uniform(0, 1))
                    continue
                break

            # Both send_message and _tg_send_media return {'ok': True/False, ...}
            if isinstance(result, dict) and result.get('ok'):
                success += 1
            else:
                fail += 1
                if result:
                    desc = result.get('description', '') if isinstance(result, dict) else str(result)
                    if 'blocked' in str(desc).lower() or 'deactivated' in str(desc).lower():
                        skipped += 1
                        fail -= 1

        except Exception as ex:
            print(f"❌ Broadcast to {uid}: {ex}")
            fail += 1

        # Conservative rate limiting: 25 msgs/s with backoff
        sleep(0.04)
        # Log progress every 50 users
        if (i + 1) % 50 == 0:
            print(f"  📢 Progress: {i+1}/{total} | ✅{success} ❌{fail} ⏭{skipped}")

    print(f"✅ Broadcast done: {success} sent | {fail} failed | {skipped} skipped")
    async_backup(f"broadcast_{admin_id}")
    return success, fail, skipped


def _run_broadcast_and_notify(chat_id, admin_id, btype, file_id, caption, btns_raw):
    """Thread target: run broadcast and notify the admin with results."""
    try:
        success, fail, skipped = do_broadcast(admin_id, btype, file_id, caption, btns_raw)
        total = success + fail
        send_message(chat_id,
            f"✅ *Broadcast Complete*\n\n"
            f"✅ Delivered: `{success}`\n"
            f"❌ Failed:    `{fail}`\n"
            f"⏭ Skipped:   `{skipped}` (blocked/admin)\n"
            f"📊 Total users: `{total + skipped}`",
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
    except Exception as e:
        print(f"❌ _run_broadcast_and_notify error: {e}")
        try:
            send_message(chat_id,
                f"❌ *Broadcast failed with error:*\n`{e}`",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        except Exception:
            pass


def _dispatch_deploy(chat_id, user_id, message_id, user_step, env_vars):
    """Central dispatch from env_done/env_skip to the correct deploy path."""
    plan         = user_step.get('plan') or 'free'
    temp_file    = user_step.get('temp_file')
    requirements = user_step.get('requirements')
    duration     = user_step.get('duration')
    cost_stars   = user_step.get('cost_stars')
    cost_coins   = user_step.get('cost_coins')

    if plan == 'free':
        set_user_step(user_id, None)
        deploy_free_bot_with_logs(chat_id, user_id, temp_file, requirements, env_vars)
    elif premium_covers_plan(user_id, plan):
        set_user_step(user_id, None)
        deploy_paid_bot(chat_id, user_id, temp_file, requirements, env_vars,
                        plan, duration, 0, 0, 'premium_free')
    else:
        # Non-premium paid plan — show payment options
        edit_message(chat_id, message_id,
            f"*💰 Choose Payment Method*\n\n"
            f"Plan: *{plan.upper()}*\n"
            f"Duration: {duration} days\n"
            f"Env vars: {len(env_vars)}\n\n"
            f"Cost: `{cost_stars}⭐` or `{cost_coins}🪙`",
            get_payment_keyboard(plan, cost_stars, cost_coins))


# ==================== CALLBACK HANDLER ====================
def handle_callback(callback):
    callback_id = callback['id']
    user_id = callback['from']['id']
    message = callback.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    message_id = message.get('message_id')
    data = callback['data']
    
    answer_callback(callback_id)
    print(f"📨 Callback: {data} from user {user_id}")
    
    # ========== MAIN MENU ==========
    if data == "main_menu":
        if is_user_verified(user_id):
            welcome = build_main_menu_text(user_id, callback['from'].get('first_name'))
            edit_message(chat_id, message_id, welcome, get_main_menu(user_id))
        else:
            user_info = get_user_info(user_id)
            send_verification_required(chat_id, user_id, user_info.get('first_name', 'User'), message_id)
        return
    
    # ========== ADMIN PANEL ==========
    if data == "admin_panel":
        if is_admin(user_id):
            show_admin_panel(chat_id, message_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return
    
    if data == "admin_list_codes":
        if is_admin(user_id):
            admin_list_codes(chat_id, message_id)
        return
    
    if data == "admin_list_users":
        if is_admin(user_id):
            admin_list_users(chat_id, message_id)
        return
    
    if data == "admin_subscriptions":
        if is_admin(user_id):
            admin_subscriptions(chat_id, message_id)
        return

    # ========== ADMIN DATABASE BACKUP/RESTORE ==========
    if data == "admin_database":
        if is_admin(user_id):
            admin_database_menu(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_resources":
        if is_admin(user_id):
            admin_resources_view(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_scan_orphans":
        if is_admin(user_id):
            admin_scan_orphans_view(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_kill_orphans":
        if is_admin(user_id):
            admin_kill_orphans_action(chat_id, message_id, user_id)
        else:
            edit_message(chat_id, message_id, "🔒 Unauthorized!")
        return

    if data == "admin_db_backup_now":
        if is_admin(user_id):
            admin_db_backup_now(chat_id, message_id, user_id)
        return

    if data == "admin_db_download":
        if is_admin(user_id):
            admin_db_download(chat_id, message_id, user_id)
        return

    if data == "admin_db_restore_start":
        if is_admin(user_id):
            admin_db_restore_start(chat_id, message_id, user_id)
        return
    
    if data == "admin_create_code":
        if is_admin(user_id):
            admin_create_code(chat_id, message_id)
        return
    
    if data.startswith("create_code_"):
        if is_admin(user_id):
            amount = int(data.split("_")[2])
            process_create_code(user_id, amount, chat_id, message_id)
        return

    if data == "code_uses_custom":
        if is_admin(user_id):
            set_user_step(user_id, 'awaiting_code_uses_custom',
                          temp_code_amount=get_user_step(user_id).get('temp_code_amount'))
            edit_message(chat_id, message_id,
                "*🎫 CREATE REDEEM CODE*\n\nSend the max number of uses as a number "
                "(send `0` for unlimited):")
        return

    if data.startswith("code_uses_"):
        if is_admin(user_id):
            max_uses = int(data.split("_")[2])
            ask_code_expiry(user_id, max_uses, chat_id, message_id)
        return

    if data == "code_expiry_custom":
        if is_admin(user_id):
            us = get_user_step(user_id)
            set_user_step(user_id, 'awaiting_code_expiry_custom',
                          temp_code_amount=us.get('temp_code_amount'),
                          temp_code_max_uses=us.get('temp_code_max_uses'))
            edit_message(chat_id, message_id,
                "*🎫 CREATE REDEEM CODE*\n\nSend the number of days until expiry "
                "(send `0` for never expires):")
        return

    if data.startswith("code_expiry_"):
        if is_admin(user_id):
            expiry_days = int(data.split("_")[2])
            if expiry_days <= 0:
                expiry_days = 36500  # "never" — a century is a practical forever
            finalize_create_code(user_id, expiry_days, chat_id, message_id)
        return
    
    # ========== ADMIN ADD COINS ==========
    if data == "admin_add_coins":
        if is_admin(user_id):
            admin_add_coins_start(user_id, chat_id, message_id)
        return
    
    if data.startswith("target_digit_"):
        if is_admin(user_id):
            digit = int(data.split("_")[2])
            update_target_id_display(user_id, digit, chat_id, message_id)
        return
    
    if data == "target_backspace":
        if is_admin(user_id):
            update_target_id_display(user_id, 'backspace', chat_id, message_id)
        return
    
    if data == "target_confirm":
        if is_admin(user_id):
            confirm_target_user(user_id, chat_id, message_id)
        return
    
    if data == "use_my_id_target":
        if is_admin(user_id):
            set_user_step(user_id, 'awaiting_coins_target', temp_target_user=str(user_id))
            confirm_target_user(user_id, chat_id, message_id)
        return
    
    if data.startswith("coin_digit_"):
        if is_admin(user_id):
            digit = int(data.split("_")[2])
            update_coin_amount_display(user_id, digit, chat_id, message_id)
        return
    
    if data == "coin_backspace":
        if is_admin(user_id):
            update_coin_amount_display(user_id, 'backspace', chat_id, message_id)
        return
    
    if data == "coin_confirm":
        if is_admin(user_id):
            confirm_add_coins(user_id, chat_id, message_id)
        return
    
    if data.startswith("coin_preset_"):
        if is_admin(user_id):
            amount = int(data.split("_")[2])
            process_coin_preset(user_id, amount, chat_id, message_id)
        return
    
    # ========== PREMIUM SUBSCRIPTION ==========
    if data == "subscribe_premium":
        show_premium_menu(chat_id, user_id, message_id)
        return

    if data.startswith("reactivate_premium_"):
        dep_id = int(data.split("_")[2])
        if is_user_premium(user_id):
            # Already premium (renewed elsewhere, or never actually lapsed) —
            # reactivate immediately, no reason to send them through a
            # purchase flow for something they already have.
            restart_deployment(dep_id, user_id, chat_id)
            view_deployment(chat_id, message_id, user_id, dep_id)
        else:
            # Not currently premium — send to the purchase flow. Once
            # payment completes, activate_premium() already auto-resumes
            # the user's most recent stopped/paused premium deployment.
            show_premium_menu(chat_id, user_id, message_id)
        return
    
    if data == "premium_monthly_stars":
        purchase_premium_stars(chat_id, user_id, "monthly", 30, PRICE_MONTHLY_STARS)
        return
    
    if data == "premium_yearly_stars":
        purchase_premium_stars(chat_id, user_id, "yearly", 365, PRICE_YEARLY_STARS)
        return
    
    if data == "premium_monthly_coins":
        purchase_premium_coins(chat_id, user_id, "monthly", 30, PRICE_MONTHLY_COINS)
        return
    
    if data == "premium_yearly_coins":
        purchase_premium_coins(chat_id, user_id, "yearly", 365, PRICE_YEARLY_COINS)
        return
    
    # ========== DEPLOYMENT PLANS ==========
    if data == "deploy_new":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        edit_message(chat_id, message_id, "*💰 DEPLOYMENT OPTIONS*\n\nChoose your plan:", get_deploy_menu(user_id))
        return
    
    if data == "plan_monthly":
        handle_paid_deployment(chat_id, user_id, message_id, "monthly", 30, PRICE_MONTHLY_COINS, PRICE_MONTHLY_STARS)
        return
    
    if data == "plan_yearly":
        handle_paid_deployment(chat_id, user_id, message_id, "yearly", 365, PRICE_YEARLY_COINS, PRICE_YEARLY_STARS)
        return

    if data == "plan_lifetime":
        if not is_admin(user_id):
            answer_callback(callback_id, "⛔ Admins only", show_alert=True)
            return
        handle_paid_deployment(chat_id, user_id, message_id, "lifetime", 0, 0, 0)
        return
    
    if data == "free_deployment":
        handle_free_deployment(chat_id, user_id, message_id)
        return
    
    # ========== PAYMENT METHODS ==========
    if data.startswith("pay_stars_"):
        plan = data.split("_")[2]
        user_step = get_user_step(user_id)
        
        if user_step.get('plan') != plan:
            return
        
        payload = f"{plan}_{user_id}_{int(datetime.now().timestamp())}"
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO pending_deployments 
            (user_id, chat_id, message_id, temp_file, requirements, env_vars, plan, duration, 
             cost_coins, cost_stars, payment_method, payload, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, chat_id, message_id, user_step.get('temp_file'), user_step.get('requirements'),
             json.dumps(user_step.get('env_vars', {})), plan, user_step.get('duration'),
             user_step.get('cost_coins'), user_step.get('cost_stars'), 'stars', payload,
             datetime.now().isoformat(), 'pending'))
        conn.commit()
        conn.close()
        
        title = f"Bot Deployment - {plan.capitalize()} Plan"
        description = f"Deploy your bot for {user_step.get('duration')} days"
        
        url = f"{TELEGRAM_API}/sendInvoice"
        prices = [{"label": title, "amount": user_step.get('cost_stars')}]
        
        invoice_data = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "currency": "XTR",
            "prices": prices,
            "need_name": False,
            "need_phone_number": False,
            "need_email": False,
            "need_shipping_address": False,
            "is_flexible": False
        }
        
        try:
            data_bytes = json.dumps(invoice_data).encode('utf-8')
            req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get('ok'):
                    edit_message(chat_id, message_id,
                        f"*⭐ STARS PAYMENT REQUIRED*\n\n"
                        f"Plan: {plan.capitalize()}\n"
                        f"Cost: {user_step.get('cost_stars')}⭐\n\n"
                        f"Please complete the payment.",
                        {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "deploy_new"}]]})
                else:
                    send_message(chat_id, f"❌ Failed: {result.get('description')}")
        except Exception as e:
            send_message(chat_id, "❌ Failed to create invoice.")
        return
    
    if data.startswith("pay_coins_"):
        plan = data.split("_")[2]
        user_step = get_user_step(user_id)
        
        if user_step.get('plan') != plan:
            return
        
        balances = get_user_balances(user_id)
        cost_coins = user_step.get('cost_coins')
        
        if balances['coins'] < cost_coins:
            edit_message(chat_id, message_id,
                f"❌ *INSUFFICIENT COINS*\n\n"
                f"Required: `{cost_coins}🪙`\n"
                f"Your balance: `{balances['coins']}🪙`\n\n"
                f"Use a redeem code to get more coins!",
                {"inline_keyboard": [[{"text": "🎫 Redeem Code", "callback_data": "redeem_code"},
                                      {"text": "🔙 Back", "callback_data": "deploy_new"}]]})
            return
        
        if plan == "lifetime" and not is_admin(user_id):
            send_message(chat_id, "⛔ Lifetime deployments are admin-only.")
            return
        if plan not in ("monthly", "yearly", "lifetime"):
            return

        update_user_coins(user_id, -cost_coins, "deployment", f"plan_{plan}")
        _deployed = deploy_paid_bot(chat_id, user_id, user_step.get('temp_file'),
                        user_step.get('requirements'), user_step.get('env_vars', {}),
                        plan, user_step.get('duration'), cost_coins, user_step.get('cost_stars'), 'coins')
        if not _deployed and not premium_covers_plan(user_id, plan):
            # Deployment failed after coins were charged — refund them.
            update_user_coins(user_id, cost_coins, "deployment_refund", f"plan_{plan}")
            send_message(chat_id, f"↩️ Deployment failed — `{cost_coins}🪙` refunded.")
        return
    
    # ========== REQUIREMENTS HANDLING ==========
    if data == "reqs_yes":
        user_step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_reqs', waiting_for_reqs=1,
                     temp_file=user_step.get('temp_file'),
                     plan=user_step.get('plan'), duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'),
                     env_vars=user_step.get('env_vars', {}))
        edit_message(chat_id, message_id,
            f"*📦 SEND requirements.txt FILE*\n\n"
            f"Please send your `requirements.txt` file now.\n\n"
            f"Example content:\n"
            f"```\npython-telegram-bot==20.7\nrequests==2.31.0\n```\n\n"
            f"Or click Auto-detect:",
            {"inline_keyboard": [[{"text": "⚡ Auto-detect & Skip", "callback_data": "reqs_no"}],
                                  [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return
    
    if data == "reqs_no":
        user_step = get_user_step(user_id)
        # Detect platform for env var hints
        platform_hint = ""
        try:
            tf = user_step.get('temp_file')
            if tf and Path(tf).exists():
                code   = Path(tf).read_text(errors='ignore')
                fws    = detect_bot_framework(code)
                plabel = get_platform_label(fws)
                phint  = get_platform_env_hint(fws)
                if fws and fws != ['generic']:
                    platform_hint = f"\n\n🔍 *Detected: {plabel}*"
                if phint:
                    platform_hint += f"\n\n*Required env vars:*\n{phint}"
        except Exception:
            pass
        set_user_step(user_id, 'awaiting_env',
                     waiting_for_env=1, waiting_for_reqs=0,
                     temp_file=user_step.get('temp_file'),
                     requirements=None,
                     plan=user_step.get('plan'),
                     duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'),
                     cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'),
                     env_vars=user_step.get('env_vars') or {})
        edit_message(chat_id, message_id,
            f"*🔧 ENVIRONMENT VARIABLES*{platform_hint}\n\n"
            "Send variables one per line:\n"
            "```\nBOT_TOKEN=your_token\nAPI_KEY=abc123\n```\n\n"
            "Or deploy without any:",
            get_env_keyboard(len(user_step.get('env_vars') or {})))
        return
    
    # ========== ENVIRONMENT VARIABLES ==========
    if data == "view_env":
        user_step = get_user_step(user_id)
        env_vars = user_step.get('env_vars', {})
        if not env_vars:
            text = "📝 *No environment variables set*\n\nSend as KEY=VALUE (one per line)\n\nExample:\n```\nBOT_TOKEN=123456:ABC\nAPI_KEY=your_key\nDATABASE_URL=postgresql://...\n```"
        else:
            text = "📝 *Current Variables:*\n\n"
            for k, v in list(env_vars.items())[:15]:
                if any(s in k.upper() for s in ['TOKEN', 'SECRET', 'KEY', 'PASSWORD', 'HASH']):
                    display_v = v[:10] + "..." if len(v) > 15 else v
                else:
                    display_v = v[:30] + "..." if len(v) > 35 else v
                text += f"• `{k}` = `{display_v}`\n"
            if len(env_vars) > 15:
                text += f"\n... and {len(env_vars) - 15} more"
            text += f"\n\nTotal: `{len(env_vars)}`\n\nAll variables will be available via `os.environ.get()`"
        edit_message(chat_id, message_id, text, get_env_keyboard(len(env_vars)))
        return
    
    if data == "env_send":
        user_step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_env', waiting_for_env=1,
                     temp_file=user_step.get('temp_file'),
                     requirements=user_step.get('requirements'),
                     env_vars=user_step.get('env_vars', {}),
                     plan=user_step.get('plan'), duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'), cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'))
        edit_message(chat_id, message_id,
            f"*📤 SEND ENVIRONMENT VARIABLES*\n\n"
            f"Send your environment variables as text:\n"
            f"```\nBOT_TOKEN=your_actual_token\nAPI_KEY=your_api_key\nDATABASE_URL=postgresql://user:pass@localhost/db\n```\n\n"
            f"One variable per line. Use KEY=VALUE format.\n\n"
            f"All variables will be available via `os.environ.get('KEY')`\n\n"
            f"Click Skip if you have none:",
            {"inline_keyboard": [[{"text": "⏭️ Skip", "callback_data": "env_skip"}],
                                  [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return
    
    if data == "env_skip":
        user_step = get_user_step(user_id)
        env_vars  = {}   # skip means deploy with no vars
        _dispatch_deploy(chat_id, user_id, message_id, user_step, env_vars)
        return

    if data == "env_done":
        user_step = get_user_step(user_id)
        raw = user_step.get('env_vars') or {}
        if isinstance(raw, str):
            try:    raw = json.loads(raw)
            except Exception: raw = {}
        _dispatch_deploy(chat_id, user_id, message_id, user_step, dict(raw))
        return
    
    if data == "cancel_deploy":
        user_step = get_user_step(user_id)
        if user_step.get('temp_file') and Path(user_step.get('temp_file')).exists():
            Path(user_step.get('temp_file')).unlink(missing_ok=True)
        set_user_step(user_id, None)
        edit_message(chat_id, message_id, "❌ Deployment cancelled",
                    {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
        return
    
    # ========== DEPLOYMENTS LIST ==========
    if data == "my_deployments":
        handle_deployments_list(chat_id, user_id, message_id)
        return
    
    if data.startswith("view_deploy_"):
        dep_id = int(data.split("_")[2])
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("view_install_logs_"):
        dep_id = int(data.split("_")[3])
        view_install_logs(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("view_runtime_logs_"):
        dep_id = int(data.split("_")[3])
        view_runtime_logs(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("stop_deploy_"):
        dep_id = int(data.split("_")[2])
        stop_deployment(dep_id)
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("restart_deploy_"):
        dep_id = int(data.split("_")[2])
        restart_deployment(dep_id, user_id, chat_id)
        view_deployment(chat_id, message_id, user_id, dep_id)
        return

    if data.startswith("restart_free_"):
        dep_id = int(data.split("_")[2])
        restart_deployment(dep_id, user_id, chat_id, force_free_downgrade=True)
        view_deployment(chat_id, message_id, user_id, dep_id)
        return
    
    if data.startswith("delete_deploy_"):
        dep_id = int(data.split("_")[2])
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ Yes, Delete", "callback_data": f"confirm_delete_{dep_id}"},
                 {"text": "❌ No", "callback_data": f"view_deploy_{dep_id}"}]
            ]
        }
        edit_message(chat_id, message_id,
            f"*⚠️ DELETE DEPLOYMENT*\n\n"
            f"Delete deployment `{dep_id}`?\n\n"
            f"This will:\n"
            f"• Stop the bot\n"
            f"• Delete all files\n"
            f"• Free a deployment slot\n\n"
            f"⚠️ Cannot be undone!",
            keyboard)
        return
    
    if data.startswith("confirm_delete_"):
        dep_id = int(data.split("_")[2])
        delete_deployment(dep_id, user_id, chat_id)
        handle_deployments_list(chat_id, user_id, message_id)
        return
    
    # ========== CHANNEL VERIFICATION ==========
    if data == "check_verification":
        if check_channel_membership(user_id):
            mark_channel_joined(user_id)
            if not has_accepted_tos(user_id):
                show_tos_prompt(chat_id, user_id, message_id)
                return
            balances = get_user_balances(user_id)
            edit_message(chat_id, message_id,
                f"✅ *VERIFIED!*\n\n"
                f"🪙 {balances['coins']} | ⭐ {balances['stars']}\n\n"
                f"Welcome!",
                get_main_menu(user_id))
        else:
            send_verification_required(chat_id, user_id, "User", message_id)
        return
    
    if data == "verify_channel":
        if check_channel_membership(user_id):
            mark_channel_joined(user_id)
            if not has_accepted_tos(user_id):
                show_tos_prompt(chat_id, user_id, message_id)
                return
            balances = get_user_balances(user_id)
            edit_message(chat_id, message_id,
                f"✅ *VERIFIED!*\n\n"
                f"Thank you for joining {REQUIRED_CHANNEL}!",
                get_main_menu(user_id))
        else:
            edit_message(chat_id, message_id,
                f"❌ *NOT VERIFIED*\n\n"
                f"Please join {REQUIRED_CHANNEL} first.",
                {"inline_keyboard": [
                    [{"text": "📢 JOIN", "url": CHANNEL_LINK},
                     {"text": "✅ VERIFY", "callback_data": "verify_channel"}]
                ]})
        return

    # ========== TERMS OF SERVICE ==========
    if data == "tos_agree":
        mark_tos_accepted(user_id)
        balances = get_user_balances(user_id)
        edit_message(chat_id, message_id,
            f"✅ *Thanks for confirming!*\n\n"
            f"🪙 {balances['coins']} | ⭐ {balances['stars']}\n\n"
            f"Welcome!",
            get_main_menu(user_id))
        return
    
    # ========== MY BALANCE ==========
    if data == "my_balance":
        handle_balance(chat_id, user_id, message_id)
        return
    
    # ========== GITHUB DEPLOY ==========
    if data == "github_deploy":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        set_user_step(user_id, 'awaiting_github_url')
        edit_message(chat_id, message_id,
            "*🐙 DEPLOY FROM GITHUB*\n\n"
            "Send me the GitHub repository URL or shorthand:\n\n"
            "*Examples:*\n"
            "`https://github.com/owner/repo`\n"
            "`github.com/owner/repo`\n"
            "`owner/repo`\n"
            "`owner/repo@branch` ← specific branch\n\n"
            "Supports *public and private* repos.\n"
            "For private repos you'll be asked for a token next.",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
        return

    # ---- GitHub: plan picker (free 24h / monthly / yearly / lifetime-admin) ----
    if data.startswith("gh_plan_"):
        plan = data[len("gh_plan_"):]
        if plan not in ('free', 'monthly', 'yearly', 'lifetime'):
            return
        step = get_user_step(user_id)
        if step.get('step') != 'awaiting_github_plan':
            return   # stale button
        if plan == 'lifetime' and not is_admin(user_id):
            answer_callback(callback_id, "⛔ Admins only", show_alert=True)
            return
        # Resource quota is checked BEFORE any payment is offered, so nobody
        # pays (especially with non-refundable Stars) for a deployment that
        # would then be refused for lack of resources.
        _q_ok, _q_used, _q_total = check_user_quota(user_id)
        if not _q_ok:
            edit_message(chat_id, message_id, quota_exceeded_text(_q_used, _q_total),
                {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}],
                                     [{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
            return
        if plan == 'free':
            can_deploy, reason = can_use_free_deployment(user_id)
            if not can_deploy:
                edit_message(chat_id, message_id,
                    f"❌ *FREE DEPLOYMENT LIMIT REACHED*\n\n{reason}\n\nPick another plan or upgrade to Premium.",
                    {"inline_keyboard": [
                        [{"text": "🔙 Choose Plan", "callback_data": "github_replan"}],
                        [{"text": "💰 Get Premium", "callback_data": "subscribe_premium"}]]})
                return
        set_user_step(user_id, 'awaiting_env_vars', temp_plan=plan, temp_source='github')
        _ask_github_env_vars(chat_id, message_id, step.get('temp_gh_summary') or '', plan)
        return

    if data == "github_replan":
        step = get_user_step(user_id)
        if step.get('temp_github_owner'):
            set_user_step(user_id, 'awaiting_github_plan')
            _show_github_plan_menu(chat_id, user_id, message_id, step.get('temp_gh_summary') or '')
        return

    # ---- GitHub: pay for a monthly/yearly plan with coins ----
    if data == "gh_pay_coins":
        step = get_user_step(user_id)
        plan = step.get('temp_plan')
        if step.get('step') != 'awaiting_github_payment' or plan not in ('monthly', 'yearly'):
            return
        _days, price_coins, _stars = _gh_plan_prices(plan)
        bal = get_user_balances(user_id)
        if bal['coins'] < price_coins:
            edit_message(chat_id, message_id,
                f"❌ *INSUFFICIENT COINS*\n\nRequired: `{price_coins}🪙`\n"
                f"Your balance: `{bal['coins']}🪙`\n\nUse a redeem code or pay with Stars!",
                {"inline_keyboard": [[{"text": "⭐ Pay with Stars", "callback_data": "gh_pay_stars"},
                                      {"text": "🎫 Redeem Code", "callback_data": "redeem_code"}]]})
            return
        # Lock the step so a double-tap can't charge twice
        set_user_step(user_id, 'github_paying')
        update_user_coins(user_id, -price_coins, "deployment", f"github_{plan}")
        edit_message(chat_id, message_id, "🪙 Payment received — starting deployment…", None)
        _launch_github_deploy(chat_id, user_id, step,
                              main_file_name=step.get('temp_main_file') or None,
                              paid_method='coins')
        return

    # ---- GitHub: pay for a monthly/yearly plan with Telegram Stars ----
    if data == "gh_pay_stars":
        step = get_user_step(user_id)
        plan = step.get('temp_plan')
        if step.get('step') != 'awaiting_github_payment' or plan not in ('monthly', 'yearly'):
            return
        days, price_coins, price_stars = _gh_plan_prices(plan)
        payload = f"ghdeploy_{plan}_{user_id}_{int(datetime.now().timestamp())}"
        saved = json.dumps({k: step.get(k) for k in (
            'temp_github_owner', 'temp_github_repo', 'temp_github_branch',
            'temp_github_token', 'temp_env_vars', 'temp_main_file', 'temp_plan')})
        db_execute('''INSERT INTO pending_deployments
            (user_id, chat_id, message_id, temp_file, requirements, env_vars, plan, duration,
             cost_coins, cost_stars, payment_method, payload, created_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (user_id, chat_id, message_id, 'github', saved, '{}', plan, days,
             price_coins, price_stars, 'stars', payload, datetime.now().isoformat(), 'pending'))
        invoice = {
            "chat_id": chat_id,
            "title": f"GitHub Deployment - {plan.capitalize()} Plan",
            "description": f"Deploy {step.get('temp_github_owner')}/{step.get('temp_github_repo')} for {days} days",
            "payload": payload, "currency": "XTR",
            "prices": [{"label": f"{plan.capitalize()} plan", "amount": price_stars}],
        }
        try:
            req = urllib.request.Request(f"{TELEGRAM_API}/sendInvoice",
                data=json.dumps(invoice).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode('utf-8'))
            if res.get('ok'):
                edit_message(chat_id, message_id,
                    f"*⭐ STARS PAYMENT REQUIRED*\n\nPlan: {plan.capitalize()}\n"
                    f"Cost: {price_stars}⭐\n\nComplete the payment above — your repo deploys automatically afterwards.",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
            else:
                send_message(chat_id, f"❌ Failed: {res.get('description')}")
        except Exception as e:
            send_message(chat_id, f"❌ Failed to create invoice: {e}")
        return

    if data == "github_public":
        step = get_user_step(user_id)
        _start_github_deploy(chat_id, user_id, message_id, step, token=None)
        return

    if data == "github_skip_env":
        step = get_user_step(user_id)
        step['temp_env_vars'] = '.'
        _launch_github_deploy(chat_id, user_id, step)
        return

    if data == "github_private":
        step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_github_token',
                      temp_github_owner=step.get('temp_github_owner'),
                      temp_github_repo=step.get('temp_github_repo'),
                      temp_github_branch=step.get('temp_github_branch'))
        edit_message(chat_id, message_id,
            "*🔑 PRIVATE REPO — GitHub Token*\n\n"
            "Send your *GitHub Personal Access Token* (PAT).\n\n"
            "The token needs the `repo` scope.\n"
            "Create one at: `github.com/settings/tokens`\n\n"
            "⚠️ Stored only in `.env` inside the deployment folder, never logged.",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
        return

    if data.startswith("github_main_"):
        # Admin selected main file from multi-file picker
        step = get_user_step(user_id)
        file_name = data[len("github_main_"):]
        _launch_github_deploy(chat_id, user_id, step, main_file_name=file_name)
        return

    # ========== REDEEM CODE ==========
    if data == "redeem_code":
        handle_redeem(chat_id, user_id, message_id)
        return

    # ========== ENV SEND (prompt user to type vars) ==========
    if data == "env_send":
        user_step = get_user_step(user_id)
        # Keep all existing state, just remind the user to type
        env_vars = user_step.get('env_vars') or {}
        if isinstance(env_vars, str):
            try: env_vars = json.loads(env_vars)
            except: env_vars = {}
        set_user_step(user_id, 'awaiting_env',
                     waiting_for_env=1,
                     temp_file=user_step.get('temp_file'),
                     requirements=user_step.get('requirements'),
                     env_vars=env_vars,
                     plan=user_step.get('plan'),
                     duration=user_step.get('duration'),
                     cost_coins=user_step.get('cost_coins'),
                     cost_stars=user_step.get('cost_stars'),
                     payment_method=user_step.get('payment_method'))
        edit_message(chat_id, message_id,
            "*📝 TYPE YOUR VARIABLES*\n\n"
            "Send one or more `KEY=VALUE` pairs, one per line:\n\n"
            "```\nBOT_TOKEN=7712345:AAFabcXYZ\nAPI_KEY=sk-abc123\nDATABASE_URL=sqlite:///bot.db\n```\n\n"
            "Send them now 👇",
            {"inline_keyboard": [
                [{"text": "⏭️ Skip", "callback_data": "env_skip"}],
                [{"text": "❌ Cancel", "callback_data": "cancel_deploy"}]]})
        return

    # ========== REFERRALS ==========
    if data == "my_referral":
        show_referral_menu(chat_id, user_id, message_id)
        return

    # ========== BROADCAST ==========
    if data == "admin_broadcast":
        if not is_admin(user_id): return
        edit_message(chat_id, message_id,
            "*📢 BROADCAST TO ALL USERS*\n\nChoose message type:",
            {"inline_keyboard": [
                [{"text": "✉️ Text Message",   "callback_data": "broadcast_type_text"}],
                [{"text": "🖼 Photo + Caption", "callback_data": "broadcast_type_photo"}],
                [{"text": "🎬 Video + Caption", "callback_data": "broadcast_type_video"}],
                [{"text": "🔙 Admin Panel",     "callback_data": "admin_panel"}],
            ]})
        return

    if data == "broadcast_type_text":
        if not is_admin(user_id): return
        set_user_step(user_id, 'awaiting_broadcast_text')
        edit_message(chat_id, message_id,
            "*✉️ TEXT BROADCAST*\n\nType the message to send to all users.\n"
            "Supports *bold*, _italic_, `code`:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_type_photo":
        if not is_admin(user_id): return
        set_user_step(user_id, 'awaiting_broadcast_media', temp_broadcast_type='photo')
        edit_message(chat_id, message_id,
            "*🖼 PHOTO BROADCAST*\n\nSend the photo now:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_type_video":
        if not is_admin(user_id): return
        set_user_step(user_id, 'awaiting_broadcast_media', temp_broadcast_type='video')
        edit_message(chat_id, message_id,
            "*🎬 VIDEO BROADCAST*\n\nSend the video now:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_no_buttons":
        if not is_admin(user_id): return
        step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_broadcast_confirm',
                     temp_broadcast_type=step.get('temp_broadcast_type'),
                     temp_broadcast_file=step.get('temp_broadcast_file', ''),
                     temp_broadcast_caption=step.get('temp_broadcast_caption', ''),
                     temp_broadcast_buttons='[]')
        _send_broadcast_preview(chat_id,
            step.get('temp_broadcast_type'),
            step.get('temp_broadcast_file', ''),
            step.get('temp_broadcast_caption', ''), [])
        return

    if data == "broadcast_edit_caption":
        if not is_admin(user_id): return
        step = get_user_step(user_id)
        set_user_step(user_id, 'awaiting_broadcast_caption',
                     temp_broadcast_type=step.get('temp_broadcast_type'),
                     temp_broadcast_file=step.get('temp_broadcast_file', ''))
        send_message(chat_id, "*✏️ New Caption*\n\nSend the new caption text:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
        return

    if data == "broadcast_confirm":
        if not is_admin(user_id): return
        step     = get_user_step(user_id)
        btype    = step.get('temp_broadcast_type', 'text')
        file_id  = step.get('temp_broadcast_file', '')
        caption  = step.get('temp_broadcast_caption', '')
        btns_raw = step.get('temp_broadcast_buttons', '[]')
        set_user_step(user_id, None)
        send_message(chat_id, "📤 *Sending to all users…*", None)
        threading.Thread(
            target=_run_broadcast_and_notify,
            args=(chat_id, user_id, btype, file_id, caption, btns_raw),
            daemon=True, name="Broadcast").start()
        return

    # ========== SECURITY REVIEW (admin approve / reject) ==========
    if data.startswith("sec_approve_") or data.startswith("sec_approve_warn_") \
            or data.startswith("sec_reject_") or data.startswith("sec_report_"):

        if not is_admin(user_id):
            answer_callback(callback_id, "⛔ Admins only", show_alert=True)
            return

        # Parse action and review_id from callback data
        if data.startswith("sec_approve_warn_"):
            action     = "approve_warn"
            review_id  = int(data[len("sec_approve_warn_"):])
        elif data.startswith("sec_approve_"):
            action    = "approve"
            review_id = int(data[len("sec_approve_"):])
        elif data.startswith("sec_reject_"):
            action    = "reject"
            review_id = int(data[len("sec_reject_"):])
        else:
            action    = "report"
            review_id = int(data[len("sec_report_"):])

        # Fetch review from DB
        conn_rv = sqlite3.connect(DATABASE_FILE, timeout=10)
        c_rv = conn_rv.cursor()
        c_rv.execute("""SELECT user_id, chat_id, file_name, file_path,
                               critical_json, warnings_json, report_text,
                               source, status, user_step_json
                        FROM security_reviews WHERE review_id=?""", (review_id,))
        row = c_rv.fetchone()

        if not row:
            conn_rv.close()
            edit_message(chat_id, message_id,
                f"❌ Review `#{review_id}` not found.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        (rv_user_id, rv_chat_id, rv_file_name, rv_file_path,
         rv_crit_json, rv_warn_json, rv_report,
         rv_source, rv_status, rv_step_json) = row

        # Show full report
        if action == "report":
            conn_rv.close()
            try:
                crits = json.loads(rv_crit_json or "[]")
                warns = json.loads(rv_warn_json or "[]")
            except Exception:
                crits, warns = [], []
            full = (
                f"📋 *Full Security Report — Review #{review_id}*\n\n"
                f"*Critical ({len(crits)}):*\n"
                + '\n'.join(f"• `{safe_md(c[:150])}`" for c in crits)
                + (f"\n\n*Warnings ({len(warns)}):*\n"
                   + '\n'.join(f"• {safe_md(w[:150])}" for w in warns[:10])
                   if warns else "")
            )
            send_message(chat_id, full[:4000],
                {"inline_keyboard": [
                    [{"text": "✅ Approve & Deploy",   "callback_data": f"sec_approve_{review_id}"},
                     {"text": "⚠️ Approve (warn)",      "callback_data": f"sec_approve_warn_{review_id}"}],
                    [{"text": "❌ Reject",              "callback_data": f"sec_reject_{review_id}"}],
                ]})
            return

        # Already actioned?
        if rv_status != "pending":
            conn_rv.close()
            edit_message(chat_id, message_id,
                f"ℹ️ Review `#{review_id}` is already *{rv_status}*.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        # ── REJECT ──────────────────────────────────────────────────────
        if action == "reject":
            c_rv.execute(
                "UPDATE security_reviews SET status='rejected', reviewed_by=?, reviewed_at=? WHERE review_id=?",
                (user_id, datetime.now().isoformat(), review_id))
            conn_rv.commit()
            conn_rv.close()
            # Clean up pending file
            try:
                if rv_file_path and Path(rv_file_path).exists():
                    Path(rv_file_path).unlink()
            except Exception:
                pass
            # Tell the user
            send_message(rv_chat_id,
                f"❌ *Deployment Rejected*\n\n"
                f"An admin reviewed file `{safe_md(rv_file_name)}` "
                f"and decided it cannot be deployed due to security concerns.\n\n"
                f"Please review the security issues and try again with a clean file.\n"
                f"🆔 Review `#{review_id}`",
                {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
            # Update admin message
            edit_message(chat_id, message_id,
                f"✅ Review `#{review_id}` — *Rejected*\n"
                f"User `{rv_user_id}` has been notified.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        # ── APPROVE (with or without warning) ──────────────────────────
        c_rv.execute(
            "UPDATE security_reviews SET status=?, reviewed_by=?, reviewed_at=? WHERE review_id=?",
            (action, user_id, datetime.now().isoformat(), review_id))
        conn_rv.commit()
        conn_rv.close()

        # Notify user of approval
        warn_note = (
            "\n\n⚠️ *Note:* This file triggered security warnings. "
            "Use it responsibly — misuse may result in account suspension."
            if action == "approve_warn" else ""
        )
        send_message(rv_chat_id,
            f"✅ *Deployment Approved!*\n\n"
            f"Admin approved `{safe_md(rv_file_name)}`."
            f"{warn_note}\n\n"
            f"🚀 Starting deployment now…",
            {"inline_keyboard": [[{"text": "⏳ Deploying…", "callback_data": "main_menu"}]]})

        # Update admin message immediately
        edit_message(chat_id, message_id,
            f"✅ Review `#{review_id}` — *Approved*\n"
            f"Deploying for user `{rv_user_id}`…",
            {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})

        # Run the actual deployment in a background thread
        def _run_approved_deployment(
                rv_id=review_id, rv_uid=rv_user_id, rv_cid=rv_chat_id,
                rv_fn=rv_file_name, rv_fp=rv_file_path,
                rv_src=rv_source, rv_step=rv_step_json):
            try:
                step = json.loads(rv_step) if rv_step else {}

                if rv_src == "github":
                    # Re-run GitHub deployment without the security block
                    from pathlib import Path as _P
                    # `step` was stored by deploy_from_github itself (owner/repo/
                    # branch/plan/...), not the temp_* keys _launch_github_deploy
                    # reads, so call deploy_from_github directly with them.
                    deploy_from_github(
                        rv_cid, rv_uid,
                        step.get('owner', ''), step.get('repo', ''),
                        step.get('branch') or 'main', step.get('token') or None,
                        step.get('env_vars') or {},
                        plan=step.get('plan') or 'free',
                        duration=step.get('duration') or FREE_DEPLOYMENT_DURATION_HOURS,
                        cost_coins=step.get('cost_coins') or 0,
                        cost_stars=step.get('cost_stars') or 0,
                        payment_method=step.get('payment_method') or 'free',
                        is_free=bool(step.get('is_free')),
                        main_file_name=None,
                        skip_security=True)

                else:
                    # File-upload path — read saved pending file
                    pending = _P(rv_fp) if rv_fp else None
                    if not pending or not pending.exists():
                        send_message(rv_cid,
                            "❌ Approved file no longer on disk.\n"
                            "Please upload your file again.",
                            {"inline_keyboard": [[{"text": "🏠 Menu",
                                                   "callback_data": "main_menu"}]]})
                        return
                    # Save to temp path the normal deploy flow expects
                    temp_path = BASE_DIR / f"temp_{rv_uid}_{sanitize_upload_filename(rv_fn)}"
                    temp_path.write_bytes(pending.read_bytes())
                    pending.unlink(missing_ok=True)

                    # Restore the user's step state so the deploy flow has
                    # plan / duration / payment_method etc.
                    set_user_step(rv_uid, 'awaiting_reqs',
                                  temp_file=str(temp_path),
                                  plan=step.get('plan'),
                                  duration=step.get('duration'),
                                  cost_coins=step.get('cost_coins'),
                                  cost_stars=step.get('cost_stars'),
                                  payment_method=step.get('payment_method'),
                                  env_vars=step.get('env_vars', {}))

                    # Proceed directly to deployment (requirements auto-detect)
                    deploy_free_bot_with_logs(
                        rv_cid, rv_uid,
                        str(temp_path),
                        step.get('requirements', ''),
                        step.get('env_vars', {}))

            except Exception as e:
                print(f"❌ Approved deployment failed (review #{rv_id}): {e}")
                send_message(rv_cid,
                    f"❌ Deployment failed after approval:\n`{e}`\n\n"
                    "Please try deploying your file again.",
                    {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})

        threading.Thread(target=_run_approved_deployment, daemon=True,
                         name=f"SecApprove-{review_id}").start()
        return

    # ========== BUG REPORT (user) ==========
    if data == "report_bug":
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, "User", message_id)
            return
        set_user_step(user_id, 'awaiting_bug_report')
        edit_message(chat_id, message_id,
            "*🐛 REPORT A BUG*\n\n"
            "Please describe the issue you're experiencing in as much detail as possible:\n\n"
            "• What were you trying to do?\n"
            "• What happened instead?\n"
            "• Any error messages?\n\n"
            "Type your report and send it:",
            {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
        return

    # ========== ADMIN BUG REPORTS ==========
    if data == "admin_sec_reviews":
        if not is_admin(user_id):
            return
        try:
            conn_sr = sqlite3.connect(DATABASE_FILE, timeout=10)
            c_sr = conn_sr.cursor()
            c_sr.execute("""SELECT review_id, user_id, file_name, source,
                                   status, created_at
                            FROM security_reviews
                            ORDER BY created_at DESC LIMIT 20""")
            reviews = c_sr.fetchall()
            pending_count = conn_sr.execute(
                "SELECT COUNT(*) FROM security_reviews WHERE status='pending'").fetchone()[0]
            conn_sr.close()
        except Exception as e:
            send_message(chat_id, f"❌ DB error: {e}")
            return

        if not reviews:
            edit_message(chat_id, message_id,
                "📋 *Security Reviews*\n\nNo reviews yet.",
                {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
            return

        icons = {"pending": "⏳", "approve": "✅", "approve_warn": "⚠️", "rejected": "❌"}
        text = f"🔒 *Security Reviews* ({pending_count} pending)\n\n"
        buttons = []
        for rv_id, rv_uid, rv_fn, rv_src, rv_status, rv_at in reviews:
            icon = icons.get(rv_status, "❓")
            dt   = datetime.fromisoformat(rv_at).strftime("%d/%m %H:%M") if rv_at else "?"
            src_icon = "🐙" if rv_src == "github" else "📤"
            text += f"{icon} *#{rv_id}* {src_icon} `{safe_md(rv_fn[:30])}` — user `{rv_uid}` — {dt}\n"
            if rv_status == "pending":
                buttons.append([
                    {"text": f"✅ #{rv_id}",  "callback_data": f"sec_approve_{rv_id}"},
                    {"text": f"⚠️ #{rv_id}",  "callback_data": f"sec_approve_warn_{rv_id}"},
                    {"text": f"❌ #{rv_id}",  "callback_data": f"sec_reject_{rv_id}"},
                ])

        buttons.append([{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}])
        edit_message(chat_id, message_id, text[:3900], {"inline_keyboard": buttons})
        return

    if data == "admin_bug_reports":
        if is_admin(user_id):
            show_admin_bug_reports(chat_id, user_id, message_id)
        return

    if data == "admin_referral_stats":
        if not is_admin(user_id):
            return
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute("""SELECT r.referrer_id, u.username, u.first_name, COUNT(*) cnt,
                                COALESCE(SUM(r.reward_coins),0) earned
                         FROM referrals r
                         LEFT JOIN users u ON u.user_id = r.referrer_id
                         GROUP BY r.referrer_id ORDER BY cnt DESC LIMIT 15""")
            rows = c.fetchall()
            total = c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
            coins_paid = c.execute("SELECT COALESCE(SUM(reward_coins),0) FROM referrals WHERE reward_given=1").fetchone()[0]
            conn.close()

            text = (f"*👥 REFERRAL STATS*\n\n"
                    f"Total referrals: `{total}`\n"
                    f"Coins paid out:  `{coins_paid}🪙`\n"
                    f"Reward per ref:  `{REFERRAL_REWARD_COINS}🪙`\n\n"
                    f"*Top Referrers:*\n")
            for i, (rid, uname, fname, cnt, earned) in enumerate(rows, 1):
                who = f"@{uname}" if uname else (fname or f"#{rid}")
                text += f"{i}. {who} — `{cnt}` refs → `{earned}🪙`\n"
            if not rows:
                text += "_No referrals yet_"
        except Exception as e:
            text = f"❌ Error: {e}"
        edit_message(chat_id, message_id, text,
                     {"inline_keyboard": [[{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
        return

    if data.startswith("continue_as_free_"):
        dep_id = int(data.split("_")[3])
        # Verify ownership
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id FROM deployments WHERE deployment_id=?", (dep_id,))
        row = c.fetchone()
        conn.close()
        if row and (row[0] == user_id or is_admin(user_id)):
            continue_deployment_as_free(dep_id, row[0], chat_id)
        else:
            send_message(chat_id, "❌ Permission denied")
        return

    if data.startswith("bug_reply_"):
        if is_admin(user_id):
            report_id = int(data.split("_")[2])
            set_user_step(user_id, f'awaiting_bug_reply_{report_id}')
            # Get report info
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute("SELECT user_id, username, first_name, message FROM bug_reports WHERE report_id = ?", (report_id,))
            row = c.fetchone()
            conn.close()
            if row:
                who = f"@{row[1]}" if row[1] else f"User {row[0]}"
                preview = row[3][:100]
                edit_message(chat_id, message_id,
                    f"*✉️ REPLY TO REPORT #{report_id}*\n\n"
                    f"From: {who}\n"
                    f"Message: _{preview}_\n\n"
                    f"Type your reply and send it:",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "admin_bug_reports"}]]})
            else:
                edit_message(chat_id, message_id, "❌ Report not found.",
                             {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "admin_bug_reports"}]]})
        return

    if data.startswith("bug_close_"):
        if is_admin(user_id):
            report_id = int(data.split("_")[2])
            conn = sqlite3.connect(DATABASE_FILE)
            conn.execute("UPDATE bug_reports SET status='closed' WHERE report_id = ?", (report_id,))
            conn.commit()
            conn.close()
            show_admin_bug_reports(chat_id, user_id, message_id)
        return

# ==================== USER STEP FUNCTIONS ====================
def set_user_step(user_id, step, **kwargs):
    """
    Persist workflow state for a user.
    Known high-frequency fields are stored in dedicated columns for query speed.
    ALL kwargs are also stored in pending_json so nothing is ever silently discarded.
    When step=None (clear state) pending_json is fully reset to prevent
    stale state (e.g. temp_source='github') from leaking into future flows.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

    _KNOWN = {'temp_file', 'requirements', 'env_vars', 'plan', 'payment_method',
              'duration', 'cost_coins', 'cost_stars', 'waiting_for_env',
              'waiting_for_reqs', 'waiting_for_redeem', 'temp_target_user',
              'temp_coins_amount', 'temp_stars_amount', 'temp_expiry', 'temp_reward_type'}

    updates = ["step = ?"]
    values  = [step]

    for key in _KNOWN:
        if key in kwargs:
            updates.append(f"{key} = ?")
            val = kwargs[key]
            if key == 'env_vars' and val is not None and not isinstance(val, str):
                val = json.dumps(val)
            values.append(val)

    # When clearing state, wipe pending_json entirely so no stale keys remain
    if step is None:
        pending = json.dumps({})
    else:
        c.execute("SELECT pending_json FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        try:
            existing = json.loads(row[0] or '{}') if row else {}
        except Exception:
            existing = {}
        existing.update(kwargs)
        existing['_step'] = step
        pending = json.dumps(existing)

    updates.append("pending_json = ?")
    values.append(pending)

    values.append(user_id)
    c.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", values)
    conn.commit()
    conn.close()


def get_user_step(user_id):
    """
    Return full workflow state for a user.
    Merges dedicated column values and pending_json into one dict so callers
    can read any kwarg that was ever passed to set_user_step.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT step, temp_file, requirements, env_vars, plan, payment_method,
                        duration, cost_coins, cost_stars, waiting_for_env,
                        waiting_for_reqs, waiting_for_redeem, temp_target_user,
                        temp_coins_amount, temp_stars_amount, temp_expiry,
                        temp_reward_type, pending_json
                 FROM users WHERE user_id = ?""", (user_id,))
    row = c.fetchone()
    conn.close()

    _DEFAULTS = {
        'step': None, 'temp_file': None, 'requirements': None, 'env_vars': {},
        'plan': None, 'payment_method': None, 'duration': None,
        'cost_coins': None, 'cost_stars': None,
        'waiting_for_env': 0, 'waiting_for_reqs': 0, 'waiting_for_redeem': 0,
        'temp_target_user': None, 'temp_coins_amount': None,
        'temp_stars_amount': None, 'temp_expiry': None, 'temp_reward_type': None,
    }

    if not row:
        return _DEFAULTS.copy()

    # Parse pending_json first (lower priority)
    try:
        pj = json.loads(row[17] or '{}')
    except Exception:
        pj = {}

    # Build result: pending_json as base, then overlay dedicated columns
    result = {**_DEFAULTS, **pj}

    col_map = ['step','temp_file','requirements','env_vars','plan','payment_method',
               'duration','cost_coins','cost_stars','waiting_for_env','waiting_for_reqs',
               'waiting_for_redeem','temp_target_user','temp_coins_amount',
               'temp_stars_amount','temp_expiry','temp_reward_type']

    for i, key in enumerate(col_map):
        val = row[i]
        if val is None:
            continue
        if key == 'env_vars':
            try:
                val = json.loads(val)
            except Exception:
                val = {}
        elif key in ('waiting_for_env','waiting_for_reqs','waiting_for_redeem'):
            val = int(val or 0)
        result[key] = val

    return result

# ==================== MESSAGE HANDLER ====================
def handle_message(message):
    chat_id = message['chat']['id']
    user_id = message['from']['id']
    first_name = message['from'].get('first_name', 'User')
    
    print(f"📨 Message from {first_name} ({user_id})")
    
    if 'text' in message and message['text'].startswith('/start'):
        parts = message['text'].split(maxsplit=1)
        start_param = parts[1].strip() if len(parts) > 1 else ""
        handle_start(chat_id, user_id, message['from'].get('username', ''), first_name, start_param)
        return
    
    user_step = get_user_step(user_id)
    
    if 'text' in message:
        text = message['text']

        # ── GitHub deploy: env vars (from _start_github_deploy) ──────
        if user_step.get('step') == 'awaiting_env_vars' and user_step.get('temp_source') == 'github':
            user_step['temp_env_vars'] = text.strip()
            _launch_github_deploy(chat_id, user_id, user_step)
            return

        # ── GitHub deploy: URL ───────────────────────────────────────
        if user_step.get('step') == 'awaiting_github_url':
            owner, repo, branch = parse_github_url(text.strip())
            if not owner or not repo:
                send_message(chat_id,
                    "❌ Couldn't parse that URL. Try `owner/repo` or full GitHub URL:",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
                return
            set_user_step(user_id, 'awaiting_github_visibility',
                          temp_github_owner=owner,
                          temp_github_repo=repo,
                          temp_github_branch=branch or '')
            send_message(chat_id,
                f"*🐙 Repo detected:* `{owner}/{repo}`"
                + (f"\n*Branch:* `{branch}`" if branch else ""),
                {"inline_keyboard": [
                    [{"text": "🌐 Public repo",  "callback_data": "github_public"}],
                    [{"text": "🔒 Private repo", "callback_data": "github_private"}],
                    [{"text": "❌ Cancel",        "callback_data": "main_menu"}],
                ]})
            return

        # ── GitHub deploy: PAT for private repo ──────────────────────
        if user_step.get('step') == 'awaiting_github_token':
            token_val = text.strip()
            if not token_val.startswith('gh'):
                send_message(chat_id,
                    "⚠️ That doesn't look like a GitHub token (should start with `ghp_` or `github_pat_`).\n"
                    "Send it again or cancel:",
                    {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
                return
            step = get_user_step(user_id)
            step['temp_github_token'] = token_val
            _start_github_deploy(chat_id, user_id, None, step, token=token_val)
            return

        # ── Bug report submission ────────────────────────────────────
        if user_step.get('step') == 'awaiting_bug_report':
            if text.strip():
                uname = message['from'].get('username', '')
                report_id = submit_bug_report(user_id, uname, first_name, text.strip())
                set_user_step(user_id, None)
                send_message(chat_id,
                    f"✅ *Bug Report #{report_id} Submitted!*\n\n"
                    f"Thank you for your report. An admin will review it and reply to you here.\n\n"
                    f"You'll receive a notification when there's a reply.",
                    {"inline_keyboard": [[{"text": "🏠 Main Menu", "callback_data": "main_menu"}]]})
            else:
                send_message(chat_id, "❌ Report cannot be empty. Please describe the bug:",
                             {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "main_menu"}]]})
            return

        # ── Admin replying to a bug report ───────────────────────────
        if is_admin(user_id) and user_step.get('step', '').startswith('awaiting_bug_reply_'):
            report_id = int(user_step['step'].split('_')[-1])
            if text.strip():
                ok, result = reply_to_bug_report(report_id, user_id, text.strip())
                set_user_step(user_id, None)
                if ok:
                    send_message(chat_id,
                        f"✅ Reply sent to user `{result}` for report #{report_id}",
                        {"inline_keyboard": [
                            [{"text": "📋 All Reports", "callback_data": "admin_bug_reports"}],
                            [{"text": "🔙 Admin Panel", "callback_data": "admin_panel"}]]})
                else:
                    send_message(chat_id, f"❌ Error: {result}")
            else:
                send_message(chat_id, "❌ Reply cannot be empty.")
            return

        # ── Redeem code ──────────────────────────────────────────────
        if user_step.get('waiting_for_redeem') == 1 and text and not text.startswith('/'):
            process_redeem(chat_id, user_id, text)
            return

        # ── Admin coins: awaiting target user(s) ─────────────────────
        # Accepts one user ID per line, so an admin can add coins to many
        # users in a single operation instead of repeating the flow.
        if is_admin(user_id) and user_step.get('step') == 'awaiting_coins_target':
            lines = [ln.strip().lstrip('@') for ln in text.strip().split('\n')]
            lines = [ln for ln in lines if ln]

            valid_ids, invalid_lines = [], []
            for ln in lines:
                if ln.isdigit():
                    valid_ids.append(int(ln))
                else:
                    invalid_lines.append(ln)

            if not valid_ids:
                send_message(chat_id,
                    "❌ No valid numeric user ID found. Send one user ID per "
                    "line (usernames aren't supported here — use the numeric "
                    "Telegram user ID).")
                return

            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute(f"SELECT user_id FROM users WHERE user_id IN ({','.join('?' * len(valid_ids))})",
                      valid_ids)
            found_ids = {row[0] for row in c.fetchall()}
            conn.close()
            not_found = [uid for uid in valid_ids if uid not in found_ids]
            found = [uid for uid in valid_ids if uid in found_ids]

            if not found:
                send_message(chat_id,
                    f"❌ None of those user ID(s) were found in the database:\n"
                    f"`{', '.join(str(u) for u in valid_ids)}`\n\n"
                    f"They need to have started the bot at least once. Try again.")
                return

            target_payload = json.dumps(found) if len(found) > 1 else str(found[0])
            set_user_step(user_id, 'awaiting_coins_amount', temp_target_user=target_payload)

            warn = ""
            if not_found:
                warn += f"\n⚠️ Not found, skipped: `{', '.join(str(u) for u in not_found)}`"
            if invalid_lines:
                warn += f"\n⚠️ Not numeric, skipped: `{', '.join(invalid_lines[:10])}`"

            target_desc = f"`{found[0]}`" if len(found) == 1 else f"*{len(found)} users*"
            send_message(chat_id,
                f"🪙 *Add coins to {target_desc}*{warn}\n\nEnter amount or pick preset:",
                {"inline_keyboard": [
                    [{"text": "50",   "callback_data": "coin_preset_50"},
                     {"text": "100",  "callback_data": "coin_preset_100"},
                     {"text": "500",  "callback_data": "coin_preset_500"}],
                    [{"text": "❌ Cancel", "callback_data": "admin_panel"}]]})
            return

        # ── Admin coins: awaiting amount ──────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_coins_amount':
            try:
                amount = int(text.strip())
                if amount <= 0:
                    raise ValueError("non-positive")
                target = user_step.get('temp_target_user', '?')
                set_user_step(user_id, 'awaiting_coins_confirm',
                             temp_target_user=target, temp_coins_amount=amount)
                send_message(chat_id,
                    f"Confirm: add *{amount}🪙* to `{target}`?",
                    {"inline_keyboard": [
                        [{"text": "✅ Confirm", "callback_data": "coin_confirm"},
                         {"text": "❌ Cancel",  "callback_data": "admin_panel"}]]})
            except (ValueError, TypeError):
                send_message(chat_id, "❌ Send a positive whole number (e.g. `50`).")
            return

        # ── Redeem code: custom max uses ──────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_code_uses_custom':
            try:
                max_uses = int(text.strip())
                if max_uses < 0:
                    raise ValueError("negative")
                ask_code_expiry(user_id, max_uses, chat_id, None)
            except (ValueError, TypeError):
                send_message(chat_id, "❌ Send a whole number (`0` for unlimited).")
            return

        # ── Redeem code: custom expiry ─────────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_code_expiry_custom':
            try:
                expiry_days = int(text.strip())
                if expiry_days < 0:
                    raise ValueError("negative")
                if expiry_days == 0:
                    expiry_days = 36500  # "never" — a century is a practical forever
                finalize_create_code(user_id, expiry_days, chat_id, None)
            except (ValueError, TypeError):
                send_message(chat_id, "❌ Send a whole number of days (`0` for never expires).")
            return

        # ── Broadcast steps (admin) ───────────────────────────────────
        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_caption':
            set_user_step(user_id, 'awaiting_broadcast_buttons',
                         temp_broadcast_caption=text.strip(),
                         temp_broadcast_type=user_step.get('temp_broadcast_type'),
                         temp_broadcast_file=user_step.get('temp_broadcast_file'))
            send_message(chat_id,
                "*🔘 Inline Buttons (Optional)*\n\n"
                "Send buttons in this format (one per line):\n"
                "`Button Text | https://url.com`\n\n"
                "Or skip for no buttons:",
                {"inline_keyboard": [
                    [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                    [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_buttons':
            buttons = []
            for line in text.strip().splitlines():
                if '|' in line:
                    label, _, url = line.partition('|')
                    label = label.strip(); url = url.strip()
                    if label and url.startswith('http'):
                        buttons.append([{"text": label, "url": url}])
            set_user_step(user_id, 'awaiting_broadcast_confirm',
                         temp_broadcast_type=user_step.get('temp_broadcast_type'),
                         temp_broadcast_file=user_step.get('temp_broadcast_file'),
                         temp_broadcast_caption=user_step.get('temp_broadcast_caption', ''),
                         temp_broadcast_buttons=json.dumps(buttons))
            _send_broadcast_preview(chat_id, user_step.get('temp_broadcast_type'),
                                    user_step.get('temp_broadcast_file'),
                                    user_step.get('temp_broadcast_caption', ''),
                                    buttons)
            return

        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_text':
            set_user_step(user_id, 'awaiting_broadcast_buttons',
                         temp_broadcast_type='text',
                         temp_broadcast_file='',
                         temp_broadcast_caption=text.strip())
            send_message(chat_id,
                "*🔘 Inline Buttons (Optional)*\n\nFormat: `Label | https://url`\n\nOr skip:",
                {"inline_keyboard": [
                    [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                    [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

        # ── Env vars (waiting_for_env=1 OR step in awaiting_env* ) ───
        _wants_env = (
            user_step.get('waiting_for_env') == 1 or
            user_step.get('step') in ('awaiting_env', 'awaiting_env_vars')
        )
        if _wants_env and text and not text.startswith('/'):
            # GitHub path
            if user_step.get('temp_source') == 'github':
                user_step['temp_env_vars'] = text.strip()
                _launch_github_deploy(chat_id, user_id, user_step)
                return
            # Regular path – initialise env_vars safely
            raw = user_step.get('env_vars') or {}
            if isinstance(raw, str):
                try:    raw = json.loads(raw)
                except Exception: raw = {}
            env_vars = dict(raw)
            added = 0
            for line in text.strip().split('\n'):
                line = line.strip()
                if '=' in line:
                    eq = line.index('=')
                    k, v = line[:eq].strip(), line[eq+1:].strip()
                    if k:
                        env_vars[k] = v
                        added += 1
            set_user_step(user_id, 'awaiting_env',
                         waiting_for_env=1,
                         temp_file=user_step.get('temp_file'),
                         requirements=user_step.get('requirements'),
                         env_vars=env_vars,
                         plan=user_step.get('plan'),
                         duration=user_step.get('duration'),
                         cost_coins=user_step.get('cost_coins'),
                         cost_stars=user_step.get('cost_stars'),
                         payment_method=user_step.get('payment_method'))
            if added:
                send_message(chat_id,
                    f"✅ *{added}* variable(s) saved — total: `{len(env_vars)}`\n\nSend more, or:",
                    get_env_keyboard(len(env_vars)))
            else:
                send_message(chat_id,
                    "❌ No valid `KEY=VALUE` pairs found.\n\n"
                    "Format (one per line):\n```\nBOT_TOKEN=123:ABC\nAPI_KEY=xyz\n```",
                    get_env_keyboard(len(env_vars)))
            return

        # ── Waiting for requirements file ─────────────────────────────
        if user_step.get('waiting_for_reqs') == 1 and text and not text.startswith('/'):
            send_message(chat_id,
                "📦 Please *upload* your `requirements.txt` file, or click *Auto-detect*.",
                get_reqs_keyboard())
            return
        
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, first_name, None)
            return
        
        send_message(chat_id, "❌ Unknown command. Use buttons below.", get_main_menu(user_id))
        return
    
    # Handle photo upload (broadcast)
    if 'photo' in message:
        user_step = get_user_step(user_id)
        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_media' and user_step.get('temp_broadcast_type') == 'photo':
            # Pick highest-resolution photo
            file_id = message['photo'][-1]['file_id']
            caption = message.get('caption', '')
            set_user_step(user_id, 'awaiting_broadcast_caption',
                         temp_broadcast_type='photo',
                         temp_broadcast_file=file_id,
                         temp_broadcast_caption=caption)
            if caption:
                # Caption included — go straight to buttons step
                set_user_step(user_id, 'awaiting_broadcast_buttons',
                             temp_broadcast_type='photo',
                             temp_broadcast_file=file_id,
                             temp_broadcast_caption=caption)
                send_message(chat_id,
                    f"✅ Photo received! Caption: _{caption[:80]}_\n\n"
                    "*🔘 Add inline buttons?* Format: `Label | https://url`\n\nOr skip:",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            else:
                send_message(chat_id,
                    "✅ Photo received!\n\n*Add a caption* (or skip):",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Caption", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

    # Handle video upload (broadcast)
    if 'video' in message:
        user_step = get_user_step(user_id)
        if is_admin(user_id) and user_step.get('step') == 'awaiting_broadcast_media' and user_step.get('temp_broadcast_type') == 'video':
            file_id = message['video']['file_id']
            caption = message.get('caption', '')
            if caption:
                set_user_step(user_id, 'awaiting_broadcast_buttons',
                             temp_broadcast_type='video',
                             temp_broadcast_file=file_id,
                             temp_broadcast_caption=caption)
                send_message(chat_id,
                    f"✅ Video received! Caption: _{caption[:80]}_\n\n"
                    "*🔘 Add inline buttons?* Format: `Label | https://url`\n\nOr skip:",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Buttons", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            else:
                set_user_step(user_id, 'awaiting_broadcast_caption',
                             temp_broadcast_type='video',
                             temp_broadcast_file=file_id)
                send_message(chat_id,
                    "✅ Video received!\n\n*Add a caption* (or skip):",
                    {"inline_keyboard": [
                        [{"text": "⏭️ No Caption", "callback_data": "broadcast_no_buttons"}],
                        [{"text": "❌ Cancel",       "callback_data": "admin_panel"}]]})
            return

    # Handle file upload
    if 'document' in message:
        doc = message['document']
        file_name = sanitize_upload_filename(doc.get('file_name', 'unknown'), default='unknown')
        file_size = doc.get('file_size', 0)
        print(f"📁 File: {file_name} ({format_file_size(file_size)})")

        # ── Admin database restore — checked first, before the normal bot-file
        # size/type gates, since a .db backup isn't subject to those limits ──
        if is_admin(user_id) and user_step.get('step') == 'awaiting_db_restore':
            handle_db_restore_upload(message, user_id, chat_id)
            return
        
        if file_size > MAX_FILE_SIZE_BYTES:
            send_message(chat_id, f"❌ File too large! Max {MAX_FILE_SIZE_MB}MB")
            return
        
        if not is_user_verified(user_id):
            send_verification_required(chat_id, user_id, first_name, None)
            return
        
        # Requirements file — accept requirements.txt/.txt (Python) or
        # package.json/.json (Node.js) when waiting for a manifest upload
        if user_step.get('waiting_for_reqs') == 1 and (
                file_name == 'requirements.txt' or
                file_name == 'package.json' or
                file_name.endswith('.json') or
                (file_name.endswith('.txt') and 'req' in file_name.lower()) or
                file_name.endswith('.txt')):
            file_id = doc['file_id']
            file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})
            if file_info and file_info.get('ok'):
                file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
                try:
                    with urllib.request.urlopen(file_url) as resp:
                        requirements_text = resp.read().decode('utf-8', errors='replace')
                    set_user_step(user_id, 'awaiting_env',
                                 waiting_for_env=1, waiting_for_reqs=0,
                                 temp_file=user_step.get('temp_file'),
                                 requirements=requirements_text,
                                 env_vars=user_step.get('env_vars') or {},
                                 plan=user_step.get('plan'),
                                 duration=user_step.get('duration'),
                                 cost_coins=user_step.get('cost_coins'),
                                 cost_stars=user_step.get('cost_stars'),
                                 payment_method=user_step.get('payment_method'))
                    is_json_manifest = requirements_text.strip().startswith('{')
                    if is_json_manifest:
                        try:
                            _pkg_preview = json.loads(requirements_text)
                            pkg_count = (len(_pkg_preview.get('dependencies', {})) +
                                         len(_pkg_preview.get('devDependencies', {})))
                        except Exception:
                            pkg_count = 0
                    else:
                        pkg_count = len([l for l in requirements_text.splitlines()
                                         if l.strip() and not l.startswith('#')])
                    send_message(chat_id,
                        f"✅ *{'package.json' if is_json_manifest else 'Requirements'} received!* "
                        f"({pkg_count} package(s))\n\n"
                        f"```\n{requirements_text[:400]}\n```\n\n"
                        "Now set environment variables, or deploy directly:",
                        get_env_keyboard(0))
                except Exception as e:
                    send_message(chat_id, f"❌ Error reading file: {e}")
            else:
                send_message(chat_id, "❌ Could not download file from Telegram.")
            return
        
        # Python / JavaScript / TypeScript bot file for deployment
        if user_step.get('step') == 'awaiting_file':
            ALLOWED_BOT_EXTS = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx'}
            ext = os.path.splitext(file_name)[1].lower()

            if ext not in ALLOWED_BOT_EXTS:
                send_message(chat_id,
                    f"❌ Unsupported file type `{ext}`.\n\n"
                    "Supported:\n"
                    "• `.py` — Python bots (Telegram, Discord, WhatsApp…)\n"
                    "• `.js` / `.mjs` — Node.js bots\n"
                    "• `.ts` / `.tsx` / `.jsx` — TypeScript / JSX bots\n\n"
                    "For Node.js projects with multiple files, zip them and send the `.zip`.")
                return

            scan_msg = send_message(chat_id, "🔒 *Running 6-layer security scan…*", None)
            scan_msg_id = (scan_msg or {}).get('result', {}).get('message_id')

            file_id   = doc['file_id']
            file_info = http_get(f"{TELEGRAM_API}/getFile", {"file_id": file_id})

            if not file_info or not file_info.get('ok'):
                send_message(chat_id, "❌ Failed to download file from Telegram.")
                return

            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['result']['file_path']}"
            try:
                with urllib.request.urlopen(file_url) as resp:
                    file_bytes_content = resp.read()
            except Exception as dl_err:
                send_message(chat_id, f"❌ Download error: {dl_err}")
                return

            # ── Security scan ─────────────────────────────────────────
            blocked, critical, scan_warnings, report = run_security_scan(
                file_bytes_content, file_name)

            if blocked:
                # Save blocked file to disk so admin can approve and deploy it later
                pending_path = BASE_DIR / f"pending_review_{user_id}_{file_name}"
                try:
                    pending_path.write_bytes(file_bytes_content)
                except Exception:
                    pending_path = None

                # Store review record in DB
                current_step_json = json.dumps(user_step)
                conn_r = sqlite3.connect(DATABASE_FILE, timeout=10)
                c_r = conn_r.cursor()
                c_r.execute("""INSERT INTO security_reviews
                    (user_id, chat_id, file_name, file_path, critical_json,
                     warnings_json, report_text, source, status, created_at, user_step_json)
                    VALUES (?,?,?,?,?,?,?,'upload','pending',?,?)""",
                    (user_id, chat_id, file_name,
                     str(pending_path) if pending_path else '',
                     json.dumps(critical), json.dumps(scan_warnings),
                     report, datetime.now().isoformat(), current_step_json))
                review_id = c_r.lastrowid
                conn_r.commit()
                conn_r.close()

                # Tell the user their file is under review
                review_text = (
                    f"🔒 *SECURITY REVIEW REQUIRED*\n\n"
                    f"Your file `{safe_md(file_name)}` triggered security checks:\n\n"
                    f"{report}\n\n"
                    f"Your file has been sent to an admin for review.\n"
                    f"You'll be notified once a decision is made.\n"
                    f"🆔 Review ID: `{review_id}`"
                )
                if scan_msg_id:
                    edit_message(chat_id, scan_msg_id, review_text,
                                 {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})
                else:
                    send_message(chat_id, review_text,
                                 {"inline_keyboard": [[{"text": "🏠 Menu", "callback_data": "main_menu"}]]})

                # Notify every admin with Approve / Reject / Approve with Warning buttons
                admin_text = (
                    f"🚨 *SECURITY REVIEW #{review_id}*\n\n"
                    f"👤 User: `{user_id}`\n"
                    f"📁 File: `{safe_md(file_name)}`\n"
                    f"⚠️ Critical issues: `{len(critical)}`\n"
                    f"🔔 Warnings: `{len(scan_warnings)}`\n\n"
                    f"*Issues found:*\n"
                    + '\n'.join(f"• {safe_md(c[:120])}" for c in critical[:8])
                    + (f"\n_...and {len(critical)-8} more_" if len(critical) > 8 else "")
                    + f"\n\n*What would you like to do?*"
                )
                admin_kb = {"inline_keyboard": [
                    [{"text": "✅ Approve & Deploy",
                      "callback_data": f"sec_approve_{review_id}"},
                     {"text": "⚠️ Approve (warn user)",
                      "callback_data": f"sec_approve_warn_{review_id}"}],
                    [{"text": "❌ Reject",
                      "callback_data": f"sec_reject_{review_id}"}],
                    [{"text": "📋 View Full Report",
                      "callback_data": f"sec_report_{review_id}"}],
                ]}
                for admin_id in ADMIN_IDS:
                    try:
                        send_message(admin_id, admin_text, admin_kb)
                    except Exception:
                        pass
                return

            # ── Save file ─────────────────────────────────────────────
            temp_file = BASE_DIR / f"temp_{user_id}_{file_name}"
            try:
                with open(temp_file, 'wb') as f:
                    f.write(file_bytes_content)
            except Exception as save_err:
                send_message(chat_id, f"❌ Could not save file: {save_err}")
                return

            set_user_step(user_id, 'awaiting_reqs',
                         temp_file=str(temp_file),
                         plan=user_step.get('plan'),
                         duration=user_step.get('duration'),
                         cost_coins=user_step.get('cost_coins'),
                         cost_stars=user_step.get('cost_stars'),
                         payment_method=user_step.get('payment_method'),
                         env_vars={})

            # Build scan summary line
            if scan_warnings:
                scan_note = f"⚠️ {len(scan_warnings)} warning(s) noted — review logs after deployment."
            else:
                scan_note = "✅ Security scan passed."

            lang_label = {
                '.py': '🐍 Python', '.js': '🟨 Node.js', '.mjs': '🟨 Node.js (ESM)',
                '.cjs': '🟨 Node.js (CJS)', '.ts': '🔷 TypeScript',
                '.tsx': '🔷 TypeScript/React', '.jsx': '🟨 JavaScript/React',
            }.get(ext, '📄 Script')

            summary = (
                f"*✅ File accepted* — {lang_label}\n\n"
                f"📁 `{file_name}` ({format_file_size(file_size)})\n"
                f"🔒 {scan_note}\n\n"
            )
            if scan_warnings:
                summary += f"*Warnings:*\n" + '\n'.join(f'• {w}' for w in scan_warnings[:4]) + "\n\n"
            summary += (
                f"📋 Plan: `{(user_step.get('plan') or 'free').upper()}`\n"
                f"⏱️ Duration: `{user_step.get('duration', 'N/A')}` days\n\n"
                "*📦 Add requirements?*\nUpload `requirements.txt` or `package.json`, or click Auto-detect:"
            )

            if scan_msg_id:
                edit_message(chat_id, scan_msg_id, summary, get_reqs_keyboard())
            else:
                send_message(chat_id, summary, get_reqs_keyboard())
            return

        else:
            send_message(chat_id, "❌ Please start deployment using the menu first.", get_main_menu(user_id))
        return
    
    if not is_user_verified(user_id):
        send_verification_required(chat_id, user_id, first_name, None)
    else:
        send_message(chat_id, "❌ Please use the buttons below.", get_main_menu(user_id))

# ==================== PAYMENT HANDLERS ====================
def handle_successful_payment(message):
    try:
        user_id = message['from']['id']
        chat_id = message['chat']['id']
        successful_payment = message['successful_payment']
        total_amount = successful_payment.get('total_amount', 0)
        invoice_payload = successful_payment.get('invoice_payload', '')
        
        print(f"💰 Payment: {total_amount}⭐ from user {user_id}")
        
        if invoice_payload.startswith("premium_"):
            parts = invoice_payload.split("_")
            plan = parts[1]
            
            if plan == "monthly":
                activate_premium(user_id, "monthly", total_amount, total_amount * STARS_PER_COIN, 30)
                send_message(chat_id, f"✅ *PREMIUM ACTIVATED!*\n\nMonthly plan active for 30 days!\nThank you! 🙏")
            elif plan == "yearly":
                activate_premium(user_id, "yearly", total_amount, total_amount * STARS_PER_COIN, 365)
                send_message(chat_id, f"✅ *PREMIUM ACTIVATED!*\n\nYearly plan active for 365 days!\nThank you! 🙏")
            return

        # ── Paid GitHub deployment ───────────────────────────────────
        if invoice_payload.startswith("ghdeploy_"):
            row = db_execute('''SELECT requirements FROM pending_deployments
                                WHERE user_id = ? AND payload = ? AND status = 'pending'
                                ORDER BY id DESC LIMIT 1''', (user_id, invoice_payload), fetch='one')
            if not row:
                send_message(chat_id, "⚠️ Payment received but the deployment details were not found. "
                                      "Please contact an admin.")
                return
            db_execute("UPDATE pending_deployments SET status = 'completed' WHERE payload = ?",
                       (invoice_payload,))
            db_execute('''INSERT INTO star_transactions
                (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, -total_amount, "deployment_payment", "telegram_stars",
                 None, datetime.now().isoformat(), 'completed', invoice_payload))
            try:
                gh_step = json.loads(row[0])
            except Exception:
                gh_step = {}
            _launch_github_deploy(chat_id, user_id, gh_step,
                                  main_file_name=gh_step.get('temp_main_file') or None,
                                  paid_method='stars')
            async_backup(f"stars_github_{user_id}")
            return

        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute('''SELECT temp_file, requirements, env_vars, plan, duration, cost_coins, cost_stars, payment_method
                     FROM pending_deployments 
                     WHERE user_id = ? AND payload = ? AND status = 'pending'
                     ORDER BY id DESC LIMIT 1''', (user_id, invoice_payload))
        pending = c.fetchone()
        
        if pending:
            temp_file, requirements, env_vars_json, plan, duration, cost_coins, cost_stars, payment_method = pending
            env_vars = json.loads(env_vars_json) if env_vars_json else {}
            
            # Record outgoing star transaction (user spent real Stars — do NOT credit their internal balance)
            try:
                conn2 = sqlite3.connect(DATABASE_FILE)
                c2 = conn2.cursor()
                c2.execute('''INSERT INTO star_transactions 
                    (user_id, amount, transaction_type, source, reference_id, timestamp, status, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, -total_amount, "deployment_payment", "telegram_stars",
                     None, datetime.now().isoformat(), 'completed', invoice_payload))
                conn2.commit()
                conn2.close()
            except Exception as tx_e:
                print(f"⚠️ Star transaction record error: {tx_e}")
            
            deploy_with_logs_enhanced(chat_id, user_id, temp_file, requirements, env_vars, 
                            plan, duration, cost_coins, cost_stars, payment_method)
            async_backup(f"stars_payment_{user_id}")
            
            c.execute('UPDATE pending_deployments SET status = "completed" WHERE payload = ?', (invoice_payload,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Payment error: {e}")

def handle_pre_checkout_query(pre_checkout_query):
    try:
        query_id = pre_checkout_query['id']
        url = f"{TELEGRAM_API}/answerPreCheckoutQuery"
        data = {"pre_checkout_query_id": query_id, "ok": True}
        data_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8')).get('ok', False)
    except Exception as e:
        print(f"❌ Pre-checkout error: {e}")
        return False

# ==================== BACKGROUND MONITORS ====================

def deployment_expiry_monitor():
    """
    Runs every 60 s. Stops bots whose expire_time has passed and updates their
    status. Free-tier bots are simply stopped; paid bots with an expired premium
    owner are paused (so they can be resumed on renewal).
    """
    print("✅ Deployment expiry monitor started")
    while True:
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            now_iso = datetime.now().isoformat()

            # Find all active/paused deployments that have expired.
            # expire_time IS NULL means a lifetime (never-expiring) deployment —
            # SQLite's NULL <= x is already NULL/false so these are naturally
            # excluded, but the explicit check makes the intent unambiguous.
            c.execute("""SELECT deployment_id, user_id, proc_pid, is_free, plan, folder_name
                         FROM deployments
                         WHERE status IN ('active', 'paused')
                           AND expire_time IS NOT NULL AND expire_time <= ?""",
                      (now_iso,))
            expired = c.fetchall()

            for dep_id, uid, proc_pid, is_free, plan, folder_name_val in expired:
                if proc_pid:
                    try:
                        kill_deployment_process(proc_pid)
                    except Exception:
                        pass

                c.execute("""UPDATE deployments
                             SET status='stopped', proc_pid=NULL, is_paused=0
                             WHERE deployment_id=?""", (dep_id,))
                with deployment_lock:
                    active_deployments.pop(dep_id, None)

                print(f"⏰ Expired deployment {dep_id} (user {uid}) stopped")

                # Notify user with relevant action buttons
                try:
                    if is_free:
                        # Free bot expired: offer restart for another 24h
                        send_message(uid,
                            f"⏰ *Free Bot #{dep_id} Expired*\n\n"
                            f"Your 24-hour deployment has ended.\n"
                            f"Restart for another 24h — your database is intact.",
                            {"inline_keyboard": [
                                [{"text": "🔄 Restart (Free 24h)",  "callback_data": f"restart_deploy_{dep_id}"}],
                                [{"text": "⭐ Get Premium",          "callback_data": "subscribe_premium"}],
                                [{"text": "📦 My Deployments",       "callback_data": "my_deployments"}],
                            ]})
                    else:
                        # Premium bot expired: reactivate or continue free
                        send_message(uid,
                            f"⏰ *Premium Bot #{dep_id} Expired*\n\n"
                            f"Your `{plan.upper()}` plan has ended.\n\n"
                            f"*Options:*\n"
                            f"• ⭐ Reactivate Premium — bot resumes immediately with full history\n"
                            f"• 🆓 Continue as Free — bot runs 24h more, database preserved",
                            {"inline_keyboard": [
                                [{"text": "⭐ Reactivate Premium",   "callback_data": f"reactivate_premium_{dep_id}"}],
                                [{"text": "🆓 Continue Free (24h)", "callback_data": f"continue_as_free_{dep_id}"}],
                                [{"text": "📦 My Deployments",       "callback_data": "my_deployments"}],
                            ]})
                except Exception:
                    pass

            conn.commit()
            conn.close()

            update_system_stats()
        except Exception as e:
            print(f"❌ Expiry monitor error: {e}")

        sleep(60)


# Crash-loop protection: cap automatic restarts within a rolling window so a
# permanently-broken script can't burn CPU restarting forever.
_AUTO_RESTART_MAX_ATTEMPTS  = 5
_AUTO_RESTART_WINDOW_SECONDS = 3600  # 1 hour


def _auto_relaunch_deployment(deployment_id, reason="crash", bypass_cap=False):
    """
    System-initiated relaunch — no chat_id/ownership check, used by the
    health monitor (crash recovery) and startup reconciliation (host process
    restart). Unlike the user-facing restart_deployment(), this NEVER extends
    expire_time — it keeps exactly the expiry the deployment already had, and
    an already-expired deployment is left alone rather than resurrected.

    Returns a status string: 'restarted', 'expired', 'paused_premium',
    'files_missing', 'cap_exceeded', 'launch_failed', 'not_found'.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("""SELECT user_id, file_name, is_paused, env_vars, status,
                        start_time, expire_time, is_free, plan,
                        crash_restart_count, last_crash_restart
                 FROM deployments WHERE deployment_id=?""", (deployment_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "not_found"

    (owner_id, file_name, is_paused, env_vars_json, status,
     start_time_str, expire_time_str, is_free, plan,
     crash_count, last_crash_str) = row
    crash_count = crash_count or 0

    # Never resurrect a deployment whose time is genuinely up.
    if expire_time_str:
        try:
            if datetime.fromisoformat(expire_time_str) < datetime.now():
                conn.close()
                return "expired"
        except Exception:
            pass

    if is_paused and not is_free and plan != "lifetime":
        conn.close()
        return "paused_premium"

    now = datetime.now()
    if not bypass_cap:
        if last_crash_str:
            try:
                if (now - datetime.fromisoformat(last_crash_str)).total_seconds() > _AUTO_RESTART_WINDOW_SECONDS:
                    crash_count = 0  # rolling window expired — reset the budget
            except Exception:
                crash_count = 0
        if crash_count >= _AUTO_RESTART_MAX_ATTEMPTS:
            conn.close()
            return "cap_exceeded"

    deploy_folder = get_deploy_folder(owner_id, deployment_id)
    dest_script   = deploy_folder / file_name

    if not deploy_folder.exists() or not dest_script.exists():
        # Files are gone — almost always means the host's disk was wiped on a
        # redeploy (see PERSISTENT_DISK_PATH). Nothing to relaunch from.
        c.execute("UPDATE deployments SET status='stopped', proc_pid=NULL WHERE deployment_id=?",
                  (deployment_id,))
        conn.commit()
        conn.close()
        try:
            send_message(owner_id,
                f"⚠️ *Bot #{deployment_id} could not be restarted*\n\n"
                f"Its files are missing on disk (most likely lost during a host "
                f"restart/redeploy). Please redeploy this bot — your account "
                f"data and coin balance are unaffected.",
                {"inline_keyboard": [[{"text": "📦 My Deployments", "callback_data": "my_deployments"}]]})
        except Exception:
            pass
        return "files_missing"

    env_vars = json.loads(env_vars_json) if env_vars_json else {}

    env_file = deploy_folder / ".env"
    env_file.write_text('\n'.join(f"{k}={v}" for k, v in env_vars.items()) + '\n')

    # Branch on file type exactly like the original deploy path does — a
    # Node.js deployment must NOT be regenerated through the Python launcher,
    # or its start.sh gets silently overwritten with a script that tries to
    # run a .js file via `python3`, which fails outright.
    ext = dest_script.suffix.lower()
    is_node = ext in ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')

    if is_node:
        start_script, _ = create_node_launcher_script(
            deploy_folder, dest_script, env_vars, lambda x: None)
    else:
        try:
            code_content = dest_script.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            code_content = ""
        launcher_script, _ = create_enhanced_launcher_script(
            deploy_folder, dest_script, env_vars, code_content, lambda x: None)
        start_script = deploy_folder / "start.sh"
        start_script.write_text(
            f'#!/bin/bash\ncd "{deploy_folder}"\nexport PYTHONUNBUFFERED=1\n'
            f'setsid nohup {sys.executable} "{launcher_script}" > output.log 2>&1 &\necho $! > pid.txt\n')
        start_script.chmod(0o755)

    subprocess.run([str(start_script)], cwd=str(deploy_folder), capture_output=True)
    sleep(4)

    pid_file = deploy_folder / "pid.txt"
    new_pid = None
    if pid_file.exists():
        try:
            new_pid = int(pid_file.read_text().strip())
        except Exception:
            pass

    is_running = False
    if new_pid:
        try:
            os.kill(new_pid, 0)
            is_running = True
        except Exception:
            pass

    if is_running:
        c.execute("""UPDATE deployments
                     SET proc_pid=?, status='active', is_paused=0,
                         crash_restart_count=?, last_crash_restart=?
                     WHERE deployment_id=?""",
                  (new_pid, crash_count + 1, now.isoformat(), deployment_id))
        conn.commit()
        conn.close()
        with deployment_lock:
            active_deployments[deployment_id] = new_pid
        print(f"🔄 Auto-restarted deployment {deployment_id} ({reason})")
        try:
            send_message(owner_id,
                f"🔄 *Bot #{deployment_id} auto-restarted*\n\n"
                f"It stopped unexpectedly and has been brought back online automatically.\n"
                f"Your database and settings are untouched.",
                {"inline_keyboard": [[{"text": "📄 View Logs", "callback_data": f"view_runtime_logs_{deployment_id}"}]]})
        except Exception:
            pass
        return "restarted"
    else:
        c.execute("""UPDATE deployments
                     SET status='stopped', proc_pid=NULL,
                         crash_restart_count=?, last_crash_restart=?
                     WHERE deployment_id=?""",
                  (crash_count + 1, now.isoformat(), deployment_id))
        conn.commit()
        conn.close()
        print(f"❌ Auto-restart launch failed for deployment {deployment_id}")
        return "launch_failed"


def process_health_monitor():
    """
    Runs every 120 s. Checks whether PIDs recorded as 'active' are still
    alive. If a process has died, it is automatically relaunched (unless the
    deployment has expired, is paused pending premium renewal, its files are
    missing, or it has hit the crash-loop cap) so bots stay up on their own
    instead of requiring the owner to notice and click Restart.
    """
    print("✅ Process health monitor started")
    while True:
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            c = conn.cursor()
            c.execute("SELECT deployment_id, user_id, proc_pid FROM deployments WHERE status = 'active' AND proc_pid IS NOT NULL")
            active = c.fetchall()
            conn.close()

            for dep_id, uid, proc_pid in active:
                alive = False
                try:
                    os.kill(proc_pid, 0)   # signal 0 — just checks existence
                    alive = True
                except (ProcessLookupError, PermissionError):
                    alive = False
                except Exception:
                    alive = True  # unknown error → assume alive

                if alive:
                    continue

                with deployment_lock:
                    active_deployments.pop(dep_id, None)
                print(f"💀 Dead process for deployment {dep_id} (user {uid}) — attempting auto-restart")

                result = _auto_relaunch_deployment(dep_id, reason="crash")

                if result in ("restarted", "expired", "files_missing", "paused_premium"):
                    # Each of these already left the deployment in a correct,
                    # user-visible state (running again, or clearly stopped
                    # with an explanation) — nothing further to do.
                    continue

                # cap_exceeded / launch_failed / not_found: auto-restart
                # couldn't bring it back — fall back to a manual prompt so
                # the owner isn't left in the dark.
                try:
                    send_message(uid,
                        f"⚠️ *Bot #{dep_id} Crashed*\n\n"
                        f"Automatic restart didn't succeed. Your database is safe — "
                        f"click Restart to try again manually.",
                        {"inline_keyboard": [
                            [{"text": "🔄 Restart Bot", "callback_data": f"restart_deploy_{dep_id}"}],
                            [{"text": "📄 View Logs",   "callback_data": f"view_runtime_logs_{dep_id}"}],
                        ]})
                except Exception:
                    pass

        except Exception as e:
            print(f"❌ Health monitor error: {e}")

        sleep(120)


# ==================== HEALTH CHECK SERVER ====================
def start_health_server():
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                # Return live deployment count so monitors know the service is up
                try:
                    conn = sqlite3.connect(DATABASE_FILE, timeout=5)
                    active = conn.execute(
                        "SELECT COUNT(*) FROM deployments WHERE status='active'").fetchone()[0]
                    conn.close()
                except Exception:
                    active = -1
                payload = (
                    b'{"status":"healthy","version":"2.0",'
                    b'"active_deployments":' + str(active).encode() +
                    b',"timestamp":"' + datetime.now().isoformat().encode() + b'"}'
                )
                self.wfile.write(payload)
            def log_message(self, format, *args):
                pass
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(('0.0.0.0', port), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="HealthServer").start()
        print(f"✅ Health check server on port {port}")
    except Exception as e:
        print(f"⚠️ Health server: {e}")


def _self_ping_thread():
    """
    Pings the bot's own health endpoint every 10 minutes so cloud platforms
    (Render free tier, Heroku eco dynos, Fly.io, etc.) never spin the service
    down for inactivity.  Without this, the host dies after ~15 min of silence
    and takes every hosted subprocess with it.
    """
    port = int(os.environ.get("PORT", 8080))

    # Try RENDER_EXTERNAL_HOSTNAME first (works even behind the load-balancer),
    # fall back to localhost for other platforms.
    ext_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    if ext_host:
        urls = [f"https://{ext_host}/", f"http://localhost:{port}/"]
    else:
        urls = [f"http://localhost:{port}/"]

    # Give the health server 20 s to start before the first ping
    sleep(20)
    print(f"✅ Self-ping keep-alive started (every 10 min) → {urls[0]}")

    while True:
        for url in urls:
            try:
                req = urllib.request.Request(url,
                    headers={"User-Agent": "BotHosting-KeepAlive/2.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    _ = r.read()
                print(f"🏓 Keep-alive ping OK ({url})")
                break                 # first successful URL is enough
            except Exception as e:
                print(f"⚠️  Keep-alive ping failed ({url}): {e}")
        sleep(600)   # 10 minutes


def _hosted_bot_watchdog():
    """
    Checks every 90 seconds that every 'active' deployment's process is still
    alive.  If a hosted bot has crashed (dead PID), it is automatically
    relaunched so the user's bot stays up 24/7 without manual intervention.
    Bots that have genuinely expired are left to the expiry monitor.
    """
    sleep(60)   # let startup reconciliation finish first
    print("✅ Hosted-bot watchdog started (90 s cycle)")

    while True:
        try:
            conn = sqlite3.connect(DATABASE_FILE, timeout=10)
            c = conn.cursor()
            c.execute("""SELECT deployment_id, proc_pid, expire_time
                         FROM deployments
                         WHERE status = 'active' AND proc_pid IS NOT NULL""")
            rows = c.fetchall()
            conn.close()

            for dep_id, pid, expire_time_str in rows:
                # Skip expired deployments — the expiry monitor handles those
                if expire_time_str:
                    try:
                        if datetime.fromisoformat(expire_time_str) < datetime.now():
                            continue
                    except Exception:
                        pass

                # Check if PID is actually alive
                alive = False
                try:
                    os.kill(pid, 0)
                    alive = True
                except (ProcessLookupError, OSError):
                    alive = False
                except Exception:
                    alive = True   # unknown error — assume alive

                if not alive:
                    print(f"⚠️  Watchdog: dep#{dep_id} PID {pid} is dead — relaunching…")
                    try:
                        result = _auto_relaunch_deployment(dep_id, reason="watchdog")
                        print(f"   → dep#{dep_id} relaunch: {result}")
                    except Exception as re:
                        print(f"   → dep#{dep_id} relaunch error: {re}")

        except Exception as e:
            print(f"⚠️  Watchdog cycle error: {e}")

        sleep(90)

# ==================== MAIN ====================
def reconcile_deployments_on_startup():
    """
    Runs once at boot, after the (possibly GitHub-restored) database is
    loaded. Any deployment marked 'active' in the DB has a proc_pid from the
    PREVIOUS process — meaningless here, since this is a fresh interpreter
    (whether from a crash, a manual restart, or a redeploy). Without this,
    those bots would just sit in the DB as "active" while nothing is actually
    running, until someone happens to notice and click Restart. Non-expired
    ones are relaunched; ones whose files didn't survive (see
    PERSISTENT_DISK_PATH) are marked stopped with a clear explanation.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT deployment_id FROM deployments WHERE status IN ('active', 'paused')")
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    print(f"🔄 Reconciling {len(rows)} deployment(s) from previous run...")
    counts = {}
    for (dep_id,) in rows:
        result = _auto_relaunch_deployment(dep_id, reason="startup", bypass_cap=True)
        counts[result] = counts.get(result, 0) + 1
        sleep(0.5)  # avoid hammering disk/CPU relaunching many bots at once

    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"✅ Reconciliation complete: {summary}")

    if counts.get("files_missing"):
        print(f"⚠️  {counts['files_missing']} deployment(s) lost their files on this restart. "
              f"This means BASE_DIR is not on a real persistent disk — set the "
              f"PERSISTENT_DISK_PATH env var to a mounted Render Disk to fix this permanently.")


def setup_memory_compression():
    """
    Best-effort RAM compression via zram (compressed swap held in RAM).

    Needs Linux, root, and a kernel with the zram module. Inside most managed
    containers (Render, Heroku, Choreo, Android) this is simply not permitted,
    so every step is guarded and failure is reported once, never raised.
    Controlled by env:
      ZRAM_ENABLE=1|0        (default 1)
      ZRAM_PERCENT=50        zram device size as % of total RAM
      ZRAM_ALGO=zstd         zstd | lz4 | lzo-rle
    On hosts where this can't run, the same effect is achieved from outside
    the app (e.g. the `zram-tools` package on a VPS, or simply more RAM).
    """
    if os.environ.get("ZRAM_ENABLE", "1") != "1":
        return False
    if platform.system() != "Linux" or IS_ANDROID:
        return False
    try:
        if os.geteuid() != 0:
            print("ℹ️  zram skipped: not running as root")
            return False
    except AttributeError:
        return False

    try:
        # Already active? Don't stack a second device.
        with open('/proc/swaps') as f:
            if any(line.startswith('/dev/zram') for line in f):
                print("✅ zram already active")
                return True

        percent = max(10, min(100, int(os.environ.get("ZRAM_PERCENT", 50))))
        algo = os.environ.get("ZRAM_ALGO", "zstd")
        total_mb, _, _ = _read_proc_meminfo()
        if not total_mb:
            return False
        size_bytes = int(total_mb * 1024 * 1024 * percent / 100)

        def _sh(cmd):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return r.returncode == 0

        if not os.path.exists('/sys/block/zram0'):
            if not _sh(['modprobe', 'zram', 'num_devices=1']):
                print("ℹ️  zram skipped: kernel module unavailable")
                return False
        if not os.path.exists('/sys/block/zram0'):
            print("ℹ️  zram skipped: /sys/block/zram0 not present")
            return False

        # Device must be reset before (re)configuring
        try:
            with open('/sys/block/zram0/reset', 'w') as f:
                f.write('1')
        except Exception:
            pass

        try:
            with open('/sys/block/zram0/comp_algorithm', 'w') as f:
                f.write(algo)
        except Exception:
            pass  # unsupported algorithm → kernel default is fine
        with open('/sys/block/zram0/disksize', 'w') as f:
            f.write(str(size_bytes))

        if not _sh(['mkswap', '/dev/zram0']):
            print("ℹ️  zram skipped: mkswap failed")
            return False
        # High priority so it is preferred over any disk swap
        if not _sh(['swapon', '-p', '100', '/dev/zram0']):
            print("ℹ️  zram skipped: swapon failed")
            return False

        # zram is cheap, so let the kernel prefer it over dropping page cache
        try:
            with open('/proc/sys/vm/swappiness', 'w') as f:
                f.write('100')
            with open('/proc/sys/vm/page-cluster', 'w') as f:
                f.write('0')
        except Exception:
            pass

        print(f"✅ zram enabled: {size_bytes/1024/1024:.0f} MB ({algo}), swappiness=100")
        return True
    except Exception as e:
        print(f"ℹ️  zram setup skipped: {e}")
        return False


def main():
    global LAST_UPDATE_ID

    # Try to enable compressed RAM (no-op where the host doesn't allow it)
    setup_memory_compression()
    
    if (IS_RENDER or IS_HEROKU or IS_CHOREO) and not USING_PERSISTENT_DISK:
        print("=" * 70)
        print("⚠️  WARNING: no persistent disk configured (PERSISTENT_DISK_PATH unset).")
        print("   Deployment files live inside the app's own source checkout, which")
        print("   most cloud platforms rebuild from git on every redeploy. Deployed")
        print("   bots' code/packages will NOT survive a redeploy unless you attach")
        print("   a real persistent disk and point PERSISTENT_DISK_PATH at its mount.")
        print("   (The database itself is separately backed up to GitHub if configured.)")
        print("=" * 70)

    # ── Restore database from GitHub BEFORE init_db() ──────────────
    # This must happen first so we never overwrite existing data with
    # a fresh empty schema.
    github_restore_db()
    # ───────────────────────────────────────────────────────────────
    
    init_db()

    # ── Bring back whatever was running before this process started ──
    reconcile_deployments_on_startup()
    # ───────────────────────────────────────────────────────────────
    
    # Start health check server for cloud platforms
    if IS_RENDER or IS_HEROKU or IS_CHOREO:
        start_health_server()
    
    # Start background monitors
    threading.Thread(target=deployment_expiry_monitor,  daemon=True, name="ExpiryMonitor").start()
    threading.Thread(target=process_health_monitor,     daemon=True, name="HealthMonitor").start()
    threading.Thread(target=_periodic_backup_thread,    daemon=True, name="PeriodicBackup").start()
    threading.Thread(target=_self_ping_thread,          daemon=True, name="KeepAlive").start()
    threading.Thread(target=_hosted_bot_watchdog,       daemon=True, name="BotWatchdog").start()
    threading.Thread(target=quota_monitor,              daemon=True, name="QuotaMonitor").start()
    
    try:
        me = http_get(f"{TELEGRAM_API}/getMe")
        if me and me.get('ok'):
            print(f"✅ Bot: @{me['result']['username']}")
        else:
            print("❌ Check BOT_TOKEN!")
            return
    except Exception as e:
        print(f"❌ Error: {e}")
        return
    
    print("=" * 70)
    print("✅ Bot running! Press Ctrl+C to stop")
    print("=" * 70)
    
    while True:
        try:
            params = {"offset": LAST_UPDATE_ID + 1, "timeout": 30}
            data = http_get(f"{TELEGRAM_API}/getUpdates", params)
            
            if data and data.get('ok'):
                for update in data['result']:
                    LAST_UPDATE_ID = update['update_id']

                    def _handle_one(update=update):
                        # Each update is isolated: if handling one throws, we log
                        # it and move on instead of silently dropping that
                        # user's message with no reply and no trace.
                        try:
                            if 'callback_query' in update:
                                handle_callback(update['callback_query'])
                            elif 'pre_checkout_query' in update:
                                handle_pre_checkout_query(update['pre_checkout_query'])
                            elif 'message' in update:
                                msg = update['message']
                                if 'successful_payment' in msg:
                                    handle_successful_payment(msg)
                                else:
                                    handle_message(msg)
                        except Exception as ue:
                            print(f"❌ Error handling update {update['update_id']}: {ue}")
                            traceback.print_exc()
                            # Best-effort: let the user know something broke instead
                            # of leaving them with total silence.
                            try:
                                fail_chat_id = None
                                if 'message' in update:
                                    fail_chat_id = update['message']['chat']['id']
                                elif 'callback_query' in update:
                                    fail_chat_id = update['callback_query']['message']['chat']['id']
                                if fail_chat_id:
                                    send_message(fail_chat_id,
                                        "⚠️ Something went wrong processing that. Please try again.")
                            except Exception:
                                pass

                    # Run in a background thread — a slow operation for one
                    # user (deployment installs, GitHub downloads, broadcasts)
                    # must never block the main loop from polling and
                    # dispatching updates for every OTHER user in the
                    # meantime. Each DB call already opens its own fresh
                    # sqlite3 connection, so this is safe across threads.
                    threading.Thread(target=_handle_one, daemon=True,
                                     name=f"Update-{update['update_id']}").start()
            sleep(0.5)
        except KeyboardInterrupt:
            print("\n🛑 Bot stopped")
            break
        except Exception as e:
            print(f"⚠️ Error: {e}")
            traceback.print_exc()
            sleep(5)

if __name__ == "__main__":
    main()
