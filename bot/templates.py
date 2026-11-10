"""One-click starter templates for new services."""
import os

TEMPLATES = {
    "telegram-bot": {
        "name": "Telegram bot", "desc": "python-telegram-bot, long polling. Needs BOT_TOKEN.",
        "kind": "worker", "start_cmd": "python bot.py", "env_hint": ["BOT_TOKEN"],
        "files": {
            "bot.py": '''import asyncio
import logging
import os

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I am running on Web-Host.")


async def echo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(update.message.text)


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
''',
            "requirements.txt": "python-telegram-bot==21.6\n",
        },
    },
    "flask-api": {
        "name": "Flask API", "desc": "Minimal Flask web app with a /health route.",
        "kind": "web", "start_cmd": "python app.py", "env_hint": [], "health_path": "/health",
        "files": {
            "app.py": '''import os

from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/")
def index():
    return jsonify(ok=True, message="Hello from Web-Host", port=os.environ.get("PORT"))


@app.get("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
''',
            "requirements.txt": "flask>=3.0\n",
        },
    },
    "fastapi": {
        "name": "FastAPI", "desc": "FastAPI + uvicorn with automatic /docs.",
        "kind": "web", "start_cmd": "uvicorn main:app --host 0.0.0.0 --port $PORT", "env_hint": [],
        "health_path": "/health",
        "files": {
            "main.py": '''from fastapi import FastAPI

app = FastAPI(title="Web-Host API")


@app.get("/")
def index():
    return {"ok": True, "message": "Hello from Web-Host"}


@app.get("/health")
def health():
    return {"status": "ok"}
''',
            "requirements.txt": "fastapi>=0.110\nuvicorn>=0.29\n",
        },
    },
    "node-express": {
        "name": "Node + Express", "desc": "Express server listening on $PORT.",
        "kind": "web", "start_cmd": "npm start", "env_hint": [], "health_path": "/health",
        "files": {
            "package.json": '''{
  "name": "webhost-express",
  "version": "1.0.0",
  "main": "server.js",
  "scripts": { "start": "node server.js" },
  "dependencies": { "express": "^4.19.2" }
}
''',
            "server.js": '''const express = require("express");
const app = express();
app.get("/", (req, res) => res.json({ ok: true, message: "Hello from Web-Host" }));
app.get("/health", (req, res) => res.send("ok"));
app.listen(process.env.PORT || 3000, "0.0.0.0", () => console.log("listening on", process.env.PORT || 3000));
''',
        },
    },
    "cron-job": {
        "name": "Cron job", "desc": "A script that runs on a schedule (every 5 minutes).",
        "kind": "cron", "schedule": "*/5 * * * *", "start_cmd": "python job.py", "env_hint": [],
        "files": {
            "job.py": '''import datetime

print("cron job ran at", datetime.datetime.now().isoformat())
''',
        },
    },
}


def public_list():
    return [{"id": k, "name": v["name"], "desc": v["desc"], "kind": v["kind"], "env_hint": v.get("env_hint", []),
             "schedule": v.get("schedule", ""), "start_cmd": v.get("start_cmd", ""),
             "health_path": v.get("health_path", "")} for k, v in TEMPLATES.items()]


def apply(spec):
    """Fill defaults from the template into spec and return the files to write (or None)."""
    t = TEMPLATES.get(spec.get("template") or "")
    if not t:
        return None
    spec["kind"] = t["kind"]
    spec["source"] = "zip"
    spec.setdefault("start_cmd", "")
    spec["start_cmd"] = spec["start_cmd"] or t.get("start_cmd", "")
    if t.get("schedule") and not spec.get("schedule"):
        spec["schedule"] = t["schedule"]
    return t["files"]


def write_files(files, dest):
    os.makedirs(dest, exist_ok=True)
    for rel, content in files.items():
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
