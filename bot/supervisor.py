"""
Service supervisor: deploy, run, monitor and log long-running apps
(web services, Telegram bots / workers) either natively or via Docker.
"""
import os
import re
import sys
import json
import time
import shlex
import signal
import shutil
import socket
import secrets
import threading
import subprocess
from collections import deque
from datetime import datetime

import psutil

from common import (
    DATA_DIR, SERVICES_DIR, WEBSITES_DIR, logger, safe_extract, flatten_nested_folder,
    add_event, valid_name, load_data, dir_stats, PORT as HOST_PORT,
)

STATE_FILE = os.path.join(DATA_DIR, "services.json")
_state_lock = threading.RLock()
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_DEPLOYS_KEPT = 20

# ------------------------------------------------------------------ state store
def _read_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            s.setdefault("services", {})
            s.setdefault("global_env", {})
            return s
        except Exception:
            logger.exception("services.json unreadable")
    return {"services": {}, "global_env": {}}


def _write_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def mutate(fn):
    with _state_lock:
        s = _read_state()
        out = fn(s)
        _write_state(s)
        return out


def get_state():
    with _state_lock:
        return _read_state()


def get_cfg(name):
    return get_state()["services"].get(name)


def update_cfg(name, **fields):
    def _m(s):
        if name in s["services"]:
            s["services"][name].update(fields)
    mutate(_m)


# ------------------------------------------------------------------ paths & logs
def svc_dir(name):
    return os.path.join(SERVICES_DIR, name)


def src_dir(name):
    return os.path.join(svc_dir(name), "src")


def runtime_log(name):
    return os.path.join(svc_dir(name), "runtime.log")


def deploy_log(name, dep_id):
    return os.path.join(svc_dir(name), "deploys", f"{int(dep_id)}.log")


class LogFile:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            with open(path, "rb") as f:
                f.seek(-MAX_LOG_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(b"--- log truncated ---\n" + tail)

    def write(self, text, stamp=True):
        ts = datetime.now().strftime("%H:%M:%S ") if stamp else ""
        with self.lock:
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                for line in str(text).rstrip("\n").split("\n"):
                    f.write(f"{ts}{line}\n")


def read_log(path, after=None, tail_bytes=65536):
    """Return {offset, text}. after=None -> last tail_bytes; else bytes after offset."""
    if not os.path.exists(path):
        return {"offset": 0, "text": ""}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if after is None:
            start = max(0, size - tail_bytes)
        else:
            start = after if 0 <= after <= size else 0
        f.seek(start)
        data = f.read(min(size - start, 512 * 1024))
    text = data.decode("utf-8", errors="replace")
    if after is None and start > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    return {"offset": start + len(data), "text": text}


# ------------------------------------------------------------------ environment capability
_cap_cache = {"t": 0, "v": None}


def capabilities():
    if time.time() - _cap_cache["t"] < 30 and _cap_cache["v"]:
        return _cap_cache["v"]
    docker_ok = False
    docker_err = ""
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=5)
            docker_ok = r.returncode == 0
            if not docker_ok:
                docker_err = (r.stderr or "").strip().splitlines()[-1:] and (r.stderr or "").strip().splitlines()[-1]
        except Exception as e:
            docker_err = str(e)
    else:
        docker_err = "docker CLI not installed on this host"

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception:
            return None

    caps = {
        "docker": docker_ok,
        "docker_reason": "" if docker_ok else (docker_err or "Docker daemon not reachable"),
        "git": bool(shutil.which("git")),
        "node": ver(["node", "--version"]),
        "npm": bool(shutil.which("npm")),
        "python": sys.version.split()[0],
    }
    _cap_cache.update(t=time.time(), v=caps)
    return caps


# ------------------------------------------------------------------ runtime registry
class Rt:
    """Non-persistent per-service runtime info."""

    def __init__(self):
        self.proc = None
        self.started = None
        self.exit_code = None
        self.building = False
        self.deploy_lock = threading.Lock()
        self.crash_times = deque(maxlen=20)
        self.next_try = 0
        self.ps = None
        self.history = deque(maxlen=120)  # (t, cpu, rss)
        self.stopping = False


_rt = {}
_rt_lock = threading.Lock()


def rt(name):
    with _rt_lock:
        if name not in _rt:
            _rt[name] = Rt()
        return _rt[name]


def is_running(name):
    p = rt(name).proc
    return bool(p and p.poll() is None)


def status_of(name):
    r = rt(name)
    cfg = get_cfg(name) or {}
    if r.building:
        return "building"
    if is_running(name):
        return "running"
    if cfg.get("desired") == "running" and time.time() < r.next_try:
        return "restarting"
    return cfg.get("last_status") or "stopped"


# ------------------------------------------------------------------ helpers
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def _free_port(exclude=()):
    used = {c.get("port") for c in get_state()["services"].values()} | \
           {c.get("host_port") for c in get_state()["services"].values()} | set(exclude) | {HOST_PORT}
    for port in range(9100, 9900):
        if port in used:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port available")


def build_env(name, cfg):
    """Clean environment: host secrets (BOT_TOKEN, API_KEY...) are NOT inherited."""
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    venv = os.path.join(svc_dir(name), "venv")
    if os.path.isdir(venv):
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = os.path.join(venv, "bin") + os.pathsep + env["PATH"]
    env["HOME"] = svc_dir(name)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(get_state()["global_env"])
    env.update(cfg.get("env") or {})
    if cfg.get("kind") == "web":
        env["PORT"] = str(cfg.get("port"))
    env["SERVICE_NAME"] = name
    return env


def git_auth_args(repo):
    """Pass GITHUB_TOKEN for github.com https repos via a one-off header (never stored in .git/config)."""
    token = get_state()["global_env"].get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token or not (repo or "").startswith("https://github.com/"):
        return ""
    import base64
    b = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return "-c " + shlex.quote(f"http.https://github.com/.extraheader=AUTHORIZATION: basic {b}") + " "


def mask_repo(url):
    return re.sub(r"//[^/@]+@", "//***@", url or "")


def public_view(name):
    cfg = get_cfg(name)
    if not cfg:
        return None
    r = rt(name)
    v = {k: cfg.get(k) for k in (
        "name", "kind", "runtime", "source", "branch", "build_cmd", "start_cmd", "port", "host_port",
        "auto_restart", "desired", "created", "restarts", "hook_token", "health_path", "last_commit",
        "workdir", "notes", "eff_start", "eff_build",
    )}
    v["repo"] = mask_repo(cfg.get("repo"))
    v["status"] = status_of(name)
    v["deploys"] = list(reversed(cfg.get("deploys", [])))
    v["env_count"] = len(cfg.get("env") or {})
    v["pid"] = r.proc.pid if is_running(name) else None
    v["uptime"] = int(time.time() - r.started) if is_running(name) and r.started else 0
    v["exit_code"] = r.exit_code
    if cfg.get("kind") == "web":
        v["path"] = f"/svc/{name}/"
    v["metrics"] = list(r.history)[-60:]
    return v


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
            for m in re.finditer(r'(\w+)=("([^"]*)"|\'([^\']*)\'|\S+)', rest):
                info["env"][m.group(1)] = (m.group(3) or m.group(4) or m.group(2)).strip("\"'")
            else:
                if "=" not in rest and " " in rest:
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
    """Guess build/start commands from the project layout."""
    d = {"build_cmd": "", "start_cmd": "", "kind": None, "notes": [], "dockerfile": os.path.exists(os.path.join(src, "Dockerfile"))}
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
            web_markers = ("express", "fastify", "koa", "next", "hono", "@nestjs/core")
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
            if any(k in reqs for k in ("flask", "fastapi", "uvicorn", "gunicorn", "django", "aiohttp", "sanic", "streamlit", "gradio")):
                d["kind"] = "web"
            elif any(k in reqs for k in ("telegram", "aiogram", "telebot", "pyrogram", "telethon", "discord")):
                d["kind"] = "worker"
    if d["dockerfile"]:
        d["notes"].append("Dockerfile found")
    d["kind"] = d["kind"] or "web"
    return d


# ------------------------------------------------------------------ subprocess runner with live logs
def run_logged(cmd, cwd, env, log, timeout=1200, label=None):
    """Run a shell command, stream every line to `log`. Returns exit code."""
    log.write(f"$ {label or cmd}", stamp=True)
    try:
        p = subprocess.Popen(
            cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1, start_new_session=True,
        )
    except Exception as e:
        log.write(f"failed to start: {e}")
        return 127
    timer = threading.Timer(timeout, lambda: _kill_group(p))
    timer.start()
    try:
        for line in p.stdout:
            log.write(line.rstrip("\n"))
        p.wait()
    finally:
        timer.cancel()
    return p.returncode


def _kill_group(p, sig=signal.SIGTERM):
    try:
        os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.send_signal(sig)
        except Exception:
            pass


# ------------------------------------------------------------------ create / delete
def create_service(spec):
    """spec: dict with name, kind, runtime, source, repo, branch, build_cmd, start_cmd, port, env"""
    name = (spec.get("name") or "").strip().lower()
    if not valid_name(name):
        raise ValueError("Name must be 2-41 chars: lowercase letters, numbers, hyphens (not reserved)")
    if name in load_data().get("sites", {}):
        raise ValueError("A static site with this name already exists")
    kind = spec.get("kind") if spec.get("kind") in ("web", "worker") else "web"
    runtime = spec.get("runtime") if spec.get("runtime") in ("native", "docker") else "native"
    source = spec.get("source") if spec.get("source") in ("zip", "git") else "zip"
    if source == "git" and not (spec.get("repo") or "").startswith(("https://", "http://", "git@")):
        raise ValueError("Enter a valid git repository URL (https://...)")
    env = {str(k).strip(): str(v) for k, v in (spec.get("env") or {}).items() if str(k).strip()}
    for k in env:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise ValueError(f"Invalid environment variable name: {k}")

    def _m(s):
        if name in s["services"]:
            raise ValueError("A service with this name already exists")
        port = None
        if kind == "web":
            used = {c.get("port") for c in s["services"].values()} | {c.get("host_port") for c in s["services"].values()}
            port = int(spec["port"]) if spec.get("port") else None
            if port is None:
                port = _free_port(exclude=used)
        s["services"][name] = {
            "name": name, "kind": kind, "runtime": runtime, "source": source,
            "repo": (spec.get("repo") or "").strip(), "branch": (spec.get("branch") or "").strip(),
            "build_cmd": (spec.get("build_cmd") or "").strip(), "start_cmd": (spec.get("start_cmd") or "").strip(),
            "port": port, "host_port": None, "env": env, "auto_restart": True, "desired": "stopped",
            "created": datetime.now().isoformat(timespec="seconds"), "restarts": 0,
            "hook_token": secrets.token_urlsafe(18), "health_path": "", "deploys": [], "last_status": "stopped",
            "workdir": "", "notes": "",
        }

    mutate(_m)
    os.makedirs(src_dir(name), exist_ok=True)
    add_event("info", f"Service '{name}' created", service=name, notify=False)
    return name


def delete_service(name):
    stop(name)
    mutate(lambda s: s["services"].pop(name, None))
    shutil.rmtree(svc_dir(name), ignore_errors=True)
    with _rt_lock:
        _rt.pop(name, None)
    add_event("info", f"Service '{name}' deleted", service=name, notify=False)


# ------------------------------------------------------------------ deploy pipeline
def deploy(name, trigger="manual", zip_path=None):
    """Start a deploy in the background. Returns deploy id."""
    cfg = get_cfg(name)
    if not cfg:
        raise ValueError("Service not found")
    r = rt(name)
    if r.building:
        raise ValueError("A deploy is already in progress")
    r.building = True
    dep_id_holder = {}

    def _add(s):
        c = s["services"][name]
        did = (c["deploys"][-1]["id"] + 1) if c["deploys"] else 1
        c["deploys"].append({
            "id": did, "status": "building", "trigger": trigger,
            "started": datetime.now().isoformat(timespec="seconds"), "finished": None, "commit": "", "error": "",
        })
        old = c["deploys"][:-MAX_DEPLOYS_KEPT]
        c["deploys"] = c["deploys"][-MAX_DEPLOYS_KEPT:]
        dep_id_holder["id"] = did
        for o in old:
            try:
                os.remove(deploy_log(name, o["id"]))
            except OSError:
                pass

    mutate(_add)
    dep_id = dep_id_holder["id"]
    threading.Thread(target=_deploy_worker, args=(name, dep_id, trigger, zip_path), daemon=True,
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
        c["last_status"] = "running" if status == "live" else ("failed" if status == "failed" else c.get("last_status"))
    mutate(_m)


def _deploy_worker(name, dep_id, trigger, zip_path):
    r = rt(name)
    log = LogFile(deploy_log(name, dep_id))
    t0 = time.time()
    commit = ""
    try:
        with r.deploy_lock:
            cfg = get_cfg(name)
            src = src_dir(name)
            caps = capabilities()
            log.write(f"==> Deploy #{dep_id} started ({trigger})")

            # ---- 1. fetch source
            log.write("==> Fetching source")
            if cfg["source"] == "git":
                if not caps["git"]:
                    raise RuntimeError("git is not installed on this host")
                env0 = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
                env0["GIT_TERMINAL_PROMPT"] = "0"
                branch = f"--branch {shlex.quote(cfg['branch'])} " if cfg.get("branch") else ""
                if os.path.isdir(os.path.join(src, ".git")) and trigger != "reclone":
                    ga = git_auth_args(cfg["repo"])
                    code = run_logged(f"git {ga}fetch --depth 1 origin && git reset --hard FETCH_HEAD", src, env0, log,
                                      label="git fetch + reset")
                    if code != 0:
                        raise RuntimeError("git update failed")
                else:
                    shutil.rmtree(src, ignore_errors=True)
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    code = run_logged(f"git {git_auth_args(cfg['repo'])}clone --depth 1 {branch}{shlex.quote(cfg['repo'])} {shlex.quote(src)}",
                                      svc_dir(name), env0, log, label=f"git clone {mask_repo(cfg['repo'])}")
                    if code != 0:
                        raise RuntimeError("git clone failed (check URL / branch / access token)")
                out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=src, capture_output=True, text=True)
                commit = out.stdout.strip()
                if commit:
                    log.write(f"checked out {commit}")
            else:
                if zip_path:
                    # keep volatile dirs (none) -> replace source entirely
                    shutil.rmtree(src, ignore_errors=True)
                    os.makedirs(src, exist_ok=True)
                    safe_extract(zip_path, src)
                    flatten_nested_folder(src)
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
                    cnt, size = dir_stats(src)
                    log.write(f"extracted {cnt} files")
                elif not os.listdir(src):
                    raise RuntimeError("No source: upload a ZIP first")
                else:
                    log.write("re-using existing source files")

            # ---- 2. detect & decide runtime
            det = detect(src)
            for n in det["notes"]:
                log.write(f"detected: {n}")
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

            updates = {}
            start_cmd = cfg["start_cmd"]
            build_cmd = cfg["build_cmd"]
            if runtime == "native" and df_info and (not start_cmd):
                start_cmd = " ".join(x for x in [df_info.get("entrypoint"), df_info.get("cmd")] if x)
                if start_cmd:
                    log.write(f"using Dockerfile start command: {start_cmd}")
                if not build_cmd:
                    steps = [r_ for r_ in df_info["run"] if re.match(r"^(python\d?\s+-m\s+)?(pip3?|npm|yarn|pnpm)\b", r_)]
                    skipped = [r_ for r_ in df_info["run"] if r_ not in steps]
                    build_cmd = " && ".join(steps)
                    for sk in skipped:
                        log.write(f"skipped Dockerfile step (needs a container): RUN {sk[:100]}")
                if df_info.get("expose") and cfg["kind"] == "web" and not cfg.get("port"):
                    updates["port"] = df_info["expose"]
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
                merged = {**df_info["env"], **(cfg.get("env") or {})}
                updates["env"] = merged
            if runtime == "docker":
                updates["host_port"] = cfg.get("host_port") or (_free_port() if cfg["kind"] == "web" else None)
                if cfg["kind"] == "web" and df_info and df_info.get("expose") and not cfg.get("port_user_set"):
                    updates["port"] = df_info["expose"]
            updates["last_commit"] = commit
            updates["runtime_used"] = runtime
            # effective commands (auto-detected values never overwrite what the user typed)
            updates["eff_start"] = start_cmd or ""
            updates["eff_build"] = build_cmd or ""
            update_cfg(name, **updates)
            cfg = get_cfg(name)

            # ---- 3. build
            env = build_env(name, cfg)
            log.write("==> Building")
            if runtime == "native":
                py_markers = any(x in (build_cmd or "") for x in ("pip", "python")) or os.path.exists(os.path.join(src, "requirements.txt"))
                venv = os.path.join(svc_dir(name), "venv")
                if py_markers and not os.path.isdir(venv):
                    code = run_logged(f"{shlex.quote(sys.executable)} -m venv {shlex.quote(venv)}", src, env, log,
                                      label="create virtualenv")
                    if code != 0:
                        raise RuntimeError("virtualenv creation failed")
                if py_markers:
                    env = build_env(name, get_cfg(name))
                if build_cmd:
                    code = run_logged(build_cmd, src, env, log, timeout=1800)
                    if code != 0:
                        raise RuntimeError(f"build command failed (exit {code})")
                else:
                    log.write("no build step")
            else:
                tag = f"wh-{name}:latest"
                code = run_logged(f"docker build -t {shlex.quote(tag)} .", src, {**os.environ}, log, timeout=3000,
                                  label=f"docker build -t {tag} .")
                if code != 0:
                    raise RuntimeError(f"docker build failed (exit {code})")

            # ---- 4. (re)start
            log.write("==> Starting")
            if is_running(name):
                _stop_proc(name)
            update_cfg(name, desired="running")
            _spawn(name, announce=log)
            time.sleep(2.5)
            if not is_running(name):
                raise RuntimeError(f"process exited right after start (exit {rt(name).exit_code}) - see runtime logs")
            log.write(f"==> Deploy #{dep_id} live in {time.time() - t0:.0f}s")
            _finish_deploy(name, dep_id, "live", commit=commit)
            add_event("success", f"'{name}' deployed successfully (#{dep_id}, {time.time() - t0:.0f}s)", service=name)
    except Exception as e:
        log.write(f"ERROR: {e}")
        log.write(f"==> Deploy #{dep_id} failed")
        _finish_deploy(name, dep_id, "failed", error=str(e))
        add_event("error", f"Deploy #{dep_id} of '{name}' failed: {e}", service=name)
        logger.exception("deploy failed for %s", name)
    finally:
        r.building = False
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass


# ------------------------------------------------------------------ process control
def _spawn(name, announce=None):
    cfg = get_cfg(name)
    r = rt(name)
    log = LogFile(runtime_log(name))
    runtime = cfg.get("runtime_used") or cfg["runtime"]
    cwd = src_dir(name)
    env = build_env(name, cfg)
    if runtime == "docker":
        tag = f"wh-{name}:latest"
        env_file = os.path.join(svc_dir(name), "docker.env")
        merged = {**get_state()["global_env"], **(cfg.get("env") or {})}
        if cfg["kind"] == "web":
            merged["PORT"] = str(cfg["port"])
        with open(env_file, "w") as f:
            f.write("\n".join(f"{k}={str(v).replace(chr(10), ' ')}" for k, v in merged.items()) + "\n")
        os.chmod(env_file, 0o600)
        subprocess.run(["docker", "rm", "-f", f"wh-{name}"], capture_output=True)
        ports = f"-p 127.0.0.1:{cfg['host_port']}:{cfg['port']} " if cfg["kind"] == "web" and cfg.get("host_port") else ""
        cmd = f"docker run --rm --name wh-{name} {ports}--env-file {shlex.quote(env_file)} {shlex.quote(tag)}"
        env = {**os.environ}
    else:
        cmd = cfg.get("start_cmd") or cfg.get("eff_start") or ""
        if not cmd:
            raise RuntimeError("No start command configured. Set one in Settings.")
    log.write(f"==> Starting: {cmd}")
    p = subprocess.Popen(
        cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1, start_new_session=True,
    )
    r.proc, r.started, r.exit_code, r.stopping = p, time.time(), None, False
    try:
        r.ps = psutil.Process(p.pid)
        r.ps.cpu_percent(None)
    except Exception:
        r.ps = None

    def _pump():
        for line in p.stdout:
            log.write(line.rstrip("\n"))
        code = p.wait()
        r.exit_code = code
        log.write(f"==> Process exited with code {code}")

    threading.Thread(target=_pump, daemon=True, name=f"pump-{name}").start()
    update_cfg(name, last_status="running")


def _stop_proc(name, timeout=10):
    r = rt(name)
    p = r.proc
    if not p or p.poll() is not None:
        return
    r.stopping = True
    cfg = get_cfg(name) or {}
    if (cfg.get("runtime_used") or cfg.get("runtime")) == "docker":
        subprocess.run(["docker", "stop", "-t", "8", f"wh-{name}"], capture_output=True)
    _kill_group(p, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and p.poll() is None:
        time.sleep(0.2)
    if p.poll() is None:
        _kill_group(p, signal.SIGKILL)
        time.sleep(0.3)


def start(name):
    cfg = get_cfg(name)
    if not cfg:
        raise ValueError("Service not found")
    if is_running(name):
        return
    if rt(name).building:
        raise ValueError("Deploy in progress")
    if not (cfg.get("start_cmd") or cfg.get("eff_start") or cfg.get("runtime_used") == "docker"):
        raise ValueError("Deploy the service first")
    r = rt(name)
    r.crash_times.clear()
    r.next_try = 0
    update_cfg(name, desired="running")
    _spawn(name)


def stop(name):
    if not get_cfg(name):
        return
    update_cfg(name, desired="stopped", last_status="stopped")
    _stop_proc(name)


def restart(name):
    cfg = get_cfg(name)
    if not cfg:
        raise ValueError("Service not found")
    _stop_proc(name)
    rt(name).crash_times.clear()
    rt(name).next_try = 0
    update_cfg(name, desired="running", restarts=(cfg.get("restarts") or 0) + 1)
    _spawn(name)


def run_oneoff(name, cmd, timeout=60):
    cfg = get_cfg(name)
    if not cfg:
        raise ValueError("Service not found")
    env = build_env(name, cfg)
    try:
        p = subprocess.run(cmd, shell=True, cwd=src_dir(name), env=env, capture_output=True, text=True,
                           errors="replace", timeout=timeout, start_new_session=True)
        out = (p.stdout or "") + (p.stderr or "")
        return {"code": p.returncode, "output": out[-100000:]}
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {"code": 124, "output": out[-100000:] + f"\n[timed out after {timeout}s]"}


# ------------------------------------------------------------------ monitor thread
SYS_HISTORY = deque(maxlen=120)


def system_stats():
    vm = psutil.virtual_memory()
    try:
        du = psutil.disk_usage(DATA_DIR)
        disk = {"total": du.total, "used": du.used, "percent": du.percent}
    except Exception:
        disk = {"total": 0, "used": 0, "percent": 0}
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cores": psutil.cpu_count() or 1,
        "mem_total": vm.total, "mem_used": vm.used, "mem_percent": vm.percent,
        "disk": disk,
        "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [0, 0, 0],
        "history": list(SYS_HISTORY),
    }


def _sample():
    SYS_HISTORY.append((int(time.time()), psutil.cpu_percent(interval=None), psutil.virtual_memory().percent))
    for name in list(get_state()["services"].keys()):
        r = rt(name)
        if is_running(name) and r.ps:
            try:
                cpu = r.ps.cpu_percent(None)
                rss = r.ps.memory_info().rss
                for ch in r.ps.children(recursive=True):
                    try:
                        rss += ch.memory_info().rss
                        cpu += ch.cpu_percent(None)
                    except Exception:
                        pass
                r.history.append((int(time.time()), round(cpu, 1), rss))
            except Exception:
                pass


def _check_exits():
    for name, cfg in list(get_state()["services"].items()):
        r = rt(name)
        if r.building or cfg.get("desired") != "running" or is_running(name) or r.stopping:
            continue
        now = time.time()
        if r.proc is not None and r.exit_code is not None and now >= r.next_try and r.next_try == 0:
            # fresh crash detected
            r.crash_times.append(now)
            recent = [t for t in r.crash_times if now - t < 300]
            add_event("error", f"'{name}' crashed (exit code {r.exit_code})", service=name)
            if cfg.get("auto_restart") and len(recent) <= 5:
                delay = min(2 ** (len(recent) - 1), 30)
                r.next_try = now + delay
                logger.info("restarting %s in %ss", name, delay)
            else:
                update_cfg(name, desired="stopped", last_status="crashed")
                add_event("error", f"'{name}' is crash-looping, auto-restart stopped", service=name)
                r.exit_code = None
        elif r.next_try and now >= r.next_try:
            r.next_try = 0
            try:
                update_cfg(name, restarts=(cfg.get("restarts") or 0) + 1)
                LogFile(runtime_log(name)).write("==> Auto-restarting after crash")
                _spawn(name)
                r.exit_code = None
                add_event("warn", f"'{name}' auto-restarted", service=name, notify=False)
            except Exception as e:
                logger.exception("auto-restart failed")
                update_cfg(name, desired="stopped", last_status="crashed")
                add_event("error", f"Auto-restart of '{name}' failed: {e}", service=name)


def autostart_all():
    """Called on boot: relaunch every service whose desired state is running."""
    for name, cfg in get_state()["services"].items():
        if cfg.get("desired") == "running" and (cfg.get("start_cmd") or cfg.get("eff_start") or cfg.get("runtime_used") == "docker"):
            try:
                LogFile(runtime_log(name)).write("==> Host restarted, starting service")
                _spawn(name)
            except Exception:
                logger.exception("autostart failed for %s", name)


def start_monitor():
    def _loop():
        autostart_all()
        tick = 0
        while True:
            time.sleep(1)
            tick += 1
            try:
                _check_exits()
                if tick % 5 == 0:
                    _sample()
            except Exception:
                logger.exception("monitor error")

    threading.Thread(target=_loop, daemon=True, name="monitor").start()


def stop_all():
    for name in list(get_state()["services"].keys()):
        try:
            _stop_proc(name, timeout=5)
        except Exception:
            pass
