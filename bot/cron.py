"""Cron jobs: 5-field schedule parser, scheduler thread, run history with logs."""
import os
import time
import threading
from datetime import datetime, timedelta, timezone

import common as C
import supervisor as S

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

TICK = int(os.environ.get("CRON_TICK", "10"))
RUNNING = {}   # name -> run id
NEXT = {}      # name -> ((schedule, tz), datetime)
_lock = threading.Lock()
_started = False

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DOWS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


def _field(txt, lo, hi, names=None):
    out = set()
    for part in txt.split(","):
        part = part.strip().lower()
        if not part:
            raise ValueError("empty field")
        step, stepped = 1, False
        if "/" in part:
            part, st = part.split("/", 1)
            if not st.isdigit() or int(st) == 0:
                raise ValueError(f"bad step '{st}'")
            step, stepped = int(st), True

        def num(x):
            if names and x in names:
                return names[x]
            if not x.isdigit():
                raise ValueError(f"bad value '{x}'")
            return int(x)

        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = num(x), num(y)
        else:
            a = num(part)
            b = hi if stepped else a
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"value out of range {lo}-{hi}: '{part}'")
        out.update(range(a, b + 1, step))
    return out


def parse(expr):
    expr = (expr or '').strip()
    expr = ALIASES.get(expr.lower(), expr)
    f = expr.split()
    if len(f) != 5:
        raise ValueError("Schedule needs 5 fields: minute hour day-of-month month day-of-week (e.g. */5 * * * *)")
    dows = _field(f[4], 0, 7, DOWS)
    return {"min": _field(f[0], 0, 59), "hour": _field(f[1], 0, 23), "dom": _field(f[2], 1, 31),
            "mon": _field(f[3], 1, 12, MONTHS), "dow": {d % 7 for d in dows},
            "dom_star": f[2].startswith("*"), "dow_star": f[4].startswith("*")}


def _day_ok(s, t):
    dom_ok = t.day in s["dom"]
    dow_ok = ((t.weekday() + 1) % 7) in s["dow"]
    return (dom_ok and dow_ok) if (s["dom_star"] or s["dow_star"]) else (dom_ok or dow_ok)


def next_run(s, after):
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = t + timedelta(days=366 * 5)
    while t < limit:
        if t.month not in s["mon"]:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
        elif not _day_ok(s, t):
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
        elif t.hour not in s["hour"]:
            t = t.replace(minute=0) + timedelta(hours=1)
        elif t.minute not in s["min"]:
            t += timedelta(minutes=1)
        else:
            return t
    raise ValueError("schedule never matches")


def tzname():
    return (S.get_state()["settings"].get("timezone") or os.environ.get("TZ") or "UTC").strip() or "UTC"


def _now():
    name = tzname()
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def run_log(name, run_id):
    return os.path.join(S.svc_dir(name), "runs", f"{int(run_id)}.log")


def is_running(name):
    return name in RUNNING


def _deployed(cfg):
    return any(d.get("status") == "live" for d in cfg.get("deploys", []))


def next_run_iso(name):
    cfg = S.get_cfg(name) or {}
    if cfg.get("kind") != "cron" or not cfg.get("enabled", True) or not _deployed(cfg):
        return None
    try:
        n = NEXT.get(name)
        if not n or n[0] != (cfg.get("schedule", ""), tzname()):
            n = ((cfg.get("schedule", ""), tzname()), next_run(parse(cfg.get("schedule", "")), _now()))
            NEXT[name] = n
        return n[1].isoformat(timespec="minutes")
    except ValueError:
        return None


def execute(name, trigger="manual"):
    cfg = S.get_cfg(name)
    if not cfg or cfg.get("kind") != "cron":
        return None
    with _lock:
        if name in RUNNING:
            return None
        run_id = (cfg["runs"][-1]["id"] + 1) if cfg.get("runs") else 1
        RUNNING[name] = run_id
    rec = {"id": run_id, "trigger": trigger, "status": "running", "code": None,
           "started": datetime.now().isoformat(timespec="seconds"), "seconds": None}
    S.mutate(lambda s: s["services"][name].setdefault("runs", []).append(rec))
    log = S.LogFile(run_log(name, run_id))
    t0 = time.time()
    timeout = int(cfg.get("timeout_s") or 1800)
    code, status = -1, "failed"
    try:
        cmd = cfg.get("start_cmd") or cfg.get("eff_start") or ""
        if not cmd:
            raise RuntimeError("No command configured for this cron job")
        code = S.run_logged(cmd, S.src_dir(name), S.build_env(name, cfg), log, timeout=timeout)
        status = "ok" if code == 0 else ("timeout" if time.time() - t0 >= timeout - 1 else "failed")
    except Exception as e:
        log.write(f"ERROR: {e}")
    finally:
        secs = round(time.time() - t0, 1)

        def _done(s):
            c = s["services"].get(name)
            if not c:
                return
            for r in c.get("runs", []):
                if r["id"] == run_id:
                    r.update(status=status, code=code, seconds=secs)
            for old in c["runs"][:-30]:
                try:
                    os.remove(run_log(name, old["id"]))
                except OSError:
                    pass
            c["runs"] = c["runs"][-30:]

        S.mutate(_done)
        RUNNING.pop(name, None)
    log.write(f"==> finished: {status} (exit {code}) in {secs}s")
    if status != "ok":
        C.add_event("error", f"Cron '{name}' run #{run_id} {status} (exit {code})", service=name, event="cron_failed")
    return run_id


def run_async(name, trigger="manual"):
    if name in RUNNING:
        raise ValueError("A run is already in progress")
    threading.Thread(target=execute, args=(name, trigger), daemon=True, name=f"cron-{name}").start()


def _tick():
    now = _now()
    tz = tzname()
    for name, cfg in list(S.get_state()["services"].items()):
        if cfg.get("kind") != "cron" or not cfg.get("enabled", True) or not _deployed(cfg):
            continue
        sig = (cfg.get("schedule", ""), tz)
        n = NEXT.get(name)
        try:
            if not n or n[0] != sig:
                NEXT[name] = (sig, next_run(parse(sig[0]), now))
                continue
            if now >= n[1]:
                NEXT[name] = (sig, next_run(parse(sig[0]), now))
                if name in RUNNING:
                    S.LogFile(os.path.join(S.svc_dir(name), "cron.log")).write("skipped a run: previous run still active")
                    continue
                threading.Thread(target=execute, args=(name, "schedule"), daemon=True, name=f"cron-{name}").start()
        except ValueError:
            continue


def _loop():
    while True:
        time.sleep(TICK)
        try:
            _tick()
        except Exception:
            C.logger.exception("cron tick failed")


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="cron").start()
