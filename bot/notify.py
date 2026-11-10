"""Event notifications: Telegram (admin chat) + Discord / Slack / generic webhooks."""
import os
import json
import uuid
import threading

import requests

import common as C

CONF = os.path.join(C.DATA_DIR, "notify.json")
EVENTS = ["deploy_ok", "deploy_failed", "crash", "health", "cron_failed", "memory"]
ICON = {"error": "\U0001F534", "warn": "\U0001F7E0", "success": "\U0001F7E2", "info": "\U0001F535"}
_lock = threading.Lock()


def load():
    try:
        with open(CONF) as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("telegram", True)
    d.setdefault("telegram_events", [])
    d.setdefault("webhooks", [])
    return d


def save(d):
    with _lock:
        tmp = CONF + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, CONF)
        try:
            os.chmod(CONF, 0o600)
        except OSError:
            pass


def clean_webhook(raw):
    url = str(raw.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Webhook URL must start with http:// or https://")
    kind = raw.get("type") if raw.get("type") in ("discord", "slack", "generic") else "generic"
    events = [e for e in (raw.get("events") or []) if e in EVENTS]
    return {"id": raw.get("id") or uuid.uuid4().hex[:8], "name": str(raw.get("name") or kind)[:40],
            "url": url, "type": kind, "events": events}


def _payload(kind, level, text, ev, extra):
    if kind == "discord":
        return {"content": text[:1900]}
    if kind == "slack":
        return {"text": text}
    return {"level": level, "event": ev, "message": text, "service": extra.get("service"),
            "url": extra.get("url"), "time": C.datetime.now().isoformat(timespec="seconds")}


def _post(wh, level, text, ev, extra):
    try:
        r = requests.post(wh["url"], json=_payload(wh["type"], level, text, ev, extra), timeout=10)
        return r.ok, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:200]


def _wanted(events, ev):
    return not events or ev in events


def dispatch(level, message, extra=None):
    extra = extra or {}
    ev = extra.get("event") or level
    cfg = load()
    text = f"{ICON.get(level, '')} {message}"
    if cfg["telegram"] and _wanted(cfg["telegram_events"], ev):
        C.notify_admin(text)
    for wh in cfg["webhooks"]:
        if _wanted(wh.get("events"), ev):
            threading.Thread(target=_post, args=(wh, level, text, ev, extra), daemon=True).start()


def test(wh):
    return _post(wh, "info", f"{ICON['info']} Web-Host test notification - webhook works", "test", {})
