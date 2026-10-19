"""
Shared config, data store and helpers for Web-Host.
"""
import os
import re
import json
import time
import hmac
import shutil
import base64
import hashlib
import zipfile
import logging
import secrets
import threading
from datetime import datetime, timedelta

import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("webhost")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Point DATA_DIR at a persistent disk (Render Disk -> /data) to keep sites/services across deploys.
DATA_DIR = os.environ.get("DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

TOKEN = os.environ.get("BOT_TOKEN", "")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
except ValueError:
    ADMIN_ID = 0
PORT = int(os.environ.get("PORT", "8000"))
DOMAIN = (os.environ.get("DOMAIN") or os.environ.get("RENDER_EXTERNAL_URL") or f"http://localhost:{PORT}").rstrip("/")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_UNZIPPED_MB = int(os.environ.get("MAX_UNZIPPED_MB", "300"))
MAX_ZIP_FILES = int(os.environ.get("MAX_ZIP_FILES", "10000"))
MAX_SITES_PER_USER = int(os.environ.get("MAX_SITES_PER_USER", "10"))

WEBSITES_DIR = os.path.join(DATA_DIR, "websites")
SERVICES_DIR = os.path.join(DATA_DIR, "services")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
SECRETS_FILE = os.path.join(DATA_DIR, ".secrets.json")
os.makedirs(WEBSITES_DIR, exist_ok=True)
os.makedirs(SERVICES_DIR, exist_ok=True)

RESERVED_NAMES = {
    "api", "console", "svc", "hooks", "static", "ui", "health", "assets",
    "favicon.ico", "robots.txt", "admin", "login",
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
STARTED_AT = time.time()


# --------------- SECRETS (never hard-coded defaults) ---------------
def _load_secrets():
    data = {}
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
    changed = False
    for key in ("api_key", "secret_key"):
        if not data.get(key):
            data[key] = secrets.token_urlsafe(32)
            changed = True
    if changed:
        with open(SECRETS_FILE, "w") as f:
            json.dump(data, f)
        try:
            os.chmod(SECRETS_FILE, 0o600)
        except OSError:
            pass
    return data


_secrets = _load_secrets()
API_KEY = os.environ.get("API_KEY") or _secrets["api_key"]
SECRET_KEY = os.environ.get("SECRET_KEY") or _secrets["secret_key"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or API_KEY
GENERATED_PASSWORD = not (os.environ.get("ADMIN_PASSWORD") or os.environ.get("API_KEY"))

TOKEN_TTL = 60 * 60 * 24 * 7


def make_token():
    exp = str(int(time.time()) + TOKEN_TTL)
    sig = hmac.new(SECRET_KEY.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_token(token):
    try:
        exp, sig = token.split(".", 1)
        good = hmac.new(SECRET_KEY.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


# --------------- JSON DATA STORE ---------------
_data_lock = threading.Lock()


def _empty():
    return {"sites": {}, "users": {}, "analytics": {}, "banned": []}


def _read_unlocked():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                d = json.load(f)
            for k, v in _empty().items():
                d.setdefault(k, v)
            return d
        except Exception:
            logger.exception("data.json unreadable, using empty store")
    return _empty()


def _write_unlocked(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, DATA_FILE)


def load_data():
    with _data_lock:
        return _read_unlocked()


def update_data(modify_fn):
    """Atomically read-modify-write data.json."""
    with _data_lock:
        data = _read_unlocked()
        modify_fn(data)
        _write_unlocked(data)
        return data


# --------------- VISIT ANALYTICS (buffered, no disk write per request) ---------------
_visit_buf = {}
_visit_lock = threading.Lock()


def track_visit(site):
    with _visit_lock:
        _visit_buf[site] = _visit_buf.get(site, 0) + 1


def _flush_visits():
    with _visit_lock:
        buf = dict(_visit_buf)
        _visit_buf.clear()
    if not buf:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")

    def _apply(data):
        an = data.setdefault("analytics", {})
        for site, n in buf.items():
            s = an.setdefault(site, {"visits": 0, "last_visit": None})
            s["visits"] = s.get("visits", 0) + n
            s["last_visit"] = datetime.now().isoformat()
            daily = s.setdefault("daily", {})
            daily[today] = daily.get(today, 0) + n
            for day in [d for d in daily if d < cutoff]:
                daily.pop(day, None)

    update_data(_apply)


def start_background_workers():
    def _loop():
        while True:
            time.sleep(5)
            try:
                _flush_visits()
                _reap_expired_sites()
            except Exception:
                logger.exception("background worker error")

    threading.Thread(target=_loop, daemon=True, name="webhost-bg").start()


def _reap_expired_sites():
    now = datetime.now().isoformat()
    data = load_data()
    expired = [n for n, i in data.get("sites", {}).items() if i.get("expires") and i["expires"] < now]
    for name in expired:
        delete_site_files(name)
        logger.info("Site %s expired and was removed", name)
        add_event("info", f"Site '{name}' expired and was removed", site=name)


# --------------- EVENTS (shown in the console) ---------------
_events_lock = threading.Lock()
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")


def add_event(level, message, **extra):
    """level: info | success | warn | error"""
    with _events_lock:
        events = []
        if os.path.exists(EVENTS_FILE):
            try:
                with open(EVENTS_FILE) as f:
                    events = json.load(f)
            except Exception:
                events = []
        events.append({"t": datetime.now().isoformat(timespec="seconds"), "level": level, "msg": message, **extra})
        events = events[-300:]
        tmp = EVENTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(events, f)
        os.replace(tmp, EVENTS_FILE)
    if level in ("error", "warn", "success") and extra.get("notify", True):
        notify_admin(f"{ {'error': '🔴', 'warn': '🟠', 'success': '🟢'}[level] } {message}")


def get_events(limit=100):
    with _events_lock:
        if os.path.exists(EVENTS_FILE):
            try:
                with open(EVENTS_FILE) as f:
                    return list(reversed(json.load(f)))[:limit]
            except Exception:
                return []
    return []


def notify_admin(text):
    """Fire-and-forget Telegram message to the admin."""
    if not (TOKEN and ADMIN_ID):
        return

    def _send():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": ADMIN_ID, "text": text[:3900], "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


# --------------- FILE HELPERS ---------------
def safe_extract(zip_path, target_dir):
    """Extract a ZIP rejecting traversal, symlinks, zip bombs and absurd file counts."""
    real_target = os.path.realpath(target_dir)
    max_bytes = MAX_UNZIPPED_MB * 1024 * 1024
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise ValueError(f"Too many files in ZIP ({len(infos)} > {MAX_ZIP_FILES})")
        total = 0
        for info in infos:
            total += info.file_size
            if total > max_bytes:
                raise ValueError(f"ZIP expands to more than {MAX_UNZIPPED_MB}MB")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Symlinks are not allowed: {info.filename}")
            dest = os.path.realpath(os.path.join(target_dir, info.filename))
            if dest != real_target and not dest.startswith(real_target + os.sep):
                raise ValueError(f"Illegal path in ZIP: {info.filename}")
        zf.extractall(target_dir)
    macosx = os.path.join(target_dir, "__MACOSX")
    if os.path.isdir(macosx):
        shutil.rmtree(macosx, ignore_errors=True)


def flatten_nested_folder(base_folder):
    """If the ZIP contained a single top-level folder, lift its content up."""
    while True:
        items = [i for i in os.listdir(base_folder) if i not in ("__MACOSX",)]
        if len(items) == 1 and os.path.isdir(os.path.join(base_folder, items[0])):
            inner = os.path.join(base_folder, items[0])
            tmp = inner + "_tmp_" + str(int(time.time()))
            os.rename(inner, tmp)
            for f in os.listdir(tmp):
                shutil.move(os.path.join(tmp, f), os.path.join(base_folder, f))
            os.rmdir(tmp)
        else:
            break


def dir_stats(folder):
    count = size = 0
    for root, _d, files in os.walk(folder):
        for fl in files:
            count += 1
            try:
                size += os.path.getsize(os.path.join(root, fl))
            except OSError:
                pass
    return count, size


def safe_join(root, rel):
    """Join rel under root; raise ValueError on escape."""
    root_real = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root_real, (rel or "").lstrip("/")))
    if path != root_real and not path.startswith(root_real + os.sep):
        raise ValueError("Path escapes root")
    return path


def valid_name(name):
    return bool(NAME_RE.match(name or "")) and name not in RESERVED_NAMES


def slugify(text):
    s = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s


def deploy_site_zip(zip_path, site_name, owner):
    """Extract zip into websites/<name>, register in data.json. Raises on failure."""
    base_folder = os.path.join(WEBSITES_DIR, site_name)
    staging = base_folder + ".__staging"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        safe_extract(zip_path, staging)
        flatten_nested_folder(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(base_folder, ignore_errors=True)
    os.rename(staging, base_folder)
    count, size = dir_stats(base_folder)
    html = [f for f in os.listdir(base_folder) if f.lower().endswith(".html")]

    def _register(d):
        prev = d.setdefault("sites", {}).get(site_name, {})
        d["sites"][site_name] = {
            **prev,
            "owner": str(owner),
            "created": prev.get("created") or datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
            "files": count,
            "size": size,
        }

    update_data(_register)
    add_event("success", f"Site '{site_name}' deployed ({count} files)", site=name_safe(site_name), notify=False)
    return {"files": count, "size": size, "html": html}


def name_safe(x):
    return str(x)


def delete_site_files(name):
    shutil.rmtree(os.path.join(WEBSITES_DIR, name), ignore_errors=True)

    def _del(d):
        d.get("sites", {}).pop(name, None)
        d.get("analytics", {}).pop(name, None)

    update_data(_del)


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
