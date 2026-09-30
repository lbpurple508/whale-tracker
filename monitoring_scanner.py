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
COOLDOWN_FILE = Path("monitor_cooldown.json")
REJECT_FILE = Path("monitor_rejections.json")
HISTORY_FILE = Path("monitor_history.json")
SIGNALS_FILE = Path("monitor_signals.json")
COOLDOWN_MINUTES = 240
COOLDOWN_BREAKOUT_MINUTES = 240
HISTORY_HOURS = 6

MONITORING_TOKENS = [
    "RAREUSDT", "ARKUSDT", "WIFUSDT", "QIUSDT", "MOVEUSDT",
    "STXUSDT", "LSKUSDT", "SYNUSDT", "MOVRUSDT", "NOMUSDT",
    "JASMYUSDT", "TLMUSDT", "GLMRUSDT", "QUICKUSDT", "ACTUSDT",
    "BLURUSDT", "RESOLVUSDT", "AVAUSDT", "DODOUSDT", "PORTALUSDT",
    "VELODROMEUSDT", "EPICUSDT", "SOPHUSDT", "AWEUSDT", "SCRUSDT",
    "HEIUSDT", "TOWNSUSDT", "GTCUSDT", "FTTUSDT", "COOKIEUSDT",
    "QKCUSDT", "GNSUSDT",
]


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
    for sym, info in data.items():
        if not isinstance(info, dict):
            continue
        ts = info.get("ts", "")
        if not isinstance(ts, str):
            continue
        t = parse_ts_safe(ts)
        if t is None:
            continue
        stage = info.get("stage", "SIGNAL")
        minutes = COOLDOWN_BREAKOUT_MINUTES if stage == "BREAKOUT" else COOLDOWN_MINUTES
        if (now - t).total_seconds() < minutes * 60:
            cleaned[sym] = info
    return cleaned


def record_alert(symbol, stage):
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
    save_json(HISTORY_FILE, history)

    cooldown = load_json(COOLDOWN_FILE)
    cooldown[symbol] = {"ts": now.isoformat(), "stage": stage}
    save_json(COOLDOWN_FILE, cooldown)


def log_signal(symbol, stage, data, session):
    try:
        signals = load_json(SIGNALS_FILE)
        if not isinstance(signals.get("signals"), list):
            signals["signals"] = []
        signals["signals"].append({
            "ts": now_utc().isoformat(),
            "session": session,
            "symbol": symbol,
            "stage": stage,
            "price": data.get("price"),
            "volume_ratio": data.get("explosion_ratio"),
            "quiet_ratio": data.get("quiet_ratio"),
            "buy_pressure": data.get("buy_pressure"),
            "bid_depth": data.get("bid_depth"),
            "ask_depth": data.get("ask_depth"),
            "bid_ask_ratio": data.get("bid_ask_ratio"),
            "change_1h": data.get("change_1h"),
            "change_6h": data.get("change_6h"),
        })
        if len(signals["signals"]) > 500:
            signals["signals"] = signals["signals"][-500:]
        save_json(SIGNALS_FILE, signals)
    except Exception as e:
        print(f"log_signal error: {e}")


def log_rejection(symbol, reasons, price=0, change_24h=0):
    try:
        data = load_json(REJECT_FILE)
        if symbol not in data or not isinstance(data.get(symbol), dict):
            data[symbol] = {"symbol": symbol, "count": 0, "price": price, "change_24h": change_24h}
        data[symbol]["count"] += 1
        data[symbol]["last_seen"] = now_utc().isoformat()
        data[symbol]["price"] = price
        data[symbol]["change_24h"] = change_24h
        # Keep ALL failing reasons joined
        data[symbol]["top_reason"] = ",".join(reasons) if reasons else "unknown"
        if len(data) > 200:
            valid = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("last_seen")}
            sorted_items = sorted(valid.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
            data = dict(sorted_items[:200])
        save_json(REJECT_FILE, data)
    except Exception as e:
        print(f"reject log error: {e}")


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


def detect_stage(symbol):
    reasons = []
    try:
        url_15m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        r_15m = requests.get(url_15m, timeout=15)
        k15 = r_15m.json()
        if not isinstance(k15, list) or len(k15) < 50:
            return None, ["not_enough_15m"]

        url_5m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=5m&limit=15"
        r_5m = requests.get(url_5m, timeout=10)
        k5 = r_5m.json()
        if not isinstance(k5, list) or len(k5) < 10:
            return None, ["not_enough_5m"]

        quiet_vols_15m = [float(k[5]) for k in k15[-26:-2]]
        quiet_avg_15m = sum(quiet_vols_15m) / len(quiet_vols_15m) if quiet_vols_15m else 0
        if quiet_avg_15m == 0:
            return None, ["quiet_avg_zero"]

        quiet_avg_5m = quiet_avg_15m / 3

        prior_24h = [float(k[5]) for k in k15[-98:-2]] if len(k15) >= 98 else quiet_vols_15m
        prior_24h_avg = sum(prior_24h) / len(prior_24h) if prior_24h else 0
        quiet_ratio = quiet_avg_15m / prior_24h_avg if prior_24h_avg > 0 else 1

        current_5m = k5[-2]
        current_open = float(current_5m[1])
        current_close = float(current_5m[4])
        current_vol_5m = float(current_5m[5])

        explosion_ratio = current_vol_5m / quiet_avg_5m if quiet_avg_5m > 0 else 0

        last_3_5m = sum(float(k[5]) for k in k5[-5:-2])
        vol_15m_ratio = last_3_5m / quiet_avg_15m if quiet_avg_15m > 0 else 0

        price_1h_ago = float(k15[-6][4])
        change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100

        price_6h_ago = float(k15[-26][4])
        change_6h = ((current_close - price_6h_ago) / price_6h_ago) * 100

        recent_taker = sum(float(k[9]) for k in k5[-5:-1])
        recent_total = sum(float(k[5]) for k in k5[-5:-1])
        buy_pressure = recent_taker / recent_total if recent_total > 0 else 0

        bid_depth, ask_depth = check_depth(symbol, current_close)
        bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 0

        # Log every failing filter so we know why each coin was rejected
        if current_close <= current_open:
            reasons.append("candle_red")
        if quiet_ratio >= 0.75:
            reasons.append(f"quiet_{quiet_ratio:.2f}")
        if explosion_ratio < 5:
            reasons.append(f"expl_{explosion_ratio:.1f}x")
        if vol_15m_ratio < 3:
            reasons.append(f"vol15m_{vol_15m_ratio:.1f}x")
        if change_1h < 1.0:
            reasons.append(f"1h_low_{change_1h:.1f}")
        if change_1h > 25:
            reasons.append(f"1h_high_{change_1h:.1f}")
        if buy_pressure < 0.55:
            reasons.append(f"buy_{buy_pressure*100:.1f}")
        if bid_ask_ratio < 0.9:
            reasons.append(f"ratio_{bid_ask_ratio:.2f}")
        if bid_depth < 15_000:
            reasons.append(f"bid_{bid_depth:.0f}")
        if ask_depth < 15_000:
            reasons.append(f"ask_{ask_depth:.0f}")

        if reasons:
            return None, reasons

        return "BREAKOUT", {
            "price": current_close,
            "quiet_ratio": quiet_ratio,
            "explosion_ratio": explosion_ratio,
            "vol_5m_ratio": vol_15m_ratio,
            "change_1h": change_1h,
            "change_6h": change_6h,
            "buy_pressure": buy_pressure * 100,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "bid_ask_ratio": bid_ask_ratio,
        }
    except Exception as e:
        return None, [f"exception_{e}"]


def scan(session):
    print(f"Scanning {len(MONITORING_TOKENS)} monitoring tokens...")
    cooldown = load_cooldown()
    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detect_stage, s): s for s in MONITORING_TOKENS}
        for future in as_completed(futures):
            symbol = futures[future]
            stage_data = future.result()
            if isinstance(stage_data, tuple):
                stage, data = stage_data
            else:
                stage, data = None, ["exception"]
            if stage:
                if symbol in cooldown:
                    continue
                data["symbol"] = symbol
                data["stage"] = stage
                hits.append(data)
            else:
                if isinstance(data, list) and data:
                    top = data[0].split("_")[0]
                    rejection[top] = rejection.get(top, 0) + 1
                    log_rejection(symbol, data)
    print(f"Rejection: {rejection}")
    return hits


def is_active_session(ist_hour, ist_minute):
    total_min = ist_hour * 60 + ist_minute
    start_min = 8 * 60 + 30
    end_min = 15 * 60 + 30
    return start_min <= total_min <= end_min


def get_session_label(hour, minute):
    if 8 <= hour <= 11:
        return "Asia"
    if hour == 12 and minute >= 30:
        return "Europe"
    if 13 <= hour <= 15:
        return "Europe"
    return "Off"


def main():
    ist = now_utc() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour, ist.minute)
    print(f"Monitoring Scanner starting at {ist} IST — Session: {session}")

    if not is_active_session(ist.hour, ist.minute):
        print("Outside 8:30-15:30 IST. Skipping.")
        return

    if not SIGNALS_FILE.exists():
        SIGNALS_FILE.write_text('{"signals": []}')
    else:
        try:
            data = json.loads(SIGNALS_FILE.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("signals"), list):
                SIGNALS_FILE.write_text('{"signals": []}')
        except Exception:
            SIGNALS_FILE.write_text('{"signals": []}')

    hits = scan(session)
    print(f"Found {len(hits)} signals")

    for h in hits:
        header = f"🚀 MONITORING BREAKOUT [{session}]"

        record_alert(h["symbol"], "BREAKOUT")
        log_signal(h["symbol"], "BREAKOUT", h, session)

        msg = (
            f"{header}\n\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])}\n"
            f"<b>Quiet Base:</b> {h['quiet_ratio']:.2f}x 24h avg\n"
            f"<b>5m Explosion:</b> {h['explosion_ratio']:.2f}x\n"
            f"<b>15m Volume:</b> {h['vol_5m_ratio']:.2f}x\n"
            f"<b>1h Change:</b> {h['change_1h']:+.2f}%\n"
            f"<b>6h Change:</b> {h['change_6h']:+.2f}%\n"
            f"<b>Buyers:</b> {h['buy_pressure']:.1f}%\n"
            f"<b>Bid Depth:</b> ${h['bid_depth']:,.0f}\n"
            f"<b>Ask Depth:</b> ${h['ask_depth']:,.0f}\n"
            f"<b>Bid/Ask:</b> {h['bid_ask_ratio']:.2f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"⏳ WATCHING — Dip watcher is tracking for entry. Wait for ENTER signal."
        )
        send_telegram(msg)


def safe_main():
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 MONITORING SCANNER CRASHED\n\nError: {html.escape(str(e)[:300])}")
        raise


if __name__ == "__main__":
    safe_main()
