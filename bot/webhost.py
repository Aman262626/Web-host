"""
Web-Host - Telegram bot + web console + app supervisor.

* Static sites:   upload a ZIP (Telegram or console) -> https://<domain>/<name>/
* Web services:   Python / Node apps (native or Docker) -> https://<domain>/svc/<name>/
* Workers:        Telegram bots & background jobs with live logs, env vars, auto-restart
* Console:        open https://<domain>/ and sign in with ADMIN_PASSWORD
"""
import os
import hmac
import html
import time
import asyncio
import threading
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote

from flask import Flask, send_from_directory, request, jsonify, abort, make_response
from flask_cors import CORS
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes,
)
from werkzeug.utils import secure_filename

import common as C
import supervisor as S
import console as console_mod
from common import logger

app_web = Flask(__name__)
app_web.config["MAX_CONTENT_LENGTH"] = max(C.MAX_UPLOAD_MB, 100) * 1024 * 1024
CORS(app_web)
app_web.register_blueprint(console_mod.bp)
user_state = {}

SANDBOX_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-modals "
               "allow-downloads allow-pointer-lock allow-presentation")


# =============================================================== HOSTED SITES
@app_web.route("/<site>/")
@app_web.route("/<site>/<path:path>")
def serve(site, path=""):
    if site in C.RESERVED_NAMES:
        abort(404)
    data = C.load_data()
    info = data.get("sites", {}).get(site)
    if not info:
        return "<h1>404 - Site not found</h1>", 404
    if info.get("expires") and info["expires"] < datetime.now().isoformat():
        return "<h1>410 - This site has expired</h1>", 410
    folder = os.path.join(C.WEBSITES_DIR, site)
    if not os.path.isdir(folder):
        return "<h1>404 - Site files missing</h1>", 404

    try:
        target = C.safe_join(folder, path)
    except ValueError:
        abort(404)
    rel = os.path.relpath(target, folder)
    rel = "" if rel == "." else rel
    if os.path.isdir(target):
        idx = os.path.join(target, "index.html")
        if os.path.isfile(idx):
            rel = os.path.relpath(idx, folder)
        elif rel == "":
            htmls = sorted(f for f in os.listdir(folder) if f.lower().endswith(".html"))
            if htmls:
                rel = htmls[0]
            else:
                return "<h1>No HTML file found</h1>", 404
        else:
            abort(404)
    elif not os.path.isfile(target):
        if info.get("spa") and "." not in os.path.basename(path) and os.path.isfile(os.path.join(folder, "index.html")):
            rel = "index.html"
        elif os.path.isfile(os.path.join(folder, "404.html")):
            resp = make_response(send_from_directory(folder, "404.html"))
            resp.status_code = 404
            return _site_headers(resp, info)
        else:
            return "<h1>404 - File not found</h1>", 404

    resp = make_response(send_from_directory(folder, rel))
    if (resp.mimetype or "") == "text/html":
        C.track_visit(site)
    return _site_headers(resp, info)


def _site_headers(resp, info):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Uploaded HTML lives on the same origin as the admin console. The CSP sandbox gives each
    # site an opaque origin so it can never read the admin session. Turn off per site if needed.
    if info.get("isolation", "sandbox") != "off":
        resp.headers["Content-Security-Policy"] = SANDBOX_CSP
    return resp


# =============================================================== LEGACY PUBLIC / API-KEY API
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(key.encode(), C.API_KEY.encode()):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


@app_web.route("/api/sites", methods=["GET"])
def api_list_sites():
    data = C.load_data()
    analytics = data.get("analytics", {})
    result = []
    for name, info in data.get("sites", {}).items():
        result.append({
            "name": name, "url": f"{C.DOMAIN}/{name}/", "created": info.get("created", ""),
            "files": info.get("files", 0), "size_bytes": info.get("size", 0),
            "visits": analytics.get(name, {}).get("visits", 0),
        })
    return jsonify({"sites": result, "total": len(result)})


@app_web.route("/api/sites/<site_name>", methods=["DELETE"])
@require_api_key
def api_delete_site(site_name):
    if site_name not in C.load_data().get("sites", {}):
        return jsonify({"error": "Site not found"}), 404
    C.delete_site_files(site_name)
    return jsonify({"message": f"Site '{site_name}' deleted"})


@app_web.route("/api/upload", methods=["POST"])
@require_api_key
def api_upload():
    f = request.files.get("file")
    site_name = C.slugify(request.form.get("site_name", ""))
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    if not C.valid_name(site_name):
        return jsonify({"error": "Invalid site name"}), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify({"error": "Only ZIP files allowed"}), 400
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=console_mod.TMP_DIR)
    os.close(fd)
    try:
        f.save(tmp)
        C.deploy_site_zip(tmp, site_name, "api")
    except Exception as e:
        return jsonify({"error": f"Deploy failed: {e}"}), 400
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return jsonify({"message": "Site deployed!", "url": f"{C.DOMAIN}/{site_name}/", "name": site_name})


@app_web.route("/api/stats", methods=["GET"])
def api_stats():
    data = C.load_data()
    total = sum(a.get("visits", 0) for a in data.get("analytics", {}).values())
    return jsonify({"total_sites": len(data.get("sites", {})), "total_users": len(data.get("users", {})),
                    "total_visits": total, "uptime": "running"})


# =============================================================== TELEGRAM BOT
def is_admin(uid):
    return bool(C.ADMIN_ID) and uid == C.ADMIN_ID


def esc(x):
    return html.escape(str(x))


def guard(admin_only=False):
    """Registers the user, blocks banned users, optionally admin-only."""
    def deco(fn):
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if not user:
                return
            data = C.load_data()
            if str(user.id) in map(str, data.get("banned", [])) and not is_admin(user.id):
                if update.effective_message:
                    await update.effective_message.reply_text("🚫 You are banned from this bot.")
                return
            if admin_only and not is_admin(user.id):
                msg = update.effective_message
                if update.callback_query:
                    await update.callback_query.answer("Admin only", show_alert=True)
                elif msg:
                    await msg.reply_text("🔒 Admin only command.")
                return

            def _track(d):
                prev = d.setdefault("users", {}).get(str(user.id), {})
                d["users"][str(user.id)] = {**prev, "name": user.full_name, "username": user.username or "",
                                            "last_active": datetime.now().isoformat(timespec="seconds")}
            C.update_data(_track)
            return await fn(update, context)
        return wrapper
    return deco


def _user_sites(uid):
    return {n: i for n, i in C.load_data().get("sites", {}).items() if str(i.get("owner")) == str(uid)}


def _site_card(name):
    data = C.load_data()
    info = data.get("sites", {}).get(name)
    if not info:
        return None, None
    a = data.get("analytics", {}).get(name, {})
    daily = a.get("daily", {})
    week = sum(daily.get((datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(7))
    url = f"{C.DOMAIN}/{name}/"
    exp = info.get("expires")
    text = (f"🌐 <b>{esc(name)}</b>\n{esc(url)}\n\n"
            f"📁 Files: {info.get('files', 0)}  •  💾 {C.fmt_bytes(info.get('size', 0))}\n"
            f"👁 Visits: {a.get('visits', 0)} (last 7 days: {week})\n"
            f"🕒 Updated: {esc((info.get('updated') or info.get('created') or '')[:16].replace('T', ' '))}")
    if exp:
        text += f"\n⏳ Expires: {esc(exp[:16].replace('T', ' '))}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Open", url=url), InlineKeyboardButton("QR code", callback_data=f"qr:{name}")],
        [InlineKeyboardButton("Delete", callback_data=f"delask:{name}")],
    ])
    return text, kb


@guard()
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = [
        [InlineKeyboardButton("📤 Upload site", callback_data="upload"), InlineKeyboardButton("🗂 My sites", callback_data="mysites")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🖥 Open console", url=C.DOMAIN + "/"),
                     InlineKeyboardButton("⚙️ Services", callback_data="services")])
    await update.message.reply_text(
        "<b>Web-Host</b> 🚀\n\nSend me a <b>ZIP</b> of your website and get a live link in seconds.\n"
        "Tip: add the site name as the file caption to deploy instantly.",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


HELP_USER = (
    "<b>Commands</b>\n"
    "/start - main menu\n/mysites - your sites\n/info &lt;name&gt; - site details & visits\n"
    "/qr &lt;name&gt; - QR code for your link\n/rename &lt;old&gt; &lt;new&gt;\n/delete &lt;name&gt;\n"
    "/expire &lt;name&gt; &lt;hours|off&gt; - auto-delete a site later\n/stats - your statistics\n\n"
    "<b>Deploy</b>\nSend a ZIP file. Add a caption to use it as the site name."
)
HELP_ADMIN = (
    "\n\n<b>Admin</b>\n/panel - open web console\n/status - server health\n/services - manage apps & bots\n"
    "/logs &lt;svc&gt; [lines]\n/deploy &lt;svc&gt;\n/svcstart | /svcstop | /restart &lt;svc&gt;\n"
    "/env &lt;svc&gt; - list variable names\n/setenv &lt;svc&gt; KEY value\n/unsetenv &lt;svc&gt; KEY\n"
    "/allusers /allsites /broadcast &lt;msg&gt;\n/ban &lt;id&gt; /unban &lt;id&gt;"
)


@guard()
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_USER + (HELP_ADMIN if is_admin(update.effective_user.id) else "")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@guard()
async def cmd_mysites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_mysites(update.message, update.effective_user.id)


async def _send_mysites(message, uid):
    sites = _user_sites(uid)
    if not sites:
        await message.reply_text("You have no sites yet. Send me a ZIP file! 📦")
        return
    an = C.load_data().get("analytics", {})
    rows = [[InlineKeyboardButton(f"{n}  •  {an.get(n, {}).get('visits', 0)} 👁", callback_data=f"site:{n}")] for n in list(sites)[:30]]
    await message.reply_text(f"<b>Your sites ({len(sites)}/{C.MAX_SITES_PER_USER})</b>\nTap one for details:",
                             reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


def _can_manage(uid, name):
    info = C.load_data().get("sites", {}).get(name)
    return bool(info) and (str(info.get("owner")) == str(uid) or is_admin(uid))


@guard()
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /info <site_name>")
    name = context.args[0].lower()
    if not _can_manage(update.effective_user.id, name):
        return await update.message.reply_text("Site not found (or not yours).")
    text, kb = _site_card(name)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _send_qr(message, name):
    url = f"{C.DOMAIN}/{name}/"
    await message.reply_photo(f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&margin=20&data={quote(url)}",
                              caption=url)


@guard()
async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /qr <site_name>")
    name = context.args[0].lower()
    if not _can_manage(update.effective_user.id, name):
        return await update.message.reply_text("Site not found (or not yours).")
    await _send_qr(update.message, name)


def _remove_site(name):
    C.delete_site_files(name)


@guard()
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /delete <site_name>")
    name = context.args[0].lower()
    if not _can_manage(update.effective_user.id, name):
        return await update.message.reply_text("Site not found (or not yours).")
    _remove_site(name)
    await update.message.reply_text(f"🗑 Site '{name}' deleted.")


@guard()
async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Usage: /rename <old_name> <new_name>")
    old, new = context.args[0].lower(), C.slugify(context.args[1])
    if not _can_manage(update.effective_user.id, old):
        return await update.message.reply_text("Site not found (or not yours).")
    sites = C.load_data().get("sites", {})
    if not C.valid_name(new) or new in sites or S.get_cfg(new):
        return await update.message.reply_text("Invalid or already used name (2-41 chars: a-z, 0-9, -).")
    import shutil
    src = os.path.join(C.WEBSITES_DIR, old)
    if os.path.isdir(src):
        shutil.move(src, os.path.join(C.WEBSITES_DIR, new))

    def _m(d):
        d["sites"][new] = d["sites"].pop(old)
        if old in d["analytics"]:
            d["analytics"][new] = d["analytics"].pop(old)
    C.update_data(_m)
    await update.message.reply_text(f"✅ Renamed.\n{old} → {new}\n{C.DOMAIN}/{new}/", disable_web_page_preview=True)


@guard()
async def cmd_expire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Usage: /expire <site> <hours|off>\nExample: /expire mysite 24")
    name, val = context.args[0].lower(), context.args[1].lower()
    if not _can_manage(update.effective_user.id, name):
        return await update.message.reply_text("Site not found (or not yours).")
    if val in ("off", "0", "never"):
        exp = None
    else:
        try:
            hours = float(val)
            if not 0 < hours <= 24 * 365:
                raise ValueError
        except ValueError:
            return await update.message.reply_text("Hours must be a positive number, or 'off'.")
        exp = (datetime.now() + timedelta(hours=hours)).isoformat()

    def _m(d):
        d["sites"][name]["expires"] = exp
    C.update_data(_m)
    await update.message.reply_text("⏳ Auto-delete removed." if not exp else f"⏳ '{name}' will be deleted on {exp[:16].replace('T', ' ')}.")


@guard()
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = C.load_data()
    sites, an = data.get("sites", {}), data.get("analytics", {})
    uid = update.effective_user.id
    if is_admin(uid):
        total = sum(a.get("visits", 0) for a in an.values())
        top = sorted(an.items(), key=lambda x: x[1].get("visits", 0), reverse=True)[:5]
        text = (f"<b>Admin stats</b>\n\nSites: {len(sites)}\nUsers: {len(data.get('users', {}))}\nVisits: {total}\n"
                f"Services: {len(S.get_state()['services'])}\n\n<b>Top sites</b>\n" +
                "\n".join(f"• {esc(n)}: {s.get('visits', 0)}" for n, s in top))
    else:
        mine = _user_sites(uid)
        text = (f"<b>Your stats</b>\n\nSites: {len(mine)}/{C.MAX_SITES_PER_USER}\n"
                f"Visits: {sum(an.get(n, {}).get('visits', 0) for n in mine)}")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---- admin: server & services
@guard(admin_only=True)
async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Web console:", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🖥 Open console", url=C.DOMAIN + "/")]]))


def _fmt_dur(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h else "") + f"{m}m"


@guard(admin_only=True)
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sy = S.system_stats()
    caps = S.capabilities()
    svcs = S.get_state()["services"]
    lines = [f"{_status_icon(S.status_of(n))} {esc(n)} - {S.status_of(n)}" for n in svcs] or ["(no services)"]
    text = (f"<b>Server</b>\nUptime: {_fmt_dur(time.time() - C.STARTED_AT)}\n"
            f"CPU: {sy['cpu']:.0f}%  •  RAM: {sy['mem_percent']:.0f}%  •  Disk: {sy['disk']['percent']:.0f}%\n"
            f"Docker: {'✅' if caps['docker'] else '❌'}  Git: {'✅' if caps['git'] else '❌'}  Node: {esc(caps['node'] or '❌')}\n\n"
            f"<b>Services</b>\n" + "\n".join(lines))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _status_icon(st):
    return {"running": "🟢", "building": "🟡", "restarting": "🟡", "failed": "🔴", "crashed": "🔴"}.get(st, "⚪")


def _svc_keyboard():
    rows = []
    for n in S.get_state()["services"]:
        rows.append([InlineKeyboardButton(f"{_status_icon(S.status_of(n))} {n}", callback_data=f"svc:menu:{n}")])
    return InlineKeyboardMarkup(rows) if rows else None


def _svc_actions(name):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start", callback_data=f"svc:start:{name}"),
         InlineKeyboardButton("⏹ Stop", callback_data=f"svc:stop:{name}"),
         InlineKeyboardButton("🔄 Restart", callback_data=f"svc:restart:{name}")],
        [InlineKeyboardButton("🚀 Redeploy", callback_data=f"svc:deploy:{name}"),
         InlineKeyboardButton("📜 Logs", callback_data=f"svc:logs:{name}")],
        [InlineKeyboardButton("« Back", callback_data="services")],
    ])


@guard(admin_only=True)
async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = _svc_keyboard()
    await update.message.reply_text("<b>Services</b>" if kb else "No services yet. Create one in the console.",
                                    reply_markup=kb, parse_mode=ParseMode.HTML)


async def _send_logs(message, name, lines=30):
    if not S.get_cfg(name):
        return await message.reply_text("Service not found.")
    txt = S.read_log(S.runtime_log(name), None, 24000)["text"].strip().splitlines()[-lines:]
    body = "\n".join(txt)[-3500:] or "(no logs yet)"
    await message.reply_text(f"<b>{esc(name)}</b> logs\n<pre>{esc(body)}</pre>", parse_mode=ParseMode.HTML)


@guard(admin_only=True)
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /logs <service> [lines]")
    n = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    await _send_logs(update.message, context.args[0].lower(), min(n, 80))


def _svc_cmd(action):
    @guard(admin_only=True)
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text(f"Usage: /{update.message.text.split()[0][1:]} <service>")
        name = context.args[0].lower()
        res = await asyncio.get_running_loop().run_in_executor(None, _do_svc_action, action, name)
        await update.message.reply_text(res)
    return handler


def _do_svc_action(action, name):
    if not S.get_cfg(name):
        return "Service not found."
    try:
        if action == "start":
            S.start(name)
        elif action == "stop":
            S.stop(name)
        elif action == "restart":
            S.restart(name)
        elif action == "deploy":
            dep = S.deploy(name, "telegram")
            return f"🚀 Deploy #{dep} started for {name}. I'll message you when it finishes."
        return f"✅ {action} → {name}: {S.status_of(name)}"
    except Exception as e:
        return f"❌ {e}"


@guard(admin_only=True)
async def cmd_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /env <service>")
    cfg = S.get_cfg(context.args[0].lower())
    if not cfg:
        return await update.message.reply_text("Service not found.")
    keys = sorted((cfg.get("env") or {}).keys())
    await update.message.reply_text("Variables (values hidden):\n" + ("\n".join(f"• {k}" for k in keys) or "(none)"))


@guard(admin_only=True)
async def cmd_setenv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(None, 3)
    if len(parts) < 4:
        return await update.message.reply_text("Usage: /setenv <service> KEY value")
    name, key, val = parts[1].lower(), parts[2], parts[3]
    cfg = S.get_cfg(name)
    if not cfg:
        return await update.message.reply_text("Service not found.")
    import re
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        return await update.message.reply_text("Invalid variable name.")
    env = dict(cfg.get("env") or {})
    env[key] = val
    S.update_cfg(name, env=env)
    try:
        await update.message.delete()  # remove the secret from the chat
    except Exception:
        pass
    await context.bot.send_message(update.effective_chat.id,
                                   f"✅ {key} saved for {name}. Message deleted. Restart the service to apply.",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Restart now", callback_data=f"svc:restart:{name}")]]))


@guard(admin_only=True)
async def cmd_unsetenv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Usage: /unsetenv <service> KEY")
    name, key = context.args[0].lower(), context.args[1]
    cfg = S.get_cfg(name)
    if not cfg:
        return await update.message.reply_text("Service not found.")
    env = dict(cfg.get("env") or {})
    env.pop(key, None)
    S.update_cfg(name, env=env)
    await update.message.reply_text(f"🗑 {key} removed from {name}.")


@guard(admin_only=True)
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /broadcast <message>")
    msg = " ".join(context.args)
    users = C.load_data().get("users", {})
    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"📢 {msg}")
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"Broadcast sent to {sent}/{len(users)} users.")


@guard(admin_only=True)
async def cmd_allusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = C.load_data().get("users", {})
    if not users:
        return await update.message.reply_text("No users yet.")
    lines = [f"{esc(u.get('name', '?'))} (@{esc(u.get('username') or '-')}) <code>{uid}</code>" for uid, u in list(users.items())[:60]]
    await update.message.reply_text(f"<b>Users ({len(users)})</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@guard(admin_only=True)
async def cmd_allsites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = C.load_data()
    sites = data.get("sites", {})
    if not sites:
        return await update.message.reply_text("No sites yet.")
    lines = [f"• {esc(n)} - {data['analytics'].get(n, {}).get('visits', 0)} visits (owner {esc(i.get('owner'))})" for n, i in list(sites.items())[:60]]
    await update.message.reply_text(f"<b>All sites ({len(sites)})</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


def _ban_cmd(ban):
    @guard(admin_only=True)
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text(f"Usage: /{'ban' if ban else 'unban'} <user_id>")
        uid = context.args[0]

        def _m(d):
            b = [str(x) for x in d.setdefault("banned", [])]
            if ban and uid not in b:
                b.append(uid)
            if not ban and uid in b:
                b.remove(uid)
            d["banned"] = b
        C.update_data(_m)
        await update.message.reply_text(f"{'🚫 Banned' if ban else '✅ Unbanned'} {uid}")
    return handler


# ---- callbacks
@guard()
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    await q.answer()
    msg = q.message

    if data == "upload":
        await msg.reply_text("Send your website ZIP file now! 📦")
    elif data == "mysites":
        await _send_mysites(msg, uid)
    elif data == "help":
        await msg.reply_text(HELP_USER + (HELP_ADMIN if is_admin(uid) else ""), parse_mode=ParseMode.HTML)
    elif data == "stats":
        mine = _user_sites(uid)
        an = C.load_data().get("analytics", {})
        await msg.reply_text(f"Sites: {len(mine)}/{C.MAX_SITES_PER_USER}\nVisits: {sum(an.get(n, {}).get('visits', 0) for n in mine)}")
    elif data.startswith("site:"):
        name = data[5:]
        if not _can_manage(uid, name):
            return await msg.reply_text("Site not found.")
        text, kb = _site_card(name)
        await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    elif data.startswith("qr:"):
        name = data[3:]
        if _can_manage(uid, name):
            await _send_qr(msg, name)
    elif data.startswith("delask:"):
        name = data[7:]
        if _can_manage(uid, name):
            await msg.reply_text(f"Delete '{name}' permanently?", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, delete", callback_data=f"delete_confirm_{name}"),
                InlineKeyboardButton("Cancel", callback_data="noop")]]))
    elif data.startswith("delete_confirm_"):
        name = data[len("delete_confirm_"):]
        if not _can_manage(uid, name):
            return await msg.reply_text("Site not found or no permission.")
        _remove_site(name)
        await msg.reply_text(f"🗑 Site '{name}' deleted.")
    elif data.startswith("usename:"):
        name = data[8:]
        await _finish_deploy(update, context, uid, name, msg)
    elif data == "services" and is_admin(uid):
        kb = _svc_keyboard()
        await msg.reply_text("<b>Services</b>" if kb else "No services yet.", reply_markup=kb, parse_mode=ParseMode.HTML)
    elif data.startswith("svc:") and is_admin(uid):
        _, action, name = data.split(":", 2)
        if action == "menu":
            cfg = S.get_cfg(name)
            if not cfg:
                return await msg.reply_text("Service not found.")
            st = S.status_of(name)
            await msg.reply_text(f"{_status_icon(st)} <b>{esc(name)}</b>\n{esc(cfg['kind'])} • {esc(cfg.get('runtime_used') or cfg['runtime'])} • {st}",
                                 reply_markup=_svc_actions(name), parse_mode=ParseMode.HTML)
        elif action == "logs":
            await _send_logs(msg, name)
        else:
            res = await asyncio.get_running_loop().run_in_executor(None, _do_svc_action, action, name)
            await msg.reply_text(res)


# ---- ZIP upload flow
@guard()
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    uid = update.effective_user.id
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        return await update.message.reply_text("Only .zip files are supported. Zip your website folder and send again.")
    mb = doc.file_size / (1024 * 1024)
    if mb > C.MAX_UPLOAD_MB:
        return await update.message.reply_text(f"File too large! Max {C.MAX_UPLOAD_MB}MB, yours is {mb:.1f}MB.")
    if not is_admin(uid) and len(_user_sites(uid)) >= C.MAX_SITES_PER_USER:
        return await update.message.reply_text(f"You reached the limit of {C.MAX_SITES_PER_USER} sites. Delete one first (/mysites).")
    user_state[uid] = {"file_id": doc.file_id, "file_name": doc.file_name, "file_size": doc.file_size}
    caption = C.slugify(update.message.caption or "")
    if caption and C.valid_name(caption):
        return await _finish_deploy(update, context, uid, caption, update.message)
    suggestion = C.slugify(os.path.splitext(doc.file_name)[0])
    kb = None
    if C.valid_name(suggestion):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Use '{suggestion}'", callback_data=f"usename:{suggestion}")]])
    await update.message.reply_text(f"📦 Got <code>{esc(doc.file_name)}</code> ({mb:.1f}MB)\n\nType a name for your site (a-z, 0-9, -):",
                                    reply_markup=kb, parse_mode=ParseMode.HTML)


@guard()
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_state:
        return
    site = C.slugify(update.message.text)
    if not C.valid_name(site):
        return await update.message.reply_text("Invalid name. Use 2-41 letters, numbers and hyphens (not reserved words).")
    await _finish_deploy(update, context, uid, site, update.message)


async def _finish_deploy(update, context, uid, site, message):
    st = user_state.get(uid)
    if not st:
        return await message.reply_text("Send a ZIP file first.")
    sites = C.load_data().get("sites", {})
    if site in sites and str(sites[site].get("owner")) != str(uid) and not is_admin(uid):
        return await message.reply_text(f"'{site}' is already taken. Choose another name.")
    if S.get_cfg(site):
        return await message.reply_text(f"'{site}' is already taken. Choose another name.")
    status = await message.reply_text("⏳ Downloading...")
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=console_mod.TMP_DIR)
    os.close(fd)
    try:
        tgfile = await context.bot.get_file(st["file_id"])
        await tgfile.download_to_drive(tmp)
        await status.edit_text("⚙️ Extracting & deploying...")
        info = await asyncio.get_running_loop().run_in_executor(None, C.deploy_site_zip, tmp, site, uid)
    except Exception as e:
        user_state.pop(uid, None)
        return await status.edit_text(f"❌ Deploy failed: {e}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    user_state.pop(uid, None)
    link = f"{C.DOMAIN}/{site}/"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Open site", url=link), InlineKeyboardButton("QR code", callback_data=f"qr:{site}")],
        [InlineKeyboardButton("My sites", callback_data="mysites"), InlineKeyboardButton("Delete", callback_data=f"delask:{site}")],
    ])
    warn = "" if info["html"] else "\n⚠️ No .html file in the root - make sure index.html exists."
    await status.edit_text(
        f"✅ <b>Site deployed!</b>\n\nName: <code>{esc(site)}</code>\nLink: {esc(link)}\n"
        f"Files: {info['files']} • {C.fmt_bytes(info['size'])}{warn}",
        reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_error(update, context):
    logger.error("Bot error", exc_info=context.error)
    console_mod.BOT_STATE["error"] = str(context.error)[:200]


async def post_init(app):
    me = await app.bot.get_me()
    console_mod.BOT_STATE.update(running=True, username=me.username or "", error="")
    user_cmds = [
        BotCommand("start", "Main menu"), BotCommand("mysites", "Your sites"), BotCommand("info", "Site details"),
        BotCommand("qr", "QR code for a site"), BotCommand("rename", "Rename a site"),
        BotCommand("delete", "Delete a site"), BotCommand("expire", "Auto-delete a site later"),
        BotCommand("stats", "Statistics"), BotCommand("help", "Help"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("panel", "Open web console"), BotCommand("status", "Server health"),
        BotCommand("services", "Manage apps & bots"), BotCommand("logs", "Service logs"),
        BotCommand("deploy", "Redeploy a service"), BotCommand("restart", "Restart a service"),
        BotCommand("svcstart", "Start a service"), BotCommand("svcstop", "Stop a service"),
        BotCommand("setenv", "Set a service variable"), BotCommand("broadcast", "Message all users"),
    ]
    await app.bot.set_my_commands(user_cmds)
    if C.ADMIN_ID:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(C.ADMIN_ID))
        except Exception:
            pass
    logger.info("Bot @%s ready", me.username)


def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(C.TOKEN).post_init(post_init).build()
    H = app.add_handler
    for cmd, fn in [
        ("start", cmd_start), ("help", cmd_help), ("mysites", cmd_mysites), ("info", cmd_info), ("qr", cmd_qr),
        ("delete", cmd_delete), ("rename", cmd_rename), ("expire", cmd_expire), ("stats", cmd_stats),
        ("panel", cmd_panel), ("status", cmd_status), ("services", cmd_services), ("logs", cmd_logs),
        ("deploy", _svc_cmd("deploy")), ("restart", _svc_cmd("restart")), ("svcstart", _svc_cmd("start")),
        ("svcstop", _svc_cmd("stop")), ("env", cmd_env), ("setenv", cmd_setenv), ("unsetenv", cmd_unsetenv),
        ("broadcast", cmd_broadcast), ("allusers", cmd_allusers), ("allsites", cmd_allsites),
        ("ban", _ban_cmd(True)), ("unban", _ban_cmd(False)),
    ]:
        H(CommandHandler(cmd, fn))
    H(CallbackQueryHandler(button_handler))
    H(MessageHandler(filters.Document.ALL, handle_file))
    H(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    logger.info("Starting Telegram bot...")
    app.run_polling(drop_pending_updates=True, stop_signals=None)


# =============================================================== MAIN
def run_web():
    try:
        from waitress import serve as wserve
        wserve(app_web, host="0.0.0.0", port=C.PORT, threads=24, max_request_body_size=max(C.MAX_UPLOAD_MB, 100) * 1024 * 1024)
    except ImportError:
        app_web.run(host="0.0.0.0", port=C.PORT, threaded=True, debug=False)


def main():
    C.start_background_workers()
    S.start_monitor()
    threading.Thread(target=run_web, daemon=True, name="web").start()
    logger.info("Console: %s/  (port %s)", C.DOMAIN, C.PORT)
    if C.GENERATED_PASSWORD:
        logger.warning("ADMIN_PASSWORD / API_KEY not set. Generated login password: %s", C.ADMIN_PASSWORD)
        logger.warning("Set ADMIN_PASSWORD in your environment settings to choose your own.")
    if not C.ADMIN_ID:
        logger.warning("ADMIN_ID not set: Telegram admin commands & notifications are disabled.")
    if C.TOKEN:
        # keep the web server alive even if the bot crashes/restarts
        while True:
            try:
                run_bot()
            except Exception as e:
                logger.exception("Bot stopped")
                console_mod.BOT_STATE.update(running=False, error=str(e)[:200])
            time.sleep(10)
    else:
        logger.warning("BOT_TOKEN not set: running web console only.")
        console_mod.BOT_STATE.update(running=False, error="BOT_TOKEN not set")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    S.stop_all()


if __name__ == "__main__":
    main()
