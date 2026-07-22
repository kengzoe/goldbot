import os
import logging
import requests
import threading
import numpy as np
from datetime import datetime, timezone
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY")
CHAT_ID, RUN_SIGNALS = None, False
PRICE_INTERVAL_SECONDS = 60
RISK_REWARD_MULTIPLIER = 2.0
MIN_STOP_POINTS = 15
ACTIVE_POSITIONS = []
STATS = {"total_signals": 0, "tp1_hits": 0, "tp2_hits": 0, "sl_hits": 0, "daily_losses": 0}
MAX_DAILY_LOSSES = 3

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

cached_candles = []
last_fetch_time = 0

def fetch_real_candles():
    global cached_candles, last_fetch_time
    now = datetime.now().timestamp()
    if cached_candles and (now - last_fetch_time) < 300:
        return cached_candles
    
    url = f"https://www.alphavantage.co/query?function=FX_DAILY&from_symbol=XAU&to_symbol=USD&apikey={ALPHA_VANTAGE_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        if "Time Series FX (Daily)" in data:
            candles = []
            ts = data["Time Series FX (Daily)"]
            for date_str, values in sorted(ts.items())[-30:]:
                candles.append({
                    "open": float(values["1. open"]),
                    "high": float(values["2. high"]),
                    "low": float(values["3. low"]),
                    "close": float(values["4. close"]),
                    "date": date_str
                })
            cached_candles = candles
            last_fetch_time = now
            return candles
    except Exception as e:
        logger.error(f"API error: {e}")
    return cached_candles

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return MIN_STOP_POINTS
    tr_list = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return np.mean(tr_list[-period:]) if tr_list else MIN_STOP_POINTS

def calculate_ema(closes, period=20):
    if len(closes) < period:
        return np.mean(closes) if closes else 0
    alpha = 2 / (period + 1)
    ema = np.mean(closes[:period])
    for price in closes[period:]:
        ema = alpha * price + (1 - alpha) * ema
    return ema

def find_swing_levels(candles, lookback=10):
    if len(candles) < lookback + 2:
        return None, None
    highs = [c["high"] for c in candles[-lookback:]]
    lows = [c["low"] for c in candles[-lookback:]]
    swing_highs, swing_lows = [], []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            swing_lows.append(lows[i])
    recent_high = max(swing_highs[-3:]) if swing_highs else max(highs)
    recent_low = min(swing_lows[-3:]) if swing_lows else min(lows)
    return recent_high, recent_low

def detect_fvg(candles):
    if len(candles) < 3:
        return None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    if c1["high"] < c3["low"] and c3["close"] > c3["open"]:
        body = abs(c3["close"] - c3["open"])
        rng = c3["high"] - c3["low"]
        if rng > 0 and (body / rng) > 0.6:
            return "BUY"
    if c1["low"] > c3["high"] and c3["close"] < c3["open"]:
        body = abs(c3["close"] - c3["open"])
        rng = c3["high"] - c3["low"]
        if rng > 0 and (body / rng) > 0.6:
            return "SELL"
    return None

def is_london_or_ny_session():
    hour = datetime.now(timezone.utc).hour
    return (8 <= hour < 17) or (13 <= hour < 22)

def process_signals():
    global RISK_REWARD_MULTIPLIER, STATS
    candles = fetch_real_candles()
    if not candles or len(candles) < 20:
        return None
    if not is_london_or_ny_session():
        return None
    if STATS["daily_losses"] >= MAX_DAILY_LOSSES:
        return None
    
    closes = [c["close"] for c in candles]
    current_price = closes[-1]
    ema_20 = calculate_ema(closes, 20)
    ema_50 = calculate_ema(closes, 50)
    uptrend = ema_20 > ema_50 and current_price > ema_20
    downtrend = ema_20 < ema_50 and current_price < ema_20
    
    if not uptrend and not downtrend:
        return None
    
    atr = calculate_atr(candles)
    stop_distance = max(atr * 1.5, MIN_STOP_POINTS)
    fvg = detect_fvg(candles)
    swing_high, swing_low = find_swing_levels(candles)
    
    sig, reason = None, ""
    
    if fvg == "BUY" and uptrend and current_price < swing_high:
        sig = "BUY"
        reason = f"FVG + Uptrend | ATR:{atr:.1f}"
        sl = current_price - stop_distance
        tp1 = current_price + (stop_distance * RISK_REWARD_MULTIPLIER)
        tp2 = current_price + (stop_distance * RISK_REWARD_MULTIPLIER * 2.0)
    elif fvg == "SELL" and downtrend and current_price > swing_low:
        sig = "SELL"
        reason = f"FVG + Downtrend | ATR:{atr:.1f}"
        sl = current_price + stop_distance
        tp1 = current_price - (stop_distance * RISK_REWARD_MULTIPLIER)
        tp2 = current_price - (stop_distance * RISK_REWARD_MULTIPLIER * 2.0)
    
    if sig:
        STATS["total_signals"] += 1
        return {"type": sig, "reason": reason, "entry": current_price, "sl": sl, "tp1": tp1, "tp2": tp2, "status": "PENDING"}
    return None

async def monitor_positions(bot, price):
    global ACTIVE_POSITIONS, CHAT_ID, STATS
    surv = []
    for p in ACTIVE_POSITIONS:
        if p["status"] == "PENDING":
            if (price <= p["entry"]) if p["type"] == "BUY" else (price >= p["entry"]):
                p["status"] = "ACTIVE"
                await bot.send_message(chat_id=CHAT_ID, text=f"✅ {p['type']} EXECUTED at ${price:.2f}")
            surv.append(p); continue
        if p["type"] == "BUY":
            if price <= p["sl"]:
                STATS["sl_hits"] += 1; STATS["daily_losses"] += 1
                await bot.send_message(chat_id=CHAT_ID, text=f"🔴 SL HIT ${p['sl']:.2f}")
            elif price >= p["tp2"]:
                STATS["tp2_hits"] += 1
                await bot.send_message(chat_id=CHAT_ID, text=f"👑 TP2 ${p['tp2']:.2f}")
            elif price >= p["tp1"] and not p.get("tp1_hit"):
                p["tp1_hit"] = True; STATS["tp1_hits"] += 1
                await bot.send_message(chat_id=CHAT_ID, text=f"💰 TP1 ${p['tp1']:.2f}")
                surv.append(p)
            else: surv.append(p)
        elif p["type"] == "SELL":
            if price >= p["sl"]:
                STATS["sl_hits"] += 1; STATS["daily_losses"] += 1
                await bot.send_message(chat_id=CHAT_ID, text=f"🔴 SL HIT ${p['sl']:.2f}")
            elif price <= p["tp2"]:
                STATS["tp2_hits"] += 1
                await bot.send_message(chat_id=CHAT_ID, text=f"👑 TP2 ${p['tp2']:.2f}")
            elif price <= p["tp1"] and not p.get("tp1_hit"):
                p["tp1_hit"] = True; STATS["tp1_hits"] += 1
                await bot.send_message(chat_id=CHAT_ID, text=f"💰 TP1 ${p['tp1']:.2f}")
                surv.append(p)
            else: surv.append(p)
    ACTIVE_POSITIONS = surv

async def signal_loop(context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, CHAT_ID, ACTIVE_POSITIONS
    if not RUN_SIGNALS or not CHAT_ID: return
    candles = fetch_real_candles()
    if candles:
        live = candles[-1]["close"]
        if ACTIVE_POSITIONS: await monitor_positions(context.bot, live)
        sig = process_signals()
        if sig:
            ACTIVE_POSITIONS.append(sig)
            await context.bot.send_message(chat_id=CHAT_ID,
                text=f"📊 NEW {sig['type']} SIGNAL\nEntry: ${sig['entry']:.2f}\nSL: ${sig['sl']:.2f}\nTP1: ${sig['tp1']:.2f}\nTP2: ${sig['tp2']:.2f}\n{sig['reason']}")

async def report_callback(context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID, STATS
    if not CHAT_ID: return
    total = STATS["tp1_hits"] + STATS["tp2_hits"] + STATS["sl_hits"]
    wr = ((STATS["tp1_hits"] + STATS["tp2_hits"]) / total * 100) if total > 0 else 0
    await context.bot.send_message(chat_id=CHAT_ID, text=f"📅 DAILY\nSignals: {STATS['total_signals']}\nTP1: {STATS['tp1_hits']} TP2: {STATS['tp2_hits']}\nSL: {STATS['sl_hits']}\nWin: {wr:.1f}%")
    STATS["total_signals"] = STATS["tp1_hits"] = STATS["tp2_hits"] = STATS["sl_hits"] = STATS["daily_losses"] = 0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text("🟢 XAUUSD SMC Bot\n/start_signals /stop_signals /status /report /set_interval /set_risk")

async def start_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, CHAT_ID, PRICE_INTERVAL_SECONDS
    CHAT_ID = update.effective_chat.id
    if RUN_SIGNALS: await update.message.reply_text("Already running"); return
    RUN_SIGNALS = True
    context.job_queue.run_repeating(signal_loop, interval=PRICE_INTERVAL_SECONDS, name="smc_job")
    context.job_queue.run_repeating(report_callback, interval=86400, first=86400, name="report_job")
    await update.message.reply_text(f"🚀 Scanning every {PRICE_INTERVAL_SECONDS}s")

async def stop_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS
    RUN_SIGNALS = False
    for j in context.job_queue.get_jobs_by_name("smc_job"): j.schedule_removal()
    for j in context.job_queue.get_jobs_by_name("report_job"): j.schedule_removal()
    await update.message.reply_text("⏸️ Stopped")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, ACTIVE_POSITIONS, STATS
    candles = fetch_real_candles()
    price = candles[-1]["close"] if candles else "N/A"
    await update.message.reply_text(f"📊 State: {'ACTIVE' if RUN_SIGNALS else 'IDLE'}\nPrice: ${price}\nTrades: {len(ACTIVE_POSITIONS)}\nLosses: {STATS['daily_losses']}/{MAX_DAILY_LOSSES}")

async def manual_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global STATS
    total = STATS["tp1_hits"] + STATS["tp2_hits"] + STATS["sl_hits"]
    wr = ((STATS["tp1_hits"] + STATS["tp2_hits"]) / total * 100) if total > 0 else 0
    await update.message.reply_text(f"📝 Signals: {STATS['total_signals']}\nTP1: {STATS['tp1_hits']} TP2: {STATS['tp2_hits']}\nSL: {STATS['sl_hits']}\nWin: {wr:.1f}%")

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRICE_INTERVAL_SECONDS, RUN_SIGNALS
    if not context.args: await update.message.reply_text("/set_interval 60"); return
    val = int(context.args[0])
    if val < 30: await update.message.reply_text("Min 30s"); return
    PRICE_INTERVAL_SECONDS = val
    if RUN_SIGNALS:
        for j in context.job_queue.get_jobs_by_name("smc_job"): j.schedule_removal()
        context.job_queue.run_repeating(signal_loop, interval=val, name="smc_job")
    await update.message.reply_text(f"✅ {val}s")

async def set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RISK_REWARD_MULTIPLIER
    if not context.args: await update.message.reply_text("/set_risk 2.0"); return
    val = float(context.args[0])
    if val < 1: await update.message.reply_text("Min 1.0"); return
    RISK_REWARD_MULTIPLIER = val
    await update.message.reply_text(f"✅ RR: {val}x")

# Build application
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("start_signals", start_signals))
application.add_handler(CommandHandler("stop_signals", stop_signals))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("report", manual_report))
application.add_handler(CommandHandler("set_interval", set_interval))
application.add_handler(CommandHandler("set_risk", set_risk))

# Start polling only when app is loaded (gunicorn will import this module)
import atexit
import time

def start_bot():
    time.sleep(2)  # Wait for gunicorn to be ready
    logger.info("Starting bot polling...")
    application.run_polling()

bot_thread = threading.Thread(target=start_bot, daemon=True)
bot_thread.start()

atexit.register(lambda: application.stop())
