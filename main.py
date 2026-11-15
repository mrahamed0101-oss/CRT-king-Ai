import time
from datetime import datetime, timezone

import ccxt
import pandas as pd

# CRT KING AI - TOP 20 BINANCE USDT PERPETUAL MARKET SCANNER
# Read-only market analysis. No API keys and no orders are used.

TIMEFRAME = "15m"
CANDLE_LIMIT = 100
TOP_N_COINS = 20
SCAN_INTERVAL_SECONDS = 60
TICKER_REFRESH_SCANS = 15
MIN_SIGNAL_SCORE = 4

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE",
    "EUR", "GBP", "TRY", "BRL", "JPY", "USTC", "PAXG"
}
COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def create_exchange():
    
    exchange = ccxt.okx({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })
    exchange.load_markets()
    return exchange


def get_top_usdt_perpetuals(exchange):
    eligible = []
    for symbol, market in exchange.markets.items():
        if (market.get("active", True)
                and market.get("swap") is True
                and market.get("linear") is True
                and market.get("quote") == "USDT"
                and market.get("settle") == "USDT"
                and market.get("base") not in EXCLUDED_BASES):
            eligible.append(symbol)

    if not eligible:
        raise RuntimeError("No active USDT-settled perpetual markets found.")

    tickers = exchange.fetch_tickers(eligible)
    ranked = []
    for symbol in eligible:
        ticker = tickers.get(symbol) or {}
        volume = ticker.get("quoteVolume")
        if volume is None and ticker.get("baseVolume") is not None and ticker.get("last") is not None:
            volume = float(ticker["baseVolume"]) * float(ticker["last"])
        try:
            volume = float(volume or 0)
        except (TypeError, ValueError):
            volume = 0
        if volume > 0:
            ranked.append((symbol, volume))

    ranked.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in ranked[:TOP_N_COINS]]


def analyze_symbol(exchange, symbol):
    candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
    if not candles or len(candles) < 30:
        print(f"{symbol}: Not enough candle data.")
        return None

    df = pd.DataFrame(candles, columns=COLUMNS)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if len(df) < 30:
        return None

    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()

    # Ignore the still-forming candle at df.iloc[-1].
    previous = df.iloc[-3]
    current = df.iloc[-2]
    recent = df.iloc[-12:-2]
    if len(recent) < 5:
        return None

    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    price = float(current["close"])

    # SMC liquidity sweep
    bullish_sweep = float(current["low"]) < swing_low and price > swing_low
    bearish_sweep = float(current["high"]) > swing_high and price < swing_high

    # CRT: sweep previous candle's range and close back inside it
    crt_high = float(previous["high"])
    crt_low = float(previous["low"])
    crt_bullish = (
        float(current["low"]) < crt_low
        and price > crt_low
        and price < crt_high
    )
    crt_bearish = (
        float(current["high"]) > crt_high
        and price < crt_high
        and price > crt_low
    )

    # EMA direction
    ema_bullish = float(current["EMA9"]) > float(current["EMA20"])
    ema_bearish = float(current["EMA9"]) < float(current["EMA20"])

    # Basic BOS / market structure break
    bullish_bos = price > swing_high
    bearish_bos = price < swing_low

    # Three-candle FVG approximation
    candle_two_back = df.iloc[-4]
    bullish_fvg = float(current["low"]) > float(candle_two_back["high"])
    bearish_fvg = float(current["high"]) < float(candle_two_back["low"])

    # Simple order-block heuristic: last opposite-colour candle in recent window.
    bullish_ob = False
    bearish_ob = False
    for _, candle in recent.iloc[::-1].iterrows():
        candle_open = float(candle["open"])
        candle_close = float(candle["close"])
        if not bullish_ob and candle_close < candle_open:
            bullish_ob = price > float(candle["high"])
        if not bearish_ob and candle_close > candle_open:
            bearish_ob = price < float(candle["low"])
        if bullish_ob and bearish_ob:
            break

    # Support / resistance from the recent lookback range
    support = swing_low
    resistance = swing_high

    long_score = (
        2 * int(crt_bullish)
        + 2 * int(bullish_sweep)
        + int(ema_bullish)
        + int(bullish_bos)
        + int(bullish_fvg)
        + int(bullish_ob)
    )
    short_score = (
        2 * int(crt_bearish)
        + 2 * int(bearish_sweep)
        + int(ema_bearish)
        + int(bearish_bos)
        + int(bearish_fvg)
        + int(bearish_ob)
    )

    if long_score >= MIN_SIGNAL_SCORE and long_score > short_score:
        signal = "LONG"
    elif short_score >= MIN_SIGNAL_SCORE and short_score > long_score:
        signal = "SHORT"
    else:
        signal = "WAIT"

    candle_time = datetime.fromtimestamp(
        int(current["time"]) / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "symbol": symbol,
        "price": price,
        "time": candle_time,
        "signal": signal,
        "long_score": long_score,
        "short_score": short_score,
        "crt_bullish": crt_bullish,
        "crt_bearish": crt_bearish,
        "bullish_sweep": bullish_sweep,
        "bearish_sweep": bearish_sweep,
        "ema_bullish": ema_bullish,
        "ema_bearish": ema_bearish,
        "bullish_bos": bullish_bos,
        "bearish_bos": bearish_bos,
        "bullish_fvg": bullish_fvg,
        "bearish_fvg": bearish_fvg,
        "bullish_ob": bullish_ob,
        "bearish_ob": bearish_ob,
        "support": support,
        "resistance": resistance,
    }


def print_result(r):
    if r is None:
        return
    print("\n" + "=" * 60)
    print(f"{r['symbol']} | {r['time']} | Price: {r['price']:.8g}")
    print(f"CRT KING SIGNAL: {r['signal']}")
    print(f"Score: LONG {r['long_score']} | SHORT {r['short_score']}")
    print(f"CRT: bullish={r['crt_bullish']} bearish={r['crt_bearish']}")
    print(f"Liquidity sweep: bullish={r['bullish_sweep']} bearish={r['bearish_sweep']}")
    print(f"EMA 9/20: bullish={r['ema_bullish']} bearish={r['ema_bearish']}")
    print(f"BOS/MSB: bullish={r['bullish_bos']} bearish={r['bearish_bos']}")
    print(f"FVG: bullish={r['bullish_fvg']} bearish={r['bearish_fvg']}")
    print(f"Order block heuristic: bullish={r['bullish_ob']} bearish={r['bearish_ob']}")
    print(f"Support: {r['support']:.8g} | Resistance: {r['resistance']:.8g}")
    print("=" * 60)
import requests
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram settings missing")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    requests.post(url, data=data, timeout=15)

def main():
    print("=" * 60)
    print("                 CRT KING AI BOT")
    print("       TOP 20 BINANCE USDT PERPETUAL SCANNER")
    print("=" * 60)
    print("READ-ONLY: market analysis only. No trades are placed.")
    print(f"Timeframe: {TIMEFRAME} | Scan interval: {SCAN_INTERVAL_SECONDS}s")

    exchange = create_exchange()
    symbols = []
    scan_count = 0

    while True:
        started = time.time()
        scan_count += 1
        try:
            if not symbols or (scan_count - 1) % TICKER_REFRESH_SCANS == 0:
                print("\nRefreshing top-volume USDT perpetual contracts...")
                symbols = get_top_usdt_perpetuals(exchange)
                if not symbols:
                    raise RuntimeError("Could not build the top-20 coin list.")
                print(f"Selected {len(symbols)} contracts:")
                print(", ".join(symbols))

            print(f"\nSCAN #{scan_count} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            for symbol in symbols:
                try:
                    print_result(analyze_symbol(exchange, symbol))
                except ccxt.NetworkError as err:
                    print(f"{symbol}: Network error: {err}")
                except ccxt.ExchangeError as err:
                    print(f"{symbol}: Exchange error: {err}")
                except Exception as err:
                    print(f"{symbol}: Analysis error: {err}")

            remaining = max(5, SCAN_INTERVAL_SECONDS - (time.time() - started))
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
