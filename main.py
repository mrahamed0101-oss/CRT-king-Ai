import os
import time
import logging
from datetime import datetime, timezone

import ccxt
import pandas as pd
import requests

# ============================================================
# CRT KING AI - OKX USDT perpetual market scanner
# READ-ONLY: this bot never places, edits, or closes trades.
#
# Railway Variables required:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
# ============================================================

TIMEFRAME = "15m"
TOP_COINS = 20
SCAN_INTERVAL_SECONDS = 60
SYMBOL_REFRESH_SCANS = 30
CANDLE_LIMIT = 150
MIN_SIGNAL_SCORE = 3
ALERT_COOLDOWN_SECONDS = 60 * 60  # avoid repeating same-direction alert per coin
ATR_SL_MULTIPLIER = 1.5
POLL_TIMEOUT_SECONDS = 20

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("crt-king-ai")

session = requests.Session()
last_alert = {}  # (symbol, signal) -> unix timestamp
telegram_offset = None


def telegram_api(method, payload=None, timeout=25):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    response = session.post(url, json=payload or {}, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        data = {"ok": False, "description": response.text}
    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram {method} failed: HTTP {response.status_code}: "
            f"{data.get('description', response.text)}"
        )
    return data["result"]


def send_telegram(text, chat_id=None):
    target = str(chat_id or CHAT_ID).strip()
    if not target:
        log.error("Telegram chat ID is missing")
        return False
    try:
        telegram_api(
            "sendMessage",
            {
                "chat_id": target,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        log.info("Telegram message sent to chat %s", target)
        return True
    except Exception as exc:
        log.error("Telegram send error: %s", exc)
        return False


def create_exchange():
    # Public OKX market data only. No API keys / trading permissions.
    return ccxt.okx({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "swap"},
    })


def get_top_symbols(exchange):
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    ranked = []

    for symbol, market in markets.items():
        try:
            if not market.get("active") or market.get("swap") is not True:
                continue
            if market.get("quote") != "USDT" or market.get("settle") != "USDT":
                continue
            if not market.get("linear"):
                continue

            ticker = tickers.get(symbol) or {}
            qv = ticker.get("quoteVolume")
            if qv is None and ticker.get("baseVolume") and ticker.get("last"):
                qv = float(ticker["baseVolume"]) * float(ticker["last"])
            if qv and float(qv) > 0:
                ranked.append((float(qv), symbol))
        except (TypeError, ValueError):
            continue

    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[:TOP_COINS]]


def calculate_atr(df, period=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def analyse(exchange, symbol):
    raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
    if len(raw) < 60:
        raise ValueError("Insufficient OHLCV candles")

    df = pd.DataFrame(
        raw, columns=["time", "open", "high", "low", "close", "volume"]
    )
    # Exclude the still-forming candle so signals use closed candles only.
    df = df.iloc[:-1].copy()
    if len(df) < 50:
        raise ValueError("Insufficient completed candles")

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    atr = calculate_atr(df)
    if pd.isna(atr) or atr <= 0:
        raise ValueError("ATR unavailable")

    last = df.iloc[-1]
    previous = df.iloc[-2]
    prior20 = df.iloc[-21:-1]
    prior10 = df.iloc[-11:-1]

    price = float(last["close"])
    support = float(prior20["low"].min())
    resistance = float(prior20["high"].max())
    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []

    # EMA trend/momentum
    if last["ema9"] > last["ema20"] and price > float(last["ema9"]):
        long_score += 1
        long_reasons.append("EMA9 above EMA20; price above EMA9")
    if last["ema9"] < last["ema20"] and price < float(last["ema9"]):
        short_score += 1
        short_reasons.append("EMA9 below EMA20; price below EMA9")

    # Liquidity sweep approximation: wick through recent range, close back inside.
    if float(last["low"]) < support and price > support:
        long_score += 1
        long_reasons.append("Bullish liquidity sweep / reclaim")
    if float(last["high"]) > resistance and price < resistance:
        short_score += 1
        short_reasons.append("Bearish liquidity sweep / reclaim")

    # BOS approximation against the previous 10 closed candles.
    prev_high = float(prior10["high"].max())
    prev_low = float(prior10["low"].min())
    if price > prev_high:
        long_score += 1
        long_reasons.append("Bullish structure break (BOS approximation)")
    if price < prev_low:
        short_score += 1
        short_reasons.append("Bearish structure break (BOS approximation)")

    # Three-candle FVG approximation.
    candle_a = df.iloc[-3]
    if float(last["low"]) > float(candle_a["high"]):
        long_score += 1
        long_reasons.append("Bullish FVG approximation")
    if float(last["high"]) < float(candle_a["low"]):
        short_score += 1
        short_reasons.append("Bearish FVG approximation")

    # Simple range reclaim / CRT-style context; not a full discretionary CRT model.
    if float(previous["low"]) < support and price > float(previous["low"]):
        long_score += 1
        long_reasons.append("Possible bullish range reclaim")
    if float(previous["high"]) > resistance and price < float(previous["high"]):
        short_score += 1
        short_reasons.append("Possible bearish range reclaim")

    signal = "WAIT"
    if long_score >= MIN_SIGNAL_SCORE and long_score > short_score:
        signal = "LONG"
    elif short_score >= MIN_SIGNAL_SCORE and short_score > long_score:
        signal = "SHORT"

    # SL/TP estimates only for qualified signals.
    # SL combines nearby structure and ATR buffer; targets are 1R/2R/3R.
    sl = tp1 = tp2 = tp3 = None
    rr = "N/A"
    if signal == "LONG":
        structural_sl = support - 0.20 * atr
        volatility_sl = price - ATR_SL_MULTIPLIER * atr
        sl = min(structural_sl, volatility_sl)
        risk = price - sl
        if risk > 0:
            tp1, tp2, tp3 = price + risk, price + 2 * risk, price + 3 * risk
            rr = "1:1 / 1:2 / 1:3"
    elif signal == "SHORT":
        structural_sl = resistance + 0.20 * atr
        volatility_sl = price + ATR_SL_MULTIPLIER * atr
        sl = max(structural_sl, volatility_sl)
        risk = sl - price
        if risk > 0:
            tp1, tp2, tp3 = price - risk, price - 2 * risk, price - 3 * risk
            rr = "1:1 / 1:2 / 1:3"

    candle_time = datetime.fromtimestamp(
        int(last["time"]) / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "symbol": symbol, "price": price, "signal": signal,
        "long_score": long_score, "short_score": short_score,
        "long_reasons": long_reasons, "short_reasons": short_reasons,
        "support": support, "resistance": resistance, "atr": atr,
        "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr,
        "candle_time": candle_time,
    }


def fmt(value):
    return "N/A" if value is None else f"{value:.8g}"


def signal_message(r):
    reasons = r["long_reasons"] if r["signal"] == "LONG" else r["short_reasons"]
    reason_lines = "\n".join(f"â¢ {item}" for item in reasons) or "â¢ No details"
    return (
        "ð CRT KING AI ALERT\n\n"
        f"Coin: {r['symbol']}\n"
        f"Signal: {'ð¢ LONG' if r['signal'] == 'LONG' else 'ð´ SHORT'}\n"
        f"Entry reference: {fmt(r['price'])}\n"
        f"Stop Loss: {fmt(r['sl'])}\n"
        f"TP1: {fmt(r['tp1'])}\n"
        f"TP2: {fmt(r['tp2'])}\n"
        f"TP3: {fmt(r['tp3'])}\n"
        f"Risk/Reward plan: {r['rr']}\n\n"
        f"LONG Score: {r['long_score']}\n"
        f"SHORT Score: {r['short_score']}\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"Support: {fmt(r['support'])}\n"
        f"Resistance: {fmt(r['resistance'])}\n"
        f"ATR(14): {fmt(r['atr'])}\n"
        f"Candle close: {r['candle_time']}\n\n"
        f"Analysis:\n{reason_lines}\n\n"
        "â ï¸ Levels are estimates, not guarantees. Confirm current price, "
        "fees, slippage and position size yourself. READ-ONLY: no trade placed."
    )


def status_text():
    return (
        "ð CRT KING AI STATUS\n"
        "Scanner: configured for OKX USDT perpetuals\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"Watchlist: Top {TOP_COINS} by reported quote volume\n"
        f"Signal threshold: {MIN_SIGNAL_SCORE}\n"
        "Mode: READ-ONLY; no orders are placed."
    )


def handle_telegram_commands():
    """Poll Telegram commands so /start, /help and /status get replies."""
    global telegram_offset
    try:
        updates = telegram_api(
            "getUpdates",
            {
                "offset": telegram_offset,
                "timeout": POLL_TIMEOUT_SECONDS,
                "allowed_updates": ["message"],
            },
            timeout=POLL_TIMEOUT_SECONDS + 10,
        )
        for update in updates:
            telegram_offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if not text.startswith("/"):
                continue

            command = text.split()[0].split("@")[0].lower()
            if command == "/start":
                reply = (
                    "ð Welcome to CRT KING AI!\n\n"
                    "I scan OKX USDT perpetual markets and send LONG/SHORT alerts "
                    "with estimated Entry, Stop Loss and TP1/TP2/TP3.\n\n"
                    "Commands:\n/start - Welcome\n/help - Help\n/status - Scanner status\n\n"
                    "READ-ONLY: I do not place trades."
                )
            elif command == "/help":
                reply = (
                    "ð CRT KING AI HELP\n\n"
                    "/start - Welcome message\n"
                    "/help - This help\n"
                    "/status - Scanner configuration\n\n"
                    "Signals use closed 15m candles and a score threshold. "
                    "WAIT conditions are logged, not sent as Telegram alerts. "
                    "SL/TP are estimates only; always verify before trading."
                )
            elif command == "/status":
                reply = status_text()
            else:
                reply = "Unknown command. Use /help."
            send_telegram(reply, chat_id=chat_id)

    except Exception as exc:
        log.error("Telegram command polling error: %s", exc)


def main():
    log.info("=" * 55)
    log.info("CRT KING AI - OKX TOP %s USDT PERPETUAL SCANNER", TOP_COINS)
    log.info("READ-ONLY | timeframe=%s | interval=%ss", TIMEFRAME, SCAN_INTERVAL_SECONDS)

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway Variables")

    exchange = create_exchange()
    symbols = []
    scan_count = 0

    send_telegram(
        "ð CRT KING AI is online.\n"
        f"OKX Top {TOP_COINS} USDT perpetual scanner started.\n"
        f"Timeframe: {TIMEFRAME}\n"
        "Mode: READ-ONLY (no trades)."
    )

    while True:
        scan_started = time.time()
        scan_count += 1

        # Handle Telegram commands every cycle, including between coin scans.
        handle_telegram_commands()

        try:
            if not symbols or (scan_count - 1) % SYMBOL_REFRESH_SCANS == 0:
                log.info("Refreshing OKX USDT perpetual markets...")
                symbols = get_top_symbols(exchange)
                if not symbols:
                    raise RuntimeError("No eligible OKX USDT perpetual markets found")
                log.info("Loaded %s symbols: %s", len(symbols), ", ".join(symbols))

            log.info("SCAN #%s started", scan_count)
            for symbol in symbols:
                try:
                    result = analyse(exchange, symbol)
                    log.info(
                        "%s | %s | LONG %s SHORT %s | price=%s | support=%s resistance=%s",
                        symbol, result["signal"], result["long_score"],
                        result["short_score"], fmt(result["price"]),
                        fmt(result["support"]), fmt(result["resistance"]),
                    )

                    if result["signal"] in ("LONG", "SHORT"):
                        key = (symbol, result["signal"])
                        now = time.time()
                        if now - last_alert.get(key, 0) >= ALERT_COOLDOWN_SECONDS:
                            if send_telegram(signal_message(result)):
                                last_alert[key] = now
                        else:
                            log.info("%s %s alert suppressed by cooldown", symbol, result["signal"])

                except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                    log.warning("%s exchange/network error: %s", symbol, exc)
                except Exception:
                    log.exception("%s analysis failed", symbol)

                # Poll commands during longer scans as well.
                handle_telegram_commands()

            log.info("Scan complete.")
        except Exception:
            log.exception("Scanner cycle failed")

        elapsed = time.time() - scan_started
        time.sleep(max(5, SCAN_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()


