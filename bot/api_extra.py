"""Console API for the advanced features: domains, password protection, health, metrics, cron,
rollback, templates, notifications, backup/restore and platform settings."""
import os
import re
import socket
import tempfile
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify, send_file

import common as C
import supervisor as S
import notify as N
import templates as T
from auth import require_admin

bp = Blueprint("extra", __name__)
A = "/console/api"
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _tmp():
    d = os.path.join(C.DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------ platform settings
@bp.get(A + "/platform")
@require_admin
def platform_get():
    s = S.get_state()["settings"]
    return jsonify({"service_domain": S.service_domain(), "service_domain_locked": bool(os.environ.get("SERVICE_DOMAIN")),
                    "timezone": s.get("timezone") or os.environ.get("TZ") or "UTC", "host": request.host.split(":")[0]})


@bp.put(A + "/platform")
@require_admin
def platform_put():
    body = request.get_json(silent=True) or {}
    upd = {}
    if "service_domain" in body:
        d = str(body["service_domain"] or "").strip().lower().lstrip(".")
        if d and not DOMAIN_RE.match(d):
            return _err("Enter a valid base domain such as apps.example.com")
        upd["service_domain"] = d
    if "timezone" in body:
        tz = str(body["timezone"] or "").strip()
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                return _err("Unknown timezone (use names like Asia/Kolkata or UTC)")
        upd["timezone"] = tz
    S.mutate(lambda s: s["settings"].update(upd))
    return platform_get()


# ------------------------------------------------------------------ custom domains + password protection
def _entity(kind, name):
    """(getter, setter) for a service or a site."""
    if kind == "svc":
        cfg = S.get_cfg(name)
        return cfg, lambda **kw: S.update_cfg(name, **kw)
    info = C.load_data().get("sites", {}).get(name)

    def setter(**kw):
        C.update_data(lambda d: d["sites"][name].update(kw))
    return info, setter


def _all_domains(except_kind, except_name):
    used = set()
    for n, c in S.get_state()["services"].items():
        if not (except_kind == "svc" and n == except_name):
            used.update(c.get("domains") or [])
    for n, i in C.load_data().get("sites", {}).items():
        if not (except_kind == "site" and n == except_name):
            used.update(i.get("domains") or [])
    return used


def _set_domains(kind, name):
    ent, setter = _entity(kind, name)
    if ent is None:
        return _err("Not found", 404)
    raw = (request.get_json(silent=True) or {}).get("domains") or []
    if not isinstance(raw, list) or len(raw) > 5:
        return _err("You can attach up to 5 domains")
    from proxy import own_hosts
    clean, used = [], _all_domains(kind, name)
    for d in raw:
        d = str(d).strip().lower().rstrip(".")
        if not DOMAIN_RE.match(d):
            return _err(f"'{d}' is not a valid domain name")
        if d in used or d in own_hosts():
            return _err(f"'{d}' is already used by this console or another app")
        if d not in clean:
            clean.append(d)
    setter(domains=clean)
    return jsonify({"domains": clean})


@bp.put(A + "/services/<name>/domains")
@require_admin
def svc_domains(name):
    return _set_domains("svc", name)


@bp.put(A + "/sites/<name>/domains")
@require_admin
def site_domains(name):
    return _set_domains("site", name)


@bp.post(A + "/domains/check")
@require_admin
def domain_check():
    d = str((request.get_json(silent=True) or {}).get("domain", "")).strip().lower()
    if not DOMAIN_RE.match(d):
        return _err("Invalid domain")

    def ips(host):
        try:
            return sorted({i[4][0] for i in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})
        except socket.gaierror:
            return []
    host = request.host.split(":")[0]
    mine, theirs = ips(host), ips(d)
    return jsonify({"domain": d, "resolves": theirs, "target_host": host, "target_ips": mine,
                    "match": bool(set(mine) & set(theirs))})


def _set_auth(kind, name):
    ent, setter = _entity(kind, name)
    if ent is None:
        return _err("Not found", 404)
    body = request.get_json(silent=True) or {}
    if body.get("enabled") is False:
        setter(basic_auth="")
        return jsonify({"has_auth": False})
    user, pw = str(body.get("user", "")).strip(), str(body.get("password", ""))
    if not user or ":" in user:
        return _err("Username is required and cannot contain ':'")
    if len(pw) < 4:
        return _err("Password must be at least 4 characters")
    setter(basic_auth=f"{user}:{pw}")
    return jsonify({"has_auth": True})


@bp.put(A + "/services/<name>/auth")
@require_admin
def svc_auth(name):
    return _set_auth("svc", name)


@bp.put(A + "/sites/<name>/auth")
@require_admin
def site_auth(name):
    return _set_auth("site", name)


# ------------------------------------------------------------------ templates, health, metrics
@bp.get(A + "/templates")
@require_admin
def templates():
    return jsonify({"templates": T.public_list()})


@bp.get(A + "/services/<name>/health")
@require_admin
def health_get(name):
    import health
    if not S.get_cfg(name):
        return _err("Not found", 404)
    return jsonify(health.summary(name))


@bp.post(A + "/services/<name>/health/check")
@require_admin
def health_check(name):
    import health
    if not S.get_cfg(name):
        return _err("Not found", 404)
    ok = health.probe(name)
    return jsonify({"ok": ok, **health.summary(name)})


@bp.get(A + "/services/<name>/metrics")
@require_admin
def metrics(name):
    cfg = S.get_cfg(name)
    if not cfg:
        return _err("Not found", 404)
    pts = list(S.rt(name).history)
    step = max(1, len(pts) // 120)
    import health
    return jsonify({"points": pts[::step], "memory_mb": cfg.get("memory_mb") or 0, "restarts": cfg.get("restarts") or 0,
                    "uptime": int(__import__("time").time() - S.rt(name).started) if S.is_running(name) and S.rt(name).started else 0,
                    "health": health.summary(name)})


# ------------------------------------------------------------------ cron
@bp.post(A + "/services/<name>/cron/run")
@require_admin
def cron_run(name):
    import cron
    cfg = S.get_cfg(name)
    if not cfg or cfg.get("kind") != "cron":
        return _err("Not a cron job", 404)
    if not any(d.get("status") == "live" for d in cfg.get("deploys", [])):
        return _err("Deploy the job first")
    try:
        cron.run_async(name, "manual")
    except ValueError as e:
        return _err(str(e), 409)
    return jsonify({"ok": True})


@bp.get(A + "/services/<name>/cron/log")
@require_admin
def cron_log(name):
    import cron
    cfg = S.get_cfg(name)
    if not cfg or cfg.get("kind") != "cron":
        return _err("Not a cron job", 404)
    runs = cfg.get("runs", [])
    rid = request.args.get("id") or (runs[-1]["id"] if runs else None)
    if rid is None:
        return jsonify({"offset": 0, "text": "", "status": S.status_of(name)})
    after = request.args.get("after")
    out = S.read_log(cron.run_log(name, rid), int(after) if after not in (None, "", "null") else None)
    rec = next((r for r in runs if str(r["id"]) == str(rid)), {})
    out["status"] = "running" if rec.get("status") == "running" else S.status_of(name)
    return jsonify(out)


# ------------------------------------------------------------------ rollback + expose
@bp.get(A + "/services/<name>/snapshots")
@require_admin
def snapshots(name):
    if not S.get_cfg(name):
        return _err("Not found", 404)
    return jsonify({"snapshots": S.list_snapshots(name)})


@bp.post(A + "/services/<name>/rollback")
@require_admin
def do_rollback(name):
    if not S.get_cfg(name):
        return _err("Not found", 404)
    try:
        dep = S.rollback(name, int((request.get_json(silent=True) or {}).get("id")))
    except (TypeError, ValueError) as e:
        return _err(str(e) if str(e) else "Pick a snapshot to roll back to")
    return jsonify({"deploy": dep})


@bp.post(A + "/services/<name>/expose")
@require_admin
def expose(name):
    cfg = S.get_cfg(name)
    if not cfg or cfg.get("kind") == "cron":
        return _err("Not found", 404)
    upd = {"kind": "web", "auto_kind": False}
    if not cfg.get("port"):
        upd["port"] = S._free_port()
    S.update_cfg(name, **upd)
    if S.is_running(name):
        S.rt(name).listen_port = None
        S.detect_listen(name)
    return jsonify(S.public_view(name))


# ------------------------------------------------------------------ notifications
@bp.get(A + "/notify")
@require_admin
def notify_get():
    c = N.load()
    c["available_events"] = N.EVENTS
    c["telegram_ready"] = bool(C.TOKEN and C.ADMIN_ID)
    return jsonify(c)


@bp.put(A + "/notify")
@require_admin
def notify_put():
    body = request.get_json(silent=True) or {}
    try:
        hooks = [N.clean_webhook(w) for w in (body.get("webhooks") or [])][:10]
    except ValueError as e:
        return _err(str(e))
    conf = {"telegram": bool(body.get("telegram", True)),
            "telegram_events": [e for e in (body.get("telegram_events") or []) if e in N.EVENTS], "webhooks": hooks}
    N.save(conf)
    return notify_get()


@bp.post(A + "/notify/test")
@require_admin
def notify_test():
    body = request.get_json(silent=True) or {}
    if body.get("telegram"):
        if not (C.TOKEN and C.ADMIN_ID):
            return _err("BOT_TOKEN and ADMIN_ID must be set for Telegram alerts")
        C.notify_admin("Web-Host test notification - Telegram alerts work")
        return jsonify({"ok": True, "info": "sent"})
    try:
        wh = N.clean_webhook(body)
    except ValueError as e:
        return _err(str(e))
    ok, info = N.test(wh)
    return jsonify({"ok": ok, "info": info})


# ------------------------------------------------------------------ backup / restore
@bp.get(A + "/backup")
@require_admin
def backup_download():
    import backup
    try:
        path = backup.make_backup(full=request.args.get("full", "1") != "0")
    except ValueError as e:
        return _err(str(e))
    name = f"webhost-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    resp = send_file(path, as_attachment=True, download_name=name)
    resp.call_on_close(lambda: os.path.exists(path) and os.remove(path))
    return resp


@bp.post(A + "/restore")
@require_admin
def backup_restore():
    import backup
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".zip"):
        return _err("Upload the .zip backup file")
    fd, path = tempfile.mkstemp(suffix=".zip", dir=_tmp())
    os.close(fd)
    try:
        f.save(path)
        return jsonify(backup.restore_backup(path))
    except (ValueError, __import__("zipfile").BadZipFile) as e:
        return _err(str(e) or "Invalid backup file")
    finally:
        if os.path.exists(path):
            os.remove(path)
