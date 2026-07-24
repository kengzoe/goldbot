# Fix for Python 3.14
import encodings.idna

import os
import json
import logging
import random
import requests
import threading
import numpy as np
from datetime import datetime, timezone
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")
CHAT_ID, RUN_SIGNALS = None, False
PRICE_INTERVAL_SECONDS = 900
RISK_REWARD_MULTIPLIER = 2.0
MIN_STOP_POINTS = 15
ACTIVE_POSITIONS = []
STATS = {"total_signals": 0, "tp1_hits": 0, "tp2_hits": 0, "sl_hits": 0, "daily_losses": 0}
MAX_DAILY_LOSSES = 6
FREE_CHANNEL_ID = -1004410090098
VIP_CHANNEL_ID = -1004416190238

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

cached_candles = []
last_fetch_time = 0

def fetch_real_candles():
    global cached_candles, last_fetch_time
    now = datetime.now().timestamp()
    if cached_candles and (now - last_fetch_time) < 60:
        return cached_candles
    
    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&outputsize=30&apikey={TWELVE_DATA_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get("status") == "ok" and "values" in data:
            candles = []
            for bar in reversed(data["values"]):
                candles.append({
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "date": bar["datetime"]
                })
            cached_candles = candles
            last_fetch_time = now
            logger.info(f"Fetched {len(candles)} real candles. Price: ${candles[-1]['close']:.2f}")
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
        if rng > 0 and (body / rng) > 0.3:
            return "BUY"
    if c1["low"] > c3["high"] and c3["close"] < c3["open"]:
        body = abs(c3["close"] - c3["open"])
        rng = c3["high"] - c3["low"]
        if rng > 0 and (body / rng) > 0.3:
            return "SELL"
    return None

def detect_order_blocks(candles, lookback=10):
    if len(candles) < lookback + 2:
        return None, None
    bullish_ob, bearish_ob = None, None
    for i in range(len(candles) - lookback, len(candles) - 1):
        if i + 1 >= len(candles):
            continue
        c = candles[i]
        next_c = candles[i + 1]
        if c["close"] < c["open"] and next_c["close"] > next_c["open"] and next_c["close"] > c["high"]:
            bullish_ob = {"high": c["high"], "low": c["low"]}
        if c["close"] > c["open"] and next_c["close"] < next_c["open"] and next_c["close"] < c["low"]:
            bearish_ob = {"high": c["high"], "low": c["low"]}
    return bullish_ob, bearish_ob

def detect_choch(candles):
    if len(candles) < 10:
        return None
    highs = [c["high"] for c in candles[-10:]]
    lows = [c["low"] for c in candles[-10:]]
    swing_highs, swing_lows = [], []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            swing_lows.append(lows[i])
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    current = candles[-1]["close"]
    if len(swing_highs) >= 2 and current > swing_highs[-2]:
        return "BULLISH"
    if len(swing_lows) >= 2 and current < swing_lows[-2]:
        return "BEARISH"
    return None

def price_at_order_block(price, ob):
    if not ob:
        return False
    return ob["low"] <= price <= ob["high"]

def score_signal(fvg, trend_strength, atr, near_swing, body_ratio, choch, at_ob):
    score = 0
    if choch: score += 25
    if fvg: score += 20
    if at_ob: score += 20
    if trend_strength > 0.3: score += 15
    elif trend_strength > 0.1: score += 10
    else: score += 5
    if 10 <= atr <= 25: score += 10
    elif 5 <= atr <= 30: score += 5
    else: score += 3
    if near_swing: score += 5
    if body_ratio > 0.7: score += 5
    elif body_ratio > 0.5: score += 3
    else: score += 1
    if score >= 85: grade = "A+"
    elif score >= 75: grade = "A"
    elif score >= 65: grade = "B+"
    elif score >= 55: grade = "B"
    elif score >= 45: grade = "C+"
    elif score >= 35: grade = "C"
    else: grade = "D"
    return grade, score

def is_london_or_ny_session():
    hour = datetime.now(timezone.utc).hour
    return (8 <= hour < 17) or (13 <= hour < 22)

def process_signals():
    global RISK_REWARD_MULTIPLIER, STATS
    candles = fetch_real_candles()
    if not candles or len(candles) < 5:
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
    downtrend = ema_20 < ema_50 and current_price < ema_50
    
    if not uptrend and not downtrend:
        return None
    
    atr = calculate_atr(candles)
    stop_distance = max(atr * 1.5, MIN_STOP_POINTS)
    fvg = detect_fvg(candles)
    swing_high, swing_low = find_swing_levels(candles)
    
    bullish_ob, bearish_ob = detect_order_blocks(candles)
    choch = detect_choch(candles)
    
    sig, reason, grade, score_val = None, "", "C", 0
    
    if ema_20 > 0:
        trend_strength = abs(ema_20 - ema_50) / ema_50
    else:
        trend_strength = 0
    
    last = candles[-1]
    rng = last["high"] - last["low"]
    body_ratio = abs(last["close"] - last["open"]) / rng if rng > 0 else 0
    near_swing = abs(current_price - swing_high) < atr or abs(current_price - swing_low) < atr if swing_high and swing_low else False
    
    if fvg == "BUY" and uptrend:
        at_ob = price_at_order_block(current_price, bullish_ob)
        grade, score_val = score_signal(True, trend_strength, atr, near_swing, body_ratio, choch == "BULLISH", at_ob)
        reasons = ["FVG"]
        if choch == "BULLISH": reasons.append("CHoCH")
        if at_ob: reasons.append("OB Touch")
        reasons.append("Uptrend")
        reason = " + ".join(reasons) + f" | ATR:{atr:.1f}"
        sig = "BUY"
        sl = current_price - stop_distance
        tp1 = current_price + (stop_distance * RISK_REWARD_MULTIPLIER)
        tp2 = current_price + (stop_distance * RISK_REWARD_MULTIPLIER * 2.0)
        
    elif fvg == "SELL" and downtrend:
        at_ob = price_at_order_block(current_price, bearish_ob)
        grade, score_val = score_signal(True, trend_strength, atr, near_swing, body_ratio, choch == "BEARISH", at_ob)
        reasons = ["FVG"]
        if choch == "BEARISH": reasons.append("CHoCH")
        if at_ob: reasons.append("OB Touch")
        reasons.append("Downtrend")
        reason = " + ".join(reasons) + f" | ATR:{atr:.1f}"
        sig = "SELL"
        sl = current_price + stop_distance
        tp1 = current_price - (stop_distance * RISK_REWARD_MULTIPLIER)
        tp2 = current_price - (stop_distance * RISK_REWARD_MULTIPLIER * 2.0)
    
    if sig:
        STATS["total_signals"] += 1
        return {"type": sig, "reason": reason, "entry": current_price, "sl": sl, "tp1": tp1, "tp2": tp2, "status": "PENDING", "grade": grade, "score": score_val}
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
    if not RUN_SIGNALS or not CHAT_ID:
        return
    
    candles = fetch_real_candles()
    if candles:
        live = candles[-1]["close"]
        if ACTIVE_POSITIONS:
            await monitor_positions(context.bot, live)
        
        sig = process_signals()
        if sig:
            ACTIVE_POSITIONS.append(sig)
            grade = sig.get("grade", "C")
            score = sig.get("score", 0)
            
            vip_msg = (
                f"{'🟢' if sig['type'] == 'BUY' else '🔴'} {grade} {sig['type']} SIGNAL\n"
                f"Score: {score}/100\n"
                f"Entry: ${sig['entry']:.2f}\n"
                f"SL: ${sig['sl']:.2f}\n"
                f"TP1: ${sig['tp1']:.2f}\n"
                f"TP2: ${sig['tp2']:.2f}\n"
                f"Reason: {sig['reason']}\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"🔒 VIP Instant Signal"
            )
            await context.bot.send_message(chat_id=VIP_CHANNEL_ID, text=vip_msg)
            
            free_msg = (
                f"{grade} {sig['type']} SIGNAL\n"
                f"Score: {score}/100\n"
                f"Entry: ${sig['entry']:.2f}\n"
                f"SL: ${sig['sl']:.2f}\n"
                f"TP1: ${sig['tp1']:.2f}\n\n"
                f"⚡ Full details in VIP: /join_vip"
            )
            await context.bot.send_message(chat_id=FREE_CHANNEL_ID, text=free_msg)
            await context.bot.send_message(chat_id=CHAT_ID, text=vip_msg)

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
    await update.message.reply_text("🟢 XAUUSD SMC Bot\n/start_signals /stop_signals /status /report /set_interval /set_risk /join_vip")

async def start_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, CHAT_ID
    CHAT_ID = update.effective_chat.id
    if RUN_SIGNALS: await update.message.reply_text("Already running"); return
    RUN_SIGNALS = True
    context.job_queue.run_repeating(signal_loop, interval=PRICE_INTERVAL_SECONDS, name="smc_job")
    context.job_queue.run_repeating(report_callback, interval=86400, first=86400, name="report_job")
    await update.message.reply_text(f"🚀 Scanning every 15min")

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
    count = len(candles) if candles else 0
    session = is_london_or_ny_session()
    await update.message.reply_text(f"📊 State: {'ACTIVE' if RUN_SIGNALS else 'IDLE'}\nPrice: ${price}\nCandles: {count}/30\nTrades: {len(ACTIVE_POSITIONS)}\nLosses: {STATS['daily_losses']}/{MAX_DAILY_LOSSES}\nSession: {'LIVE' if session else 'CLOSED'}")

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

async def join_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔒 *XAUUSD VIP Signals*\n\n"
        "Get instant signals before free channel!\n\n"
        "💰 *$25/month*\n\n"
        "💎 Pay with USDT (TRC20):\n"
        "`TFEYT12uggMhmhncqFSc8SAFzpdz6YfS2j`\n\n"
        "✅ After payment, send screenshot to @pipzoe",
        parse_mode="Markdown"
    )

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("start_signals", start_signals))
application.add_handler(CommandHandler("stop_signals", stop_signals))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("report", manual_report))
application.add_handler(CommandHandler("set_interval", set_interval))
application.add_handler(CommandHandler("set_risk", set_risk))
application.add_handler(CommandHandler("join_vip", join_vip))

RENDER_URL = os.getenv("RENDER_URL", "https://goldbot-0xwy.onrender.com")

async def init_bot():
    await application.initialize()
    await application.bot.set_webhook(url=f"{RENDER_URL}/webhook")
    logger.info("Webhook set!")

@app.route('/webhook', methods=['POST'])
def webhook():
    import asyncio
    update = Update.de_json(request.get_json(force=True), application.bot)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.process_update(update))
    return "ok"

def run_init():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_bot())

threading.Thread(target=run_init, daemon=True).start()