import os
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd
import requests


# ============================================================
# CRT KING AI - Binance USDT-M Futures market scanner
# READ-ONLY: This bot analyses prices and sends Telegram alerts.
# It DOES NOT place, modify, or close trades.
#
# Railway Variables required:
#   TELEGRAM_BOT_TOKEN = your new Telegram bot token
#   TELEGRAM_CHAT_ID   = your Telegram chat ID
# ============================================================

TIMEFRAME = "15m"
TOP_COINS = 20
SCAN_INTERVAL_SECONDS = 60
REFRESH_SYMBOLS_EVERY_SCANS = 30
CANDLE_LIMIT = 120
MIN_SIGNAL_SCORE = 3

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def send_telegram(message):
    """Send a Telegram message. Never print the bot token."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram variables missing. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if response.ok:
            print("Telegram message sent.")
            return True

        print(f"Telegram error: HTTP {response.status_code} - {response.text[:300]}")
        return False
    except requests.RequestException as err:
        print(f"Telegram network error: {err}")
        return False


def create_exchange():
    exchange = ccxt.binanceusdm({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "future"},
    })
    return exchange


def get_top_usdt_perpetuals(exchange):
    """Return the top active USDT-settled perpetual contracts by quote volume."""
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    ranked = []

    for symbol, market in markets.items():
        try:
            if not market.get("active"):
                continue
            if market.get("swap") is not True:
                continue
            if market.get("quote") != "USDT":
                continue
            if market.get("settle") != "USDT":
                continue
            if not market.get("linear"):
                continue

            ticker = tickers.get(symbol) or {}
            quote_volume = ticker.get("quoteVolume")
            if quote_volume is None:
                base_volume = ticker.get("baseVolume") or 0
                last_price = ticker.get("last") or 0
                quote_volume = base_volume * last_price

            if quote_volume and quote_volume > 0:
                ranked.append((float(quote_volume), symbol))
        except (TypeError, ValueError):
            continue

    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[:TOP_COINS]]


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def analyse_symbol(exchange, symbol):
    candles = exchange.fetch_ohlcv(
        symbol,
        timeframe=TIMEFRAME,
        limit=CANDLE_LIMIT,
    )

    if len(candles) < 60:
        raise ValueError("Not enough candle data")

    df = pd.DataFrame(
        candles,
        columns=["time", "open", "high", "low", "close", "volume"],
    )

    # Use completed candles only; the last candle may still be forming.
    df = df.iloc[:-1].copy()
    if len(df) < 50:
        raise ValueError("Not enough completed candles")

    df["ema9"] = ema(df["close"], 9)
    df["ema20"] = ema(df["close"], 20)

    last = df.iloc[-1]
    previous = df.iloc[-2]
    recent = df.iloc[-21:-1]

    price = float(last["close"])
    long_score = 0
    short_score = 0
    reasons_long = []
    reasons_short = []

    # EMA direction and momentum
    if last["ema9"] > last["ema20"] and last["close"] > last["ema9"]:
        long_score += 1
        reasons_long.append("EMA 9 above EMA 20")
    if last["ema9"] < last["ema20"] and last["close"] < last["ema9"]:
        short_score += 1
        reasons_short.append("EMA 9 below EMA 20")

    # Simple liquidity sweep: wick beyond recent range, then close back inside.
    prior_high = float(recent["high"].max())
    prior_low = float(recent["low"].min())

    if float(last["low"]) < prior_low and price > prior_low:
        long_score += 1
        reasons_long.append("Bullish liquidity sweep")
    if float(last["high"]) > prior_high and price < prior_high:
        short_score += 1
        reasons_short.append("Bearish liquidity sweep")

    # Basic structure break using the previous 10 completed candles.
    structure = df.iloc[-11:-1]
    structure_high = float(structure["high"].max())
    structure_low = float(structure["low"].min())

    if price > structure_high:
        long_score += 1
        reasons_long.append("Bullish structure break")
    if price < structure_low:
        short_score += 1
        reasons_short.append("Bearish structure break")

    # Three-candle Fair Value Gap approximation.
    candle_a = df.iloc[-3]
    candle_c = df.iloc[-1]

    if float(candle_c["low"]) > float(candle_a["high"]):
        long_score += 1
        reasons_long.append("Bullish FVG")
    if float(candle_c["high"]) < float(candle_a["low"]):
        short_score += 1
        reasons_short.append("Bearish FVG")

    # Simple CRT range reclaim approximation using the prior candle's range.
    if float(previous["low"]) < float(recent["low"].min()) and price > float(previous["open"]):
        long_score += 1
        reasons_long.append("Possible bullish range reclaim")
    if float(previous["high"]) > float(recent["high"].max()) and price < float(previous["open"]):
        short_score += 1
        reasons_short.append("Possible bearish range reclaim")

    signal = "WAIT"
    if long_score >= MIN_SIGNAL_SCORE and long_score > short_score:
        signal = "LONG"
    elif short_score >= MIN_SIGNAL_SCORE and short_score > long_score:
        signal = "SHORT"

    support = float(df.iloc[-20:]["low"].min())
    resistance = float(df.iloc[-20:]["high"].max())

    return {
        "symbol": symbol,
        "price": price,
        "signal": signal,
        "long_score": long_score,
        "short_score": short_score,
        "long_reasons": reasons_long,
        "short_reasons": reasons_short,
        "support": support,
        "resistance": resistance,
        "candle_time": datetime.fromtimestamp(
            int(last["time"]) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC"),
    }


def print_result(result):
    print("-" * 55)
    print(f"{result['symbol']} | {result['candle_time']}")
    print(f"Price: {result['price']}")
    print(f"CRT KING SIGNAL: {result['signal']}")
    print(f"Score: LONG {result['long_score']} | SHORT {result['short_score']}")
    print(f"Support: {result['support']} | Resistance: {result['resistance']}")
    if result["long_reasons"]:
        print("LONG reasons: " + ", ".join(result["long_reasons"]))
    if result["short_reasons"]:
        print("SHORT reasons: " + ", ".join(result["short_reasons"]))


def make_alert_message(result):
    reasons = (
        result["long_reasons"]
        if result["signal"] == "LONG"
        else result["short_reasons"]
    )
    reason_text = "\n".join(f"â¢ {reason}" for reason in reasons) or "No extra details"

    return (
        "ð CRT KING AI ALERT\n\n"
        f"Coin: {result['symbol']}\n"
        f"Signal: {result['signal']}\n"
        f"Price: {result['price']}\n"
        f"LONG Score: {result['long_score']}\n"
        f"SHORT Score: {result['short_score']}\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"Candle: {result['candle_time']}\n\n"
        f"Reasons:\n{reason_text}\n\n"
        f"Support: {result['support']}\n"
        f"Resistance: {result['resistance']}\n\n"
        "â ï¸ Analysis alert only, not a guaranteed trade. Confirm manually."
    )


def main():
    print("=" * 55)
    print("CRT KING AI - TOP 20 BINANCE USDT PERPETUAL SCANNER")
    print("READ-ONLY: no trades will be placed.")
    print(f"Timeframe: {TIMEFRAME} | Scan interval: {SCAN_INTERVAL_SECONDS}s")
    print("=" * 55)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway Variables.")
        raise RuntimeError("Telegram environment variables are missing.")

    exchange = create_exchange()
    symbols = []
    scan_count = 0

    # Startup test message confirms the Telegram settings work.
    send_telegram(
        "ð CRT KING AI is online.\n"
        "Top 20 USDT perpetual scanner started.\n"
        "Timeframe: 15m\n"
        "Mode: READ-ONLY (no trades)."
    )

    while True:
        started = time.time()
        scan_count += 1

        try:
            if not symbols or (scan_count - 1) % REFRESH_SYMBOLS_EVERY_SCANS == 0:
                print("\nRefreshing top-volume USDT perpetual contracts...")
                symbols = get_top_usdt_perpetuals(exchange)
                if not symbols:
                    raise RuntimeError("Could not find active USDT perpetual markets.")
                print(f"Selected {len(symbols)} contracts:")
                print(", ".join(symbols))

            print(f"\nSCAN #{scan_count} | {datetime.now().astimezone().isoformat()}")

            for symbol in symbols:
                try:
                    result = analyse_symbol(exchange, symbol)
                    print_result(result)

                    if result["signal"] in ("LONG", "SHORT"):
                        send_telegram(make_alert_message(result))

                except ccxt.NetworkError as err:
                    print(f"{symbol}: Network error: {err}")
                except ccxt.ExchangeError as err:
                    print(f"{symbol}: Exchange error: {err}")
                except Exception as err:
                    print(f"{symbol}: Analysis error: {err}")

            elapsed = time.time() - started
            remaining = max(5, SCAN_INTERVAL_SECONDS - elapsed)
            print(f"\nScan complete. Next scan in about {int(remaining)} seconds.")
            time.sleep(remaining)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as err:
            print(f"\nScanner error: {err}")
            print("Retrying in 60 seconds...")
            time.sleep(60)


if __name__ == "__main__":
    main()
