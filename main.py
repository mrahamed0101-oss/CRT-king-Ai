import ccxt
import pandas as pd
import time

print("================================")
print("       CRT KING AI BOT")
print("================================")

exchange = ccxt.binanceusdm({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})

exchange.set_sandbox_mode(True)

SYMBOL = 'BTC/USDT'
TIMEFRAME = '15m'

def analyze_market():

    candles = exchange.fetch_ohlcv(
        SYMBOL,
        TIMEFRAME,
        limit=50
    )

    df = pd.DataFrame(
        candles,
        columns=[
            'time',
            'open',
            'high',
            'low',
            'close',
            'volume'
        ]
    )

    df['EMA9'] = df['close'].ewm(
        span=9
    ).mean()

    df['EMA20'] = df['close'].ewm(
        span=20
    ).mean()

    previous = df.iloc[-2]
    current = df.iloc[-1]

    price = current['close']

    print("\nBTC PRICE:", price)

    bullish_sweep = (
        current['low'] < previous['low']
        and current['close'] > previous['low']
    )

    bearish_sweep = (
        current['high'] > previous['high']
        and current['close'] < previous['high']
    )

    if (
        bullish_sweep
        and current['EMA9'] > current['EMA20']
    ):
        print("SIGNAL: LONG")

    elif (
        bearish_sweep
        and current['EMA9'] < current['EMA20']
    ):
        print("SIGNAL: SHORT")

    else:
        print("SIGNAL: WAIT")

while True:

    try:
        analyze_market()

    except Exception as error:
        print("ERROR:", error)

    print("\nNext scan in 60 seconds...")
    time.sleep(60)
