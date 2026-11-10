"""Deploy pipeline: fetch (ZIP / Git / snapshot), detect, build, start, snapshots and rollback."""
import os
import re
import sys
import json
import time
import shlex
import shutil
import tarfile
import threading
import subprocess
from datetime import datetime

import common as C
import supervisor as S
from common import safe_extract, flatten_nested_folder, add_event, dir_stats, logger

MAX_DEPLOYS_KEPT = 20
MAX_SNAPSHOTS = 5
MAX_SNAPSHOT_MB = 150
SNAP_SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv"}


# ------------------------------------------------------------------ Dockerfile parsing (native fallback)
def parse_dockerfile(path):
    info = {"env": {}, "run": [], "cmd": None, "entrypoint": None, "expose": None, "workdir": None, "base": ""}
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return info
    text = re.sub(r"\\\s*\n", " ", text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ins, _, rest = line.partition(" ")
        ins = ins.upper()
        rest = rest.strip()
        if ins == "FROM":
            info["base"] = rest.split()[0] if rest else ""
        elif ins == "ENV":
            found = False
            for m in re.finditer(r'(\w+)=("([^"]*)"|\'([^\']*)\'|\S+)', rest):
                found = True
                info["env"][m.group(1)] = (m.group(3) or m.group(4) or m.group(2)).strip("\"'")
            if not found and " " in rest:
                k, _, v = rest.partition(" ")
                info["env"][k] = v.strip().strip("\"'")
        elif ins == "RUN":
            info["run"].append(rest)
        elif ins in ("CMD", "ENTRYPOINT"):
            val = rest
            if rest.startswith("["):
                try:
                    val = " ".join(shlex.quote(x) for x in json.loads(rest))
                except Exception:
                    pass
            info["cmd" if ins == "CMD" else "entrypoint"] = val
        elif ins == "EXPOSE":
            m = re.search(r"\d+", rest)
            if m:
                info["expose"] = int(m.group(0))
        elif ins == "WORKDIR":
            info["workdir"] = rest
    return info


def detect(src):
    """Guess service type, build and start commands from the project layout."""
    d = {"build_cmd": "", "start_cmd": "", "kind": None, "notes": [],
         "dockerfile": os.path.exists(os.path.join(src, "Dockerfile"))}
    names = set(os.listdir(src)) if os.path.isdir(src) else set()
    reqs = ""
    if "requirements.txt" in names:
        reqs = open(os.path.join(src, "requirements.txt"), errors="replace").read().lower()
    if "Procfile" in names:
        for line in open(os.path.join(src, "Procfile"), errors="replace").read().splitlines():
            m = re.match(r"^(web|worker)\s*:\s*(.+)$", line.strip())
            if m:
                d["kind"] = m.group(1)
                d["start_cmd"] = m.group(2).strip()
                d["notes"].append(f"Start command taken from Procfile ({m.group(1)})")
                break
    if "package.json" in names:
        try:
            pj = json.load(open(os.path.join(src, "package.json")))
        except Exception:
            pj = {}
        scripts = pj.get("scripts", {})
        deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
        d["build_cmd"] = "npm install" + (" && npm run build" if "build" in scripts else "")
        if not d["start_cmd"]:
            if "start" in scripts:
                d["start_cmd"] = "npm start"
            elif pj.get("main"):
                d["start_cmd"] = f"node {pj['main']}"
            elif "index.js" in names:
                d["start_cmd"] = "node index.js"
        if d["kind"] is None:
            web_markers = ("express", "fastify", "koa", "next", "hono", "@nestjs/core", "vite", "nuxt")
            d["kind"] = "web" if any(m in deps for m in web_markers) else "worker"
    if names & {"requirements.txt", "pyproject.toml"} or any(n.endswith(".py") for n in names):
        if "requirements.txt" in names:
            d["build_cmd"] = d["build_cmd"] or "pip install -r requirements.txt"
        if not d["start_cmd"]:
            for cand in ("main.py", "app.py", "bot.py", "webhost.py", "server.py", "run.py", "index.py"):
                if cand in names:
                    d["start_cmd"] = f"python {cand}"
                    break
        if d["kind"] is None:
            if any(k in reqs for k in ("flask", "fastapi", "uvicorn", "gunicorn", "django", "aiohttp", "sanic",
                                       "streamlit", "gradio", "tornado", "starlette", "bottle")):
                d["kind"] = "web"
            elif any(k in reqs for k in ("telegram", "aiogram", "telebot", "pyrogram", "telethon", "discord")):
                d["kind"] = "worker"
    if d["dockerfile"]:
        d["notes"].append("Dockerfile found")
    d["kind"] = d["kind"] or "web"
    return d


# ------------------------------------------------------------------ snapshots + rollback
def _snap_dir(name):
    return os.path.join(S.svc_dir(name), "snapshots")


def _snap_path(name, i):
    return os.path.join(_snap_dir(name), f"{int(i)}.tar.gz")


def _src_size_mb(src):
    total = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SNAP_SKIP]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1048576


def list_snapshots(name):
    d = _snap_dir(name)
    if not os.path.isdir(d):
        return []
    cfg = S.get_cfg(name) or {}
    meta = {x["id"]: x for x in cfg.get("deploys", [])}
    out = []
    for fn in os.listdir(d):
        m = re.match(r"^(\d+)\.tar\.gz$", fn)
        if m:
            i = int(m.group(1))
            st = os.stat(os.path.join(d, fn))
            out.append({"id": i, "size": st.st_size, "time": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "commit": (meta.get(i) or {}).get("commit", ""), "trigger": (meta.get(i) or {}).get("trigger", "")})
    return sorted(out, key=lambda x: -x["id"])


def _snapshot(name, dep_id, log):
    src = S.src_dir(name)
    mb = _src_size_mb(src)
    if mb > MAX_SNAPSHOT_MB:
        log.write(f"snapshot skipped: source is {mb:.0f}MB (limit {MAX_SNAPSHOT_MB}MB)")
        return
    os.makedirs(_snap_dir(name), exist_ok=True)
    final = _snap_path(name, dep_id)
    tmp = final + ".tmp"
    with tarfile.open(tmp, "w:gz") as tf:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SNAP_SKIP]
            rel = os.path.relpath(root, src)
            for f in files:
                full = os.path.join(root, f)
                if os.path.islink(full):
                    continue
                tf.add(full, arcname=os.path.normpath(os.path.join(rel, f)))
    os.replace(tmp, final)
    for old in list_snapshots(name)[MAX_SNAPSHOTS:]:
        try:
            os.remove(_snap_path(name, old["id"]))
        except OSError:
            pass
    log.write(f"saved rollback snapshot of deploy #{dep_id}")


def _restore(name, rid, log):
    p = _snap_path(name, rid)
    src = S.src_dir(name)
    shutil.rmtree(src, ignore_errors=True)
    os.makedirs(src, exist_ok=True)
    real = os.path.realpath(src)
    with tarfile.open(p, "r:gz") as tf:
        for m in tf.getmembers():
            dest = os.path.realpath(os.path.join(src, m.name))
            if not (m.isreg() or m.isdir()) or not (dest == real or dest.startswith(real + os.sep)):
                raise ValueError(f"unsafe entry in snapshot: {m.name}")
        try:
            tf.extractall(src, filter="data")
        except TypeError:
            tf.extractall(src)
    log.write(f"restored files from the snapshot of deploy #{rid}")


def rollback(name, dep_id):
    return deploy(name, trigger=f"rollback #{int(dep_id)}", restore_id=int(dep_id))


# ------------------------------------------------------------------ deploy
def deploy(name, trigger="manual", zip_path=None, restore_id=None):
    """Start a deploy in the background. Returns the deploy id."""
    cfg = S.get_cfg(name)
    if not cfg:
        raise ValueError("Service not found")
    r = S.rt(name)
    if r.building:
        raise ValueError("A deploy is already in progress")
    if restore_id is not None and not os.path.exists(_snap_path(name, restore_id)):
        raise ValueError("That rollback snapshot no longer exists")
    r.building = True
    holder = {}

    def _add(s):
        c = s["services"][name]
        did = (c["deploys"][-1]["id"] + 1) if c["deploys"] else 1
        c["deploys"].append({"id": did, "status": "building", "trigger": trigger,
                             "started": datetime.now().isoformat(timespec="seconds"), "finished": None,
                             "commit": "", "error": ""})
        old = c["deploys"][:-MAX_DEPLOYS_KEPT]
        c["deploys"] = c["deploys"][-MAX_DEPLOYS_KEPT:]
        holder["id"] = did
        for o in old:
            try:
                os.remove(S.deploy_log(name, o["id"]))
            except OSError:
                pass

    S.mutate(_add)
    dep_id = holder["id"]
    threading.Thread(target=_deploy_worker, args=(name, dep_id, trigger, zip_path, restore_id), daemon=True,
                     name=f"deploy-{name}").start()
    return dep_id


def _finish_deploy(name, dep_id, status, error="", commit=""):
    def _m(s):
        c = s["services"].get(name)
        if not c:
            return
        for d in c["deploys"]:
            if d["id"] == dep_id:
                d.update(status=status, finished=datetime.now().isoformat(timespec="seconds"), error=error)
                if commit:
                    d["commit"] = commit
        if status == "live":
            c["last_status"] = "scheduled" if c.get("kind") == "cron" else "running"
        elif status == "failed":
            c["last_status"] = "failed"
    S.mutate(_m)


def _deploy_worker(name, dep_id, trigger, zip_path, restore_id):
    r = S.rt(name)
    log = S.LogFile(S.deploy_log(name, dep_id))
    t0 = time.time()
    commit = ""
    try:
        with r.deploy_lock:
            cfg = S.get_cfg(name)
            src = S.src_dir(name)
            caps = S.capabilities()
            log.write(f"==> Deploy #{dep_id} started ({trigger})")
            if cfg.get("kind") != "cron" and not cfg.get("port"):
                S.update_cfg(name, port=S._free_port())
                cfg = S.get_cfg(name)

            # ---- 1. fetch source
            log.write("==> Fetching source")
            if restore_id is not None:
                _restore(name, restore_id, log)
            elif cfg["source"] == "git":
                if not caps["git"]:
                    raise RuntimeError("git is not installed on this host")
                env0 = {k: os.environ[k] for k in S._ENV_ALLOW if k in os.environ}
                env0["GIT_TERMINAL_PROMPT"] = "0"
                branch = f"--branch {shlex.quote(cfg['branch'])} " if cfg.get("branch") else ""
                if os.path.isdir(os.path.join(src, ".git")) and trigger != "reclone":
                    ga = S.git_auth_args(cfg["repo"])
                    code = S.run_logged(f"git {ga}fetch --depth 1 origin && git reset --hard FETCH_HEAD", src, env0, log,
                                        label="git fetch + reset")
                    if code != 0:
                        raise RuntimeError("git update failed")
                else:
                    shutil.rmtree(src, ignore_errors=True)
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    code = S.run_logged(
                        f"git {S.git_auth_args(cfg['repo'])}clone --depth 1 {branch}{shlex.quote(cfg['repo'])} {shlex.quote(src)}",
                        S.svc_dir(name), env0, log, label=f"git clone {S.mask_repo(cfg['repo'])}")
                    if code != 0:
                        raise RuntimeError("git clone failed (check URL / branch / access token)")
                out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=src, capture_output=True, text=True)
                commit = out.stdout.strip()
                if commit:
                    log.write(f"checked out {commit}")
            else:
                if zip_path:
                    shutil.rmtree(src, ignore_errors=True)
                    os.makedirs(src, exist_ok=True)
                    safe_extract(zip_path, src)
                    flatten_nested_folder(src)
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
                    cnt, _size = dir_stats(src)
                    log.write(f"extracted {cnt} files")
                elif not os.listdir(src):
                    raise RuntimeError("No source: upload a ZIP first")
                else:
                    log.write("re-using existing source files")

            # ---- 2. detect type, commands and runtime
            det = detect(src)
            for n in det["notes"]:
                log.write(f"detected: {n}")
            kind = cfg.get("kind")
            if kind == "auto" or (cfg.get("auto_kind") and kind in ("web", "worker")):
                new_kind = det["kind"] or "web"
                if new_kind != kind:
                    log.write(f"detected service type: {new_kind}")
                    S.update_cfg(name, kind=new_kind)
                    kind = new_kind
            runtime = cfg["runtime"]
            df_info = None
            if det["dockerfile"]:
                df_info = parse_dockerfile(os.path.join(src, "Dockerfile"))
                if runtime == "docker" and not caps["docker"]:
                    log.write(f"WARNING: Docker daemon unavailable ({caps['docker_reason']}).")
                    log.write("WARNING: Falling back to native mode using the Dockerfile's RUN/CMD (pip/npm lines only).")
                    runtime = "native"
            elif runtime == "docker":
                raise RuntimeError("Docker runtime selected but no Dockerfile found in the project")
            if kind == "cron" and runtime == "docker":
                log.write("WARNING: cron jobs run natively; ignoring the Docker runtime")
                runtime = "native"

            updates = {}
            start_cmd = cfg["start_cmd"]
            build_cmd = cfg["build_cmd"]
            if runtime == "native" and df_info and not start_cmd:
                start_cmd = " ".join(x for x in [df_info.get("entrypoint"), df_info.get("cmd")] if x)
                if start_cmd:
                    log.write(f"using Dockerfile start command: {start_cmd}")
                if not build_cmd:
                    steps = [x for x in df_info["run"] if re.match(r"^(python\d?\s+-m\s+)?(pip3?|npm|yarn|pnpm)\b", x)]
                    for sk in [x for x in df_info["run"] if x not in steps]:
                        log.write(f"skipped Dockerfile step (needs a container): RUN {sk[:100]}")
                    build_cmd = " && ".join(steps)
            if not start_cmd and runtime == "native":
                start_cmd = det["start_cmd"]
                if start_cmd:
                    log.write(f"auto-detected start command: {start_cmd}")
            if not build_cmd and runtime == "native":
                build_cmd = det["build_cmd"]
                if build_cmd:
                    log.write(f"auto-detected build command: {build_cmd}")
            if runtime == "native" and not start_cmd:
                raise RuntimeError("Could not detect a start command. Set one in Settings (e.g. `python bot.py`).")
            if df_info and df_info.get("env"):
                updates["env"] = {**df_info["env"], **(cfg.get("env") or {})}
            if runtime == "docker":
                updates["host_port"] = cfg.get("host_port") or (S._free_port() if kind != "cron" else None)
                if kind == "web" and df_info and df_info.get("expose"):
                    updates["port"] = df_info["expose"]
            updates.update(last_commit=commit, runtime_used=runtime, eff_start=start_cmd or "", eff_build=build_cmd or "")
            S.update_cfg(name, **updates)
            cfg = S.get_cfg(name)

            # ---- 3. build
            env = S.build_env(name, cfg)
            log.write("==> Building")
            if runtime == "native":
                py = any(x in (build_cmd or "") for x in ("pip", "python")) or os.path.exists(os.path.join(src, "requirements.txt"))
                venv = os.path.join(S.svc_dir(name), "venv")
                if py and not os.path.isdir(venv):
                    code = S.run_logged(f"{shlex.quote(sys.executable)} -m venv {shlex.quote(venv)}", src, env, log,
                                        label="create virtualenv")
                    if code != 0:
                        raise RuntimeError("virtualenv creation failed")
                if py:
                    env = S.build_env(name, S.get_cfg(name))
                if build_cmd:
                    code = S.run_logged(build_cmd, src, env, log, timeout=1800)
                    if code != 0:
                        raise RuntimeError(f"build command failed (exit {code})")
                else:
                    log.write("no build step")
            else:
                tag = f"wh-{name}:latest"
                code = S.run_logged(f"docker build -t {shlex.quote(tag)} .", src, {**os.environ}, log, timeout=3000,
                                    label=f"docker build -t {tag} .")
                if code != 0:
                    raise RuntimeError(f"docker build failed (exit {code})")

            # ---- 4. start (cron jobs are only installed)
            url = ""
            if kind == "cron":
                import cron
                log.write("==> Cron job installed")
                log.write(f"==> Schedule '{cfg['schedule']}' ({cron.tzname()}) - next run {cron.next_run_iso(name) or 'n/a'}")
                S.update_cfg(name, last_status="scheduled")
            else:
                log.write("==> Starting")
                if S.is_running(name):
                    S._stop_proc(name)
                S.update_cfg(name, desired="running")
                S._spawn(name, announce=log)
                time.sleep(2.5)
                if not S.is_running(name):
                    raise RuntimeError(f"process exited right after start (exit {r.exit_code}) - see runtime logs")
                kind = S.get_cfg(name)["kind"]
                wait_s = 25 if kind == "web" else 6
                port = None
                end = time.time() + wait_s
                while time.time() < end and S.is_running(name):
                    port = S.detect_listen(name)
                    if port:
                        break
                    time.sleep(1)
                kind = S.get_cfg(name)["kind"]
                if kind == "web":
                    if port:
                        url = S.public_url(name)
                        log.write(f"==> Listening on :{port}")
                        log.write(f"==> Public URL: {url}")
                    else:
                        log.write("WARNING: the app is running but has not opened a network port yet.")
                        log.write("WARNING: web apps must listen on 0.0.0.0 and the PORT environment variable.")
                        url = S.public_url(name)
                elif port is None:
                    log.write("==> Background worker (no network port) - no public URL")
            secs = time.time() - t0
            log.write(f"==> Deploy #{dep_id} live in {secs:.0f}s")
            _finish_deploy(name, dep_id, "live", commit=commit)
            if trigger == "restore-stopped" and kind != "cron":
                S.stop(name)
                log.write("==> This service was stopped when the backup was made: leaving it stopped")
            try:
                _snapshot(name, dep_id, log)
            except Exception as e:
                log.write(f"snapshot failed: {e}")
            add_event("success", f"'{name}' deployed (#{dep_id}, {secs:.0f}s)" + (f" -> {url}" if url else ""),
                      service=name, event="deploy_ok", url=url)
    except Exception as e:
        log.write(f"ERROR: {e}")
        log.write(f"==> Deploy #{dep_id} failed")
        _finish_deploy(name, dep_id, "failed", error=str(e))
        add_event("error", f"Deploy #{dep_id} of '{name}' failed: {e}", service=name, event="deploy_failed")
        logger.exception("deploy failed for %s", name)
    finally:
        r.building = False
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass
