#!/usr/bin/env python3
"""
CRT KING AI — Strict multi-timeframe scanner (OKX public market data).
READ ONLY: this program never places or manages orders.

Required Railway variables:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Optional:
  SCAN_INTERVAL_SECONDS=180
  TOP_N=20
  ENABLE_SCALPING=true
  ENABLE_SWING=true

Strict, rule-based definitions are documented in the alert and README comments.
This is an educational scanner, not a guarantee of profitable signals.
"""
import os
import time
import logging
import csv
import threading
import math
from bisect import bisect_right
from datetime import datetime, timezone, timedelta

import requests

# -------------------- SETTINGS --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("CRT_KING_STRICT")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SCAN_INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "180")))
TOP_N = max(5, min(50, int(os.getenv("TOP_N", "20"))))
ENABLE_SCALPING = os.getenv("ENABLE_SCALPING", "true").lower() == "true"
ENABLE_SWING = os.getenv("ENABLE_SWING", "true").lower() == "true"

OKX = "https://www.okx.com"
COOLDOWN_SECONDS = 3600
TIMEFRAMES = ("4H", "1H", "15m")

http = requests.Session()
http.headers.update({"User-Agent": "CRT-KING-AI-STRICT/3.0"})
last_alert = {}
telegram_update_id = 0
scan_status = "Starting"
last_scan_time = "Not scanned yet"
last_scan_alerts = 0


# -------------------- TELEGRAM / API --------------------
def okx_get(path, params=None):
    response = http.get(OKX + path, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "0":
        raise RuntimeError("OKX API error: " + str(payload.get("msg", "unknown")))
    return payload.get("data", [])


def telegram_send(text, chat_id=None):
    target = str(chat_id or CHAT_ID)
    response = http.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": target,
            "text": text,
            "disable_web_page_preview": True
        },
        timeout=15
    )
    if not response.ok:
        log.error("Telegram send failed: %s", response.text[:300])
        return False
    log.info("Telegram message sent to %s", target)
    return True


def poll_telegram_commands():
    global telegram_update_id
    response = http.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"offset": telegram_update_id + 1, "timeout": 1},
        timeout=5
    )
    if not response.ok:
        log.warning("Telegram getUpdates failed: %s", response.text[:200])
        return

    for update in response.json().get("result", []):
        telegram_update_id = max(telegram_update_id, update.get("update_id", 0))
        message = update.get("message") or update.get("edited_message") or {}
        command_parts = (message.get("text") or "").strip().split()
        if not command_parts:
            continue

        chat = str(message.get("chat", {}).get("id", ""))
        command = command_parts[0].split("@")[0].lower()

        if command in ("/start", "/help"):
            telegram_send(
                "👑 CRT KING AI — STRICT MODE\n\n"
                "/start - Start\n/help - Help\n/status - Scanner status\n\n"
                "4H = higher-timeframe structure\n"
                "1H = confirmation\n"
                "15m = entry setup\n"
                "Strict confirmations only; otherwise WAIT.\n"
                "READ-ONLY: no orders are placed.",
                chat
            )
        elif command == "/backtest":
            threading.Thread(target=run_backtest, args=(chat,), daemon=True).start()
        elif command == "/status":
            telegram_send(
                "👑 CRT KING AI STATUS\n"
                f"Status: {scan_status}\n"
                f"Last scan: {last_scan_time}\n"
                f"Alerts last scan: {last_scan_alerts}\n"
                f"Top markets: {TOP_N} OKX USDT swaps\n"
                "Timeframes: 4H / 1H / 15m\n"
                "Mode: STRICT | READ-ONLY",
                chat
            )


# -------------------- MARKET DATA --------------------
def get_universe():
    tickers = okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
    ranked = []
    for ticker in tickers:
        instrument = ticker.get("instId", "")
        if not instrument.endswith("-USDT-SWAP"):
            continue
        try:
            volume = float(ticker.get("volCcy24h") or 0)
            if volume > 0:
                ranked.append((volume, instrument))
        except (ValueError, TypeError):
            continue
    ranked.sort(reverse=True)
    return [instrument for _, instrument in ranked[:TOP_N]]


def get_candles(instrument, timeframe, limit=220):
    rows = okx_get(
        "/api/v5/market/candles",
        {"instId": instrument, "bar": timeframe, "limit": str(limit)}
    )
    candles = []
    for row in reversed(rows):
        try:
            candles.append({
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "confirm": str(row[8]) if len(row) > 8 else "1"
            })
        except (ValueError, TypeError, IndexError):
            continue

    # Never use an unfinished candle for signal confirmation.
    if candles and candles[-1]["confirm"] == "0":
        candles.pop()
    return candles


# -------------------- INDICATORS / STRUCTURE --------------------
def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def average_true_range(candles, period=14):
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))
    return sum(true_ranges[-period:]) / period


def find_swings(candles, left=3, right=3):
    swing_highs, swing_lows = [], []
    for index in range(left, len(candles) - right):
        high = candles[index]["high"]
        low = candles[index]["low"]
        if all(
            high > candles[j]["high"]
            for j in range(index-left, index+right+1) if j != index
        ):
            swing_highs.append((index, high))
        if all(
            low < candles[j]["low"]
            for j in range(index-left, index+right+1) if j != index
        ):
            swing_lows.append((index, low))
    return swing_highs, swing_lows


def structure_analysis(candles):
    """Confirmed pivot sequence, directional structure and latest BOS/CHoCH."""
    if len(candles) < 35:
        return None

    # Exclude the most recent candle from pivot confirmation to avoid repainting.
    highs, lows = find_swings(candles[:-1])
    if len(highs) < 2 or len(lows) < 2:
        return None

    last_highs = [value for _, value in highs[-2:]]
    last_lows = [value for _, value in lows[-2:]]
    higher_high = last_highs[-1] > last_highs[-2]
    higher_low = last_lows[-1] > last_lows[-2]
    lower_high = last_highs[-1] < last_highs[-2]
    lower_low = last_lows[-1] < last_lows[-2]

    if higher_high and higher_low:
        bias = "Bullish"
    elif lower_high and lower_low:
        bias = "Bearish"
    else:
        bias = "Neutral"

    last = candles[-1]
    previous = candles[-2]
    broken_up = last["close"] > last_highs[-1] and previous["close"] <= last_highs[-1]
    broken_down = last["close"] < last_lows[-1] and previous["close"] >= last_lows[-1]

    bos = "None"
    choch = "None"
    if broken_up:
        bos = "Bullish" if bias == "Bullish" else "None"
        choch = "Bullish" if bias == "Bearish" else "None"
    elif broken_down:
        bos = "Bearish" if bias == "Bearish" else "None"
        choch = "Bearish" if bias == "Bullish" else "None"

    return {
        "bias": bias,
        "swing_high": last_highs[-1],
        "swing_low": last_lows[-1],
        "bos": bos,
        "choch": choch,
        "higher_high": higher_high,
        "higher_low": higher_low,
        "lower_high": lower_high,
        "lower_low": lower_low,
        "all_highs": highs,
        "all_lows": lows
    }



def liquidity_pools(candles, atr_value, lookback=80):
    """Cluster confirmed swing highs/lows into equal-high/equal-low liquidity."""
    highs, lows = find_swings(candles[:-1])
    recent = len(candles) - lookback
    hi = [v for i, v in highs if i >= recent]
    lo = [v for i, v in lows if i >= recent]
    price = candles[-1]["close"]
    tolerance = max(price * 0.001, atr_value * 0.15)

    def cluster(values):
        groups = []
        for value in values:
            group = next((g for g in groups if abs(value-sum(g)/len(g)) <= tolerance), None)
            if group is None:
                groups.append([value])
            else:
                group.append(value)
        levels = [sum(g)/len(g) for g in groups if len(g) >= 2]
        return min(levels, key=lambda x: abs(x-price)) if levels else None

    return cluster(hi), cluster(lo)


def detect_liquidity_sweep(candles, structure, atr_value):
    last = candles[-1]
    eqh, eql = liquidity_pools(candles, atr_value)
    buy_level = eqh if eqh is not None else structure["swing_high"]
    sell_level = eql if eql is not None else structure["swing_low"]
    buy_sweep = last["high"] > buy_level and last["close"] < buy_level
    sell_sweep = last["low"] < sell_level and last["close"] > sell_level
    return {
        "label": "Sell-side" if sell_sweep else "Buy-side" if buy_sweep else "None",
        "equal_high": eqh, "equal_low": eql,
        "buy_level": buy_level, "sell_level": sell_level
    }


def detect_fvg_zone(candles, atr_value, lookback=80):
    """Latest unfilled 3-candle FVG with a minimum displacement body."""
    zones = []
    for i in range(max(2, len(candles)-lookback), len(candles)):
        a, b, c = candles[i-2], candles[i-1], candles[i]
        if abs(b["close"]-b["open"]) < atr_value*0.5:
            continue
        if c["low"] > a["high"]:
            direction, low, high = "Bullish", a["high"], c["low"]
            invalid = any(x["low"] <= low for x in candles[i+1:])
        elif c["high"] < a["low"]:
            direction, low, high = "Bearish", c["high"], a["low"]
            invalid = any(x["high"] >= high for x in candles[i+1:])
        else:
            continue
        if not invalid:
            zones.append((direction, low, high, i))
    return zones[-1] if zones else None


def detect_order_block(candles, structure, atr_value, lookback=60):
    """Last opposite candle before a >=1 ATR displacement through a swing."""
    highs, lows = find_swings(candles[:-1])
    found = None
    for i in range(max(5, len(candles)-lookback), len(candles)):
        candle, previous = candles[i], candles[i-1]
        if abs(candle["close"]-candle["open"]) < atr_value:
            continue
        bull_break = any(candle["close"] > level and previous["close"] <= level
                         for idx, level in highs if idx < i)
        bear_break = any(candle["close"] < level and previous["close"] >= level
                         for idx, level in lows if idx < i)
        direction = "Bullish" if bull_break else "Bearish" if bear_break else None
        if not direction:
            continue
        for j in range(i-1, max(max(5, len(candles)-lookback)-1, i-8), -1):
            candidate = candles[j]
            opposite = ((direction == "Bullish" and candidate["close"] < candidate["open"])
                        or (direction == "Bearish" and candidate["close"] > candidate["open"]))
            if not opposite:
                continue
            low, high = candidate["low"], candidate["high"]
            later = candles[i+1:]
            invalid = (any(x["close"] < low for x in later) if direction == "Bullish"
                       else any(x["close"] > high for x in later))
            if not invalid:
                found = (direction, low, high, j, i)
            break
    return found


def zone_retest(candle, zone, direction):
    if not zone or zone[0] != direction:
        return False
    _, low, high = zone[:3]
    overlap = candle["low"] <= high and candle["high"] >= low
    if direction == "Bullish":
        return overlap and candle["close"] > candle["open"] and candle["close"] >= low
    return overlap and candle["close"] < candle["open"] and candle["close"] <= high


def premium_discount(candles):
    recent = candles[-50:-1]
    dealing_high = max(x["high"] for x in recent)
    dealing_low = min(x["low"] for x in recent)
    midpoint = (dealing_high + dealing_low) / 2
    price = candles[-1]["close"]
    location = "Discount" if price < midpoint else "Premium" if price > midpoint else "Equilibrium"
    return location, dealing_low, dealing_high, midpoint


def crt_range_reclaim(candles, direction):
    """
    Mechanical CRT proxy: sweep the prior four closed-candle range edge,
    then close back inside the range with a confirming candle.
    This is not a full session-based CRT implementation.
    """
    prior = candles[-5:-1]
    current = candles[-1]
    range_high = max(x["high"] for x in prior)
    range_low = min(x["low"] for x in prior)
    if direction == "Bullish":
        valid = current["low"] < range_low and current["close"] > range_low and current["close"] > current["open"]
    else:
        valid = current["high"] > range_high and current["close"] < range_high and current["close"] < current["open"]
    return valid, range_high, range_low


def ema_bias(candles):
    closes = [x["close"] for x in candles]
    e9, e20, e50 = ema(closes, 9), ema(closes, 20), ema(closes, 50)
    if None in (e9, e20, e50):
        return "Neutral", (e9, e20, e50)
    if e9 > e20 > e50 and closes[-1] > e20:
        return "Bullish", (e9, e20, e50)
    if e9 < e20 < e50 and closes[-1] < e20:
        return "Bearish", (e9, e20, e50)
    return "Neutral", (e9, e20, e50)


# -------------------- STRICT SETUP ENGINE --------------------
def evaluate_market(instrument, candles_4h, candles_1h, candles_15m):
    if min(len(candles_4h), len(candles_1h), len(candles_15m)) < 60:
        return None

    structure_4h = structure_analysis(candles_4h)
    structure_1h = structure_analysis(candles_1h)
    structure_15m = structure_analysis(candles_15m)
    atr_15m = average_true_range(candles_15m)
    atr_1h = average_true_range(candles_1h)
    if not all((structure_4h, structure_1h, structure_15m, atr_15m, atr_1h)):
        return None

    bias_4h = structure_4h["bias"]
    bias_1h = structure_1h["bias"]
    ema_4h, ema_values_4h = ema_bias(candles_4h)
    ema_1h, ema_values_1h = ema_bias(candles_1h)

    # Historical, still-valid SMC zones and liquidity pools on 1H and 15m.
    last = candles_15m[-1]
    sweep_info_15m = detect_liquidity_sweep(candles_15m, structure_15m, atr_15m)
    sweep_15m = sweep_info_15m["label"]
    fvg_15m = detect_fvg_zone(candles_15m, atr_15m)
    ob_15m = detect_order_block(candles_15m, structure_15m, atr_15m)
    fvg_1h = detect_fvg_zone(candles_1h, atr_1h)
    ob_1h = detect_order_block(candles_1h, structure_1h, atr_1h)
    sweep_info_1h = detect_liquidity_sweep(candles_1h, structure_1h, atr_1h)
    pd_location, dealing_low, dealing_high, midpoint = premium_discount(candles_15m)

    # Require a fresh 15m structure shift in the intended direction.
    bullish_shift = (
        structure_15m["choch"] == "Bullish"
        or structure_15m["bos"] == "Bullish"
    )
    bearish_shift = (
        structure_15m["choch"] == "Bearish"
        or structure_15m["bos"] == "Bearish"
    )

    candidates = []
    for direction in ("Bullish", "Bearish"):
        is_long = direction == "Bullish"
        expected_sweep = "Sell-side" if is_long else "Buy-side"
        expected_zone = "Discount" if is_long else "Premium"

        # HTF strict confirmation.
        htf_ok = (
            bias_4h == direction
            and bias_1h == direction
            and ema_4h == direction
            and ema_1h == direction
            and structure_4h["bos"] == direction
            and structure_1h["bos"] == direction
        )

        # 15m execution confirmation: sweep + structure shift + CRT reclaim
        # + valid displacement OB retest + FVG retest + candle confirmation.
        crt_ok, crt_high, crt_low = crt_range_reclaim(candles_15m, direction)
        ob_ok = zone_retest(last, ob_15m, direction)
        fvg_ok = (
            fvg_15m is not None and fvg_15m[0] == direction
            and last["low"] <= fvg_15m[2] and last["high"] >= fvg_15m[1]
        )
        ob_1h_ok = ob_1h is not None and ob_1h[0] == direction
        fvg_1h_ok = fvg_1h is not None and fvg_1h[0] == direction
        sweep_ok = sweep_15m == expected_sweep
        sweep_1h_ok = sweep_info_1h["label"] == expected_sweep
        shift_ok = bullish_shift if is_long else bearish_shift
        candle_ok = last["close"] > last["open"] if is_long else last["close"] < last["open"]
        location_ok = pd_location == expected_zone

        # Strict swing requires 4H + 1H BOS alignment.
        swing_checks = {
            "4H structure + BOS": htf_ok,
            "1H aligned liquidity sweep": sweep_1h_ok,
            "1H valid OB": ob_1h_ok,
            "1H valid FVG": fvg_1h_ok,
            "15m liquidity sweep": sweep_ok,
            "15m BOS/CHoCH": shift_ok,
            "15m CRT reclaim": crt_ok,
            "15m OB retest": ob_ok,
            "15m FVG retest": fvg_ok,
            "Premium/Discount": location_ok,
            "15m candle confirmation": candle_ok
        }
        if all(swing_checks.values()) and ENABLE_SWING:
            candidates.append(("SWING TRADE", direction, swing_checks, crt_high, crt_low,
                               ob_15m, fvg_15m, sweep_15m, structure_4h, structure_1h,
                               structure_15m, pd_location, ema_values_4h, ema_values_1h))

        # Strict scalp allows neutral 4H, but never a 4H trend against the trade.
        # 1H structure + EMA + BOS must align, with all 15m execution checks.
        scalp_htf_ok = (
            bias_4h in (direction, "Neutral")
            and bias_1h == direction
            and ema_1h == direction
            and structure_1h["bos"] == direction
        )
        scalp_checks = dict(swing_checks)
        scalp_checks["4H not opposing 1H"] = bias_4h in (direction, "Neutral")
        scalp_checks["1H structure + BOS"] = scalp_htf_ok
        if all((
            scalp_htf_ok, sweep_1h_ok, ob_1h_ok, fvg_1h_ok, sweep_ok, shift_ok, crt_ok, ob_ok,
            fvg_ok, location_ok, candle_ok
        )) and ENABLE_SCALPING:
            candidates.append(("SCALPING TRADE", direction, scalp_checks, crt_high, crt_low,
                               ob_15m, fvg_15m, sweep_15m, structure_4h, structure_1h,
                               structure_15m, pd_location, ema_values_4h, ema_values_1h))

    if not candidates:
        return {"direction": "WAIT"}

    # Prefer swing when both strict conditions are met.
    chosen = next((x for x in candidates if x[0] == "SWING TRADE"), candidates[0])
    (trade_type, direction, checks, crt_high, crt_low, ob, fvg, sweep,
     s4, s1, s15, pd_location, e4, e1) = chosen

    entry = last["close"]
    # SL beyond the sweep candle extreme plus ATR buffer.
    if direction == "Bullish":
        stop = min(last["low"], ob[1] if ob else last["low"]) - atr_15m * 0.20
        risk = entry - stop
        targets = [entry + risk * r for r in (1, 2, 3)]
    else:
        stop = max(last["high"], ob[2] if ob else last["high"]) + atr_15m * 0.20
        risk = stop - entry
        targets = [entry - risk * r for r in (1, 2, 3)]

    if risk <= 0:
        return {"direction": "WAIT"}

    passed = [name for name, ok in checks.items() if ok]
    return {
        "direction": "LONG" if direction == "Bullish" else "SHORT",
        "trade_type": trade_type,
        "instrument": instrument,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "risk_reward": "1:1 / 1:2 / 1:3",
        "bias_4h": bias_4h,
        "bias_1h": bias_1h,
        "ema_4h": e4,
        "ema_1h": e1,
        "structure_4h": s4,
        "structure_1h": s1,
        "structure_15m": s15,
        "sweep": sweep,
        "liquidity_1h": sweep_info_1h,
        "liquidity_15m": sweep_info_15m,
        "ob": ob,
        "ob_1h": ob_1h,
        "fvg": fvg,
        "fvg_1h": fvg_1h,
        "crt_high": crt_high,
        "crt_low": crt_low,
        "pd_location": pd_location,
        "dealing_low": dealing_low,
        "dealing_high": dealing_high,
        "midpoint": midpoint,
        "checks": passed
    }




# -------------------- 12-MONTH HISTORICAL BACKTEST --------------------
BACKTEST_MONTHS = 12
BACKTEST_SYMBOLS = max(1, min(10, int(os.getenv("BACKTEST_SYMBOLS", "5"))))
BACKTEST_FEE_RATE = float(os.getenv("BACKTEST_FEE_RATE", "0.0005"))  # 0.05% per side
BACKTEST_SLIPPAGE = float(os.getenv("BACKTEST_SLIPPAGE", "0.0002"))  # 0.02% per side
BACKTEST_RUNNING = False
BACKTEST_LOCK = threading.Lock()


def get_history_candles(instrument, timeframe, start_ms, end_ms):
    """Download closed historical OKX candles, paging backwards."""
    all_rows = {}
    cursor = end_ms
    page_count = 0

    while cursor > start_ms:
        rows = okx_get(
            "/api/v5/market/history-candles",
            {
                "instId": instrument,
                "bar": timeframe,
                "after": str(cursor),
                "limit": "100"
            }
        )
        if not rows:
            break

        oldest = None
        for row in rows:
            try:
                ts = int(row[0])
                if start_ms <= ts <= end_ms and str(row[8]) == "1":
                    all_rows[ts] = {
                        "ts": ts,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "confirm": "1"
                    }
                oldest = ts if oldest is None else min(oldest, ts)
            except (ValueError, TypeError, IndexError):
                continue

        if oldest is None or oldest >= cursor:
            break
        cursor = oldest
        page_count += 1
        if page_count > 600:
            log.warning("Historical paging safety limit reached for %s %s", instrument, timeframe)
            break
        time.sleep(0.12)

    return [all_rows[k] for k in sorted(all_rows)]


def candle_duration_ms(timeframe):
    return {"15m": 15 * 60_000, "1H": 60 * 60_000, "4H": 4 * 60 * 60_000}[timeframe]


def simulate_trade(candles, signal_index, signal):
    """
    Enter at the next 15m candle open. Split exits equally at 1R/2R/3R.
    If SL and a target are touched in the same candle, count SL first.
    """
    if signal_index + 1 >= len(candles):
        return None

    direction = signal["direction"]
    raw_entry = candles[signal_index + 1]["open"]
    stop = signal["stop"]
    if direction == "LONG":
        risk = raw_entry - stop
        if risk <= 0:
            return None
        targets = [raw_entry + risk * r for r in (1, 2, 3)]
    else:
        risk = stop - raw_entry
        if risk <= 0:
            return None
        targets = [raw_entry - risk * r for r in (1, 2, 3)]

    remaining = 1.0
    realized_r = 0.0
    hit_targets = 0
    exit_index = None
    exit_reason = "OPEN"
    entry_fill = raw_entry * (1 + BACKTEST_SLIPPAGE if direction == "LONG" else 1 - BACKTEST_SLIPPAGE)
    entry_fee_pct = BACKTEST_FEE_RATE

    for j in range(signal_index + 1, len(candles)):
        bar = candles[j]
        if direction == "LONG":
            stop_hit = bar["low"] <= stop
            target_hits = [bar["high"] >= t for t in targets]
        else:
            stop_hit = bar["high"] >= stop
            target_hits = [bar["low"] <= t for t in targets]

        # Conservative intrabar rule: stop is assumed first if both sides touched.
        if stop_hit:
            stop_fill = stop * (1 - BACKTEST_SLIPPAGE if direction == "LONG" else 1 + BACKTEST_SLIPPAGE)
            pnl_fraction = ((stop_fill - entry_fill) / entry_fill if direction == "LONG"
                            else (entry_fill - stop_fill) / entry_fill)
            realized_r += remaining * (pnl_fraction / (risk / raw_entry))
            exit_index = j
            exit_reason = "SL" if hit_targets == 0 else "SL_AFTER_PARTIAL"
            remaining = 0.0
            break

        for target_number, is_hit in enumerate(target_hits):
            if is_hit and target_number >= hit_targets:
                fraction = 1.0 / 3.0
                target = targets[target_number]
                target_fill = target * (1 - BACKTEST_SLIPPAGE if direction == "LONG" else 1 + BACKTEST_SLIPPAGE)
                pnl_fraction = ((target_fill - entry_fill) / entry_fill if direction == "LONG"
                                else (entry_fill - target_fill) / entry_fill)
                realized_r += fraction * (pnl_fraction / (risk / raw_entry))
                remaining -= fraction
                hit_targets = target_number + 1
                if remaining <= 1e-9:
                    exit_index = j
                    exit_reason = "TP3"
                    break

        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        # Close remaining size at final available candle close.
        last_close = candles[-1]["close"]
        close_fill = last_close * (1 - BACKTEST_SLIPPAGE if direction == "LONG" else 1 + BACKTEST_SLIPPAGE)
        pnl_fraction = ((close_fill - entry_fill) / entry_fill if direction == "LONG"
                        else (entry_fill - close_fill) / entry_fill)
        realized_r += remaining * (pnl_fraction / (risk / raw_entry))
        exit_index = len(candles) - 1
        exit_reason = "DATA_END"

    # Approximate round-trip taker fees on full notional (entry + weighted exits).
    # Fee drag converted to R using the initial price risk.
    fee_r = (2 * BACKTEST_FEE_RATE) / max(risk / raw_entry, 1e-9)
    net_r = realized_r - fee_r
    return {
        "direction": direction,
        "trade_type": signal.get("trade_type", ""),
        "entry_time": datetime.fromtimestamp(candles[signal_index + 1]["ts"] / 1000, timezone.utc).isoformat(),
        "entry": raw_entry,
        "stop": stop,
        "tp1": targets[0],
        "tp2": targets[1],
        "tp3": targets[2],
        "exit_time": datetime.fromtimestamp(candles[exit_index]["ts"] / 1000, timezone.utc).isoformat(),
        "exit_reason": exit_reason,
        "targets_hit": hit_targets,
        "gross_R": realized_r,
        "fees_R_est": fee_r,
        "net_R": net_r
    }


def max_drawdown_r(trades):
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for trade in trades:
        equity += trade["net_R"]
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def run_backtest(chat_id):
    global BACKTEST_RUNNING
    with BACKTEST_LOCK:
        if BACKTEST_RUNNING:
            telegram_send("⏳ 12-month backtest already running. Please wait.", chat_id)
            return
        BACKTEST_RUNNING = True

    try:
        telegram_send(
            "👑 CRT KING AI — 12 MONTH BACKTEST STARTED\n"
            f"Period: last {BACKTEST_MONTHS} months\n"
            f"Markets: top {BACKTEST_SYMBOLS} OKX USDT perpetuals by 24h volume\n"
            "Timeframes: 4H + 1H + 15m\n"
            "Entry: next 15m candle open\n"
            "Fees/slippage included; conservative same-candle SL rule.\n"
            "This may take a while. No live orders are placed.",
            chat_id
        )

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=365)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        symbols = get_universe()[:BACKTEST_SYMBOLS]
        all_trades = []
        symbol_stats = []
        errors = []

        for symbol in symbols:
            try:
                telegram_send(f"📥 Downloading 12-month history: {symbol}", chat_id)
                c15 = get_history_candles(symbol, "15m", start_ms, end_ms)
                c1 = get_history_candles(symbol, "1H", start_ms - 60*60_000*100, end_ms)
                c4 = get_history_candles(symbol, "4H", start_ms - 4*60*60_000*100, end_ms)

                if len(c15) < 500 or len(c1) < 100 or len(c4) < 60:
                    errors.append(f"{symbol}: insufficient historical candles (15m={len(c15)}, 1H={len(c1)}, 4H={len(c4)})")
                    continue

                t1 = [x["ts"] + candle_duration_ms("1H") for x in c1]
                t4 = [x["ts"] + candle_duration_ms("4H") for x in c4]
                symbol_trades = []
                next_allowed_index = 0

                # Signal is evaluated only on a closed 15m candle.
                for i in range(220, len(c15) - 2):
                    if i < next_allowed_index:
                        continue
                    signal_close_time = c15[i]["ts"] + candle_duration_ms("15m")
                    idx1 = bisect_right(t1, signal_close_time)
                    idx4 = bisect_right(t4, signal_close_time)
                    hist1 = c1[max(0, idx1 - 220):idx1]
                    hist4 = c4[max(0, idx4 - 220):idx4]
                    hist15 = c15[max(0, i - 219):i + 1]

                    if len(hist1) < 60 or len(hist4) < 60:
                        continue

                    signal = evaluate_market(symbol, hist4, hist1, hist15)
                    if not signal or signal.get("direction") == "WAIT":
                        continue

                    trade = simulate_trade(c15, i, signal)
                    if trade:
                        trade["symbol"] = symbol
                        symbol_trades.append(trade)
                        # One open trade per symbol; do not overlap positions.
                        exit_ts = int(datetime.fromisoformat(trade["exit_time"]).timestamp() * 1000)
                        next_allowed_index = bisect_right([x["ts"] for x in c15], exit_ts)

                all_trades.extend(symbol_trades)
                wins = sum(1 for x in symbol_trades if x["net_R"] > 0)
                losses = sum(1 for x in symbol_trades if x["net_R"] <= 0)
                symbol_stats.append((symbol, len(symbol_trades), wins, losses,
                                     sum(x["net_R"] for x in symbol_trades)))
                telegram_send(
                    f"✅ Finished {symbol}\nTrades: {len(symbol_trades)} | "
                    f"Wins: {wins} | Losses: {losses} | Net: {sum(x['net_R'] for x in symbol_trades):.2f}R",
                    chat_id
                )
            except Exception as exc:
                log.exception("Backtest failed for %s", symbol)
                errors.append(f"{symbol}: {str(exc)[:180]}")

        if not all_trades:
            msg = "⚠️ Backtest completed, but no qualifying trades were found."
            if errors:
                msg += "\n\nIssues:\n" + "\n".join(errors[:8])
            msg += "\n\nThis strict strategy may be too restrictive; zero trades is a valid result, not a failure to be hidden."
            telegram_send(msg, chat_id)
            return

        all_trades.sort(key=lambda x: x["entry_time"])
        total = len(all_trades)
        wins = [x for x in all_trades if x["net_R"] > 0]
        losses = [x for x in all_trades if x["net_R"] <= 0]
        net_r = sum(x["net_R"] for x in all_trades)
        gross_profit = sum(x["net_R"] for x in wins)
        gross_loss = abs(sum(x["net_R"] for x in losses))
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
        avg_r = net_r / total
        win_rate = 100 * len(wins) / total
        dd = max_drawdown_r(all_trades)

        report_path = "/tmp/CRT_KING_AI_12_MONTH_BACKTEST.csv"
        with open(report_path, "w", newline="", encoding="utf-8") as csvfile:
            fields = ["symbol", "direction", "trade_type", "entry_time", "entry", "stop",
                      "tp1", "tp2", "tp3", "exit_time", "exit_reason", "targets_hit",
                      "gross_R", "fees_R_est", "net_R"]
            writer = csv.DictWriter(csvfile, fieldnames=fields)
            writer.writeheader()
            for trade in all_trades:
                writer.writerow({k: trade.get(k, "") for k in fields})

        summary = (
            "👑 CRT KING AI — 12 MONTH BACKTEST RESULT\n\n"
            f"Period: {start_dt.date()} to {end_dt.date()} UTC\n"
            f"Markets tested: {len(symbols)} selected | completed trades: {total}\n"
            f"Win rate: {win_rate:.2f}% ({len(wins)} wins / {len(losses)} losses)\n"
            f"Net result: {net_r:.2f}R | Average: {avg_r:.3f}R/trade\n"
            f"Profit factor: {'∞' if math.isinf(profit_factor) else f'{profit_factor:.2f}'}\n"
            f"Max drawdown: {dd:.2f}R\n"
            f"Estimated fees: {BACKTEST_FEE_RATE*100:.3f}% per side; "
            f"slippage: {BACKTEST_SLIPPAGE*100:.3f}% per side\n\n"
            "PER MARKET\n" +
            "\n".join(f"{sym}: {n} trades | {w}W/{l}L | {r:.2f}R"
                      for sym, n, w, l, r in symbol_stats) +
            "\n\n⚠️ Historical simulation only. Funding costs, spread variation, market impact "
            "and intrabar tick order are not fully modeled. Past results do not guarantee future results."
        )
        if errors:
            summary += "\n\nDATA/API ISSUES:\n" + "\n".join(errors[:5])
        telegram_send(summary, chat_id)

        # Send detailed trade-by-trade CSV to Telegram.
        try:
            with open(report_path, "rb") as file_obj:
                response = http.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                    data={"chat_id": chat_id, "caption": "CRT KING AI — 12-month trade-by-trade backtest CSV"},
                    files={"document": ("CRT_KING_AI_12_MONTH_BACKTEST.csv", file_obj, "text/csv")},
                    timeout=60
                )
                if not response.ok:
                    log.error("Could not send backtest CSV: %s", response.text[:300])
        except Exception:
            log.exception("Could not upload backtest CSV")

    except Exception as exc:
        log.exception("12-month backtest failed")
        telegram_send("❌ Backtest error: " + str(exc)[:500], chat_id)
    finally:
        with BACKTEST_LOCK:
            BACKTEST_RUNNING = False


# -------------------- ALERT FORMAT --------------------
def price(value):
    return f"{value:.10g}"


def format_alert(signal):
    e4 = " / ".join(price(x) if x is not None else "N/A" for x in signal["ema_4h"])
    e1 = " / ".join(price(x) if x is not None else "N/A" for x in signal["ema_1h"])
    s4 = signal["structure_4h"]
    s1 = signal["structure_1h"]
    s15 = signal["structure_15m"]
    fvg = signal["fvg"]
    ob = signal["ob"]

    return (
        "👑 CRT KING AI — STRICT SIGNAL\n\n"
        f"Coin: {signal['instrument'].replace('-', '/')}\n"
        f"Signal: {signal['direction']}\n"
        f"Trade Type: {signal['trade_type']}\n"
        "Entry Timeframe: 15m\n\n"
        "MULTI-TIMEFRAME ANALYSIS\n"
        f"4H Structure: {s4['bias']} | BOS: {s4['bos']} | CHoCH: {s4['choch']}\n"
        f"1H Structure: {s1['bias']} | BOS: {s1['bos']} | CHoCH: {s1['choch']}\n"
        f"15m Structure: {s15['bias']} | BOS: {s15['bos']} | CHoCH: {s15['choch']}\n"
        f"4H EMA 9/20/50: {e4}\n"
        f"1H EMA 9/20/50: {e1}\n\n"
        "ENTRY PLAN\n"
        f"Entry: {price(signal['entry'])}\n"
        f"Stop Loss: {price(signal['stop'])}\n"
        f"TP1 (1R): {price(signal['targets'][0])}\n"
        f"TP2 (2R): {price(signal['targets'][1])}\n"
        f"TP3 (3R): {price(signal['targets'][2])}\n"
        f"Risk/Reward: {signal['risk_reward']}\n\n"
        "CRT / SMC CONFIRMATION\n"
        f"CRT Range Low/High: {price(signal['crt_low'])} / {price(signal['crt_high'])}\n"
        f"1H Liquidity: {signal['liquidity_1h']}\n"
        f"15m Liquidity: {signal['liquidity_15m']}\n"
        f"Liquidity Sweep: {signal['sweep']}\n"
        f"1H Order Block: {signal['ob_1h']}\n"
        f"15m Order Block: {ob}\n"
        f"1H Fair Value Gap: {signal['fvg_1h']}\n"
        f"15m Fair Value Gap: {fvg}\n"
        f"Premium/Discount: {signal['pd_location']}\n"
        f"Dealing Range Low/High: {price(signal['dealing_low'])} / {price(signal['dealing_high'])}\n"
        f"Midpoint: {price(signal['midpoint'])}\n\n"
        "STRICT CHECKS PASSED\n"
        + "\n".join("✅ " + item for item in signal["checks"])
        + "\n\n"
        "⚠️ Rule-based educational analysis, not guaranteed. Verify market conditions. "
        "READ-ONLY: no orders are placed."
    )


# -------------------- MAIN LOOP --------------------
def main():
    global scan_status, last_scan_time, last_scan_alerts

    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Railway Variables missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )

    telegram_send(
        "👑 CRT KING AI STRICT MODE is online.\n"
        "Scanning OKX USDT perpetual markets: 4H + 1H + 15m.\n"
        "Only strict confirmed setups are alerted. READ-ONLY."
    )

    while True:
        try:
            poll_telegram_commands()
            scan_status = "Scanning"
            alerts = 0
            symbols = get_universe()

            for instrument in symbols:
                poll_telegram_commands()
                try:
                    c4h = get_candles(instrument, "4H")
                    c1h = get_candles(instrument, "1H")
                    c15 = get_candles(instrument, "15m")
                    signal = evaluate_market(instrument, c4h, c1h, c15)

                    if not signal or signal.get("direction") == "WAIT":
                        continue

                    key = (instrument, signal["direction"], signal["trade_type"])
                    now = time.time()
                    if now - last_alert.get(key, 0) < COOLDOWN_SECONDS:
                        continue

                    if telegram_send(format_alert(signal)):
                        last_alert[key] = now
                        alerts += 1
                    time.sleep(0.2)

                except Exception as error:
                    log.warning("%s scan error: %s", instrument, error)

            last_scan_alerts = alerts
            last_scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            scan_status = "Running"
            log.info("Strict scan complete: %d markets, %d alerts", len(symbols), alerts)

        except Exception as error:
            scan_status = "Error: " + str(error)
            log.exception("Scan cycle failed")

        # Poll commands every few seconds during the scan interval.
        for _ in range(max(1, SCAN_INTERVAL // 5)):
            try:
                poll_telegram_commands()
            except Exception:
                log.exception("Telegram command polling failed")
            time.sleep(5)


if __name__ == "__main__":
    main()





