"""
Service supervisor core: state, processes, port detection, public URLs, monitor.
The deploy pipeline (fetch, detect, build, snapshots, rollback) lives in deploy.py.
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

import common as C
from common import DATA_DIR, SERVICES_DIR, logger, add_event, valid_name, load_data, PORT as HOST_PORT

STATE_FILE = os.path.join(DATA_DIR, "services.json")
_state_lock = threading.RLock()
MAX_LOG_BYTES = 5 * 1024 * 1024
BROWSER_RE = re.compile(r"chrom|firefox|webkit|xvfb|geckodriver|chromedriver|headless", re.I)
KINDS = ("auto", "web", "worker", "cron")


# ------------------------------------------------------------------ state store
def _read_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            s.setdefault("services", {})
            s.setdefault("global_env", {})
            s.setdefault("settings", {})
            return s
        except Exception:
            logger.exception("services.json unreadable")
    return {"services": {}, "global_env": {}, "settings": {}}


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


def service_domain():
    """Base domain for <name>.<domain> URLs (env SERVICE_DOMAIN or console setting)."""
    d = os.environ.get("SERVICE_DOMAIN") or get_state()["settings"].get("service_domain") or ""
    return d.strip().lower().lstrip(".").rstrip(".")


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


# ------------------------------------------------------------------ host capabilities
_cap_cache = {"t": 0, "v": None}


def capabilities():
    if time.time() - _cap_cache["t"] < 30 and _cap_cache["v"]:
        return _cap_cache["v"]
    docker_ok, docker_err = False, ""
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=5)
            docker_ok = r.returncode == 0
            if not docker_ok:
                lines = (r.stderr or "").strip().splitlines()
                docker_err = lines[-1] if lines else ""
        except Exception as e:
            docker_err = str(e)
    else:
        docker_err = "docker CLI not installed on this host"

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception:
            return None

    caps = {"docker": docker_ok, "docker_reason": "" if docker_ok else (docker_err or "Docker daemon not reachable"),
            "git": bool(shutil.which("git")), "node": ver(["node", "--version"]),
            "npm": bool(shutil.which("npm")), "python": sys.version.split()[0]}
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
        self.history = deque(maxlen=720)  # (t, cpu, rss) every 5s -> 1h
        self.stopping = False
        self.listen_port = None
        self.mem_strikes = 0


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


def _has_live_deploy(cfg):
    return any(d.get("status") == "live" for d in cfg.get("deploys", []))


def status_of(name):
    r = rt(name)
    cfg = get_cfg(name) or {}
    if r.building:
        return "building"
    if cfg.get("kind") == "cron":
        try:
            import cron
            if cron.is_running(name):
                return "running"
        except Exception:
            pass
        if _has_live_deploy(cfg):
            return "scheduled" if cfg.get("enabled", True) else "paused"
        return cfg.get("last_status") or "stopped"
    if is_running(name):
        return "running"
    if cfg.get("desired") == "running" and time.time() < r.next_try:
        return "restarting"
    return cfg.get("last_status") or "stopped"


# ------------------------------------------------------------------ ports & public URLs
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def _free_port(exclude=()):
    st = get_state()["services"]
    used = {c.get("port") for c in st.values()} | {c.get("host_port") for c in st.values()} | set(exclude) | {HOST_PORT}
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


def _tcp_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except Exception:
        return False


def _listeners(pid):
    """[(port, ip, process-name)] of LISTEN sockets in the process tree."""
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except Exception:
        return []
    out = []
    for p in procs:
        try:
            pname = p.name()
            if BROWSER_RE.search(pname):
                continue
            fn = getattr(p, "net_connections", None) or p.connections
            for c in fn(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr:
                    out.append((c.laddr.port, c.laddr.ip, pname))
        except Exception:
            continue
    return out


def target_port(name):
    """Port the proxy should talk to (None until the app is listening)."""
    cfg = get_cfg(name) or {}
    if cfg.get("kind") in ("cron", None):
        return None
    if (cfg.get("runtime_used") or cfg.get("runtime")) == "docker":
        return cfg.get("host_port")
    return rt(name).listen_port


def detect_listen(name):
    """Find the port the app really listens on (it may ignore $PORT). Promotes workers to web."""
    cfg = get_cfg(name) or {}
    r = rt(name)
    if cfg.get("kind") == "cron" or not is_running(name):
        return None
    if (cfg.get("runtime_used") or cfg.get("runtime")) == "docker":
        return cfg.get("host_port")
    if r.listen_port:
        return r.listen_port
    exp = cfg.get("port")
    found = [x for x in _listeners(r.proc.pid) if x[0] != HOST_PORT]
    if cfg.get("kind") == "worker":
        # Only expose a worker when it clearly serves the public: $PORT or a wildcard bind.
        found = [x for x in found if x[0] == exp or x[1] in ("0.0.0.0", "::")]
    ports = {x[0] for x in found}
    port = exp if exp in ports else (min(ports) if ports else None)
    if port is None and exp and _tcp_open(exp):
        port = exp
    if port is None:
        return None
    r.listen_port = port
    log = LogFile(runtime_log(name))
    if exp and port != exp:
        log.write(f"==> App listens on :{port} instead of $PORT ({exp}); proxying to :{port}")
    else:
        log.write(f"==> App is listening on :{port}")
    if cfg.get("kind") in ("worker", "auto"):
        update_cfg(name, kind="web")
        log.write("==> Open network port detected: switched to a web service (public URL enabled)")
        add_event("info", f"'{name}' opened a port, so it is now a web service: {public_url(name)}",
                  service=name, notify=False, url=public_url(name))
    return port


def base_url():
    """Public base URL: the address the admin really uses (learned from console requests), else DOMAIN."""
    return (get_state()["settings"].get("public_base") or C.DOMAIN).rstrip("/")


def public_url(name):
    """Absolute public URL: custom domain > wildcard sub-domain > /svc/<name>/ path."""
    cfg = get_cfg(name) or {}
    base = base_url()
    scheme = "https" if base.startswith("https://") else "http"
    doms = cfg.get("domains") or []
    if doms:
        return f"{scheme}://{doms[0]}/"
    sd = service_domain()
    if sd:
        return f"{scheme}://{name}.{sd}/"
    return f"{base}/svc/{name}/"


# ------------------------------------------------------------------ helpers
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
    if cfg.get("kind") != "cron" and cfg.get("port"):
        env["PORT"] = str(cfg["port"])
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


_PUBLIC_KEYS = ("name", "kind", "runtime", "source", "branch", "build_cmd", "start_cmd", "port", "host_port",
                "auto_restart", "desired", "created", "restarts", "hook_token", "health_path", "health_restart",
                "last_commit", "workdir", "notes", "eff_start", "eff_build", "domains", "memory_mb", "schedule",
                "enabled", "timeout_s", "auto_kind", "runtime_used")


def public_view(name):
    cfg = get_cfg(name)
    if not cfg:
        return None
    r = rt(name)
    v = {k: cfg.get(k) for k in _PUBLIC_KEYS}
    v["domains"] = list(cfg.get("domains") or [])
    v["repo"] = mask_repo(cfg.get("repo"))
    v["status"] = status_of(name)
    v["deploys"] = list(reversed(cfg.get("deploys", [])))
    v["env_count"] = len(cfg.get("env") or {})
    v["pid"] = r.proc.pid if is_running(name) else None
    v["uptime"] = int(time.time() - r.started) if is_running(name) and r.started else 0
    v["exit_code"] = r.exit_code
    v["has_auth"] = bool(cfg.get("basic_auth"))
    v["listen_port"] = target_port(name)
    v["metrics"] = list(r.history)[-60:]
    if cfg.get("kind") == "web":
        v["path"] = f"/svc/{name}/"
        sd = service_domain()
        v["sub_host"] = f"{name}.{sd}" if sd else None
        v["abs_url"] = public_url(name)
    try:
        import health
        v["health"] = health.summary(name)
    except Exception:
        v["health"] = {"status": "n/a"}
    if cfg.get("kind") == "cron":
        try:
            import cron
            v["next_run"] = cron.next_run_iso(name)
            v["tz"] = cron.tzname()
        except Exception:
            v["next_run"] = None
        v["runs"] = list(reversed(cfg.get("runs", [])))[:20]
    v["snapshots"] = list_snapshots(name)
    return v


# ------------------------------------------------------------------ subprocess runner with live logs
def run_logged(cmd, cwd, env, log, timeout=1200, label=None):
    """Run a shell command, stream every line to `log`. Returns exit code."""
    log.write(f"$ {label or cmd}", stamp=True)
    try:
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1, start_new_session=True)
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


# ------------------------------------------------------------------ create / delete / deploy wrappers
def create_service(spec):
    """spec: name, kind(auto|web|worker|cron), runtime, source, repo, branch, build_cmd, start_cmd, env, schedule..."""
    name = (spec.get("name") or "").strip().lower()
    if not valid_name(name):
        raise ValueError("Name must be 2-41 chars: lowercase letters, numbers, hyphens (not reserved)")
    if name in load_data().get("sites", {}):
        raise ValueError("A static site with this name already exists")
    kind = spec.get("kind") if spec.get("kind") in KINDS else "auto"
    runtime = spec.get("runtime") if spec.get("runtime") in ("native", "docker") else "native"
    source = spec.get("source") if spec.get("source") in ("zip", "git") else "zip"
    if source == "git" and not (spec.get("repo") or "").startswith(("https://", "http://", "git@")):
        raise ValueError("Enter a valid git repository URL (https://...)")
    schedule = (spec.get("schedule") or "").strip()
    if kind == "cron":
        import cron
        cron.parse(schedule)
        if not (spec.get("start_cmd") or "").strip():
            raise ValueError("A cron job needs a command to run (e.g. python job.py)")
    env = {str(k).strip(): str(v) for k, v in (spec.get("env") or {}).items() if str(k).strip()}
    for k in env:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise ValueError(f"Invalid environment variable name: {k}")
    try:
        memory_mb = max(0, int(spec.get("memory_mb") or 0))
    except (TypeError, ValueError):
        raise ValueError("Memory limit must be a number of MB")

    def _m(s):
        if name in s["services"]:
            raise ValueError("A service with this name already exists")
        port = None
        if kind != "cron":
            port = int(spec["port"]) if spec.get("port") else _free_port(
                exclude={c.get("port") for c in s["services"].values()} | {c.get("host_port") for c in s["services"].values()})
        s["services"][name] = {
            "name": name, "kind": kind, "auto_kind": kind == "auto", "runtime": runtime, "source": source,
            "repo": (spec.get("repo") or "").strip(), "branch": (spec.get("branch") or "").strip(),
            "build_cmd": (spec.get("build_cmd") or "").strip(), "start_cmd": (spec.get("start_cmd") or "").strip(),
            "port": port, "host_port": None, "env": env, "auto_restart": True, "desired": "stopped",
            "created": datetime.now().isoformat(timespec="seconds"), "restarts": 0,
            "hook_token": secrets.token_urlsafe(18), "health_path": "", "health_restart": True,
            "deploys": [], "last_status": "stopped", "workdir": "", "notes": "", "domains": [], "basic_auth": "",
            "memory_mb": memory_mb, "schedule": schedule, "enabled": True, "timeout_s": 1800, "runs": [],
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


def deploy(name, trigger="manual", zip_path=None, restore_id=None):
    import deploy as _d
    return _d.deploy(name, trigger, zip_path, restore_id)


def rollback(name, dep_id):
    import deploy as _d
    return _d.rollback(name, dep_id)


def list_snapshots(name):
    try:
        import deploy as _d
        return _d.list_snapshots(name)
    except Exception:
        return []


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
        if cfg.get("port"):
            merged["PORT"] = str(cfg["port"])
        with open(env_file, "w") as f:
            f.write("\n".join(f"{k}={str(v).replace(chr(10), ' ')}" for k, v in merged.items()) + "\n")
        os.chmod(env_file, 0o600)
        subprocess.run(["docker", "rm", "-f", f"wh-{name}"], capture_output=True)
        ports = f"-p 127.0.0.1:{cfg['host_port']}:{cfg['port']} " if cfg.get("host_port") and cfg.get("port") else ""
        cmd = f"docker run --rm --name wh-{name} {ports}--env-file {shlex.quote(env_file)} {shlex.quote(tag)}"
        env = {**os.environ}
    else:
        cmd = cfg.get("start_cmd") or cfg.get("eff_start") or ""
        if not cmd:
            raise RuntimeError("No start command configured. Set one in Settings.")
    log.write(f"==> Starting: {cmd}")
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", bufsize=1, start_new_session=True)
    r.proc, r.started, r.exit_code, r.stopping = p, time.time(), None, False
    r.listen_port, r.mem_strikes = None, 0
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
    if cfg.get("kind") == "cron":
        update_cfg(name, enabled=True, desired="running", last_status="scheduled")
        return
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
    cfg = get_cfg(name)
    if not cfg:
        return
    if cfg.get("kind") == "cron":
        update_cfg(name, enabled=False, desired="stopped", last_status="paused")
        return
    update_cfg(name, desired="stopped", last_status="stopped")
    _stop_proc(name)


def restart(name):
    cfg = get_cfg(name)
    if not cfg:
        raise ValueError("Service not found")
    if cfg.get("kind") == "cron":
        raise ValueError("Cron jobs run on a schedule - use 'Run now' instead")
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
        return {"code": p.returncode, "output": ((p.stdout or "") + (p.stderr or ""))[-100000:]}
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
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
    return {"cpu": psutil.cpu_percent(interval=None), "cores": psutil.cpu_count() or 1,
            "mem_total": vm.total, "mem_used": vm.used, "mem_percent": vm.percent, "disk": disk,
            "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [0, 0, 0],
            "history": list(SYS_HISTORY)}


def _sample():
    SYS_HISTORY.append((int(time.time()), psutil.cpu_percent(interval=None), psutil.virtual_memory().percent))
    for name, cfg in list(get_state()["services"].items()):
        if cfg.get("kind") == "cron":
            continue
        r = rt(name)
        if not is_running(name):
            continue
        if r.listen_port is None:
            try:
                detect_listen(name)
            except Exception:
                logger.exception("port detection failed for %s", name)
        if not r.ps:
            continue
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
            continue
        limit = int(cfg.get("memory_mb") or 0)
        r.mem_strikes = r.mem_strikes + 1 if limit and rss > limit * 1048576 else 0
        if r.mem_strikes >= 3:
            r.mem_strikes = 0
            LogFile(runtime_log(name)).write(f"==> Memory limit exceeded ({rss // 1048576}MB > {limit}MB), restarting")
            add_event("error", f"'{name}' exceeded its {limit}MB memory limit and was restarted",
                      service=name, event="memory")
            threading.Thread(target=restart, args=(name,), daemon=True).start()


def _check_exits():
    for name, cfg in list(get_state()["services"].items()):
        if cfg.get("kind") == "cron":
            continue
        r = rt(name)
        if r.building or cfg.get("desired") != "running" or is_running(name) or r.stopping:
            continue
        now = time.time()
        if r.proc is not None and r.exit_code is not None and now >= r.next_try and r.next_try == 0:
            r.crash_times.append(now)
            recent = [t for t in r.crash_times if now - t < 300]
            add_event("error", f"'{name}' crashed (exit code {r.exit_code})", service=name, event="crash")
            if cfg.get("auto_restart") and len(recent) <= 5:
                delay = min(2 ** (len(recent) - 1), 30)
                r.next_try = now + delay
                logger.info("restarting %s in %ss", name, delay)
            else:
                update_cfg(name, desired="stopped", last_status="crashed")
                add_event("error", f"'{name}' is crash-looping, auto-restart stopped", service=name, event="crash")
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
                add_event("error", f"Auto-restart of '{name}' failed: {e}", service=name, event="crash")


def autostart_all():
    """Called on boot: relaunch every service whose desired state is running."""
    for name, cfg in get_state()["services"].items():
        if cfg.get("kind") == "cron":
            continue
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
    for mod in ("health", "cron"):
        try:
            __import__(mod).start()
        except Exception:
            logger.exception("could not start %s", mod)


def stop_all():
    for name in list(get_state()["services"].keys()):
        try:
            _stop_proc(name, timeout=5)
        except Exception:
            pass
