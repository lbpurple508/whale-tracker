import os
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"

# Filter out tokenized stocks (they end with B before USDT)
STOCK_SUFFIXES = ("BUSDT", "BUSD")

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
        # Skip tokenized stocks like SNDKBUSDT, SOXLBUSDT
        if symbol.endswith("BUSDT"):
            continue
        # Skip obvious non-crypto
        skip = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
        if symbol.endswith(skip):
            continue
        try:
            quote_vol = float(t["quoteVolume"])
            change = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
        except (KeyError, ValueError):
            continue
        if quote_vol < 5_000_000:
            continue
        if change < 2 or change > 25:
            continue
        candidates.append({
            "symbol": symbol,
            "price": price,
            "change_24h": change,
            "quote_vol": quote_vol,
        })
    return candidates

def check_volume(symbol):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=25"
        r = requests.get(url, timeout=10)
        klines = r.json()
        if not isinstance(klines, list) or len(klines) < 21:
            return None
        volumes = [float(k[5]) for k in klines[:-1]]
        avg = sum(volumes[-20:]) / 20
        current = float(klines[-1][5])
        if avg == 0:
            return None
        return current / avg
    except Exception:
        return None

def scan():
    candidates = get_candidates()
    print(f"Candidates after filter: {len(candidates)}")
    hits = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_volume, c["symbol"]): c for c in candidates}
        for future in as_completed(futures):
            c = futures[future]
            ratio = future.result()
            if ratio and ratio >= 3:
                c["vol_ratio"] = ratio
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
            f"<b>Price:</b> ${h['price']}\n"
            f"<b>24h Change:</b> {h['change_24h']:.2f}%\n"
            f"<b>Vol Ratio:</b> {h['vol_ratio']:.2f}x\n"
            f"<b>24h Vol:</b> ${h['quote_vol']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST"
        )
        send_telegram(msg)

if __name__ == "__main__":
    main()
