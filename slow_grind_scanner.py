import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"
COOLDOWN_FILE = Path("grind_cooldown.json")
REJECT_FILE = Path("grind_rejections.json")
COOLDOWN_MINUTES = 60

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
    "RAVEUSDT", "SIRENUSDT", "AKEUSDT", "XPINUSDT",
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

def log_rejection(symbol, reasons, price, change_24h, reason_type):
    try:
        data = {}
        if REJECT_FILE.exists():
            try:
                data = json.loads(REJECT_FILE.read_text())
            except Exception:
                data = {}
        key = f"{symbol}"
        if key not in data:
            data[key] = {
                "symbol": symbol,
                "first_seen": datetime.utcnow().isoformat(),
                "count": 0,
                "type": reason_type,
                "price": price,
                "change_24h": change_24h,
            }
        data[key]["count"] += 1
        data[key]["last_seen"] = datetime.utcnow().isoformat()
        data[key]["price"] = price
        data[key]["change_24h"] = change_24h
        data[key]["top_reason"] = reasons[0] if reasons else "unknown"
        data[key]["all_reasons"] = reasons[:3]
        if len(data) > 200:
            sorted_items = sorted(data.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
            data = dict(sorted_items[:200])
        REJECT_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"reject log error: {e}")

def btc_is_healthy():
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return True
        current_close = float(data[-1][4])
        prev_close = float(data[-2][4])
        if prev_close == 0:
            return True
        change = ((current_close - prev_close) / prev_close) * 100
        print(f"BTC 1h change: {change:.2f}%")
        return change > -2
    except Exception:
        return True

def get_candidates():
    url = f"{BASE_URL}/api/v3/ticker/24hr"
    r = requests.get(url, timeout=20)
    tickers = r.json()
    candidates = []
    rejected = {"vol_low": 0, "change_low": 0, "change_high": 0, "price_high": 0}
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
        if quote_vol < 5_000_000:
            rejected["vol_low"] += 1
            continue
        if change < 3:
            rejected["change_low"] += 1
            continue
        if change > 150:
            rejected["change_high"] += 1
            continue
        if price > 1.00:
            rejected["price_high"] += 1
            continue
        candidates.append({
            "symbol": symbol,
            "price": price,
            "change_24h": change,
            "quote_vol": quote_vol,
        })
    print(f"Stage1 rejections: {rejected}")
    return candidates

def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
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
        low, high = price * 0.98, price * 1.02
        bid_depth = sum(float(b[1]) * float(b[0]) for b in book.get("bids", []) if float(b[0]) >= low)
        ask_depth = sum(float(a[1]) * float(a[0]) for a in book.get("asks", []) if float(a[0]) <= high)
        return bid_depth, ask_depth
    except Exception:
        return 0, 0

def check_grind(symbol, price):
    reasons = []
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=25"
        r = requests.get(url, timeout=10)
        klines = r.json()
        if not isinstance(klines, list) or len(klines) < 21:
            return None, ["not_enough_klines"]

        current_close = float(klines[-1][4])
        price_1h_ago = float(klines[-5][4])
        change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100
        # FIXED: 1h cap now 0.3 - 4%
        if change_1h < 0.3 or change_1h > 4:
            reasons.append(f"1h_{change_1h:.1f}%")

        price_4h_ago = float(klines[-17][4])
        change_4h = ((current_close - price_4h_ago) / price_4h_ago) * 100
        if change_4h < 3 or change_4h > 60:
            reasons.append(f"4h_{change_4h:.1f}%")

        current_open = float(klines[-1][1])
        if current_close <= current_open:
            reasons.append("red_candle")

        recent_1h_vol = sum(float(k[5]) for k in klines[-4:])
        prior_vols = [float(k[5]) for k in klines[-20:-4]]
        avg_1h_vol = sum(prior_vols) / len(prior_vols) * 4 if prior_vols else 0
        if avg_1h_vol == 0:
            return None, ["avg_vol_zero"]
        vol_ratio_1h = recent_1h_vol / avg_1h_vol
        if vol_ratio_1h < 1.5:
            reasons.append(f"1hvol_{vol_ratio_1h:.1f}x")
        if vol_ratio_1h > 5:
            reasons.append(f"too_fast_{vol_ratio_1h:.1f}x")

        greens = sum(1 for k in klines[-4:] if float(k[4]) > float(k[1]))
        if greens < 2:
            reasons.append(f"greens_{greens}")

        closes = [float(k[4]) for k in klines]
        rsi = compute_rsi(closes, 14)
        if rsi < 55:
            reasons.append(f"RSI_low_{rsi:.1f}")
        if rsi > 72:
            reasons.append(f"RSI_high_{rsi:.1f}")

        total_vol = float(klines[-1][5])
        taker_buy = float(klines[-1][9])
        if total_vol == 0:
            reasons.append("vol_zero")
        taker_pct = taker_buy / total_vol if total_vol else 0
        if taker_pct < 0.55:
            reasons.append(f"taker_{taker_pct*100:.1f}%")

        bid_depth, ask_depth = check_depth(symbol, price)
        if bid_depth < 50_000:
            reasons.append(f"bid_thin_{bid_depth:.0f}")
        if ask_depth < 50_000:
            reasons.append(f"ask_thin_{ask_depth:.0f}")

        if reasons:
            return None, reasons

        return {
            "change_1h": change_1h, "change_4h": change_4h,
            "vol_ratio_1h": vol_ratio_1h, "greens": greens,
            "rsi": rsi, "taker_pct": taker_pct * 100,
            "bid_depth": bid_depth, "ask_depth": ask_depth,
        }, []
    except Exception as e:
        return None, [f"exception_{e}"]

def scan():
    candidates = get_candidates()
    print(f"Candidates after Stage1: {len(candidates)}")
    cooldown = load_cooldown()
    print(f"Cooldown: {list(cooldown.keys())}")
    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(check_grind, c["symbol"], c["price"]): c for c in candidates}
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
                    top = reasons[0].split("_")[0]
                    rejection[top] = rejection.get(top, 0) + 1
                    log_rejection(c["symbol"], reasons, c["price"], c["change_24h"], "grind")
    print(f"Stage2 rejection: {rejection}")
    now = datetime.utcnow()
    for h in hits:
        cooldown[h["symbol"]] = now.isoformat()
    save_cooldown(cooldown)
    return hits

def get_session_label(hour, minute):
    if hour == 5 and minute >= 30:
        return "Asia"
    if 6 <= hour <= 10:
        return "Asia"
    if hour == 11 and minute < 30:
        return "Asia"
    if hour == 12 and minute >= 30:
        return "Europe"
    if 13 <= hour <= 14:
        return "Europe"
    if hour == 15 and minute < 30:
        return "Europe"
    if hour == 18 and minute >= 30:
        return "US"
    if 19 <= hour <= 20:
        return "US"
    if hour == 21 and minute < 30:
        return "US"
    return "Dead-Zone"

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour, ist.minute)
    print(f"Slow Grind Scanner starting at {ist} IST — Session: {session}")

    if not btc_is_healthy():
        print("BTC dumping >2%. Skipping.")
        return

    hits = scan()
    print(f"Found {len(hits)} slow grind signals")
    for h in hits:
        stop = h["price"] * 0.97
        msg = (
            f"📈 <b>SLOW GRIND DETECTED</b> [{session}]\n\n"
            f"<b>Type:</b> ACCUMULATION (4-6h grind)\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])}\n"
            f"<b>24h:</b> {h['change_24h']:.2f}%\n"
            f"<b>1h:</b> {h['change_1h']:.2f}%\n"
            f"<b>4h:</b> {h['change_4h']:.2f}%\n"
            f"<b>RSI:</b> {h['rsi']:.1f}\n"
            f"<b>Vol (1h):</b> {h['vol_ratio_1h']:.2f}x\n"
            f"<b>Greens:</b> {h['greens']}/4\n"
            f"<b>Taker:</b> {h['taker_pct']:.1f}%\n"
            f"<b>Bid:</b> ${h['bid_depth']:,.0f}\n"
            f"<b>Ask:</b> ${h['ask_depth']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"📋 <b>PLAN</b>\n"
            f"Entry: {format_price(h['price'])}\n"
            f"Stop: {format_price(stop)} (-3%)\n"
            f"Trail: +3%→BE, +5%→+2%, +10%→+6%, +25%→+18%\n\n"
            f"⚠️ Check tag: Seed(half) / Monitoring(skip)"
        )
        send_telegram(msg)

if __name__ == "__main__":
    main()
