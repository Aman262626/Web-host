"""Admin authentication decorator shared by every console blueprint."""
import hmac
from functools import wraps

from flask import request, jsonify

import common as C
import supervisor as S


def _note_base():
    """Remember the public address the admin really uses, so service links are right even if DOMAIN is stale."""
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    if not host or host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0"):
        return
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
    base = f"{proto}://{host}"
    try:
        if S.get_state()["settings"].get("public_base") != base:
            S.mutate(lambda s: s["settings"].__setitem__("public_base", base))
    except Exception:
        pass


def require_admin(f):
    @wraps(f)
    def wrapper(*a, **kw):
        auth = request.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        if tok and C.verify_token(tok):
            _note_base()
            return f(*a, **kw)
        key = request.headers.get("X-API-Key", "")
        if key and hmac.compare_digest(key.encode(), C.API_KEY.encode()):
            return f(*a, **kw)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapper
