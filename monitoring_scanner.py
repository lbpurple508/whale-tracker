import os
import json
import html
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_DIR = Path(__file__).resolve().parent

BASE_URL = "https://data-api.binance.vision"
COOLDOWN_FILE = BASE_DIR / "monitor_cooldown.json"
REJECT_FILE = BASE_DIR / "monitor_rejections.json"
HISTORY_FILE = BASE_DIR / "monitor_history.json"
SIGNALS_FILE = BASE_DIR / "monitor" / "monitor_signals.json"

COOLDOWN_MINUTES = 240
COOLDOWN_BREAKOUT_MINUTES = 240
COOLDOWN_WATCHLIST_MINUTES = 60
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

OWN_CHAIN = {
    "MOVRUSDT", "GLMRUSDT", "ARKUSDT", "SCRUSDT", "EPICUSDT",
    "STXUSDT", "LSKUSDT", "SYNUSDT", "SOPHUSDT", "HEIUSDT",
    "QKCUSDT", "TOWNSUSDT", "AVAUSDT",
}

ECOSYSTEM_PAIRS = {
    "GLMRUSDT": "MOVRUSDT",
    "MOVRUSDT": "GLMRUSDT",
}

PRIME_START_H = 10
PRIME_END_H = 13

MAX_BASE_RANGE_PCT = 4.0
MIN_DIST_TO_BASE_HIGH = 0.5

# Base window: 16 completed 15m candles = 4 hours
BASE_BARS = 16
# Volume baseline: previous 20 completed 5m candles
VOLUME_BASELINE_BARS = 20
# 1h = 12 x 5m; 6h = 72 x 5m
ONE_HOUR_BARS = 12
SIX_HOUR_BARS = 72


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
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram HTTP error: {response.status_code} - {response.text}")
            return False
        body = response.json()
        if not body.get("ok"):
            print(f"Telegram API error: {body}")
            return False
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


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
        stage = info.get("stage", "BREAKOUT")
        if stage == "BREAKOUT":
            minutes = COOLDOWN_BREAKOUT_MINUTES
        elif stage == "WATCHLIST":
            minutes = COOLDOWN_WATCHLIST_MINUTES
        else:
            minutes = COOLDOWN_MINUTES
        if (now - t).total_seconds() < minutes * 60:
            cleaned[sym] = info
    return cleaned


def cooldown_blocks(cooldown, symbol, stage):
    info = cooldown.get(symbol)
    if not isinstance(info, dict):
        return False
    ts = parse_ts_safe(info.get("ts", ""))
    if ts is None:
        return False
    age_min = (now_utc() - ts).total_seconds() / 60
    prev_stage = info.get("stage", "")
    if prev_stage == "BREAKOUT":
        return age_min < COOLDOWN_BREAKOUT_MINUTES
    if prev_stage == "WATCHLIST":
        return stage == "WATCHLIST" and age_min < COOLDOWN_WATCHLIST_MINUTES
    return False


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
            "tier": data.get("tier"),
            "price": data.get("price"),
            "dead_hours": data.get("dead_hours"),
            "change_5m": data.get("change_5m"),
            "volume_ratio": data.get("explosion_ratio"),
            "rsi": data.get("rsi"),
            "buy_pressure": data.get("buy_pressure"),
            "bid_depth": data.get("bid_depth"),
            "ask_depth": data.get("ask_depth"),
            "bid_ask_ratio": data.get("bid_ask_ratio"),
            "change_1h": data.get("change_1h"),
            "change_6h": data.get("change_6h"),
            "base_range_pct": data.get("base_range_pct"),
            "base_high": data.get("base_high"),
            "base_low": data.get("base_low"),
            "dist_to_base_high": data.get("dist_to_base_high"),
            "watch_volume_ratio": data.get("vol_ratio"),
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
        data[symbol]["top_reason"] = ",".join(reasons) if reasons else "unknown"
        if len(data) > 200:
            valid = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("last_seen")}
            sorted_items = sorted(valid.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
            data = dict(sorted_items[:200])
        save_json(REJECT_FILE, data)
    except Exception as e:
        print(f"reject log error: {e}")


def check_depth(symbol, price):
    """Depth limited to 100 levels (weight 5 instead of 25)."""
    url = f"{BASE_URL}/api/v3/depth?symbol={symbol}&limit=100"
    book = http_get_json(url, timeout=10, retries=1)
    if not isinstance(book, dict):
        return None, None
    try:
        low, high = price * 0.98, price * 1.02
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


def get_trigger_base(k15, k5):
    """
    FIX 1: Timestamp-aligned base.
    Returns (base_high, base_low, base_slice) where base_slice is the
    last BASE_BARS completed 15m candles whose close was BEFORE the
    latest completed 5m trigger candle opened.
    """
    if len(k5) < 3 or len(k15) < BASE_BARS + 2:
        return None, None, None

    trigger_open_ms = int(k5[-2][0])

    eligible = []
    for k in k15:
        try:
            close_ms = int(k[6])
        except (TypeError, ValueError, IndexError):
            continue
        if close_ms <= trigger_open_ms:
            eligible.append(k)

    if len(eligible) < BASE_BARS:
        return None, None, None

    base_slice = eligible[-BASE_BARS:]

    try:
        base_high = max(float(k[2]) for k in base_slice)
        base_low = min(float(k[3]) for k in base_slice)
    except (TypeError, ValueError, IndexError):
        return None, None, None

    if base_low <= 0:
        return None, None, None

    return base_high, base_low, base_slice


def count_dead_base_hours(base_slice):
    """Counts consecutive quiet (range<1%) 15m candles within base_slice."""
    if not base_slice:
        return 0.0
    dead = 0
    for k in reversed(base_slice):
        try:
            h = float(k[2])
            l = float(k[3])
            if l <= 0:
                break
            rng = (h - l) / l * 100
            if rng < 1.0:
                dead += 1
            else:
                break
        except Exception:
            break
    return dead / 4.0


def get_volume_ratio(k5):
    """
    FIX 3: Single volume-baseline helper used by both stages.
    Latest completed 5m vs previous VOLUME_BASELINE_BARS completed 5m.
    """
    trigger_idx = len(k5) - 2
    if trigger_idx < VOLUME_BASELINE_BARS:
        return None

    try:
        current_vol = float(k5[trigger_idx][5])
    except (TypeError, ValueError, IndexError):
        return None

    start = trigger_idx - VOLUME_BASELINE_BARS
    baseline = []
    for k in k5[start:trigger_idx]:
        try:
            baseline.append(float(k[5]))
        except (TypeError, ValueError, IndexError):
            continue

    if not baseline:
        return None

    avg_vol = sum(baseline) / len(baseline)
    if avg_vol <= 0:
        return None

    return current_vol / avg_vol


def get_timeframe_change(k5, bars_back):
    """
    FIX 2: Percentage change from N completed 5m candles before the
    latest completed 5m trigger candle.
    """
    trigger_idx = len(k5) - 2
    target_idx = trigger_idx - bars_back

    if target_idx < 0:
        return None

    try:
        trigger_close = float(k5[trigger_idx][4])
        old_close = float(k5[target_idx][4])
    except (TypeError, ValueError, IndexError):
        return None

    if trigger_close <= 0 or old_close <= 0:
        return None

    return ((trigger_close - old_close) / old_close) * 100


def check_pre_pump(k15, k5):
    try:
        base_high, base_low, base_slice = get_trigger_base(k15, k5)
        if base_high is None:
            return None

        base_range_pct = ((base_high - base_low) / base_low) * 100
        if base_range_pct > MAX_BASE_RANGE_PCT:
            return None

        current_price = float(k5[-2][4])
        previous_price = float(k5[-3][4])
        if previous_price <= 0 or current_price <= 0:
            return None

        if current_price < base_low:
            return None

        change_5m = ((current_price - previous_price) / previous_price) * 100
        if change_5m <= 0:
            return None

        vol_ratio = get_volume_ratio(k5)
        if vol_ratio is None or vol_ratio < 1.5:
            return None

        dist_to_base_high = ((base_high - current_price) / current_price) * 100
        if dist_to_base_high < MIN_DIST_TO_BASE_HIGH:
            return None

        dead_hours = count_dead_base_hours(base_slice)

        return {
            "price": current_price,
            "change_5m": change_5m,
            "base_range_pct": base_range_pct,
            "vol_ratio": vol_ratio,
            "dist_to_base_high": dist_to_base_high,
            "base_high": base_high,
            "base_low": base_low,
            "dead_hours": dead_hours,
        }
    except Exception as e:
        print(f"pre_pump error: {e}")
        return None


def detect_breakout(symbol, k15, k5, ist_hour):
    reasons = []

    completed_5m = k5[-2]
    prev_completed_5m = k5[-3]

    c_open = float(completed_5m[1])
    c_high = float(completed_5m[2])
    c_low = float(completed_5m[3])
    c_close = float(completed_5m[4])

    if c_close <= 0 or c_low <= 0:
        return None, ["bad_price"]

    prev_close = float(prev_completed_5m[4])
    if prev_close <= 0:
        return None, ["bad_prev"]
    change_5m = ((c_close - prev_close) / prev_close) * 100

    # Timestamp-aligned base
    base_high, base_low, base_slice = get_trigger_base(k15, k5)
    if base_high is None:
        return None, ["no_base"]

    base_range_pct = ((base_high - base_low) / base_low) * 100
    if base_range_pct > MAX_BASE_RANGE_PCT:
        return None, [f"wide_base_{base_range_pct:.1f}%"]

    if c_close <= base_high:
        reasons.append("no_structure_break")

    if c_close <= c_open:
        reasons.append("red_candle")

    tier = 1 if symbol in OWN_CHAIN else 2
    min_5m = 1.2 if tier == 1 else 1.5
    min_vol = 2.0 if tier == 1 else 2.5

    prime = PRIME_START_H <= ist_hour < PRIME_END_H
    if prime:
        min_5m -= 0.3
        min_vol -= 0.5

    if change_5m < min_5m:
        reasons.append(f"5m_{change_5m:.1f}%")
    if change_5m > 6.0:
        reasons.append(f"5m_high_{change_5m:.1f}%")

    explosion_ratio = get_volume_ratio(k5)
    if explosion_ratio is None:
        return None, ["volume_baseline_zero"]
    if explosion_ratio < min_vol:
        reasons.append(f"vol_{explosion_ratio:.1f}x")

    rng = c_high - c_low
    if rng <= 0:
        return None, ["zero_range"]
    upper_wick = (c_high - max(c_open, c_close)) / rng
    clv = (c_close - c_low) / rng
    if upper_wick > 0.35:
        reasons.append(f"wick_{upper_wick*100:.0f}%")
    if clv < 0.70:
        reasons.append(f"clv_{clv:.2f}")

    recent_taker = sum(float(k[9]) for k in k5[-5:-1])
    recent_total = sum(float(k[5]) for k in k5[-5:-1])
    buy_pressure = recent_taker / recent_total if recent_total > 0 else 0
    if buy_pressure < 0.55:
        reasons.append(f"buy_{buy_pressure*100:.0f}%")

    if reasons:
        return None, reasons

    # Depth is the LAST check
    bid_depth, ask_depth = check_depth(symbol, c_close)
    if bid_depth is None:
        return None, ["depth_unavailable"]
    bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 0
    if bid_ask_ratio < 0.9:
        reasons.append(f"ratio_{bid_ask_ratio:.2f}")
    if bid_depth < 15_000:
        reasons.append(f"bid_{bid_depth:.0f}")
    if ask_depth < 15_000:
        reasons.append(f"ask_{ask_depth:.0f}")
    if reasons:
        return None, reasons

    dead_hours = count_dead_base_hours(base_slice)
    closes_15m = [float(k[4]) for k in k15[:-1]]
    rsi_15m = compute_rsi(closes_15m, 14)

    change_1h = get_timeframe_change(k5, ONE_HOUR_BARS)
    change_6h = get_timeframe_change(k5, SIX_HOUR_BARS)
    if change_1h is None or change_6h is None:
        return None, ["not_enough_5m_history"]

    return {
        "price": c_close,
        "tier": tier,
        "dead_hours": dead_hours,
        "change_5m": change_5m,
        "change_1h": change_1h,
        "change_6h": change_6h,
        "explosion_ratio": explosion_ratio,
        "vol_5m_ratio": explosion_ratio,
        "rsi": rsi_15m,
        "buy_pressure": buy_pressure * 100,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "bid_ask_ratio": bid_ask_ratio,
        "prime": prime,
        "base_high": base_high,
        "base_low": base_low,
        "base_range_pct": base_range_pct,
    }, []


def detect_stage(symbol, ist_hour):
    try:
        url_15m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        k15 = http_get_json(url_15m, timeout=15, retries=1)
        if not isinstance(k15, list) or len(k15) < 50:
            return None, ["not_enough_15m"]

        # 100 x 5m candles for 6h + safety
        url_5m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=5m&limit=100"
        k5 = http_get_json(url_5m, timeout=10, retries=1)
        if not isinstance(k5, list) or len(k5) < 20:
            return None, ["not_enough_5m"]

        breakout_data, breakout_reasons = detect_breakout(symbol, k15, k5, ist_hour)
        if breakout_data is not None:
            return "BREAKOUT", breakout_data

        pre_pump = check_pre_pump(k15, k5)
        if pre_pump is not None:
            return "WATCHLIST", {
                "price": pre_pump["price"],
                "base_range_pct": pre_pump["base_range_pct"],
                "vol_ratio": pre_pump["vol_ratio"],
                "dist_to_base_high": pre_pump["dist_to_base_high"],
                "base_high": pre_pump["base_high"],
                "base_low": pre_pump["base_low"],
                "tier": 1 if symbol in OWN_CHAIN else 2,
                "dead_hours": pre_pump["dead_hours"],
                "change_5m": pre_pump["change_5m"],
                "change_1h": 0,
                "change_6h": 0,
                "explosion_ratio": pre_pump["vol_ratio"],
                "rsi": 50,
                "buy_pressure": 50,
                "bid_depth": 0,
                "ask_depth": 0,
                "bid_ask_ratio": 0,
                "prime": False,
            }

        return None, breakout_reasons if breakout_reasons else ["no_setup"]
    except Exception as e:
        return None, [f"exception_{e}"]


def scan(session, ist_hour):
    print(f"Scanning {len(MONITORING_TOKENS)} monitoring tokens...")
    cooldown = load_cooldown()
    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detect_stage, s, ist_hour): s for s in MONITORING_TOKENS}
        for future in as_completed(futures):
            symbol = futures[future]
            stage_data = future.result()
            if isinstance(stage_data, tuple):
                stage, data = stage_data
            else:
                stage, data = None, ["exception"]
            if stage:
                if cooldown_blocks(cooldown, symbol, stage):
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


def get_session_label(hour):
    if 8 <= hour <= 11:
        return "Asia"
    if 12 <= hour <= 15:
        return "Europe"
    if 18 <= hour <= 22:
        return "US"
    return "Off"


def main():
    ist = now_utc() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour)
    print(f"Monitoring Scanner starting at {ist} IST — Session: {session}")

    SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not SIGNALS_FILE.exists():
        SIGNALS_FILE.write_text('{"signals": []}')
    else:
        try:
            data = json.loads(SIGNALS_FILE.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("signals"), list):
                SIGNALS_FILE.write_text('{"signals": []}')
        except Exception:
            SIGNALS_FILE.write_text('{"signals": []}')

    hits = scan(session, ist.hour)
    print(f"Found {len(hits)} signals")

    fired_symbols = set()
    for h in hits:
        stage = h.get("stage")

        if stage == "WATCHLIST":
            msg = (
                f"👀 <b>WATCHLIST: {h['symbol']}</b>\n\n"
                f"<b>Price:</b> {format_price(h['price'])}\n"
                f"<b>Base Range:</b> {h['base_range_pct']:.2f}%\n"
                f"<b>Volume Ratio:</b> {h['vol_ratio']:.2f}x quiet\n"
                f"<b>Dead Base:</b> {h['dead_hours']:.1f}h\n"
                f"<b>Distance to Base High:</b> {h['dist_to_base_high']:.2f}%\n\n"
                f"⚠️ <b>DO NOT BUY YET.</b> Watch for a breakout above {format_price(h['base_high'])}."
            )
            if send_telegram(msg):
                record_alert(h["symbol"], "WATCHLIST")
                log_signal(h["symbol"], "WATCHLIST", h, session)
            continue

        if stage == "BREAKOUT":
            tier = h.get("tier", 2)
            tier_label = "Tier 1 — Own Chain" if tier == 1 else "Tier 2 — Token"
            prime_flag = " ⚡PRIME" if h.get("prime") else ""
            header = f"🚀 BREAKOUT [{tier_label}]{prime_flag} [{session}]"

            stop = h["price"] * 0.97
            target_5 = h["price"] * 1.05
            target_10 = h["price"] * 1.10
            target_25 = h["price"] * 1.25

            msg = (
                f"{header}\n\n"
                f"<b>Coin:</b> {h['symbol']}\n"
                f"<b>Price:</b> {format_price(h['price'])}\n"
                f"<b>Base High:</b> {format_price(h.get('base_high', 0))}\n"
                f"<b>Dead Base:</b> {h['dead_hours']:.1f}h\n"
                f"<b>5m Change:</b> {h['change_5m']:+.2f}%\n"
                f"<b>1h Change:</b> {h['change_1h']:+.2f}%\n"
                f"<b>6h Change:</b> {h['change_6h']:+.2f}%\n"
                f"<b>Volume:</b> {h['explosion_ratio']:.2f}x quiet\n"
                f"<b>RSI(15m):</b> {h['rsi']:.1f}\n"
                f"<b>Buyers:</b> {h['buy_pressure']:.1f}%\n"
                f"<b>Bid/Ask:</b> {h['bid_ask_ratio']:.2f}\n"
                f"<b>Bid Depth:</b> ${h['bid_depth']:,.0f}\n"
                f"<b>Ask Depth:</b> ${h['ask_depth']:,.0f}\n"
                f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
                f"<b>Entry:</b> {format_price(h['price'])}\n"
                f"<b>Stop Loss:</b> {format_price(stop)} (-3%)\n"
                f"<b>Targets:</b> +5% {format_price(target_5)} | +10% {format_price(target_10)} | +25% {format_price(target_25)}\n\n"
                f"✅ ENTER NOW — Buy at market. SL {format_price(stop)}."
            )
            if send_telegram(msg):
                record_alert(h["symbol"], "BREAKOUT")
                log_signal(h["symbol"], "BREAKOUT", h, session)
                fired_symbols.add(h["symbol"])

    for fired in fired_symbols:
        pair = ECOSYSTEM_PAIRS.get(fired)
        if pair and pair not in fired_symbols:
            pair_msg = (
                f"🔗 <b>ECOSYSTEM ROTATION</b>\n\n"
                f"<b>{fired}</b> just fired.\n"
                f"<b>{pair}</b> is paired (same ecosystem).\n"
                f"Watch {pair} next — whales often rotate between them."
            )
            send_telegram(pair_msg)


def safe_main():
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 MONITORING SCANNER CRASHED\n\nError: {html.escape(str(e)[:300])}")
        raise


if __name__ == "__main__":
    safe_main()
