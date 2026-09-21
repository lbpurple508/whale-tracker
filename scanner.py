import os
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"

MAJORS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "AVAXUSDT", "DOTUSDT",
    "MATICUSDT", "LTCUSDT", "LINKUSDT", "TONUSDT", "SHIBUSDT",
    "BCHUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT",
    "APTUSDT", "NEARUSDT", "ICPUSDT", "VETUSDT", "OPUSDT",
    "ARBUSDT", "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT",
    "XLMUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT",
    "STXUSDT", "IMXUSDT", "RUNEUSDT", "AAVEUSDT", "MKRUSDT",
    "GRTUSDT", "SANDUSDT", "MANAUSDT", "AXSUSDT", "CRVUSDT",
    "ALGOUSDT", "EGLDUSDT", "FTMUSDT", "THETAUSDT", "FLOWUSDT",
}

BLACKLIST = {
    "GPSUSDT", "SHELLUSDT", "ENAUSDT", "ZKJUSDT", "KOGEUSDT",
    "COAIUSDT", "SAITAMAUSDT", "ROBOUSDT", "VZZNUSDT", "LABUSDT",
    "RAVEUSDT", "BROCCOLIUSDT", "SIRENUSDT", "AKEUSDT", "XPINUSDT",
    "BTRUSDT", "ANTHROPICUSDT", "SKHYNIXUSDT", "REUSDT", "SNDKUSDT",
    "XPLUSDT",
}

def format_price(p):
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_candidates():
    url = f"{BASE_URL}/api/v3/ticker/24hr"
    r = requests.get(url, timeout=20)
    tickers = r.json()
    candidates = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol in MAJORS:
            continue
        if symbol in BLACKLIST:
            continue
        if symbol.endswith("BUSDT"):
            continue
        if symbol.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        try:
            quote_vol = float(t["quoteVolume"])
            change = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
        except (KeyError, ValueError):
            continue
        if quote_vol < 10_000_000:
            continue
        # Wider 24h range: allow up to 30% (fresh pumps can be high)
        if change < 2 or change > 30:
            continue
        candidates.append({
            "symbol": symbol,
            "price": price,
            "change_24h": change,
            "quote_vol": quote_vol,
        })
    return candidates

def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def check_depth(symbol, price):
    try:
        url = f"{BASE_URL}/api/v3/depth?symbol={symbol}&limit=100"
        r = requests.get(url, timeout=10)
        book = r.json()
        low = price * 0.98
        high = price * 1.02
        bid_depth = sum(float(b[1]) * float(b[0]) for b in book.get("bids", []) if float(b[0]) >= low)
        ask_depth = sum(float(a[1]) * float(a[0]) for a in book.get("asks", []) if float(a[0]) <= high)
        return bid_depth, ask_depth
    except Exception:
        return 0, 0

def check_signal(symbol, price):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=25"
        r = requests.get(url, timeout=10)
        klines = r.json()
        if not isinstance(klines, list) or len(klines) < 21:
            return None

        # Volume filter
        volumes = [float(k[5]) for k in klines[:-1]]
        avg = sum(volumes[-20:]) / 20
        current_vol = float(klines[-1][5])
        if avg == 0:
            return None
        vol_ratio = current_vol / avg
        if vol_ratio < 4:
            return None

        # Green candle
        current_open = float(klines[-1][1])
        current_close = float(klines[-1][4])
        if current_close <= current_open:
            return None

        # 1h change (4 candles back)
        if len(klines) >= 5:
            price_1h_ago = float(klines[-5][4])
            change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100
        else:
            change_1h = 0
        # 1h change must be positive and not exhausted
        if change_1h < 1 or change_1h > 20:
            return None

        # 4h change (16 candles back)
        if len(klines) >= 17:
            price_4h_ago = float(klines[-17][4])
            change_4h = ((current_close - price_4h_ago) / price_4h_ago) * 100
        else:
            change_4h = 0
        if change_4h > 40:
            return None

        # RSI check (bot-side, under 75)
        closes = [float(k[4]) for k in klines]
        rsi = compute_rsi(closes, 14)
        if rsi > 75:
            return None

        # Taker buy %
        total_vol = float(klines[-1][5])
        taker_buy = float(klines[-1][9])
        if total_vol == 0:
            return None
        taker_buy_pct = taker_buy / total_vol
        if taker_buy_pct < 0.55:
            return None

        # Depth
        bid_depth, ask_depth = check_depth(symbol, price)
        if bid_depth < 20000 or ask_depth < 20000:
            return None
        if bid_depth / ask_depth < 0.7:
            return None

        return {
            "vol_ratio": vol_ratio,
            "taker_buy_pct": taker_buy_pct * 100,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "change_1h": change_1h,
            "change_4h": change_4h,
            "rsi": rsi,
        }
    except Exception:
        return None

def scan():
    candidates = get_candidates()
    print(f"Candidates after filter: {len(candidates)}")
    hits = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(check_signal, c["symbol"], c["price"]): c for c in candidates}
        for future in as_completed(futures):
            c = futures[future]
            result = future.result()
            if result:
                c.update(result)
                hits.append(c)
    return hits

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    print(f"Scanner starting at {ist} IST")
    hits = scan()
    print(f"Found {len(hits)} hits")
    for h in hits:
        msg = (
            f"🚨 <b>VOLUME BREAKOUT</b>\n\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])}\n"
            f"<b>24h Change:</b> {h['change_24h']:.2f}%\n"
            f"<b>1h Change:</b> {h['change_1h']:.2f}%\n"
            f"<b>4h Change:</b> {h['change_4h']:.2f}%\n"
            f"<b>RSI:</b> {h['rsi']:.1f}\n"
            f"<b>Vol Ratio:</b> {h['vol_ratio']:.2f}x\n"
            f"<b>Taker Buy:</b> {h['taker_buy_pct']:.1f}%\n"
            f"<b>Bid Depth:</b> ${h['bid_depth']:,.0f}\n"
            f"<b>Ask Depth:</b> ${h['ask_depth']:,.0f}\n"
            f"<b>24h Vol:</b> ${h['quote_vol']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST"
        )
        send_telegram(msg)

if __name__ == "__main__":
    main()
