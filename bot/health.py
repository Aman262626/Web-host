"""HTTP health checks for web services: uptime history, status, optional auto-restart."""
import os
import time
import threading
from collections import deque

import requests

import common as C
import supervisor as S

INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "15"))
FAILS_LIMIT = int(os.environ.get("HEALTH_FAILS", "4"))
GRACE = int(os.environ.get("HEALTH_GRACE", "60"))
HIST = {}    # name -> deque[(ts, ok, ms, code)]
STREAK = {}  # name -> consecutive failures
STATE = {}   # name -> "healthy" | "unhealthy"
_started = False


def probe(name, cfg=None):
    cfg = cfg or S.get_cfg(name) or {}
    port = S.target_port(name)
    if not port:
        return None
    path = cfg.get("health_path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    strict = bool(cfg.get("health_path"))
    t0 = time.time()
    try:
        r = requests.get(f"http://127.0.0.1:{port}{path}", timeout=5, allow_redirects=False,
                         headers={"User-Agent": "webhost-health"})
        code, ok = r.status_code, r.status_code < (400 if strict else 500)
    except Exception:
        code, ok = 0, False
    HIST.setdefault(name, deque(maxlen=2880)).append((int(time.time()), ok, int((time.time() - t0) * 1000), code))
    return ok


def summary(name):
    cfg = S.get_cfg(name) or {}
    h = list(HIST.get(name, []))
    if cfg.get("kind") != "web":
        return {"status": "n/a"}
    if not h:
        return {"status": "unknown", "path": cfg.get("health_path") or "/", "recent": [], "checks": 0,
                "uptime24": None, "avg_ms": None, "last_code": None, "last_ts": None}
    day = [x for x in h if x[0] > time.time() - 86400]
    return {"status": STATE.get(name) or ("healthy" if h[-1][1] else "unhealthy"),
            "path": cfg.get("health_path") or "/", "recent": [1 if x[1] else 0 for x in h[-60:]],
            "checks": len(h), "uptime24": round(100.0 * sum(1 for x in day if x[1]) / max(1, len(day)), 2),
            "avg_ms": int(sum(x[2] for x in day) / max(1, len(day))), "last_code": h[-1][3], "last_ts": h[-1][0]}


def _check_all():
    for name, cfg in list(S.get_state()["services"].items()):
        if cfg.get("kind") != "web" or not S.is_running(name):
            continue
        ok = probe(name, cfg)
        if ok is None:
            continue
        prev = STATE.get(name)
        if ok:
            STREAK[name] = 0
            STATE[name] = "healthy"
            if prev == "unhealthy":
                C.add_event("info", f"'{name}' is healthy again", service=name, event="health")
            continue
        STREAK[name] = STREAK.get(name, 0) + 1
        if STREAK[name] == FAILS_LIMIT:
            STATE[name] = "unhealthy"
            C.add_event("warn", f"'{name}' failed {FAILS_LIMIT} health checks in a row", service=name, event="health")
        up = time.time() - (S.rt(name).started or time.time())
        if STREAK[name] >= FAILS_LIMIT and cfg.get("health_path") and cfg.get("health_restart", True) and up > GRACE:
            C.add_event("warn", f"Restarting '{name}' after failed health checks", service=name, event="health")
            STREAK[name] = 0
            try:
                S.restart(name)
            except Exception:
                C.logger.exception("health restart failed")


def _loop():
    while True:
        time.sleep(INTERVAL)
        try:
            _check_all()
        except Exception:
            C.logger.exception("health loop")


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="health").start()
