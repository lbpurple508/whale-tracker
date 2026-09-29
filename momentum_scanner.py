import os
import json
import html
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"
COOLDOWN_FILE = Path("momentum_cooldown.json")
REJECT_FILE = Path("momentum_rejections.json")
HISTORY_FILE = Path("momentum_history.json")
SIGNALS_FILE = Path("momentum_signals.json")
COOLDOWN_MINUTES = 45
HISTORY_HOURS = 4

MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}

BLACKLIST = {
    "GPSUSDT", "SHELLUSDT", "ENAUSDT", "ZKJUSDT", "KOGEUSDT",
    "COAIUSDT", "SAITAMAUSDT", "ROBOUSDT", "VZZNUSDT", "LABUSDT",
    "RAVEUSDT", "SIRENUSDT", "AKEUSDT", "XPINUSDT",
    "BTRUSDT", "ANTHROPICUSDT", "SKHYNIXUSDT", "REUSDT", "SNDKUSDT",
    "XPLUSDT",
}

MONITORING_BLACKLIST = {
    "RAREUSDT", "ARKUSDT", "WIFUSDT", "QIUSDT", "MOVEUSDT",
    "STXUSDT", "LSKUSDT", "SYNUSDT", "MOVRUSDT", "NOMUSDT",
    "JASMYUSDT", "TLMUSDT", "GLMRUSDT", "QUICKUSDT", "ACTUSDT",
    "BLURUSDT", "RESOLVUSDT", "AVAUSDT", "DODOUSDT", "PORTALUSDT",
    "VELODROMEUSDT", "EPICUSDT", "SOPHUSDT", "AWEUSDT", "SCRUSDT",
    "HEIUSDT", "TOWNSUSDT", "GTCUSDT", "FTTUSDT", "COOKIEUSDT",
    "QKCUSDT", "GNSUSDT",
}


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_price(p):
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram HTTP error: {response.status_code} - {response.text}")
            return
        body = response.json()
        if not body.get("ok"):
            print(f"Telegram API error: {body}")
    except Exception as e:
        print(f"Telegram error: {e}")


def load_json(path):
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_json(path, data):
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception as e:
        print(f"save error: {e}")


def parse_ts_safe(ts_str):
    try:
        t = datetime.fromisoformat(ts_str)
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return t
    except Exception:
        return None


def load_cooldown():
    data = load_json(COOLDOWN_FILE)
    now = now_utc()
    cleaned = {}
    for sym, ts in data.items():
        if not isinstance(ts, str):
            continue
        t = parse_ts_safe(ts)
        if t is None:
            continue
        if (now - t).total_seconds() < COOLDOWN_MINUTES * 60:
            cleaned[sym] = ts
    return cleaned


def get_alert_count(symbol):
    history = load_json(HISTORY_FILE)
    now = now_utc()
    entry = history.get(symbol)
    if not isinstance(entry, dict):
        return 0
    events = entry.get("events", [])
    if not isinstance(events, list):
        return 0
    count = 0
    for ts_str in events:
        if not isinstance(ts_str, str):
            continue
        t = parse_ts_safe(ts_str)
        if t is None:
            continue
        if (now - t).total_seconds() < HISTORY_HOURS * 3600:
            count += 1
    return count


def record_alert(symbol):
    history = load_json(HISTORY_FILE)
    now = now_utc()
    if symbol not in history or not isinstance(history[symbol], dict):
        history[symbol] = {"events": []}
    if not isinstance(history[symbol].get("events"), list):
        history[symbol]["events"] = []
    history[symbol]["events"].append(now.isoformat())
    cleaned = []
    for ts in history[symbol]["events"]:
        if not isinstance(ts, str):
            continue
        t = parse_ts_safe(ts)
        if t is None:
            continue
        if (now - t).total_seconds() < HISTORY_HOURS * 3600:
            cleaned.append(ts)
    history[symbol]["events"] = cleaned
    if len(history) > 300:
        sorted_items = sorted(
            history.items(),
            key=lambda x: x[1]["events"][-1] if isinstance(x[1], dict) and x[1].get("events") else "",
            reverse=True
        )
        history = dict(sorted_items[:300])
    save_json(HISTORY_FILE, history)


def log_signal(symbol, data, session):
    try:
        signals = load_json(SIGNALS_FILE)
        if not isinstance(signals.get("signals"), list):
            signals["signals"] = []
        signals["signals"].append({
            "ts": now_utc().isoformat(),
            "session": session,
            "symbol": symbol,
            "price": data.get("price"),
            "change_24h": data.get("change_24h"),
            "change_1h": data.get("change_1h"),
            "change_4h": data.get("change_4h"),
            "rsi": data.get("rsi"),
            "vol_ratio": data.get("vol_ratio"),
            "taker_pct": data.get("taker_pct"),
            "bid_depth": data.get("bid_depth"),
            "ask_depth": data.get("ask_depth"),
            "bid_ask_ratio": data.get("bid_ask_ratio"),
            "above_ma7": data.get("above_ma7"),
            "above_ma25": data.get("above_ma25"),
        })
        if len(signals["signals"]) > 500:
            signals["signals"] = signals["signals"][-500:]
        save_json(SIGNALS_FILE, signals)
    except Exception as e:
        print(f"log_signal error: {e}")


def log_rejection(symbol, reasons, price, change_24h):
    try:
        data = load_json(REJECT_FILE)
        if symbol not in data or not isinstance(data.get(symbol), dict):
            data[symbol] = {"symbol": symbol, "count": 0, "price": price, "change_24h": change_24h}
        data[symbol]["count"] += 1
        data[symbol]["last_seen"] = now_utc().isoformat()
        data[symbol]["price"] = price
        data[symbol]["change_24h"] = change_24h
        data[symbol]["top_reason"] = reasons[0] if reasons else "unknown"
        if len(data) > 200:
            valid = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("last_seen")}
            sorted_items = sorted(valid.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
            data = dict(sorted_items[:200])
        save_json(REJECT_FILE, data)
    except Exception as e:
        print(f"reject log error: {e}")


def btc_is_healthy():
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=3"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 3:
            return True
        current_close = float(data[-2][4])
        prev_close = float(data[-3][4])
        if prev_close == 0:
            return True
        change = ((current_close - prev_close) / prev_close) * 100
        print(f"BTC 1h change: {change:.2f}%")
        return change > -2
    except Exception:
        return True


def get_candidates():
    try:
        url = f"{BASE_URL}/api/v3/ticker/24hr"
        r = requests.get(url, timeout=20)
        tickers = r.json()
    except Exception as e:
        print(f"ticker fetch failed: {e}")
        return []
    if not isinstance(tickers, list):
        print(f"Unexpected tickers response: {tickers}")
        return []
    candidates = []
    rejected = {"vol_low": 0, "change_range": 0, "price_high": 0, "major": 0, "blacklist": 0, "monitoring": 0}
    for t in tickers:
        if not isinstance(t, dict):
            continue
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol in MAJORS:
            rejected["major"] += 1
            continue
        if symbol in BLACKLIST:
            rejected["blacklist"] += 1
            continue
        if symbol in MONITORING_BLACKLIST:
            rejected["monitoring"] += 1
            continue
        if symbol.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        try:
            quote_vol = float(t["quoteVolume"])
            change = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue
        if quote_vol < 10_000_000:
            rejected["vol_low"] += 1
            continue
        if change < 5 or change > 35:
            rejected["change_range"] += 1
            continue
        if price > 1.00 or price <= 0:
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
    if avg_loss == 0 and avg_gain == 0:
        return 50
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def check_depth(symbol, price):
    try:
        url = f"{BASE_URL}/api/v3/depth?symbol={symbol}&limit=500"
        r = requests.get(url, timeout=10)
        book = r.json()
        if not isinstance(book, dict):
            return 0, 0
        low, high = price * 0.98, price * 1.02
        bid_depth = sum(float(b[1]) * float(b[0]) for b in book.get("bids", []) if float(b[0]) >= low)
        ask_depth = sum(float(a[1]) * float(a[0]) for a in book.get("asks", []) if float(a[0]) <= high)
        return bid_depth, ask_depth
    except Exception:
        return 0, 0


def check_momentum(symbol):
    reasons = []
    try:
        url_4h = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=4h&limit=50"
        r = requests.get(url_4h, timeout=10)
        k4 = r.json()
        if not isinstance(k4, list) or len(k4) < 30:
            return None, ["not_enough_4h"]

        completed_4h = k4[-2]
        close_now = float(completed_4h[4])
        if close_now <= 0:
            return None, ["zero_price"]

        close_4h_ago = float(k4[-3][4])
        change_4h = ((close_now - close_4h_ago) / close_4h_ago) * 100
        if change_4h < 3 or change_4h > 40:
            reasons.append(f"4h_{change_4h:.1f}%")

        close_24h_ago = float(k4[-8][4])
        change_24h = ((close_now - close_24h_ago) / close_24h_ago) * 100
        if change_24h < 5 or change_24h > 35:
            reasons.append(f"24h_{change_24h:.1f}%")

        closes_4h = [float(k[4]) for k in k4[:-1]]
        rsi_4h = compute_rsi(closes_4h, 14)
        if rsi_4h < 35 or rsi_4h > 75:
            reasons.append(f"RSI_{rsi_4h:.1f}")

        ma7 = sum(closes_4h[-7:]) / 7 if len(closes_4h) >= 7 else 0
        ma25 = sum(closes_4h[-25:]) / 25 if len(closes_4h) >= 25 else 0
        above_ma7 = close_now > ma7 if ma7 > 0 else False
        above_ma25 = close_now > ma25 if ma25 > 0 else False
        if not above_ma7:
            reasons.append("below_ma7")
        if not above_ma25:
            reasons.append("below_ma25")

        url_15m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=30"
        r = requests.get(url_15m, timeout=10)
        k15 = r.json()
        if not isinstance(k15, list) or len(k15) < 25:
            return None, ["not_enough_15m"]

        last_4_vols = [float(k[5]) for k in k15[-5:-1]]
        recent_1h_vol = sum(last_4_vols)
        prior_vols = [float(k[5]) for k in k15[-25:-5]]
        avg_1h_vol = sum(prior_vols) / len(prior_vols) * 4 if prior_vols else 0
        if avg_1h_vol == 0:
            return None, ["avg_vol_zero"]
        vol_ratio = recent_1h_vol / avg_1h_vol
        if vol_ratio < 1.5:
            reasons.append(f"vol_{vol_ratio:.1f}x")

        recent_1h_usdt = sum(float(k[7]) for k in k15[-5:-1])
        if recent_1h_usdt < 1_000_000:
            reasons.append(f"flow_{recent_1h_usdt/1e6:.2f}M")

        close_1h_ago = float(k15[-6][4])
        change_1h = ((close_now - close_1h_ago) / close_1h_ago) * 100

        completed_15m = k15[-2]
        total_vol_15m = float(completed_15m[5])
        taker_buy = float(completed_15m[9])
        taker_pct = taker_buy / total_vol_15m if total_vol_15m else 0
        if taker_pct < 0.45:
            reasons.append(f"taker_{taker_pct*100:.1f}%")

        bid_depth, ask_depth = check_depth(symbol, close_now)
        bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 0
        if bid_depth < 30_000:
            reasons.append(f"bid_low_{bid_depth:.0f}")
        if bid_ask_ratio < 0.7:
            reasons.append(f"ratio_{bid_ask_ratio:.2f}")

        if reasons:
            return None, reasons

        return {
            "price": close_now,
            "change_24h": change_24h,
            "change_4h": change_4h,
            "change_1h": change_1h,
            "rsi": rsi_4h,
            "vol_ratio": vol_ratio,
            "taker_pct": taker_pct * 100,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "bid_ask_ratio": bid_ask_ratio,
            "above_ma7": above_ma7,
            "above_ma25": above_ma25,
        }, []
    except Exception as e:
        return None, [f"exception_{e}"]


def scan(session):
    candidates = get_candidates()
    print(f"Candidates after Stage1: {len(candidates)}")
    cooldown = load_cooldown()
    active_candidates = [c for c in candidates if c["symbol"] not in cooldown]
    print(f"After cooldown filter: {len(active_candidates)}")
    hits = []
    rejection_summary = {}
    if not active_candidates:
        return hits
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(check_momentum, c["symbol"]): c for c in active_candidates}
        for future in as_completed(futures):
            c = futures[future]
            result, reasons = future.result()
            if result:
                c.update(result)
                hits.append(c)
            else:
                if reasons:
                    top = reasons[0].split("_")[0]
                    rejection_summary[top] = rejection_summary.get(top, 0) + 1
                    log_rejection(c["symbol"], reasons, c["price"], c["change_24h"])
    print(f"Stage2 rejection: {rejection_summary}")
    now = now_utc()
    for h in hits:
        cooldown[h["symbol"]] = now.isoformat()
    save_json(COOLDOWN_FILE, cooldown)
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
    ist = now_utc() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour, ist.minute)
    print(f"Momentum Scanner starting at {ist} IST — Session: {session}")

    if not SIGNALS_FILE.exists():
        SIGNALS_FILE.write_text('{"signals": []}')
    else:
        try:
            data = json.loads(SIGNALS_FILE.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("signals"), list):
                SIGNALS_FILE.write_text('{"signals": []}')
        except Exception:
            SIGNALS_FILE.write_text('{"signals": []}')

    if not btc_is_healthy():
        print("BTC dumping >2%. Skipping.")
        return

    hits = scan(session)
    print(f"Found {len(hits)} signals")
    for h in hits:
        count = get_alert_count(h["symbol"]) + 1
        record_alert(h["symbol"])
        log_signal(h["symbol"], h, session)
        stop = h["price"] * 0.97

        if count == 1:
            header = f"🚀 MOMENTUM DETECTED [{session}]"
            action = "✅ ENTER NOW — Buy at signal price. SL -3%."
        elif count == 2:
            header = f"🚀🚀 MOMENTUM DETECTED — 2ND [{session}]"
            action = "✅ ADD MORE — Buy another $5."
        else:
            header = f"🔥 MOMENTUM DETECTED — {count}X [{session}]"
            action = f"✅ ADD MORE — Confirmation #{count}. Buy another $5."

        msg = (
            f"{header}\n\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])}\n"
            f"<b>24h:</b> {h['change_24h']:.2f}%\n"
            f"<b>4h:</b> {h['change_4h']:.2f}%\n"
            f"<b>1h:</b> {h['change_1h']:.2f}%\n"
            f"<b>RSI(4h):</b> {h['rsi']:.1f}\n"
            f"<b>Volume:</b> {h['vol_ratio']:.2f}x normal\n"
            f"<b>Buyers:</b> {h['taker_pct']:.1f}%\n"
            f"<b>Bid Depth:</b> ${h['bid_depth']:,.0f}\n"
            f"<b>Ask Depth:</b> ${h['ask_depth']:,.0f}\n"
            f"<b>Bid/Ask:</b> {h['bid_ask_ratio']:.2f}\n"
            f"<b>Above MA7:</b> {'Yes' if h['above_ma7'] else 'No'}\n"
            f"<b>Above MA25:</b> {'Yes' if h['above_ma25'] else 'No'}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"<b>Stop Loss:</b> {format_price(stop)} (-3%)\n"
            f"<b>Trail:</b> +3%→BE, +5%→+2%, +10%→+6%, +25%→+18%\n\n"
            f"{action}"
        )
        send_telegram(msg)


def safe_main():
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 MOMENTUM SCANNER CRASHED\n\nError: {html.escape(str(e)[:300])}")
        raise


if __name__ == "__main__":
    safe_main()
