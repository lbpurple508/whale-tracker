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
COOLDOWN_MINUTES = 45
COOLDOWN_BREAKOUT_MINUTES = 60
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
        stage = info.get("stage", "COILING")
        minutes = COOLDOWN_BREAKOUT_MINUTES if stage == "BREAKOUT" else COOLDOWN_MINUTES
        if (now - t).total_seconds() < minutes * 60:
            cleaned[sym] = info
    return cleaned


def is_on_cooldown(symbol, cooldown, stage):
    if symbol not in cooldown:
        return False
    entry = cooldown[symbol]
    if not isinstance(entry, dict):
        return False
    existing_stage = entry.get("stage", "COILING")
    if stage == "BREAKOUT" and existing_stage == "COILING":
        return False
    return True


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
            "rsi_now": data.get("rsi"),
            "rsi_2h_ago": data.get("rsi_2h_ago"),
            "volume_ratio": data.get("vol_ratio"),
            "buy_pressure": data.get("buy_pressure"),
            "bid_depth": data.get("bid_depth"),
            "ask_depth": data.get("ask_depth"),
            "bid_ask_ratio": data.get("bid_ask_ratio"),
            "change_1h": data.get("change_1h"),
            "change_6h": data.get("change_6h"),
            "range_pct": data.get("range_pct"),
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
        return change > -3
    except Exception:
        return True


def compute_rsi_series(closes, period=14):
    if len(closes) < period + 1:
        return []
    rsis = []
    for i in range(period, len(closes)):
        window = closes[i-period:i+1]
        gains, losses = [], []
        for j in range(1, len(window)):
            diff = window[j] - window[j-1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0 and avg_gain == 0:
            rsis.append(50)
        elif avg_loss == 0:
            rsis.append(100)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100 - (100 / (1 + rs)))
    return rsis


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
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=48"
        r = requests.get(url, timeout=15)
        klines = r.json()
        if not isinstance(klines, list) or len(klines) < 30:
            return None, ["not_enough_klines"]

        completed = klines[-2]
        current_close = float(completed[4])
        current_open = float(completed[1])
        current_vol = float(completed[5])

        prior_vols = [float(k[5]) for k in klines[-23:-2]]
        avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_vol == 0:
            return None, ["avg_vol_zero"]
        vol_ratio = current_vol / avg_vol

        highs_6h = [float(k[2]) for k in klines[-25:-1]]
        lows_6h = [float(k[3]) for k in klines[-25:-1]]
        if min(lows_6h) == 0:
            return None, ["zero_low"]
        range_pct = ((max(highs_6h) - min(lows_6h)) / min(lows_6h)) * 100

        price_6h_ago = float(klines[-26][4])
        change_6h = ((current_close - price_6h_ago) / price_6h_ago) * 100

        price_1h_ago = float(klines[-6][4])
        change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100

        closes = [float(k[4]) for k in klines[:-1]]
        rsi_series = compute_rsi_series(closes, 14)
        if not rsi_series:
            return None, ["rsi_fail"]
        rsi_now = rsi_series[-1]
        rsi_2h_ago = rsi_series[-9] if len(rsi_series) >= 9 else rsi_now
        rsi_declining = rsi_now < rsi_2h_ago

        recent_taker = sum(float(k[9]) for k in klines[-9:-1])
        recent_total = sum(float(k[5]) for k in klines[-9:-1])
        buy_pressure = recent_taker / recent_total if recent_total > 0 else 0

        bid_depth, ask_depth = check_depth(symbol, current_close)
        bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 0

        breakout_conditions = [
            current_close > current_open,
            vol_ratio >= 1.5,
            1.5 <= change_1h <= 6,
            50 <= rsi_now <= 75,
            rsi_2h_ago < 70,
            buy_pressure >= 0.50,
            bid_ask_ratio >= 0.7,
            bid_depth >= 15_000,
            ask_depth >= 15_000,
        ]
        if all(breakout_conditions):
            return "BREAKOUT", {
                "price": current_close,
                "vol_ratio": vol_ratio,
                "rsi": rsi_now,
                "rsi_2h_ago": rsi_2h_ago,
                "change_1h": change_1h,
                "change_6h": change_6h,
                "range_pct": range_pct,
                "buy_pressure": buy_pressure * 100,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "bid_ask_ratio": bid_ask_ratio,
            }

        coiling_reasons = []
        if rsi_now < 30 or rsi_now > 55:
            coiling_reasons.append(f"RSI_{rsi_now:.1f}")
        if not rsi_declining:
            coiling_reasons.append("rsi_rising")
        if range_pct > 10:
            coiling_reasons.append(f"range_{range_pct:.1f}%")
        if change_6h < -5 or change_6h > 5:
            coiling_reasons.append(f"6h_{change_6h:.1f}%")
        if vol_ratio < 1.0:
            coiling_reasons.append(f"vol_{vol_ratio:.1f}x_low")
        if vol_ratio > 2.5:
            coiling_reasons.append(f"vol_{vol_ratio:.1f}x_high")
        if buy_pressure < 0.50:
            coiling_reasons.append(f"buy_{buy_pressure*100:.1f}%")
        greens = sum(1 for k in klines[-7:-1] if float(k[4]) > float(k[1]))
        if greens < 3:
            coiling_reasons.append(f"greens_{greens}")
        if bid_depth < 5_000:
            coiling_reasons.append(f"bid_{bid_depth:.0f}")
        if ask_depth < 5_000:
            coiling_reasons.append(f"ask_{ask_depth:.0f}")
        if bid_ask_ratio < 0.7:
            coiling_reasons.append(f"ratio_{bid_ask_ratio:.2f}")

        if coiling_reasons:
            return None, coiling_reasons

        return "COILING", {
            "price": current_close,
            "vol_ratio": vol_ratio,
            "rsi": rsi_now,
            "rsi_2h_ago": rsi_2h_ago,
            "change_1h": change_1h,
            "change_6h": change_6h,
            "range_pct": range_pct,
            "buy_pressure": buy_pressure * 100,
            "greens": greens,
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
                if is_on_cooldown(symbol, cooldown, stage):
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
    print(f"Monitoring Scanner starting at {ist} IST — Session: {session}")

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
        print("BTC dumping >3%. Skipping.")
        return

    hits = scan(session)
    print(f"Found {len(hits)} signals")

    for h in hits:
        count = get_alert_count(h["symbol"]) + 1
        stop = h["price"] * 0.97
        stage = h.get("stage", "COILING")

        if stage == "BREAKOUT":
            header = f"🚀 MONITORING BREAKOUT [{session}]"
        elif count == 1:
            header = f"⭐ MONITORING COILING (WATCH) [{session}]"
        elif count == 2:
            header = f"⭐⭐ MONITORING COILING — 2ND [{session}]"
        else:
            header = f"🎯 MONITORING COILING — {count}X [{session}]"

        record_alert(h["symbol"], stage)
        log_signal(h["symbol"], stage, h, session)

        msg = (
            f"{header}\n\n"
            f"<b>Stage:</b> {stage}\n"
            f"<b>Alerts (6h):</b> {count}\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])}\n"
            f"<b>6h Range:</b> {h['range_pct']:.2f}%\n"
            f"<b>6h Change:</b> {h['change_6h']:+.2f}%\n"
            f"<b>1h Change:</b> {h['change_1h']:+.2f}%\n"
            f"<b>RSI now:</b> {h['rsi']:.1f}\n"
            f"<b>RSI 2h ago:</b> {h['rsi_2h_ago']:.1f}\n"
            f"<b>Volume:</b> {h['vol_ratio']:.2f}x normal\n"
            f"<b>Buyers:</b> {h['buy_pressure']:.1f}%\n"
            f"<b>Bid Depth:</b> ${h['bid_depth']:,.0f}\n"
            f"<b>Ask Depth:</b> ${h['ask_depth']:,.0f}\n"
            f"<b>Bid/Ask:</b> {h['bid_ask_ratio']:.2f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"<b>Stop Loss:</b> {format_price(stop)} (-3%)\n"
            f"<b>Trail:</b> +3%→BE, +5%→+2%, +10%→+6%, +25%→+18%\n\n"
        )

        if stage == "BREAKOUT":
            msg += "✅ ENTER NOW — Buy at current price. SL -3%."
        else:
            msg += "⏸️ WAIT — Do NOT enter yet. Wait for BREAKOUT alert."

        send_telegram(msg)


def safe_main():
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 MONITORING SCANNER CRASHED\n\nError: {html.escape(str(e)[:300])}")
        raise


if __name__ == "__main__":
    safe_main()
