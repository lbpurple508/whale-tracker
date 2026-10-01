import os
import json
import html
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# FIX: anchor all paths to the script directory
BASE_DIR = Path(__file__).resolve().parent

BINANCE_API = "https://data-api.binance.vision"
TRACKER_FILE = BASE_DIR / "tracker_state.json"
COOLDOWN_FILE = BASE_DIR / "dist_cooldown.json"
COOLDOWN_MINUTES = 15

ALERT_THRESHOLD = 4
WARN_THRESHOLD = 3


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


def http_get_json(url, timeout=10, retries=1):
    """GET with one retry on 429/5xx. Returns parsed JSON or None."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return None
            return None
        except Exception:
            if attempt < retries:
                time.sleep(2)
                continue
            return None
    return None


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
        path.parent.mkdir(parents=True, exist_ok=True)
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


def get_klines(symbol):
    url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=30"
    data = http_get_json(url, timeout=10, retries=1)
    if not isinstance(data, list) or len(data) < 25:
        return None
    return data


def get_depth(symbol, price):
    """FIX: return (None, None) on API failure so we do NOT count fake zero depth."""
    url = f"{BINANCE_API}/api/v3/depth?symbol={symbol}&limit=500"
    book = http_get_json(url, timeout=10, retries=1)
    if not isinstance(book, dict):
        return None, None
    try:
        low = price * 0.98
        high = price * 1.02
        bid_depth = sum(float(b[1]) * float(b[0]) for b in book.get("bids", []) if float(b[0]) >= low)
        ask_depth = sum(float(a[1]) * float(a[0]) for a in book.get("asks", []) if float(a[0]) <= high)
        return bid_depth, ask_depth
    except Exception:
        return None, None


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


def check_distribution(symbol, entry_price):
    try:
        klines = get_klines(symbol)
        if not klines:
            return None

        completed = klines[-2]
        o = float(completed[1])
        h = float(completed[2])
        l = float(completed[3])
        c = float(completed[4])
        vol = float(completed[5])
        taker_buy = float(completed[9])

        if vol <= 0:
            return None

        prior_vols = [float(k[5]) for k in klines[-23:-2]]
        avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        vol_ratio = vol / avg_vol if avg_vol > 0 else 0

        closes = [float(k[4]) for k in klines[:-1]]
        rsi = compute_rsi(closes, 14)

        taker_pct = taker_buy / vol

        candle_range = h - l
        upper_wick = h - max(o, c)
        upper_wick_pct = upper_wick / candle_range if candle_range > 0 else 0

        price_1h_ago = float(klines[-6][4])
        change_1h = ((c - price_1h_ago) / price_1h_ago) * 100

        bid_depth, ask_depth = get_depth(symbol, c)
        # FIX: only use bid/ask score when depth was actually fetched
        depth_available = bid_depth is not None and ask_depth is not None
        bid_ask_ratio = None
        if depth_available and ask_depth > 0:
            bid_ask_ratio = bid_depth / ask_depth

        score = 0
        reasons = []

        if bid_ask_ratio is not None and bid_ask_ratio < 0.7:
            score += 2
            reasons.append(f"bid/ask {bid_ask_ratio:.2f}")
        if taker_pct < 0.45:
            score += 2
            reasons.append(f"taker {taker_pct*100:.1f}%")
        if rsi > 78:
            score += 1
            reasons.append(f"RSI {rsi:.1f}")
        if upper_wick_pct > 0.5:
            score += 1
            reasons.append(f"wick {upper_wick_pct*100:.0f}%")
        if vol_ratio > 2 and abs(change_1h) < 1:
            score += 2
            reasons.append(f"vol {vol_ratio:.1f}x flat")

        current_pct = ((c - entry_price) / entry_price) * 100

        return {
            "symbol": symbol,
            "price": c,
            "entry": entry_price,
            "current_pct": current_pct,
            "score": score,
            "reasons": reasons,
            "rsi": rsi,
            "bid_ask": bid_ask_ratio if bid_ask_ratio is not None else 0.0,
            "depth_available": depth_available,
            "taker_pct": taker_pct * 100,
            "vol_ratio": vol_ratio,
            "upper_wick_pct": upper_wick_pct * 100,
            "change_1h": change_1h,
        }
    except Exception as e:
        print(f"check_distribution error for {symbol}: {e}")
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


def main():
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not COOLDOWN_FILE.exists():
        COOLDOWN_FILE.write_text("{}")
    else:
        try:
            data = json.loads(COOLDOWN_FILE.read_text())
            if not isinstance(data, dict):
                COOLDOWN_FILE.write_text("{}")
        except Exception:
            COOLDOWN_FILE.write_text("{}")

    tracker = load_json(TRACKER_FILE)
    signals = tracker.get("signals", [])
    if not isinstance(signals, list):
        print("Bad tracker state")
        return

    # Only ACTIVE positions are monitored (tracker uses ACTIVE / TP30_HIT / STOPPED)
    trackable = [s for s in signals if isinstance(s, dict) and s.get("status") == "ACTIVE"]
    if not trackable:
        print("No active positions to monitor")
        return

    seen = {}
    for s in trackable:
        sym = s.get("symbol")
        if not sym:
            continue
        entry = s.get("entry")
        ts = s.get("entry_ts", "")
        if not entry:
            continue
        try:
            entry_f = float(entry)
        except (ValueError, TypeError):
            continue
        if sym not in seen or ts > seen[sym]["entry_ts"]:
            seen[sym] = {"symbol": sym, "entry": entry_f, "entry_ts": ts}

    print(f"Checking {len(seen)} active positions...")

    results = []
    failures = 0
    if seen:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(check_distribution, v["symbol"], v["entry"]): v["symbol"] for v in seen.values()}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
                else:
                    failures += 1

    if failures:
        print(f"check_distribution failed for {failures} symbols")

    cooldown = load_cooldown()
    now = now_utc()
    to_alert = []
    for r in results:
        if r["score"] < WARN_THRESHOLD:
            continue
        if r["symbol"] in cooldown:
            continue
        to_alert.append(r)

    if not to_alert:
        print(f"No distribution signals (checked {len(results)})")
        save_json(COOLDOWN_FILE, cooldown)
        return

    for r in to_alert:
        cooldown[r["symbol"]] = now.isoformat()
    save_json(COOLDOWN_FILE, cooldown)

    to_alert.sort(key=lambda x: x["score"], reverse=True)

    lines = ["🔴 <b>DISTRIBUTION SCANNER</b>", ""]
    for r in to_alert:
        level = "🚨 EXIT NOW" if r["score"] >= ALERT_THRESHOLD else "⚠️ WARNING"
        lines.append(f"{level} — <b>{r['symbol']}</b>")
        lines.append(
            f"Entry {format_price(r['entry'])} → Now {format_price(r['price'])} "
            f"({r['current_pct']:+.2f}%)"
        )
        lines.append(f"Score: {r['score']}/8")
        lines.append(f"Reasons: {', '.join(r['reasons'])}")
        if r["depth_available"]:
            lines.append(
                f"RSI {r['rsi']:.1f} | Bid/Ask {r['bid_ask']:.2f} | "
                f"Taker {r['taker_pct']:.1f}%"
            )
        else:
            lines.append(
                f"RSI {r['rsi']:.1f} | Taker {r['taker_pct']:.1f}% | "
                f"(depth unavailable)"
            )
        lines.append("")

    send_telegram("\n".join(lines))
    print(f"Alerts sent: {len(to_alert)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 DISTRIBUTION SCANNER CRASHED\n\nError: {html.escape(str(e)[:300])}")
        raise
