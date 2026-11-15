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
exchange.load_markets()
SYMBOL = 'BTC/USDT:USDT'
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

    previous = df.iloc[-3]
    current = df.iloc[-2]

    price = current['close']

    print("\nBTC PRICE:", price)

# LIQUIDITY SWEEP DETECTION

recent = df.iloc[-12:-2]

swing_high = recent['high'].max()
swing_low = recent['low'].min()

bullish_sweep = (
    current['low'] < swing_low
    and current['close'] > swing_low
)

bearish_sweep = (
    current['high'] > swing_high
    and current['close'] < swing_high
)

print("Liquidity High:", swing_high)
print("Liquidity Low:", swing_low)

print("Bullish Sweep:", bullish_sweep)
print("Bearish Sweep:", bearish_sweep)
    


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
