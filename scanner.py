import os
import requests
import time
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_all_usdt_pairs():
    url = f"{BASE_URL}/api/v3/exchangeInfo"
    r = requests.get(url, timeout=15)
    data = r.json()
    pairs = []
    for s in data.get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            pairs.append(s["symbol"])
    return pairs

def get_klines(symbol, interval="15m", limit=25):
    url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=10)
    return r.json()

def check_volume_breakout(symbol):
    try:
        klines = get_klines(symbol)
        if not isinstance(klines, list) or len(klines) < 21:
            return None
        volumes = [float(k[5]) for k in klines[:-1]]
        avg_vol = sum(volumes[-20:]) / 20
        current_vol = float(klines[-1][5])
        if avg_vol == 0:
            return None
        ratio = current_vol / avg_vol
        if ratio < 3:
            return None
        ticker = requests.get(f"{BASE_URL}/api/v3/ticker/24hr?symbol={symbol}", timeout=10).json()
        change_24h = float(ticker["priceChangePercent"])
        quote_vol = float(ticker["quoteVolume"])
        current_price = float(ticker["lastPrice"])
        if quote_vol < 5_000_000:
            return None
        if change_24h < 2 or change_24h > 25:
            return None
        return {
            "symbol": symbol,
            "price": current_price,
            "change_24h": change_24h,
            "vol_ratio": ratio,
            "quote_vol": quote_vol,
        }
    except Exception as e:
        return None

def scan():
    pairs = get_all_usdt_pairs()
    print(f"Scanning {len(pairs)} pairs...")
    hits = []
    for symbol in pairs:
        result = check_volume_breakout(symbol)
        if result:
            hits.append(result)
        time.sleep(0.05)
    return hits

def main():
    print(f"Scanner starting at {datetime.now()}")
    hits = scan()
    print(f"Found {len(hits)} hits")
    for h in hits:
        msg = (
            f"🚨 <b>VOLUME BREAKOUT</b>\n\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> ${h['price']}\n"
            f"<b>24h Change:</b> {h['change_24h']:.2f}%\n"
            f"<b>Vol Ratio:</b> {h['vol_ratio']:.2f}x\n"
            f"<b>24h Vol:</b> ${h['quote_vol']:,.0f}\n"
            f"<b>Time:</b> {datetime.now().strftime('%H:%M:%S')}"
        )
        send_telegram(msg)

if __name__ == "__main__":
    main()
