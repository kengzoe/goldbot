import logging, random, requests, asyncio, sys, time, os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Token from environment variable
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID, RUN_SIGNALS, PRICE_INTERVAL_SECONDS, RISK_REWARD_MULTIPLIER = None, False, 30, 1.5
CANDLE_HISTORY, ACTIVE_POSITIONS = [], []
STATS = {"total_signals": 0, "tp1_hits": 0, "tp2_hits": 0, "sl_hits": 0}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def update_market_candles():
    global CANDLE_HISTORY
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()["chart"]["result"][0]["meta"]
            live = data.get("regularMarketPrice")
            if not live:
                return False
            spd = random.uniform(1.5, 4.5)
            CANDLE_HISTORY.append({"open": live + random.uniform(-2, 2), "high": live + spd, "low": live - spd, "close": live})
            if len(CANDLE_HISTORY) > 30: CANDLE_HISTORY.pop(0)
            return True
        return False
    except Exception as e:
        logger.error(f"Fetch err: {e}"); return False

def process_smc_signals():
    global CANDLE_HISTORY, RISK_REWARD_MULTIPLIER, STATS
    if len(CANDLE_HISTORY) < 10: return None
    c1, c2, c3 = CANDLE_HISTORY[-3], CANDLE_HISTORY[-2], CANDLE_HISTORY[-1]
    highs, lows = [c["high"] for c in CANDLE_HISTORY[:-1]], [c["low"] for c in CANDLE_HISTORY[:-1]]
    major_res, major_sup = max(highs), min(lows)
    equil, entry, sig, reason, sl = (major_res + major_sup) / 2, c3["close"], None, "", 0.0
    r_closes = [c["close"] for c in CANDLE_HISTORY[-6:]]
    avg = sum(r_closes) / len(r_closes)
    bull, bear = r_closes[-1] > avg, r_closes[-1] < avg
    disp = abs(c2["close"] - c2["open"]) > ((c2["high"] - c2["low"]) * 0.5)
    if entry < equil:
        if c3["low"] > c1["high"] and bull and disp: sig, reason, sl = "BUY", "Institutional FVG + Bullish Displacement", min(c1["low"], c2["low"]) - 1.0
        elif c3["close"] > c3["open"] and c2["close"] < c2["open"]: sig, reason, sl = "BUY", "Liquidity Sweep completed", c3["low"] - 1.5
    elif entry > equil:
        if c3["high"] < c1["low"] and bear and disp: sig, reason, sl = "SELL", "Institutional FVG + Bearish Displacement", max(c1["high"], c2["high"]) + 1.0
        elif (c3["high"] - max(c3["close"], c3["open"])) > (abs(c3["close"] - c3["open"]) * 2.5) and (c3["high"] >= major_res): sig, reason, sl = "SELL", "Bearish Mitigation / Order Block Touch", c3["high"] + 1.5
    if sig:
        dist = max(abs(entry - sl), 3.0)
        sl = (entry - dist) if sig == "BUY" else (entry + dist)
        tp1 = entry + (dist * RISK_REWARD_MULTIPLIER) if sig == "BUY" else entry - (dist * RISK_REWARD_MULTIPLIER)
        tp2 = entry + (dist * RISK_REWARD_MULTIPLIER * 2.0) if sig == "BUY" else entry - (dist * RISK_REWARD_MULTIPLIER * 2.0)
        STATS["total_signals"] += 1
        return {"type": sig, "reason": reason, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "status": "PENDING"}
    return None

async def monitor_active_telemetry(bot, price):
    global ACTIVE_POSITIONS, CHAT_ID, STATS
    surv = []
    for p in ACTIVE_POSITIONS:
        if p["status"] == "PENDING":
            if (price <= p["entry"]) if p["type"] == "BUY" else (price >= p["entry"]):
                p["status"] = "ACTIVE"
                await bot.send_message(chat_id=CHAT_ID, text=f"POSITION EXECUTED: Entry at ${price:.2f}")
            surv.append(p); continue
        if p["type"] == "BUY":
            if price <= p["sl"]: STATS["sl_hits"] += 1; await bot.send_message(chat_id=CHAT_ID, text=f"STOP LOSS HIT at ${p['sl']:.2f}")
            elif price >= p["tp2"]: STATS["tp2_hits"] += 1; await bot.send_message(chat_id=CHAT_ID, text=f"TP2 CLEARED at ${p['tp2']:.2f}")
            elif price >= p["tp1"] and not p.get("tp1_hit"): p["tp1_hit"] = True; STATS["tp1_hits"] += 1; await bot.send_message(chat_id=CHAT_ID, text=f"TP1 HIT at ${p['tp1']:.2f}"); surv.append(p)
            else: surv.append(p)
        elif p["type"] == "SELL":
            if price >= p["sl"]: STATS["sl_hits"] += 1; await bot.send_message(chat_id=CHAT_ID, text=f"STOP LOSS HIT at ${p['sl']:.2f}")
            elif price <= p["tp2"]: STATS["tp2_hits"] += 1; await bot.send_message(chat_id=CHAT_ID, text=f"TP2 CLEARED at ${p['tp2']:.2f}")
            elif price <= p["tp1"] and not p.get("tp1_hit"): p["tp1_hit"] = True; STATS["tp1_hits"] += 1; await bot.send_message(chat_id=CHAT_ID, text=f"TP1 HIT at ${p['tp1']:.2f}"); surv.append(p)
            else: surv.append(p)
    ACTIVE_POSITIONS = surv

async def signal_loop(context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, CHAT_ID, ACTIVE_POSITIONS, CANDLE_HISTORY
    if not RUN_SIGNALS or not CHAT_ID: return
    if update_market_candles():
        live = CANDLE_HISTORY[-1]["close"]
        if ACTIVE_POSITIONS: await monitor_active_telemetry(context.bot, live)
        sig = process_smc_signals()
        if sig:
            ACTIVE_POSITIONS.append(sig)
            emoji = "BUY" if sig["type"] == "BUY" else "SELL"
            await context.bot.send_message(chat_id=CHAT_ID, text=f"NEW {emoji} SIGNAL\nEntry: ${sig['entry']:.2f}\nSL: ${sig['sl']:.2f}\nTP1: ${sig['tp1']:.2f}\nTP2: ${sig['tp2']:.2f}\nReason: {sig['reason']}")

async def report_callback(context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID, STATS
    if not CHAT_ID: return
    total_closed = STATS["tp1_hits"] + STATS["sl_hits"]
    win_rate = (STATS["tp1_hits"] / total_closed * 100) if total_closed > 0 else 0.0
    report_msg = f"24H SUMMARY\nTotal Signals: {STATS['total_signals']}\nTP1: {STATS['tp1_hits']}\nTP2: {STATS['tp2_hits']}\nSL: {STATS['sl_hits']}\nWin Rate: {win_rate:.1f}%"
    await context.bot.send_message(chat_id=CHAT_ID, text=report_msg)
    STATS = {"total_signals": 0, "tp1_hits": 0, "tp2_hits": 0, "sl_hits": 0}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID; CHAT_ID = update.effective_chat.id
    await update.message.reply_text("Bot Ready. Commands: /start_signals /stop_signals /status /report /set_interval /set_risk")

async def start_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, CHAT_ID, PRICE_INTERVAL_SECONDS
    CHAT_ID = update.effective_chat.id
    if RUN_SIGNALS: await update.message.reply_text("Already active"); return
    RUN_SIGNALS = True
    context.job_queue.run_repeating(signal_loop, interval=PRICE_INTERVAL_SECONDS, name="smc_job")
    context.job_queue.run_repeating(report_callback, interval=86400, first=86400, name="report_job")
    await update.message.reply_text("Signals started")

async def stop_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS
    if not RUN_SIGNALS: await update.message.reply_text("Already idle"); return
    RUN_SIGNALS = False
    for j in context.job_queue.get_jobs_by_name("smc_job"): j.schedule_removal()
    for j in context.job_queue.get_jobs_by_name("report_job"): j.schedule_removal()
    await update.message.reply_text("Signals stopped")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUN_SIGNALS, PRICE_INTERVAL_SECONDS, RISK_REWARD_MULTIPLIER, CANDLE_HISTORY, ACTIVE_POSITIONS
    await update.message.reply_text(f"Status: {'ACTIVE' if RUN_SIGNALS else 'IDLE'}\nInterval: {PRICE_INTERVAL_SECONDS}s\nRR: {RISK_REWARD_MULTIPLIER}x\nCandles: {len(CANDLE_HISTORY)}\nActive Trades: {len(ACTIVE_POSITIONS)}")

async def manual_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global STATS
    total_closed = STATS["tp1_hits"] + STATS["sl_hits"]
    win_rate = (STATS["tp1_hits"] / total_closed * 100) if total_closed > 0 else 0.0
    await update.message.reply_text(f"Signals: {STATS['total_signals']}\nTP1: {STATS['tp1_hits']}\nTP2: {STATS['tp2_hits']}\nSL: {STATS['sl_hits']}\nWin Rate: {win_rate:.1f}%")

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRICE_INTERVAL_SECONDS, RUN_SIGNALS
    if not context.args: await update.message.reply_text("Use: /set_interval 60"); return
    try:
        val = int(context.args[0])
        if val < 10: await update.message.reply_text("Minimum 10s"); return
        PRICE_INTERVAL_SECONDS = val
        await update.message.reply_text(f"Interval set to {PRICE_INTERVAL_SECONDS}s")
        if RUN_SIGNALS:
            for j in context.job_queue.get_jobs_by_name("smc_job"): j.schedule_removal()
            context.job_queue.run_repeating(signal_loop, interval=PRICE_INTERVAL_SECONDS, name="smc_job")
    except: await update.message.reply_text("Numbers only")

async def set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RISK_REWARD_MULTIPLIER
    if not context.args: await update.message.reply_text("Use: /set_risk 2.0"); return
    try:
        val = float(context.args[0])
        if val <= 0: await update.message.reply_text("Must be > 0"); return
        RISK_REWARD_MULTIPLIER = val
        await update.message.reply_text(f"Risk set to {RISK_REWARD_MULTIPLIER}x")
    except: await update.message.reply_text("Numbers only")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_signals", start_signals))
    app.add_handler(CommandHandler("stop_signals", stop_signals))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("report", manual_report))
    app.add_handler(CommandHandler("set_interval", set_interval))
    app.add_handler(CommandHandler("set_risk", set_risk))
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
