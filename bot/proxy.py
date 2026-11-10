"""Public access layer for services and sites.

* /svc/<name>/...            path URL (always available)
* <name>.<SERVICE_DOMAIN>    wildcard sub-domain URL (optional)
* any custom domain          added per service/site
* optional basic-auth password protection
* HTML / CSS / redirect / cookie rewriting so apps work under a path prefix,
  plus a Referer/cookie fallback for apps that use root-absolute URLs.
"""
import os
import re
import base64
import hmac
from urllib.parse import urlparse

import requests
from flask import Blueprint, request, Response, current_app, abort

import common as C
import supervisor as S

bp = Blueprint("proxy", __name__)

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host"}
SKIP_RESP = HOP | {"content-length", "content-encoding", "set-cookie", "location"}
KNOWN = {"console", "svc", "hooks", "api", "ui", "health"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
MAX_REWRITE = 5 * 1024 * 1024


def _hostname():
    return (request.host or "").split(":")[0].lower()


def own_hosts():
    hs = {"localhost", "127.0.0.1", "0.0.0.0"}
    h = urlparse(C.DOMAIN).hostname
    if h:
        hs.add(h.lower())
    ex = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if ex:
        hs.add(ex.lower())
    return hs


def match_host(host):
    """Return ('svc'|'site', name) when the Host header belongs to a service/site."""
    st = S.get_state()["services"]
    sites = C.load_data().get("sites", {})
    for n, c in st.items():
        if host in (c.get("domains") or []):
            return "svc", n
    for n, i in sites.items():
        if host in (i.get("domains") or []):
            return "site", n
    sd = S.service_domain()
    if sd and host.endswith("." + sd):
        label = host[: -(len(sd) + 1)]
        if label and "." not in label:
            if label in st:
                return "svc", label
            if label in sites:
                return "site", label
    return None


# ---------------------------------------------------------------- auth
def check_auth(kind, name):
    if kind == "svc":
        cred = (S.get_cfg(name) or {}).get("basic_auth", "")
    else:
        cred = (C.load_data().get("sites", {}).get(name) or {}).get("basic_auth", "")
    if not cred:
        return None
    h = request.headers.get("Authorization", "")
    if h.startswith("Basic "):
        try:
            given = base64.b64decode(h[6:]).decode("utf-8", "replace")
        except Exception:
            given = ""
        if hmac.compare_digest(given.encode(), cred.encode()):
            return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Web-Host"'})


# ---------------------------------------------------------------- rewriting
_ATTR = re.compile(r"""(\b(?:src|href|action|poster|data-src|formaction)\s*=\s*)(["'])/(?!/)""", re.I)
_CSS_URL = re.compile(r"""(url\(\s*)(["']?)/(?!/)""", re.I)
_CSS_IMPORT = re.compile(r"""(@import\s+)(["'])/(?!/)""", re.I)


def _mk(prefix):
    p = prefix.lstrip("/") + "/"

    def f(m):
        if m.string.startswith(p, m.end()):
            return m.group(0)
        return m.group(1) + m.group(2) + prefix + "/"
    return f


def rewrite_css(text, prefix):
    f = _mk(prefix)
    return _CSS_IMPORT.sub(f, _CSS_URL.sub(f, text))


def _shim(prefix):
    return ('<script>(function(){var P="%s";function f(u){return typeof u==="string"&&u.charAt(0)==="/"&&'
            'u.charAt(1)!=="/"&&u.indexOf(P+"/")!==0&&u!==P?P+u:u}'
            'var F=window.fetch;if(F)window.fetch=function(i,o){return F.call(this,f(i),o)};'
            'var O=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=f(u);return O.apply(this,arguments)};'
            'var E=window.EventSource;if(E){window.EventSource=function(u,c){return new E(f(u),c)};window.EventSource.prototype=E.prototype}'
            '["pushState","replaceState"].forEach(function(k){var H=history[k];history[k]=function(s,t,u){return H.call(this,s,t,typeof u==="string"?f(u):u)}});'
            '})();</script>') % prefix


def rewrite_html(text, prefix):
    text = rewrite_css(_ATTR.sub(_mk(prefix), text), prefix)
    m = re.search(r"<head[^>]*>", text, re.I)
    tag = _shim(prefix)
    return text[: m.end()] + tag + text[m.end():] if m else tag + text


def _fix_location(loc, prefix, port):
    for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://0.0.0.0:{port}"):
        if loc.startswith(origin):
            loc = loc[len(origin):] or "/"
            break
    if not prefix:
        return loc
    host = request.host
    for scheme in ("http://", "https://"):
        if loc.startswith(f"{scheme}{host}/"):
            path = loc[len(f"{scheme}{host}"):]
            if not path.startswith(prefix + "/"):
                return f"{scheme}{host}{prefix}{path}"
            return loc
    if loc.startswith("/") and not loc.startswith("//") and loc != prefix and not loc.startswith(prefix + "/"):
        return prefix + loc
    return loc


def _fix_cookie(c, prefix):
    if prefix:
        return re.sub(r"(?i);\s*path=/(?=;|$)", f"; Path={prefix}/", c)
    return c


def _cookies(up):
    try:
        got = up.raw.headers.getlist("Set-Cookie")
        if got:
            return got
    except Exception:
        pass
    v = up.headers.get("Set-Cookie")
    return [v] if v else []


def _page(title, msg, status, refresh=None):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    html = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{title}</title><body style="font:16px/1.5 system-ui;background:#0b0c09;color:#ebe8da;display:grid;'
            f'place-items:center;min-height:100vh;margin:0"><div style="max-width:520px;padding:24px"><h1 style="margin:0 0 8px">'
            f'{title}</h1><p style="color:#8d8c78">{msg}</p></div></body>')
    return Response(html, status=status, mimetype="text/html")


# ---------------------------------------------------------------- forwarding
def forward(name, rest, prefix):
    cfg = S.get_cfg(name)
    if not cfg or cfg.get("kind") == "cron":
        abort(404)
    if not S.is_running(name):
        return _page(f"{name} is {S.status_of(name)}", "This service is not running right now. Start it from the console.", 503)
    port = S.target_port(name) or S.detect_listen(name)
    if not port:
        return _page("Starting up...", "Waiting for the app to open a network port. If this never changes, make sure it "
                     "listens on 0.0.0.0 and the PORT environment variable.", 503, refresh=3)
    url = f"http://127.0.0.1:{port}/{rest}"
    qs = request.query_string.decode("latin-1")
    if qs:
        url += "?" + qs
    headers = {k: v for k, v in request.headers if k.lower() not in HOP and k.lower() != "accept-encoding"}
    headers["Accept-Encoding"] = "gzip, deflate"
    headers["Host"] = request.host
    headers["X-Forwarded-For"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", request.scheme)
    headers["X-Forwarded-Host"] = request.host
    if prefix:
        headers["X-Forwarded-Prefix"] = prefix
    if cfg.get("basic_auth"):
        headers.pop("Authorization", None)
    body = request.get_data() if request.method not in ("GET", "HEAD") else None
    try:
        up = requests.request(request.method, url, data=body, headers=headers, allow_redirects=False,
                              stream=True, timeout=(5, 120))
    except requests.RequestException as e:
        return _page("502 Bad gateway", f"{name} is running but did not answer on port {port}: {e}", 502)

    out = [(k, v) for k, v in up.headers.items() if k.lower() not in SKIP_RESP]
    loc = up.headers.get("Location")
    if loc:
        out.append(("Location", _fix_location(loc, prefix, port)))
    for c in _cookies(up):
        out.append(("Set-Cookie", _fix_cookie(c, prefix)))
    mime = up.headers.get("Content-Type", "").split(";")[0].strip().lower()
    try:
        size = int(up.headers.get("Content-Length") or 0)
    except ValueError:
        size = 0
    if prefix and mime in ("text/html", "text/css") and size <= MAX_REWRITE and request.method != "HEAD":
        raw = up.content
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode(up.encoding or "latin-1", "replace")
        text = rewrite_html(text, prefix) if mime == "text/html" else rewrite_css(text, prefix)
        out = [(k, v) for k, v in out if k.lower() != "content-type"]
        out.append(("Content-Type", f"{mime}; charset=utf-8"))
        if mime == "text/html":
            out.append(("Set-Cookie", f"wh_svc={name}; Path=/; Max-Age=3600; SameSite=Lax"))
        return Response(text.encode("utf-8"), status=up.status_code, headers=out)
    return Response(up.iter_content(16384), status=up.status_code, headers=out)


def _ref_service():
    """(service, strong) guessed from the Referer (strong) or the wh_svc cookie (weak)."""
    ref = request.headers.get("Referer", "")
    if ref:
        try:
            m = re.match(r"^/svc/([a-z0-9-]+)(/|$)", urlparse(ref).path)
        except ValueError:
            m = None
        if m:
            return m.group(1), True
    c = request.cookies.get("wh_svc")
    if c and re.match(r"^[a-z0-9-]+$", c):
        return c, False
    return None


@bp.before_app_request
def gate():
    path = request.path
    host = _hostname()
    if host and host not in own_hosts():
        m = match_host(host)
        if m:
            kind, name = m
            denied = check_auth(kind, name)
            if denied:
                return denied
            rest = path.lstrip("/")
            if kind == "svc":
                return forward(name, rest, "")
            return current_app.view_functions["serve"](site=name, path=rest)
    seg = [p for p in path.split("/") if p]
    first = seg[0] if seg else ""
    if first == "svc":
        return check_auth("svc", seg[1]) if len(seg) > 1 else None
    if first and first not in KNOWN and first in C.load_data().get("sites", {}):
        return check_auth("site", first)
    if first:
        ref = _ref_service()
        if ref and (ref[1] or first not in KNOWN):
            name = ref[0]
            cfg = S.get_cfg(name)
            if cfg and cfg.get("kind") != "cron" and S.is_running(name):
                denied = check_auth("svc", name)
                if denied:
                    return denied
                return forward(name, path.lstrip("/"), f"/svc/{name}")
    return None


@bp.route("/svc/<name>/", defaults={"path": ""}, methods=METHODS)
@bp.route("/svc/<name>/<path:path>", methods=METHODS)
def svc_route(name, path):
    return forward(name, path, f"/svc/{name}")
