import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_URL = "https://goldbot-0xwy.onrender.com"

app = Flask(__name__)

# Create bot application
application = Application.builder().token(TELEGRAM_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is alive!")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Working!")

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("status", status_cmd))

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run(application.process_update(update))
    return "ok"

@app.route('/set_webhook')
def set_webhook():
    async def _set():
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.set_webhook(url=f"{RENDER_URL}/webhook")
    asyncio.run(_set())
    return f"Webhook set!"
