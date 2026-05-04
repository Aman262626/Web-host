"""
Web-Host Bot - Telegram Bot + Flask Web Server
Upload ZIP files via Telegram or Web UI to instantly deploy websites.
"""

import os
import json
import time
import shutil
import zipfile
import threading
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Flask, send_from_directory, request, jsonify,
    abort
)
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from werkzeug.utils import secure_filename

# --------------- CONFIG ---------------
TOKEN = os.environ.get("BOT_TOKEN", "")
DOMAIN = os.environ.get("DOMAIN", "http://localhost:8000")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
API_KEY = os.environ.get("API_KEY", "webhost-secret-key")
WEBSITES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "websites")
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
MAX_UPLOAD_MB = 50

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------- DATA STORE ---------------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"sites": {}, "users": {}, "analytics": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# --------------- FLASK APP ---------------
app_web = Flask(__name__)
CORS(app_web)
user_state = {}


@app_web.route("/")
def home():
    data = load_data()
    site_count = len(data.get("sites", {}))
    return jsonify({
        "status": "running",
        "service": "Web-Host",
        "total_sites": site_count,
        "version": "2.0",
    })


# Serve hosted websites
@app_web.route("/<site>/")
@app_web.route("/<site>/<path:path>")
def serve(site, path=""):
    if site == "api":
        abort(404)

    folder = os.path.join(WEBSITES_DIR, site)
    if not os.path.exists(folder):
        return "<h1>404 - Site not found</h1>", 404

    # Track visit
    data = load_data()
    analytics = data.setdefault("analytics", {})
    site_stats = analytics.setdefault(site, {"visits": 0, "last_visit": None})
    site_stats["visits"] += 1
    site_stats["last_visit"] = datetime.now().isoformat()
    save_data(data)

    try:
        if path:
            return send_from_directory(folder, path)

        # Serve first HTML file found
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(".html"):
                return send_from_directory(folder, f)
    except Exception as e:
        return f"<h1>Error: {e}</h1>", 500

    return "<h1>No HTML file found</h1>", 404


# --------------- API ENDPOINTS ---------------
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


@app_web.route("/api/sites", methods=["GET"])
def api_list_sites():
    data = load_data()
    sites = data.get("sites", {})
    analytics = data.get("analytics", {})

    result = []
    for name, info in sites.items():
        folder = os.path.join(WEBSITES_DIR, name)
        file_count = 0
        total_size = 0
        if os.path.exists(folder):
            for root, dirs, files in os.walk(folder):
                file_count += len(files)
                for fl in files:
                    total_size += os.path.getsize(os.path.join(root, fl))

        site_analytics = analytics.get(name, {})
        result.append({
            "name": name,
            "url": f"{DOMAIN}/{name}/",
            "owner": info.get("owner", "unknown"),
            "created": info.get("created", ""),
            "files": file_count,
            "size_bytes": total_size,
            "visits": site_analytics.get("visits", 0),
            "last_visit": site_analytics.get("last_visit"),
        })

    return jsonify({"sites": result, "total": len(result)})


@app_web.route("/api/sites/<site_name>", methods=["DELETE"])
@require_api_key
def api_delete_site(site_name):
    data = load_data()
    sites = data.get("sites", {})

    if site_name not in sites:
        return jsonify({"error": "Site not found"}), 404

    folder = os.path.join(WEBSITES_DIR, site_name)
    if os.path.exists(folder):
        shutil.rmtree(folder)

    del sites[site_name]
    data.get("analytics", {}).pop(site_name, None)
    save_data(data)

    return jsonify({"message": f"Site '{site_name}' deleted"})


@app_web.route("/api/upload", methods=["POST"])
@require_api_key
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    site_name = request.form.get("site_name", "").strip()

    if not site_name:
        return jsonify({"error": "site_name is required"}), 400

    if not file.filename.endswith(".zip"):
        return jsonify({"error": "Only ZIP files allowed"}), 400

    site_name = secure_filename(site_name)
    if not site_name:
        return jsonify({"error": "Invalid site name"}), 400
    base_folder = os.path.join(WEBSITES_DIR, site_name)
    os.makedirs(base_folder, exist_ok=True)

    zip_path = os.path.join(base_folder, "site.zip")
    file.save(zip_path)

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(base_folder)
    except Exception as e:
        shutil.rmtree(base_folder, ignore_errors=True)
        return jsonify({"error": f"ZIP extraction failed: {e}"}), 400

    os.remove(zip_path)
    _flatten_nested_folder(base_folder)

    data = load_data()
    data.setdefault("sites", {})[site_name] = {
        "owner": "web-upload",
        "created": datetime.now().isoformat(),
    }
    save_data(data)

    link = f"{DOMAIN}/{site_name}/"
    return jsonify({"message": "Site deployed!", "url": link, "name": site_name})


@app_web.route("/api/stats", methods=["GET"])
def api_stats():
    data = load_data()
    sites = data.get("sites", {})
    analytics = data.get("analytics", {})
    users = data.get("users", {})

    total_visits = sum(a.get("visits", 0) for a in analytics.values())

    return jsonify({
        "total_sites": len(sites),
        "total_users": len(users),
        "total_visits": total_visits,
        "uptime": "running",
    })


# --------------- HELPERS ---------------
def _flatten_nested_folder(base_folder):
    """Flatten single nested directory inside extracted ZIP."""
    while True:
        items = os.listdir(base_folder)
        if len(items) == 1 and os.path.isdir(os.path.join(base_folder, items[0])):
            inner = os.path.join(base_folder, items[0])
            for f in os.listdir(inner):
                src = os.path.join(inner, f)
                dst = os.path.join(base_folder, f)
                shutil.move(src, dst)
            os.rmdir(inner)
        else:
            break


def _get_site_list(user_id=None):
    """Get formatted list of sites for a user or all sites."""
    data = load_data()
    sites = data.get("sites", {})
    analytics = data.get("analytics", {})

    if not sites:
        return "No sites deployed yet."

    lines = []
    for name, info in sites.items():
        if user_id and str(info.get("owner")) != str(user_id):
            continue
        visits = analytics.get(name, {}).get("visits", 0)
        link = f"{DOMAIN}/{name}/"
        lines.append(f"  {name}\n     {link}\n     Visits: {visits}")

    if not lines:
        return "You have no deployed sites."

    return "\n\n".join(lines)


# --------------- FLASK SERVER ---------------
def run_web():
    app_web.run(host="0.0.0.0", port=8000, threaded=True, debug=False)


# --------------- TELEGRAM BOT ---------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("Upload Site", callback_data="upload"),
            InlineKeyboardButton("My Sites", callback_data="mysites"),
        ],
        [
            InlineKeyboardButton("Help", callback_data="help"),
            InlineKeyboardButton("Stats", callback_data="stats"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "**Web-Host Bot**\n\n"
        "Host your website in seconds!\n\n"
        " Send a ZIP file to deploy\n"
        " Manage your sites easily\n"
        " Get instant live links\n\n"
        "Choose an option below or send a ZIP file:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "**Commands:**\n\n"
        "/start - Main menu\n"
        "/help - Show this help\n"
        "/mysites - List your sites\n"
        "/delete `<name>` - Delete a site\n"
        "/rename `<old>` `<new>` - Rename a site\n"
        "/stats - View statistics\n\n"
        "**How to deploy:**\n"
        "1. Send a ZIP file containing your website\n"
        "2. Enter a name for your site\n"
        "3. Get your live link!\n\n"
        "**Supported:** HTML, CSS, JS, images, fonts"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_mysites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    site_list = _get_site_list(user_id)
    await update.message.reply_text(
        f"**Your Sites:**\n\n{site_list}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text("Usage: /delete `<site_name>`", parse_mode="Markdown")
        return

    site_name = context.args[0]
    data = load_data()
    sites = data.get("sites", {})

    if site_name not in sites:
        await update.message.reply_text(f"Site '{site_name}' not found.")
        return

    # Only owner or admin can delete
    owner = str(sites[site_name].get("owner", ""))
    if str(user_id) != owner and user_id != ADMIN_ID:
        await update.message.reply_text("You don't have permission to delete this site.")
        return

    folder = os.path.join(WEBSITES_DIR, site_name)
    if os.path.exists(folder):
        shutil.rmtree(folder)

    del sites[site_name]
    data.get("analytics", {}).pop(site_name, None)
    save_data(data)

    await update.message.reply_text(f"Site '{site_name}' has been deleted.")


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /rename `<old_name>` `<new_name>`", parse_mode="Markdown"
        )
        return

    old_name, new_name = context.args[0], secure_filename(context.args[1])
    data = load_data()
    sites = data.get("sites", {})

    if old_name not in sites:
        await update.message.reply_text(f"Site '{old_name}' not found.")
        return

    owner = str(sites[old_name].get("owner", ""))
    if str(user_id) != owner and user_id != ADMIN_ID:
        await update.message.reply_text("You don't have permission to rename this site.")
        return

    if new_name in sites:
        await update.message.reply_text(f"Site '{new_name}' already exists.")
        return

    old_folder = os.path.join(WEBSITES_DIR, old_name)
    new_folder = os.path.join(WEBSITES_DIR, new_name)

    if os.path.exists(old_folder):
        shutil.move(old_folder, new_folder)

    sites[new_name] = sites.pop(old_name)
    analytics = data.get("analytics", {})
    if old_name in analytics:
        analytics[new_name] = analytics.pop(old_name)
    save_data(data)

    link = f"{DOMAIN}/{new_name}/"
    await update.message.reply_text(
        f"Site renamed!\n\n"
        f"Old: {old_name}\n"
        f"New: {new_name}\n"
        f"Link: {link}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    sites = data.get("sites", {})
    analytics = data.get("analytics", {})
    users = data.get("users", {})
    total_visits = sum(a.get("visits", 0) for a in analytics.values())

    user_id = update.message.from_user.id
    if user_id == ADMIN_ID:
        text = (
            f"**Admin Stats**\n\n"
            f"Total Sites: {len(sites)}\n"
            f"Total Users: {len(users)}\n"
            f"Total Visits: {total_visits}\n\n"
            f"**Top Sites:**\n"
        )
        sorted_sites = sorted(
            analytics.items(), key=lambda x: x[1].get("visits", 0), reverse=True
        )[:5]
        for name, stats in sorted_sites:
            text += f"  {name}: {stats.get('visits', 0)} visits\n"
    else:
        user_sites = {k: v for k, v in sites.items() if str(v.get("owner")) == str(user_id)}
        user_visits = sum(analytics.get(name, {}).get("visits", 0) for name in user_sites)
        text = (
            f"**Your Stats**\n\n"
            f"Your Sites: {len(user_sites)}\n"
            f"Total Visits: {user_visits}\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")


# Admin-only commands
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Admin only command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    message = " ".join(context.args)
    data = load_data()
    users = data.get("users", {})
    sent = 0

    for uid in users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"**Announcement:**\n{message}", parse_mode="Markdown")
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"Broadcast sent to {sent}/{len(users)} users.")


async def cmd_allusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Admin only command.")
        return

    data = load_data()
    users = data.get("users", {})

    if not users:
        await update.message.reply_text("No users registered yet.")
        return

    text = "**All Users:**\n\n"
    for uid, info in users.items():
        name = info.get("name", "Unknown")
        username = info.get("username", "N/A")
        text += f"  {name} (@{username}) - ID: {uid}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_allsites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Admin only command.")
        return

    site_list = _get_site_list()
    await update.message.reply_text(
        f"**All Sites:**\n\n{site_list}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# Callback query handler for inline buttons
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "upload":
        await query.message.reply_text("Send your website ZIP file now!")
    elif query.data == "mysites":
        user_id = query.from_user.id
        site_list = _get_site_list(user_id)
        await query.message.reply_text(
            f"**Your Sites:**\n\n{site_list}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    elif query.data == "help":
        await query.message.reply_text(
            "**Commands:**\n"
            "/start - Main menu\n"
            "/mysites - Your sites\n"
            "/delete `<name>` - Delete site\n"
            "/rename `<old>` `<new>` - Rename\n"
            "/stats - Statistics\n\n"
            "Just send a ZIP file to deploy!",
            parse_mode="Markdown",
        )
    elif query.data == "stats":
        data = load_data()
        sites = data.get("sites", {})
        analytics = data.get("analytics", {})
        user_id = query.from_user.id
        user_sites = {k: v for k, v in sites.items() if str(v.get("owner")) == str(user_id)}
        user_visits = sum(analytics.get(name, {}).get("visits", 0) for name in user_sites)
        await query.message.reply_text(
            f"**Your Stats**\n\n"
            f"Sites: {len(user_sites)}\n"
            f"Visits: {user_visits}",
            parse_mode="Markdown",
        )
    elif query.data.startswith("delete_confirm_"):
        site_name = query.data.replace("delete_confirm_", "")
        user_id = query.from_user.id
        data = load_data()
        sites = data.get("sites", {})
        if site_name in sites:
            owner = str(sites[site_name].get("owner", ""))
            if str(user_id) != owner and user_id != ADMIN_ID:
                await query.message.reply_text("You don't have permission to delete this site.")
                return
            folder = os.path.join(WEBSITES_DIR, site_name)
            if os.path.exists(folder):
                shutil.rmtree(folder)
            del sites[site_name]
            data.get("analytics", {}).pop(site_name, None)
            save_data(data)
            await query.message.reply_text(f"Site '{site_name}' deleted.")
        else:
            await query.message.reply_text("Site not found.")


# Handle ZIP file uploads
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    user_id = update.message.from_user.id

    # Track user
    data = load_data()
    users = data.setdefault("users", {})
    user = update.message.from_user
    users[str(user_id)] = {
        "name": user.full_name,
        "username": user.username or "",
        "last_active": datetime.now().isoformat(),
    }
    save_data(data)

    if not doc.file_name.endswith(".zip"):
        await update.message.reply_text(
            "Only `.zip` files are supported.\n"
            "Please zip your website folder and send again.",
            parse_mode="Markdown",
        )
        return

    file_size_mb = doc.file_size / (1024 * 1024)
    if file_size_mb > MAX_UPLOAD_MB:
        await update.message.reply_text(
            f"File too large! Max size: {MAX_UPLOAD_MB}MB\n"
            f"Your file: {file_size_mb:.1f}MB"
        )
        return

    user_state[user_id] = {
        "file_id": doc.file_id,
        "file_name": doc.file_name,
        "file_size": doc.file_size,
    }

    await update.message.reply_text(
        f"ZIP received: `{doc.file_name}` ({file_size_mb:.1f}MB)\n\n"
        "Now enter a name for your website:",
        parse_mode="Markdown",
    )


# Handle site name input
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in user_state:
        return

    site = secure_filename(update.message.text.strip())
    if not site:
        await update.message.reply_text("Invalid name. Use letters, numbers, and hyphens only.")
        return

    data = load_data()
    sites = data.setdefault("sites", {})

    if site in sites and str(sites[site].get("owner")) != str(user_id) and user_id != ADMIN_ID:
        await update.message.reply_text(
            f"Site name '{site}' is already taken. Choose another name."
        )
        return

    await update.message.reply_text("Deploying your site...")

    base_folder = os.path.join(WEBSITES_DIR, site)
    if os.path.exists(base_folder):
        shutil.rmtree(base_folder)
    os.makedirs(base_folder, exist_ok=True)

    # Download ZIP
    try:
        file = await context.bot.get_file(user_state[user_id]["file_id"])
        zip_path = os.path.join(base_folder, "site.zip")
        await file.download_to_drive(zip_path)
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        del user_state[user_id]
        return

    # Extract ZIP
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(base_folder)
    except Exception as e:
        await update.message.reply_text(f"ZIP extraction failed: {e}")
        shutil.rmtree(base_folder, ignore_errors=True)
        del user_state[user_id]
        return

    os.remove(zip_path)
    _flatten_nested_folder(base_folder)

    # Count files
    file_count = sum(len(files) for _, _, files in os.walk(base_folder))
    html_files = [f for f in os.listdir(base_folder) if f.lower().endswith(".html")]

    # Save site info
    sites[site] = {
        "owner": str(user_id),
        "created": datetime.now().isoformat(),
        "files": file_count,
    }
    save_data(data)

    link = f"{DOMAIN}/{site}/"

    keyboard = [
        [InlineKeyboardButton("Open Site", url=link)],
        [
            InlineKeyboardButton("My Sites", callback_data="mysites"),
            InlineKeyboardButton("Delete", callback_data=f"delete_confirm_{site}"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"**Site Deployed!**\n\n"
        f"Name: `{site}`\n"
        f"Link: {link}\n"
        f"Files: {file_count}\n"
        f"HTML Pages: {', '.join(html_files) if html_files else 'None found'}\n\n"
        f"Your site is now live!",
        reply_markup=reply_markup,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    del user_state[user_id]


# --------------- RUN BOT ---------------
def run_bot():
    if not TOKEN:
        logger.error("BOT_TOKEN not set! Set the BOT_TOKEN environment variable.")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mysites", cmd_mysites))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("allusers", cmd_allusers))
    app.add_handler(CommandHandler("allsites", cmd_allsites))

    # Callback queries (inline buttons)
    app.add_handler(CallbackQueryHandler(button_handler))

    # File and text handlers
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


# --------------- MAIN ---------------
if __name__ == "__main__":
    os.makedirs(WEBSITES_DIR, exist_ok=True)
    threading.Thread(target=run_web, daemon=True).start()
    logger.info(f"Web server started on port 8000")
    logger.info(f"Domain: {DOMAIN}")
    run_bot()
