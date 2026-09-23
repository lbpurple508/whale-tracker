import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"
COOLDOWN_FILE = Path("cooldown.json")
COOLDOWN_MINUTES = 30

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
    "HBARUSDT",
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

def load_cooldown():
    if COOLDOWN_FILE.exists():
        try:
            data = json.loads(COOLDOWN_FILE.read_text())
            now = datetime.utcnow()
            cleaned = {}
            for sym, ts in data.items():
                try:
                    t = datetime.fromisoformat(ts)
                    if (now - t).total_seconds() < COOLDOWN_MINUTES * 60:
                        cleaned[sym] = ts
                except Exception:
                    pass
            return cleaned
        except Exception:
            return {}
    return {}

def save_cooldown(data):
    try:
        COOLDOWN_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"cooldown save error: {e}")

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
        # Widened: 1-80% (was 1-50%)
        if change < 1 or change > 80:
            continue
        if price > 1.00:
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
    reasons = []
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=25"
        r = requests.get(url, timeout=10)
        klines = r.json()
        if not isinstance(klines, list) or len(klines) < 21:
            return None, ["not enough klines"]

        # Volume: check CURRENT or PREVIOUS candle for spike
        volumes = [float(k[5]) for k in klines[:-2]]
        avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        current_vol = float(klines[-1][5])
        prev_vol = float(klines[-2][5])
        if avg == 0:
            return None, ["avg vol zero"]
        current_ratio = current_vol / avg
        prev_ratio = prev_vol / avg
        vol_ratio = max(current_ratio, prev_ratio)
        if vol_ratio < 5:
            reasons.append(f"vol {vol_ratio:.1f}x")

        # Green candle: current OR previous
        current_open = float(klines[-1][1])
        current_close = float(klines[-1][4])
        prev_open = float(klines[-2][1])
        prev_close = float(klines[-2][4])
        current_green = current_close > current_open
        prev_green = prev_close > prev_open
        if not (current_green or prev_green):
            reasons.append("both candles red")

        # 1h change: widened to 0.5-60%
        if len(klines) >= 5:
            price_1h_ago = float(klines[-5][4])
            change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100
        else:
            change_1h = 0
        if change_1h < 0.5 or change_1h > 60:
            reasons.append(f"1h {change_1h:.1f}%")

        # 4h change: 80%
        if len(klines) >= 17:
            price_4h_ago = float(klines[-17][4])
            change_4h = ((current_close - price_4h_ago) / price_4h_ago) * 100
        else:
            change_4h = 0
        if change_4h > 80:
            reasons.append(f"4h {change_4h:.1f}%")

        # RSI under 72
        closes = [float(k[4]) for k in klines]
        rsi = compute_rsi(closes, 14)
        if rsi > 72:
            reasons.append(f"RSI {rsi:.1f}")

        # Taker Buy 60%+
        total_vol = float(klines[-1][5])
        taker_buy = float(klines[-1][9])
        if total_vol == 0:
            reasons.append("vol zero")
        taker_buy_pct = taker_buy / total_vol if total_vol else 0
        if taker_buy_pct < 0.60:
            reasons.append(f"taker {taker_buy_pct*100:.1f}%")

        # Depth $50k
        bid_depth, ask_depth = check_depth(symbol, price)
        if bid_depth < 50_000:
            reasons.append(f"bid ${bid_depth:,.0f}")
        if ask_depth < 50_000:
            reasons.append(f"ask ${ask_depth:,.0f}")
        if ask_depth > 0 and bid_depth / ask_depth < 0.6:
            reasons.append("bid/ask")

        if reasons:
            return None, reasons

        return {
            "vol_ratio": vol_ratio,
            "taker_buy_pct": taker_buy_pct * 100,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "change_1h": change_1h,
            "change_4h": change_4h,
            "rsi": rsi,
        }, []
    except Exception as e:
        return None, [f"exception {e}"]

def scan():
    candidates = get_candidates()
    print(f"Candidates after filter: {len(candidates)}")
    cooldown = load_cooldown()
    print(f"Cooldown active: {list(cooldown.keys())}")
    hits = []
    rejection_summary = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(check_signal, c["symbol"], c["price"]): c for c in candidates}
        for future in as_completed(futures):
            c = futures[future]
            if c["symbol"] in cooldown:
                continue
            result, reasons = future.result()
            if result:
                c.update(result)
                hits.append(c)
            else:
                if reasons:
                    top = reasons[0].split()[0]
                    rejection_summary[top] = rejection_summary.get(top, 0) + 1
    print(f"Rejection reasons: {rejection_summary}")
    now = datetime.utcnow()
    for h in hits:
        cooldown[h["symbol"]] = now.isoformat()
    save_cooldown(cooldown)
    return hits

def is_active_session(hour, minute):
    if hour == 5 and minute >= 30:
        return True, "Asia"
    if 6 <= hour <= 10:
        return True, "Asia"
    if hour == 11 and minute < 30:
        return True, "Asia"
    if hour == 12 and minute >= 30:
        return True, "Europe"
    if 13 <= hour <= 14:
        return True, "Europe"
    if hour == 15 and minute < 30:
        return True, "Europe"
    return False, "Off-hours"

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    active, session = is_active_session(ist.hour, ist.minute)
    print(f"Scanner starting at {ist} IST — Session: {session} — Active: {active}")

    if not active:
        print("Outside fresh cycle window. Skipping.")
        return

    hits = scan()
    print(f"Found {len(hits)} hits")
    for h in hits:
        msg = (
            f"🚨 <b>VOLUME BREAKOUT</b> [{session}]\n\n"
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
