import os
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import pytz
import ccxt
import pandas as pd
import pandas_ta as ta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========= CONFIG =========
TG_TOKEN = "8093240618:AAG8o5u_tllnuzmL_hxQqGNilGn2bmIMyDo"  # Ton vrai token Telegram
SCAN_TOKEN = "scan_secret_123"  # Token secret pour la route /scan
TZ = pytz.timezone("Europe/Paris")
PUBLIC_BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

USERS_FILE = Path("users.json")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("scanner-bot")

# ========= Binance via ccxt =========
spot = ccxt.binance({"enableRateLimit": True})

# ========= Telegram & FastAPI =========
tg_app = Application.builder().token(TG_TOKEN).build()
app = FastAPI(title="Crypto Scanner Telegram Bot")

# --------- Fonctions utilitaires ----------
def now_paris_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

def load_users():
    if USERS_FILE.exists():
        try:
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            return set(data.get("users", []))
        except Exception:
            return set()
    return set()

def save_users(users):
    USERS_FILE.write_text(json.dumps({"users": list(users)}, indent=2), encoding="utf-8")

def suggest_tp_sl(close, atr, rr=2.0):
    sl = round(close - 1.0 * atr, 6)
    tp = round(close + rr * atr, 6)
    return sl, tp

def score_signal(row):
    score = 0
    if row["EMA50"] > row["EMA200"]:
        score += 40
    if row["MACD"] > row["MACD_signal"]:
        score += 25
    if 50 <= row["RSI"] <= 70:
        score += 20
    if row["close"] > row["EMA50"]:
        score += 15
    return int(score)

def fetch_ohlcv(symbol, limit=300):
    candles = spot.fetch_ohlcv(symbol, timeframe="1h", limit=limit)
    df = pd.DataFrame(candles, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(TZ)
    return df

def compute_indicators(df):
    df = df.copy()
    df["EMA50"] = ta.ema(df["close"], length=50)
    df["EMA200"] = ta.ema(df["close"], length=200)
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["MACD"] = macd["MACD_12_26_9"]
    df["MACD_signal"] = macd["MACDs_12_26_9"]
    df["RSI"] = ta.rsi(df["close"], length=14)
    df["ATR"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    return df

def bullish_signal_row(df):
    last = df.iloc[-1]
    cond = (
        last["EMA50"] > last["EMA200"]
        and last["MACD"] > last["MACD_signal"]
        and 50 <= last["RSI"] <= 70
        and last["close"] > last["EMA50"]
    )
    return cond, last

def load_universe(max_pairs=120):
    markets = spot.load_markets()
    usdt = [s for s in markets if s.endswith("/USDT") and markets[s].get("active")]
    usdt.sort()
    return usdt[:max_pairs]

def scan_once():
    results = []
    for symbol in load_universe():
        try:
            df = fetch_ohlcv(symbol, limit=250)
            if len(df) < 200:
                continue
            df = compute_indicators(df).dropna()
            ok, last = bullish_signal_row(df)
            if not ok:
                continue
            sl, tp = suggest_tp_sl(last["close"], last["ATR"])
            results.append({
                "symbol": symbol,
                "time": df.iloc[-1]["ts"].strftime("%Y-%m-%d %H:%M"),
                "close": float(last["close"]),
                "RSI": float(last["RSI"]),
                "EMA50": float(last["EMA50"]),
                "EMA200": float(last["EMA200"]),
                "MACD": float(last["MACD"]),
                "MACD_signal": float(last["MACD_signal"]),
                "ATR": float(last["ATR"]),
                "SL": sl, "TP": tp,
                "score": score_signal(last)
            })
        except Exception:
            continue
    results.sort(key=lambda x: (x["score"], -abs(60 - x["RSI"])), reverse=True)
    return results

async def send_message_to_all(text):
    users = load_users()
    for uid in users:
        try:
            await tg_app.bot.send_message(chat_id=uid, text=text, disable_web_page_preview=True)
            await asyncio.sleep(0.05)
        except:
            pass

def fmt_row(r):
    return (
        f"• {r['symbol']} — {r['close']:.6f}\n"
        f"  Score:{r['score']} | RSI:{r['RSI']:.1f} | MACD:{r['MACD']:.4f}≥{r['MACD_signal']:.4f}\n"
        f"  SL:{r['SL']} | TP:{r['TP']} | ATR:{r['ATR']:.6f}"
    )

# --------- Commandes Telegram ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users = load_users()
    users.add(user.id)
    save_users(users)
    await update.message.reply_text("👋 Salut ! Envoie /detail BTC/USDT pour un signal précis.")

async def detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation: /detail BTC/USDT")
        return
    symbol = context.args[0].upper()
    try:
        df = compute_indicators(fetch_ohlcv(symbol)).dropna()
        last = df.iloc[-1]
        sl, tp = suggest_tp_sl(last["close"], last["ATR"])
        sc = score_signal(last)
        txt = (
            f"📊 {symbol} ({now_paris_str()})\n"
            f"Prix: {last['close']:.6f}\n"
            f"RSI: {last['RSI']:.2f} | MACD:{last['MACD']:.4f}≥{last['MACD_signal']:.4f}\n"
            f"EMA50:{last['EMA50']:.6f} | EMA200:{last['EMA200']:.6f}\n"
            f"ATR:{last['ATR']:.6f}\n"
            f"Score:{sc}\nSL:{sl} | TP:{tp}"
        )
        await update.message.reply_text(txt)
    except Exception:
        await update.message.reply_text("Données indispo pour ce symbole.")

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("detail", detail))

# --------- Endpoints FastAPI ----------
@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"

@app.post("/scan", response_class=PlainTextResponse)
async def scan(request: Request):
    token = request.headers.get("X-Scan-Token")
    if token != SCAN_TOKEN:
        raise HTTPException(status_code=403)
    results = scan_once()
    if not results:
        await send_message_to_all(f"🕒 {now_paris_str()} — Aucun signal trouvé.")
        return "no"
    txt = f"🕒 {now_paris_str()} — TOP signaux Binance (1h)\n\n"
    txt += "\n".join(fmt_row(r) for r in results[:10])
    txt += "\n\nTape /detail BTC/USDT"
    await send_message_to_all(txt)
    return "ok"

@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    if PUBLIC_BASE_URL:
        url = f"{PUBLIC_BASE_URL}/webhook/{TG_TOKEN}"
        try:
            await tg_app.bot.set_webhook(url)
            logger.info(f"Webhook défini: {url}")
        except Exception as e:
            logger.warning(f"Webhook non défini: {e}")

# Pour lancer: uvicorn app:app --host 0.0.0.0 --port 10000
