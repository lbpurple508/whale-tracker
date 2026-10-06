import os
import json
import html
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_DIR = Path(__file__).resolve().parent
BASE_URL = "https://data-api.binance.vision"
COOLDOWN_FILE = BASE_DIR / "monitor_cooldown.json"
REJECT_FILE = BASE_DIR / "monitor_rejections.json"
HISTORY_FILE = BASE_DIR / "monitor_history.json"
SIGNALS_FILE = BASE_DIR / "monitor_signals.json"
WATCHLIST_STATE_FILE = BASE_DIR / "monitor_watchlist_state.json"

COOLDOWN_BREAKOUT_MINUTES = 240
COOLDOWN_WATCHLIST_MINUTES = 60
HISTORY_HOURS = 6

SIG_ATR_SLOPE_12H = 20.0
SIG_VOL_SLOPE_12H = 30.0
SIG_TRADES_SLOPE_12H = 20.0
SIG_ATR_1H_PCT_MIN = 2.0
SIG_RET_1H_MIN = 5.0

CONFIRM_MIN_HOURS = 1.0
CONFIRM_MAX_HOURS = 6.0
MAX_EXTENSION_PCT = 5.0
MAX_CONFIRM_SEND_FAILURES = 3

# Binance Spot kline field indexes.
KLINE_OPEN_TIME = 0
KLINE_OPEN = 1
KLINE_HIGH = 2
KLINE_LOW = 3
KLINE_CLOSE = 4
KLINE_VOLUME = 5
KLINE_CLOSE_TIME = 6
KLINE_QUOTE_VOLUME = 7
KLINE_TRADES = 8
KLINE_TAKER_BUY_BASE_VOLUME = 9
KLINE_TAKER_BUY_QUOTE_VOLUME = 10

KLINE_INTERVAL = "5m"
KLINE_LIMIT = 500
MIN_CLOSED_CANDLES = 288
MIN_RAW_CANDLES = MIN_CLOSED_CANDLES + 10
MAX_WORKERS = 8

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

if KLINE_LIMIT < MIN_RAW_CANDLES:
    raise ValueError(
        f"KLINE_LIMIT ({KLINE_LIMIT}) must be >= MIN_RAW_CANDLES ({MIN_RAW_CANDLES})"
    )


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def display_name(symbol):
    return str(symbol).removesuffix("USDT")


def format_price(price):
    price = float(price)
    if price >= 1:
        return f"${price:.4f}"
    if price >= 0.01:
        return f"${price:.5f}"
    if price >= 0.0001:
        return f"${price:.6f}"
    return f"${price:.8f}"


def http_get_json(url, params=None, timeout=10, retries=1):
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    print(f"Invalid JSON from {url}")
                    return None

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(0.5, min(float(retry_after), 10.0)) if retry_after else 2.0
                except (TypeError, ValueError):
                    delay = 2.0
                time.sleep(delay)
                continue

            if response.status_code != 404:
                print(f"HTTP error {response.status_code} from {url}")
            return None

        except requests.RequestException as exc:
            if attempt < retries:
                time.sleep(2)
                continue
            print(f"HTTP request error for {url}: {exc}")
            return None

    return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram HTTP error: {response.status_code}")
            return False

        try:
            body = response.json()
        except ValueError:
            print("Telegram returned invalid JSON")
            return False

        if not body.get("ok"):
            print(f"Telegram API error: {body}")
            return False

        return True
    except requests.RequestException as exc:
        print(f"Telegram error: {exc}")
        return False


def load_json(path):
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"load error for {path}: {exc}")
        return {}


def save_json(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        print(f"save error for {path}: {exc}")


def parse_ts_safe(ts_str):
    if not isinstance(ts_str, str) or not ts_str:
        return None

    try:
        timestamp = datetime.fromisoformat(ts_str)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        return timestamp
    except ValueError:
        return None


def load_cooldown():
    data = load_json(COOLDOWN_FILE)
    now = now_utc()
    cleaned = {}

    for symbol, info in data.items():
        if not isinstance(info, dict):
            continue

        timestamp = parse_ts_safe(info.get("ts", ""))
        if timestamp is None:
            continue

        stage = info.get("stage")
        if stage == "BREAKOUT":
            minutes = COOLDOWN_BREAKOUT_MINUTES
        elif stage == "WATCHLIST":
            minutes = COOLDOWN_WATCHLIST_MINUTES
        else:
            continue

        age_seconds = (now - timestamp).total_seconds()
        if 0 <= age_seconds < minutes * 60:
            cleaned[symbol] = {
                "ts": timestamp.isoformat(),
                "stage": stage,
            }

    if cleaned != data:
        save_json(COOLDOWN_FILE, cleaned)

    return cleaned


def cooldown_blocks(cooldown, symbol, stage, now=None):
    info = cooldown.get(symbol)
    if not isinstance(info, dict):
        return False

    timestamp = parse_ts_safe(info.get("ts", ""))
    if timestamp is None:
        return False

    current_time = now or now_utc()
    age_minutes = (current_time - timestamp).total_seconds() / 60.0
    if age_minutes < 0:
        return True

    previous_stage = info.get("stage", "")

    if previous_stage == "BREAKOUT":
        return age_minutes < COOLDOWN_BREAKOUT_MINUTES

    if previous_stage == "WATCHLIST":
        return stage == "WATCHLIST" and age_minutes < COOLDOWN_WATCHLIST_MINUTES

    return False


def record_alert(symbol, stage, now=None):
    alert_time = now or now_utc()

    history = load_json(HISTORY_FILE)
    if symbol not in history or not isinstance(history[symbol], dict):
        history[symbol] = {"events": []}

    events = history[symbol].get("events")
    if not isinstance(events, list):
        events = []

    events.append(alert_time.isoformat())

    cleaned_events = []
    for ts in events:
        timestamp = parse_ts_safe(ts)
        if timestamp is None:
            continue
        age_seconds = (alert_time - timestamp).total_seconds()
        if 0 <= age_seconds < HISTORY_HOURS * 3600:
            cleaned_events.append(ts)

    history[symbol]["events"] = cleaned_events
    save_json(HISTORY_FILE, history)

    cooldown = load_json(COOLDOWN_FILE)
    cooldown[symbol] = {"ts": alert_time.isoformat(), "stage": stage}
    save_json(COOLDOWN_FILE, cooldown)

    return {"ts": alert_time.isoformat(), "stage": stage}


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
            "price": data.get("price") or data.get("current_price"),
            "atr_slope_12h": data.get("atr_slope_12h"),
            "vol_slope_12h": data.get("vol_slope_12h"),
            "trades_slope_12h": data.get("trades_slope_12h"),
            "range_4h_pct": data.get("range_4h_pct"),
            "range_12h_pct": data.get("range_12h_pct"),
            "rsi_15m": data.get("rsi_15m") if data.get("rsi_15m") is not None else data.get("rsi"),
            "taker_buy_ratio": data.get("taker_buy_ratio") if data.get("taker_buy_ratio") is not None else data.get("taker_ratio"),
            "vol_ratio_1h_24h": data.get("vol_ratio_1h_24h") if data.get("vol_ratio_1h_24h") is not None else data.get("vol_ratio_now"),
            "base_high_4h": data.get("base_high_4h") if data.get("base_high_4h") is not None else data.get("base_high"),
            "atr_slope_now": data.get("atr_slope_now"),
            "vol_ratio_now": data.get("vol_ratio_now"),
            "trades_slope_now": data.get("trades_slope_now"),
            "break_pct": data.get("break_pct"),
        })

        if len(signals["signals"]) > 500:
            signals["signals"] = signals["signals"][-500:]

        save_json(SIGNALS_FILE, signals)
    except Exception as exc:
        print(f"log_signal error: {exc}")


def log_rejection(symbol, reasons, price=0):
    try:
        data = load_json(REJECT_FILE)
        if symbol not in data or not isinstance(data.get(symbol), dict):
            data[symbol] = {"symbol": symbol, "count": 0}

        data[symbol]["count"] = int(data[symbol].get("count", 0)) + 1
        data[symbol]["last_seen"] = now_utc().isoformat()
        data[symbol]["price"] = price
        data[symbol]["top_reason"] = ",".join(reasons) if reasons else "unknown"

        if len(data) > 200:
            valid = {
                key: value
                for key, value in data.items()
                if isinstance(value, dict) and value.get("last_seen")
            }
            sorted_items = sorted(
                valid.items(),
                key=lambda item: item[1].get("last_seen", ""),
                reverse=True,
            )
            data = dict(sorted_items[:200])

        save_json(REJECT_FILE, data)
    except Exception as exc:
        print(f"reject log error: {exc}")


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0

    gains = []
    losses = []
    for previous, current in zip(closes, closes[1:]):
        diff = current - previous
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_rsi_15m(closes_5m):
    if len(closes_5m) < 45:
        return 50.0
    closes_15m = closes_5m[-45::3]
    return compute_rsi(closes_15m, 14)


def _closed_klines(k5, now_ms=None):
    if not isinstance(k5, list):
        return []

    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    closed = []

    for kline in k5:
        if not isinstance(kline, list) or len(kline) < 12:
            continue

        try:
            close_time = int(kline[KLINE_CLOSE_TIME])
        except (TypeError, ValueError):
            continue

        if close_time < current_ms:
            closed.append(kline)

    return closed


def compute_trend_features(k5):
    if not isinstance(k5, list) or len(k5) < MIN_RAW_CANDLES:
        return None

    closed = _closed_klines(k5)
    if len(closed) < MIN_CLOSED_CANDLES:
        return None

    try:
        highs = [float(k[KLINE_HIGH]) for k in closed]
        lows = [float(k[KLINE_LOW]) for k in closed]
        closes = [float(k[KLINE_CLOSE]) for k in closed]
        volumes = [float(k[KLINE_VOLUME]) for k in closed]
        trades = [float(k[KLINE_TRADES]) for k in closed]
        taker_buy_base = [float(k[KLINE_TAKER_BUY_BASE_VOLUME]) for k in closed]
    except (TypeError, ValueError, IndexError):
        return None

    if any(price <= 0 for price in highs + lows + closes):
        return None
    if any(high < low for high, low in zip(highs, lows)):
        return None
    if any(volume < 0 for volume in volumes):
        return None
    if any(count < 0 for count in trades):
        return None
    if any(volume < 0 for volume in taker_buy_base):
        return None

    trs = []
    for i in range(1, len(closed)):
        previous_close = closes[i - 1]
        true_range = max(
            highs[i] - lows[i],
            abs(highs[i] - previous_close),
            abs(lows[i] - previous_close),
        )
        trs.append(true_range)

    atr_period = 14
    if len(trs) < atr_period:
        return None

    atr_series = [
        sum(trs[i - atr_period:i]) / atr_period
        for i in range(atr_period, len(trs) + 1)
    ]

    if len(atr_series) < 144 or len(volumes) < 288 or len(trades) < 144:
        return None

    atr_1h = sum(atr_series[-12:]) / 12
    atr_12h = sum(atr_series[-144:]) / 144
    if atr_12h <= 0:
        return None

    atr_slope = (atr_1h - atr_12h) / atr_12h * 100.0

    vol_1h = sum(volumes[-12:]) / 12
    vol_12h = sum(volumes[-144:]) / 144
    vol_24h = sum(volumes[-288:]) / 288
    if vol_12h <= 0 or vol_24h <= 0:
        return None

    vol_slope = (vol_1h - vol_12h) / vol_12h * 100.0
    vol_ratio_1h_24h = vol_1h / vol_24h

    trades_1h = sum(trades[-12:]) / 12
    trades_12h = sum(trades[-144:]) / 144
    if trades_12h <= 0:
        return None

    trades_slope = (trades_1h - trades_12h) / trades_12h * 100.0

    range_4h = (max(highs[-48:]) - min(lows[-48:])) / min(lows[-48:]) * 100.0
    atr_1h_pct = atr_1h / closes[-1] * 100.0
    ret_1h_pct = (closes[-1] / closes[-13] - 1.0) * 100.0
    range_12h = (max(highs[-144:]) - min(lows[-144:])) / min(lows[-144:]) * 100.0

    rsi_15m = compute_rsi_15m(closes)

    recent_taker_buy_base = sum(taker_buy_base[-12:])
    recent_base_volume = sum(volumes[-12:])
    taker_ratio = (
        recent_taker_buy_base / recent_base_volume
        if recent_base_volume > 0
        else 0.5
    )
    taker_ratio = min(max(taker_ratio, 0.0), 1.0)

    base_high_4h = max(highs[-48:])

    return {
        "atr_slope_12h": atr_slope,
        "atr_1h_pct": atr_1h_pct,
        "ret_1h_pct": ret_1h_pct,
        "vol_slope_12h": vol_slope,
        "trades_slope_12h": trades_slope,
        "range_4h_pct": range_4h,
        "range_12h_pct": range_12h,
        "vol_ratio_1h_24h": vol_ratio_1h_24h,
        "rsi_15m": rsi_15m,
        "taker_buy_ratio": taker_ratio,
        "base_high_4h": base_high_4h,
        "current_price": closes[-1],
    }


def fetch_current_price(symbol, fallback=None):
    data = http_get_json(
        f"{BASE_URL}/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=10,
        retries=1,
    )

    try:
        if isinstance(data, dict):
            price = float(data["price"])
        elif isinstance(data, list) and data:
            price = float(data[0]["price"])
        else:
            raise ValueError("unexpected ticker response")

        if price > 0:
            return price
    except (KeyError, TypeError, ValueError, IndexError):
        pass

    return float(fallback) if fallback is not None else None


def detect_watchlist(features):
    if features is None:
        return False, "no_features"

    if features["atr_1h_pct"] < SIG_ATR_1H_PCT_MIN:
        return False, f"atr1h_{features['atr_1h_pct']:.2f}%"
    if features["ret_1h_pct"] < SIG_RET_1H_MIN:
        return False, f"ret1h_{features['ret_1h_pct']:.2f}%"
    return True, features

    if reasons:
        return False, ",".join(reasons)
    return True, features


def check_confirmation(entry, features_now):
    if features_now is None:
        return False, "no_features"

    try:
        base_high = float(entry.get("base_high_4h", 0))
        atr_before = float(entry.get("atr_slope_12h", 0))
        current_price = float(features_now["current_price"])
    except (TypeError, ValueError, KeyError):
        return False, "bad_entry"

    if base_high <= 0 or current_price <= 0:
        return False, "bad_price"

    if current_price <= base_high:
        return False, "no_break"

    break_pct = (current_price - base_high) / base_high * 100.0
    if break_pct > MAX_EXTENSION_PCT:
        return False, f"too_late_{break_pct:.1f}%"

    atr_now = float(features_now["atr_slope_12h"])

    if atr_before > 0 and atr_now < atr_before * 0.5:
        return False, "atr_collapse"

    if features_now["vol_ratio_1h_24h"] < 0.5:
        return False, "vol_dead"
    if features_now["trades_slope_12h"] < 0:
        return False, "trades_dead"

    return True, {
        "base_high": base_high,
        "current_price": current_price,
        "break_pct": break_pct,
        "atr_slope_now": atr_now,
        "vol_ratio_now": features_now["vol_ratio_1h_24h"],
        "trades_slope_now": features_now["trades_slope_12h"],
        "rsi": features_now["rsi_15m"],
        "taker_ratio": features_now["taker_buy_ratio"],
    }


def fetch_klines_5m(symbol, limit=KLINE_LIMIT):
    data = http_get_json(
        f"{BASE_URL}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": KLINE_INTERVAL,
            "limit": limit,
        },
        timeout=15,
        retries=1,
    )

    if not isinstance(data, list) or len(data) < MIN_RAW_CANDLES:
        return None
    return data


def scan(session, ist_hour):
    print(f"Scanning {len(MONITORING_TOKENS)} monitoring tokens...")

    cooldown = load_cooldown()
    watchlist_state = load_json(WATCHLIST_STATE_FILE)
    if not isinstance(watchlist_state, dict):
        watchlist_state = {}

    now = now_utc()

    def _scan_one(symbol):
        try:
            k5 = fetch_klines_5m(symbol)
            if k5 is None:
                return symbol, None, "no_data"

            features = compute_trend_features(k5)
            if features is None:
                return symbol, None, "no_features"

            live_price = fetch_current_price(symbol, fallback=features["current_price"])
            if live_price is None:
                return symbol, None, "no_price"

            features["current_price"] = live_price
            return symbol, features, None
        except Exception as exc:
            return symbol, None, f"scan_error:{type(exc).__name__}"

    # Fetch the complete 32-coin snapshot first. Partial coverage is not acceptable
    # for the riskier monitoring scanner because a missing coin can hide a signal.
    features_by_symbol = {}
    coverage_failures = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_scan_one, symbol): symbol
            for symbol in MONITORING_TOKENS
        }

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                symbol, features, reason = future.result()
            except Exception as exc:
                print(f"Worker failure for {symbol}: {exc}")
                coverage_failures[symbol] = f"worker:{type(exc).__name__}"
                continue

            if features is not None:
                features_by_symbol[symbol] = features
            else:
                coverage_failures[symbol] = reason or "unknown"

    print(
        f"Coverage: {len(features_by_symbol)}/{len(MONITORING_TOKENS)}"
        + (
            f" | Missing: {', '.join(sorted(coverage_failures))}"
            if coverage_failures
            else ""
        )
    )

    if len(features_by_symbol) != len(MONITORING_TOKENS):
        missing = sorted(set(MONITORING_TOKENS) - set(features_by_symbol))
        raise RuntimeError(
            f"Monitoring coverage incomplete: {len(features_by_symbol)}/{len(MONITORING_TOKENS)} "
            f"coins available. Missing: {', '.join(missing)}"
        )

    # STAGE 2: confirmations use the same fresh snapshot as the WATCH detector.
    confirmations = []
    for symbol, entry in list(watchlist_state.items()):
        if not isinstance(entry, dict):
            del watchlist_state[symbol]
            continue

        entry_dt = parse_ts_safe(entry.get("ts", ""))
        if entry_dt is None:
            del watchlist_state[symbol]
            continue

        hours_since = (now - entry_dt).total_seconds() / 3600.0
        if hours_since < 0 or hours_since > CONFIRM_MAX_HOURS:
            del watchlist_state[symbol]
            continue
        if hours_since < CONFIRM_MIN_HOURS:
            continue

        features_now = features_by_symbol.get(symbol)
        if features_now is None:
            # Defensive only; full coverage check above should make this unreachable.
            raise RuntimeError(f"Missing confirmation snapshot for {symbol}")

        ok, info = check_confirmation(entry, features_now)
        if ok:
            confirmations.append({"symbol": symbol, "info": info})

    # STAGE 1: new WATCH candidates from the researched winning pattern.
    active_watchlist = set(watchlist_state.keys())
    watchlist_hits = []
    rejection = {}

    for symbol in MONITORING_TOKENS:
        if symbol in active_watchlist:
            continue

        features = features_by_symbol[symbol]
        ok, info = detect_watchlist(features)

        if ok:
            watchlist_hits.append({"symbol": symbol, "features": info})
        elif isinstance(info, str):
            reason_key = info.split("_", 1)[0]
            rejection[reason_key] = rejection.get(reason_key, 0) + 1
            log_rejection(symbol, [info])

    watchlist_hits.sort(key=lambda item: item["symbol"])
    confirmations.sort(key=lambda item: item["symbol"])

    print(f"Rejection: {rejection}")
    print(f"New WATCHLIST: {len(watchlist_hits)}")
    print(f"Confirmations ready: {len(confirmations)}")

    return watchlist_hits, confirmations, watchlist_state, cooldown

def get_session_label(hour):
    if 8 <= hour <= 11:
        return "Asia"
    if 12 <= hour <= 15:
        return "Europe"
    if 18 <= hour <= 22:
        return "US"
    return "Off"


def main():
    now = now_utc()
    ist = now + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour)
    print(f"Monitoring Scanner starting at {ist} IST — Session: {session}")

    if not SIGNALS_FILE.exists():
        save_json(SIGNALS_FILE, {"signals": []})

    watchlist_hits, confirmations, watchlist_state, cooldown = scan(
        session,
        ist.hour,
    )

    for confirmation in confirmations:
        symbol = confirmation["symbol"]
        info = confirmation["info"]

        msg = (
            f"🟢 <b>MONITORING • ENTER</b>\n\n"
            f"<b>NAME:</b> {html.escape(display_name(symbol))}\n"
            f"<b>ENTRY:</b> {format_price(info['current_price'])}\n"
            f"<b>BREAK:</b> +{info['break_pct']:.2f}%\n\n"
            f"✅ <b>ENTER NOW.</b>"
        )

        if send_telegram(msg):
            alert_ts = now_utc()
            cooldown[symbol] = record_alert(symbol, "BREAKOUT", now=alert_ts)
            log_signal(symbol, "BREAKOUT", info, session)
            watchlist_state.pop(symbol, None)
        else:
            entry = watchlist_state.get(symbol)
            if not isinstance(entry, dict):
                continue
            failures = int(entry.get("failed_sends", 0)) + 1
            if failures >= MAX_CONFIRM_SEND_FAILURES:
                print(f"Dropping {symbol} after {failures} failed confirmation sends")
                watchlist_state.pop(symbol, None)
            else:
                entry["failed_sends"] = failures

    for hit in watchlist_hits:
        symbol = hit["symbol"]
        features = hit["features"]

        if cooldown_blocks(cooldown, symbol, stage="WATCHLIST"):
            continue

        msg = (
            f"🔴 <b>MONITORING • WATCH</b>\n\n"
            f"<b>NAME:</b> {html.escape(display_name(symbol))}\n"
            f"<b>ENTRY:</b> {format_price(features['base_high_4h'])}\n"
            f"<b>NOW:</b> {format_price(features['current_price'])}\n\n"
            f"⚠️ <b>DO NOT BUY YET.</b>"
        )

        if send_telegram(msg):
            alert_ts = now_utc()
            cooldown[symbol] = record_alert(symbol, "WATCHLIST", now=alert_ts)
            log_signal(symbol, "WATCHLIST", features, session)
            watchlist_state[symbol] = {
                "ts": alert_ts.isoformat(),
                "base_high_4h": features["base_high_4h"],
                "atr_slope_12h": features["atr_slope_12h"],
                "vol_slope_12h": features["vol_slope_12h"],
                "trades_slope_12h": features["trades_slope_12h"],
                "range_4h_pct": features["range_4h_pct"],
                "entry_price": features["current_price"],
                "failed_sends": 0,
            }

    save_json(WATCHLIST_STATE_FILE, watchlist_state)
    print(f"Saved state with {len(watchlist_state)} active watchlist entries")


def safe_main():
    try:
        main()
    except Exception as exc:
        send_telegram(
            "🚨 SCANNER CRASHED\n\n"
            f"Error: {html.escape(str(exc)[:300])}"
        )
        raise


if __name__ == "__main__":
    safe_main()
