"""Backup & restore: config, sites and service sources in one ZIP (survives free-tier disk wipes)."""
import os
import re
import json
import shutil
import tempfile
import zipfile
from datetime import datetime

import common as C
import supervisor as S

CONFIG_FILES = ["data.json", "services.json", "notify.json", "events.json"]
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv"}
MAX_MB = 300
ALLOWED = re.compile(r"^(data\.json|services\.json|notify\.json|events\.json|backup-info\.json|"
                     r"websites/[^/].*|services/[a-z0-9-]+/src/.+)$")


def _tmp_dir():
    d = os.path.join(C.DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _add_tree(z, root, arcbase, counter):
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            full = os.path.join(cur, f)
            if os.path.islink(full):
                continue
            counter[0] += os.path.getsize(full)
            if counter[0] > MAX_MB * 1048576:
                raise ValueError(f"Backup would exceed {MAX_MB}MB. Download a config-only backup instead.")
            z.write(full, os.path.join(arcbase, os.path.relpath(full, root)))


def make_backup(full=True):
    fd, path = tempfile.mkstemp(suffix=".zip", dir=_tmp_dir())
    os.close(fd)
    counter = [0]
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("backup-info.json", json.dumps({"created": datetime.now().isoformat(timespec="seconds"),
                                                       "full": bool(full), "domain": C.DOMAIN, "format": 1}))
            for fn in CONFIG_FILES:
                p = os.path.join(C.DATA_DIR, fn)
                if os.path.exists(p):
                    z.write(p, fn)
            if full:
                if os.path.isdir(C.WEBSITES_DIR):
                    _add_tree(z, C.WEBSITES_DIR, "websites", counter)
                for name in S.get_state()["services"]:
                    if os.path.isdir(S.src_dir(name)):
                        _add_tree(z, S.src_dir(name), f"services/{name}/src", counter)
    except Exception:
        os.remove(path)
        raise
    return path


def restore_backup(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        names = {i.filename for i in infos}
        if not names & {"services.json", "data.json", "backup-info.json"}:
            raise ValueError("This does not look like a Web-Host backup")
        if sum(i.file_size for i in infos) > MAX_MB * 2 * 1048576:
            raise ValueError("Backup is too large")
        for i in infos:
            n = i.filename
            if i.is_dir():
                continue
            if n.startswith("/") or ".." in n.split("/") or not ALLOWED.match(n):
                raise ValueError(f"Unexpected entry in backup: {n}")
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Symlink in backup: {n}")
        # stop running processes without touching their saved state
        for name in list(S.get_state()["services"]):
            try:
                S._stop_proc(name, timeout=5)
            except Exception:
                pass
        in_backup = {m.group(1) for n in names for m in [re.match(r"^services/([a-z0-9-]+)/src/", n)] if m}
        for name in in_backup:
            shutil.rmtree(S.src_dir(name), ignore_errors=True)
        if any(n.startswith("websites/") for n in names):
            shutil.rmtree(C.WEBSITES_DIR, ignore_errors=True)
            os.makedirs(C.WEBSITES_DIR, exist_ok=True)
        for i in infos:
            if i.is_dir() or i.filename == "backup-info.json":
                continue
            dest = os.path.realpath(os.path.join(C.DATA_DIR, i.filename))
            if not dest.startswith(os.path.realpath(C.DATA_DIR) + os.sep):
                raise ValueError(f"Unsafe path: {i.filename}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(i) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    with S._rt_lock:
        S._rt.clear()
    state = S.get_state()
    redeploy = []
    for name, cfg in state["services"].items():
        has_src = os.path.isdir(S.src_dir(name)) and bool(os.listdir(S.src_dir(name)))
        if any(d.get("status") == "live" for d in cfg.get("deploys", [])) and (has_src or cfg.get("source") == "git"):
            was_running = cfg.get("kind") == "cron" or cfg.get("desired") == "running"
            try:
                S.deploy(name, "restore" if was_running else "restore-stopped")
                redeploy.append(name)
            except Exception as e:
                C.logger.warning("restore redeploy of %s failed: %s", name, e)
    C.add_event("success", f"Backup restored: {len(state['services'])} services, "
                f"{len(C.load_data().get('sites', {}))} sites", notify=False)
    return {"services": len(state["services"]), "sites": len(C.load_data().get("sites", {})), "redeploying": redeploy}
