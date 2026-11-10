"""
Web console (the UI + its JSON API) for Web-Host.
Mounted by webhost.py. All /console/api/* routes need admin auth except /login and /hooks.
"""
import io
import os
import re
import time
import json
import shutil
import tempfile
import zipfile
import threading
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Blueprint, request, jsonify, send_from_directory, send_file, Response, abort

import common as C
import supervisor as S
import templates as T
import api_extra
import proxy
from auth import require_admin

bp = Blueprint("console", __name__)
bp.register_blueprint(proxy.bp)
bp.register_blueprint(api_extra.bp)
UI_DIR = os.path.join(C.BASE_DIR, "ui")
TMP_DIR = os.path.join(C.DATA_DIR, "tmp")
os.makedirs(TMP_DIR, exist_ok=True)

BOT_STATE = {"running": False, "username": "", "error": ""}


# ------------------------------------------------------------------ auth
_login_fail = {}


@bp.post("/console/api/login")
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    fails = [t for t in _login_fail.get(ip, []) if now - t < 300]
    if len(fails) >= 6:
        return jsonify({"error": "Too many attempts. Try again in a few minutes."}), 429
    pw = (request.get_json(silent=True) or {}).get("password", "")
    import hmac
    if pw and hmac.compare_digest(pw.encode(), C.ADMIN_PASSWORD.encode()):
        _login_fail.pop(ip, None)
        return jsonify({"token": C.make_token()})
    fails.append(now)
    _login_fail[ip] = fails
    return jsonify({"error": "Wrong password"}), 401


@bp.get("/console/api/me")
@require_admin
def me():
    return jsonify({"ok": True})


# ------------------------------------------------------------------ UI
def _ui(path="index.html"):
    resp = send_from_directory(UI_DIR, path)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/")
def root():
    return _ui()


@bp.get("/console")
@bp.get("/console/")
def console_index():
    return _ui()


@bp.get("/ui/<path:path>")
def ui_static(path):
    return _ui(path)


@bp.get("/health")
def health():
    return jsonify({"status": "ok", "uptime": int(time.time() - C.STARTED_AT)})


# ------------------------------------------------------------------ helpers
def _site_row(name, info, analytics):
    folder = os.path.join(C.WEBSITES_DIR, name)
    files, size = C.dir_stats(folder) if os.path.isdir(folder) else (0, 0)
    a = analytics.get(name, {})
    daily = a.get("daily", {})
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    return {
        "name": name, "url": f"{C.DOMAIN}/{name}/", "owner": info.get("owner", "unknown"),
        "created": info.get("created", ""), "updated": info.get("updated", ""),
        "files": files, "size": size, "visits": a.get("visits", 0), "last_visit": a.get("last_visit"),
        "spark": [daily.get(d, 0) for d in days],
        "isolation": info.get("isolation", "sandbox"), "spa": bool(info.get("spa")),
        "expires": info.get("expires"), "domains": list(info.get("domains") or []),
        "has_auth": bool(info.get("basic_auth")), "sub_host": f"{name}.{S.service_domain()}" if S.service_domain() else None,
    }


def _save_upload(field="file", suffix=".zip"):
    f = request.files.get(field)
    if not f or not f.filename:
        raise ValueError("No file uploaded")
    if suffix and not f.filename.lower().endswith(suffix):
        raise ValueError(f"Only {suffix} files are allowed")
    fd, path = tempfile.mkstemp(suffix=suffix, dir=TMP_DIR)
    os.close(fd)
    f.save(path)
    return path


def _root(scope, name):
    if scope == "sites":
        if name not in C.load_data().get("sites", {}):
            abort(404)
        return os.path.join(C.WEBSITES_DIR, name)
    if scope == "services":
        if not S.get_cfg(name):
            abort(404)
        return S.src_dir(name)
    abort(404)


# ------------------------------------------------------------------ overview
@bp.get("/console/api/overview")
@require_admin
def overview():
    data = C.load_data()
    sites = [_site_row(n, i, data.get("analytics", {})) for n, i in data.get("sites", {}).items()]
    services = [S.public_view(n) for n in S.get_state()["services"]]
    total_visits = sum(s["visits"] for s in sites)
    spark = [0] * 14
    for s in sites:
        spark = [a + b for a, b in zip(spark, s["spark"])]
    return jsonify({
        "system": S.system_stats(),
        "caps": S.capabilities(),
        "domain": C.DOMAIN,
        "uptime": int(time.time() - C.STARTED_AT),
        "sites": {"count": len(sites), "visits": total_visits, "spark": spark,
                  "size": sum(s["size"] for s in sites)},
        "services": [{"name": s["name"], "kind": s["kind"], "status": s["status"], "runtime": s["runtime"],
                      "uptime": s["uptime"], "url": s.get("abs_url"), "health": (s.get("health") or {}).get("status"),
                      "next_run": s.get("next_run")} for s in services],
        "users": len(data.get("users", {})),
        "bot": BOT_STATE,
        "events": C.get_events(25),
        "persistent": C.DATA_DIR != C.BASE_DIR,
    })


@bp.get("/console/api/events")
@require_admin
def events():
    return jsonify({"events": C.get_events(200)})


# ------------------------------------------------------------------ static sites
@bp.get("/console/api/sites")
@require_admin
def sites_list():
    data = C.load_data()
    rows = [_site_row(n, i, data.get("analytics", {})) for n, i in data.get("sites", {}).items()]
    rows.sort(key=lambda r: r["updated"] or r["created"], reverse=True)
    return jsonify({"sites": rows})


@bp.post("/console/api/sites")
@require_admin
def sites_create():
    name = C.slugify(request.form.get("name", ""))
    if not C.valid_name(name):
        return jsonify({"error": "Name must be 2-41 chars: lowercase letters, numbers, hyphens (not reserved)"}), 400
    if S.get_cfg(name):
        return jsonify({"error": "A service with this name already exists"}), 400
    try:
        path = _save_upload()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        info = C.deploy_site_zip(path, name, "console")
    except Exception as e:
        return jsonify({"error": f"Deploy failed: {e}"}), 400
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return jsonify({"message": "Site deployed", "name": name, "url": f"{C.DOMAIN}/{name}/", **info})


@bp.delete("/console/api/sites/<name>")
@require_admin
def sites_delete(name):
    if name not in C.load_data().get("sites", {}):
        return jsonify({"error": "Not found"}), 404
    C.delete_site_files(name)
    C.add_event("info", f"Site '{name}' deleted from console", notify=False)
    return jsonify({"ok": True})


@bp.post("/console/api/sites/<name>/rename")
@require_admin
def sites_rename(name):
    new = C.slugify((request.get_json(silent=True) or {}).get("name", ""))
    data = C.load_data()
    if name not in data["sites"]:
        return jsonify({"error": "Not found"}), 404
    if not C.valid_name(new) or new in data["sites"] or S.get_cfg(new):
        return jsonify({"error": "Invalid or already used name"}), 400
    old_dir, new_dir = os.path.join(C.WEBSITES_DIR, name), os.path.join(C.WEBSITES_DIR, new)
    if os.path.isdir(old_dir):
        shutil.move(old_dir, new_dir)

    def _m(d):
        d["sites"][new] = d["sites"].pop(name)
        if name in d["analytics"]:
            d["analytics"][new] = d["analytics"].pop(name)

    C.update_data(_m)
    return jsonify({"ok": True, "name": new})


@bp.post("/console/api/sites/<name>/settings")
@require_admin
def sites_settings(name):
    body = request.get_json(silent=True) or {}

    def _m(d):
        s = d["sites"].get(name)
        if s is None:
            return
        if "isolation" in body:
            s["isolation"] = "off" if body["isolation"] == "off" else "sandbox"
        if "spa" in body:
            s["spa"] = bool(body["spa"])
        if "expires_hours" in body:
            h = body["expires_hours"]
            s["expires"] = (datetime.now() + timedelta(hours=float(h))).isoformat() if h else None

    if name not in C.load_data()["sites"]:
        return jsonify({"error": "Not found"}), 404
    C.update_data(_m)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ files (sites + services)
@bp.get("/console/api/<scope>/<name>/files")
@require_admin
def files_list(scope, name):
    root = _root(scope, name)
    try:
        path = C.safe_join(root, request.args.get("path", ""))
    except ValueError:
        return jsonify({"error": "Bad path"}), 400
    if not os.path.isdir(path):
        return jsonify({"entries": [], "path": ""})
    entries = []
    for e in sorted(os.scandir(path), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.name in (".git",) and scope == "services":
            continue
        st = e.stat()
        entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
    rel = os.path.relpath(path, root)
    return jsonify({"entries": entries, "path": "" if rel == "." else rel})


@bp.get("/console/api/<scope>/<name>/file")
@require_admin
def file_read(scope, name):
    root = _root(scope, name)
    try:
        path = C.safe_join(root, request.args.get("path", ""))
    except ValueError:
        return jsonify({"error": "Bad path"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    size = os.path.getsize(path)
    if size > 512 * 1024:
        return jsonify({"binary": True, "size": size, "reason": "File larger than 512KB"})
    raw = open(path, "rb").read()
    try:
        return jsonify({"content": raw.decode("utf-8"), "size": size})
    except UnicodeDecodeError:
        return jsonify({"binary": True, "size": size, "reason": "Binary file"})


@bp.put("/console/api/<scope>/<name>/file")
@require_admin
def file_write(scope, name):
    root = _root(scope, name)
    body = request.get_json(silent=True) or {}
    try:
        path = C.safe_join(root, body.get("path", ""))
    except ValueError:
        return jsonify({"error": "Bad path"}), 400
    if os.path.isdir(path) or not body.get("path"):
        return jsonify({"error": "Not a file"}), 400
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body.get("content", ""))
    return jsonify({"ok": True})


@bp.post("/console/api/<scope>/<name>/file")
@require_admin
def file_upload(scope, name):
    root = _root(scope, name)
    try:
        folder = C.safe_join(root, request.form.get("path", ""))
    except ValueError:
        return jsonify({"error": "Bad path"}), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400
    from werkzeug.utils import secure_filename
    os.makedirs(folder, exist_ok=True)
    f.save(os.path.join(folder, secure_filename(f.filename)))
    return jsonify({"ok": True})


@bp.delete("/console/api/<scope>/<name>/file")
@require_admin
def file_delete(scope, name):
    root = _root(scope, name)
    try:
        path = C.safe_join(root, request.args.get("path", ""))
    except ValueError:
        return jsonify({"error": "Bad path"}), 400
    if path == os.path.realpath(root):
        return jsonify({"error": "Refusing to delete root"}), 400
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)
    return jsonify({"ok": True})


@bp.get("/console/api/<scope>/<name>/download")
@require_admin
def download(scope, name):
    root = _root(scope, name)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=TMP_DIR)
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != ".git"]
            for fl in files:
                full = os.path.join(r, fl)
                z.write(full, os.path.relpath(full, root))
    resp = send_file(tmp, as_attachment=True, download_name=f"{name}.zip")
    resp.call_on_close(lambda: os.path.exists(tmp) and os.remove(tmp))
    return resp


# ------------------------------------------------------------------ services
def _spec_from_request():
    if request.content_type and request.content_type.startswith("multipart/"):
        return json.loads(request.form.get("spec", "{}"))
    return request.get_json(silent=True) or {}


@bp.get("/console/api/services")
@require_admin
def services_list():
    return jsonify({"services": [S.public_view(n) for n in S.get_state()["services"]], "caps": S.capabilities()})


@bp.post("/console/api/services")
@require_admin
def services_create():
    try:
        spec = _spec_from_request()
        files = T.apply(spec)
        spec["name"] = C.slugify(spec.get("name", ""))
        name = S.create_service(spec)
        if files:
            T.write_files(files, S.src_dir(name))
            hp = T.TEMPLATES[spec["template"]].get("health_path")
            if hp:
                S.update_cfg(name, health_path=hp)
            return jsonify({"name": name, "deploy": S.deploy(name, "template")})
        zip_path = None
        if spec.get("source", "zip") == "zip" and request.files.get("file"):
            zip_path = _save_upload()
        dep = S.deploy(name, "initial" if spec.get("source") == "git" else "upload", zip_path) \
            if (spec.get("source") == "git" or zip_path) else None
        return jsonify({"name": name, "deploy": dep})
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 400


@bp.get("/console/api/services/<name>")
@require_admin
def service_get(name):
    v = S.public_view(name)
    if not v:
        return jsonify({"error": "Not found"}), 404
    return jsonify(v)


@bp.patch("/console/api/services/<name>")
@require_admin
def service_patch(name):
    cfg = S.get_cfg(name)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    allowed = {"build_cmd", "start_cmd", "branch", "auto_restart", "health_path", "health_restart", "runtime",
               "workdir", "notes", "kind", "port", "repo", "memory_mb", "schedule", "timeout_s"}
    upd = {k: v for k, v in body.items() if k in allowed}
    if "kind" in upd:
        if upd["kind"] not in S.KINDS:
            return jsonify({"error": "Bad service type"}), 400
        upd["auto_kind"] = upd["kind"] == "auto"
        if upd["kind"] == "auto" and cfg.get("kind") in ("web", "worker"):
            upd["kind"] = cfg["kind"]  # keep the detected type; it is re-detected on the next deploy
    if "memory_mb" in upd or "timeout_s" in upd:
        try:
            if "memory_mb" in upd:
                upd["memory_mb"] = max(0, int(upd["memory_mb"] or 0))
            if "timeout_s" in upd:
                upd["timeout_s"] = min(86400, max(10, int(upd["timeout_s"] or 1800)))
        except (TypeError, ValueError):
            return jsonify({"error": "Memory limit and timeout must be numbers"}), 400
    if "health_path" in upd:
        hp = (upd["health_path"] or "").strip()
        upd["health_path"] = ("/" + hp.lstrip("/")) if hp else ""
    kind_after = upd.get("kind", cfg.get("kind"))
    if "schedule" in upd or kind_after == "cron":
        import cron
        sched = (upd.get("schedule", cfg.get("schedule")) or "").strip()
        if kind_after == "cron":
            try:
                cron.parse(sched)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        upd["schedule"] = sched
    if "port" in upd:
        try:
            upd["port"] = int(upd["port"]) if upd["port"] not in (None, "") else None
        except ValueError:
            return jsonify({"error": "Port must be a number"}), 400
        for n, c in S.get_state()["services"].items():
            if n != name and upd["port"] in (c.get("port"), c.get("host_port")):
                return jsonify({"error": f"Port {upd['port']} is used by '{n}'"}), 400
    if upd.get("kind") in ("web", "worker", "auto") and not (upd.get("port") or cfg.get("port")):
        upd["port"] = S._free_port()
    if "runtime" in upd and upd["runtime"] not in ("native", "docker"):
        return jsonify({"error": "Bad runtime"}), 400
    if "runtime" in upd:
        upd["runtime_used"] = None
    S.update_cfg(name, **upd)
    return jsonify(S.public_view(name))


@bp.delete("/console/api/services/<name>")
@require_admin
def service_delete(name):
    if not S.get_cfg(name):
        return jsonify({"error": "Not found"}), 404
    S.delete_service(name)
    return jsonify({"ok": True})


@bp.post("/console/api/services/<name>/<action>")
@require_admin
def service_action(name, action):
    if not S.get_cfg(name):
        return jsonify({"error": "Not found"}), 404
    try:
        if action == "start":
            S.start(name)
        elif action == "stop":
            S.stop(name)
        elif action == "restart":
            S.restart(name)
        elif action == "deploy":
            zip_path = _save_upload() if request.files.get("file") else None
            reclone = request.args.get("clean") == "1"
            dep = S.deploy(name, "reclone" if reclone else ("upload" if zip_path else "redeploy"), zip_path)
            return jsonify({"deploy": dep})
        elif action == "regen-hook":
            import secrets
            S.update_cfg(name, hook_token=secrets.token_urlsafe(18))
        else:
            return jsonify({"error": "Unknown action"}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(S.public_view(name))


@bp.get("/console/api/services/<name>/logs")
@require_admin
def service_logs(name):
    cfg = S.get_cfg(name)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    kind = request.args.get("type", "runtime")
    after = request.args.get("after")
    after = int(after) if after not in (None, "", "null") else None
    if kind == "deploy":
        dep_id = request.args.get("id")
        if not dep_id and cfg["deploys"]:
            dep_id = cfg["deploys"][-1]["id"]
        path = S.deploy_log(name, dep_id) if dep_id else ""
    else:
        path = S.runtime_log(name)
    out = S.read_log(path, after) if path else {"offset": 0, "text": ""}
    out["status"] = S.status_of(name)
    return jsonify(out)


@bp.post("/console/api/services/<name>/exec")
@require_admin
def service_exec(name):
    body = request.get_json(silent=True) or {}
    cmd = (body.get("cmd") or "").strip()
    if not cmd:
        return jsonify({"error": "Empty command"}), 400
    try:
        return jsonify(S.run_oneoff(name, cmd, timeout=min(int(body.get("timeout", 60)), 300)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@bp.get("/console/api/services/<name>/env")
@require_admin
def env_get(name):
    cfg = S.get_cfg(name)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"env": cfg.get("env") or {}})


def _clean_env(raw):
    env = {}
    for k, v in (raw or {}).items():
        k = str(k).strip()
        if not k:
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise ValueError(f"Invalid variable name: {k}")
        env[k] = str(v)
    return env


@bp.put("/console/api/services/<name>/env")
@require_admin
def env_put(name):
    if not S.get_cfg(name):
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    try:
        env = _clean_env(body.get("env"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    S.update_cfg(name, env=env)
    restarted = False
    if body.get("restart") and S.is_running(name):
        S.restart(name)
        restarted = True
    return jsonify({"ok": True, "restarted": restarted})


@bp.get("/console/api/env")
@require_admin
def genv_get():
    return jsonify({"env": S.get_state()["global_env"]})


@bp.put("/console/api/env")
@require_admin
def genv_put():
    body = request.get_json(silent=True) or {}
    try:
        env = _clean_env(body.get("env"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    S.mutate(lambda s: s.__setitem__("global_env", env))
    return jsonify({"ok": True})


# deploy hook (no auth: secret token in URL)
@bp.route("/hooks/<name>/<token>", methods=["GET", "POST"])
def hook(name, token):
    import hmac
    cfg = S.get_cfg(name)
    if not cfg or not hmac.compare_digest(cfg.get("hook_token", ""), token):
        abort(404)
    if cfg["source"] != "git":
        return jsonify({"error": "Only git services can be deployed by webhook"}), 400
    try:
        dep = S.deploy(name, "webhook")
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"deploy": dep})


# ------------------------------------------------------------------ telegram admin
@bp.get("/console/api/bot")
@require_admin
def bot_info():
    data = C.load_data()
    users = []
    banned = set(map(str, data.get("banned", [])))
    sites = data.get("sites", {})
    for uid, u in data.get("users", {}).items():
        users.append({"id": uid, "name": u.get("name", ""), "username": u.get("username", ""),
                      "last_active": u.get("last_active", ""), "banned": uid in banned,
                      "sites": sum(1 for s in sites.values() if str(s.get("owner")) == uid)})
    users.sort(key=lambda x: x["last_active"], reverse=True)
    return jsonify({"bot": BOT_STATE, "users": users, "token_set": bool(C.TOKEN), "admin_id": C.ADMIN_ID})


@bp.post("/console/api/bot/<action>")
@require_admin
def bot_action(action):
    body = request.get_json(silent=True) or {}
    if action in ("ban", "unban"):
        uid = str(body.get("id", ""))

        def _m(d):
            b = [str(x) for x in d.setdefault("banned", [])]
            if action == "ban" and uid not in b:
                b.append(uid)
            if action == "unban" and uid in b:
                b.remove(uid)
            d["banned"] = b

        C.update_data(_m)
        return jsonify({"ok": True})
    if action == "broadcast":
        msg = (body.get("message") or "").strip()
        if not msg or not C.TOKEN:
            return jsonify({"error": "Message empty or BOT_TOKEN not configured"}), 400
        users = list(C.load_data().get("users", {}))
        result = {"sent": 0, "failed": 0}

        def _run():
            for uid in users:
                try:
                    r = requests.post(f"https://api.telegram.org/bot{C.TOKEN}/sendMessage",
                                      json={"chat_id": int(uid), "text": f"📢 {msg}"}, timeout=10)
                    result["sent" if r.ok else "failed"] += 1
                except Exception:
                    result["failed"] += 1
                time.sleep(0.05)
            C.add_event("info", f"Broadcast finished: {result['sent']} sent, {result['failed']} failed", notify=False)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "queued": len(users)})
    return jsonify({"error": "Unknown action"}), 400


@bp.get("/console/api/github/repos")
@require_admin
def github_repos():
    token = S.get_state()["global_env"].get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        return jsonify({"error": "Add a GITHUB_TOKEN (Settings > Shared variables, or your host's environment) to list your repositories. Public repos still work by pasting the URL."}), 400
    try:
        r = requests.get("https://api.github.com/user/repos", params={"per_page": 100, "sort": "updated"},
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15)
    except requests.RequestException as e:
        return jsonify({"error": f"GitHub unreachable: {e}"}), 502
    if r.status_code != 200:
        return jsonify({"error": f"GitHub returned {r.status_code}. Check that GITHUB_TOKEN is valid."}), 400
    return jsonify({"repos": [{"full_name": x["full_name"], "clone_url": x["clone_url"], "private": x["private"],
                               "branch": x.get("default_branch", ""), "updated": x.get("pushed_at", "")} for x in r.json()]})


@bp.get("/console/api/settings")
@require_admin
def settings():
    return jsonify({
        "domain": C.DOMAIN, "api_key": C.API_KEY, "max_upload_mb": C.MAX_UPLOAD_MB,
        "max_unzipped_mb": C.MAX_UNZIPPED_MB, "max_sites_per_user": C.MAX_SITES_PER_USER,
        "data_dir": C.DATA_DIR, "persistent": C.DATA_DIR != C.BASE_DIR, "bot_token_set": bool(C.TOKEN),
        "admin_id": C.ADMIN_ID, "password_generated": C.GENERATED_PASSWORD,
    })
