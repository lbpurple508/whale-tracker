import html
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "market_state.json"
HISTORY_FILE = BASE_DIR / "market_events.json"

BINANCE_BASE = "https://data-api.binance.vision"
INTERVAL = "5m"
KLINE_LIMIT = 500
MAX_WORKERS = 16
REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 2

PROFILE_RAW = os.environ.get("SCANNER_PROFILE", "").strip()
if not PROFILE_RAW:
    raise RuntimeError("SCANNER_PROFILE secret is required")

try:
    PROFILE = json.loads(PROFILE_RAW)
except json.JSONDecodeError as exc:
    raise RuntimeError("SCANNER_PROFILE must be valid JSON") from exc

PRICE_LIMIT = float(PROFILE["price_cap"])
RANGE_QUANTILE = float(PROFILE["range_quantile"])
ZONE_MIN_PCT = float(PROFILE["zone_low"])
ZONE_MAX_PCT = float(PROFILE["zone_high"])
WATCH_EXPIRY_HOURS = float(PROFILE["watch_ttl_hours"])
COOLDOWN_HOURS = float(PROFILE["symbol_ttl_hours"])
MAX_ENTRY_DRIFT_PCT = float(PROFILE["entry_drift_limit"])
MAX_REVIEW_DRIFT_PCT = float(PROFILE["review_drift_limit"])
RECENT_BARS = int(PROFILE["recent_bars"])

TARGETS = [int(x) for x in PROFILE.get("targets", [5, 10, 20, 50, 100])]

EXCLUDED = {
    str(x).upper()
    for x in PROFILE.get("excluded_symbols", [])
}



def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def tg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
            return False
        body = r.json()
        if not body.get("ok"):
            print(f"Telegram error: {body}")
            return False
        return True
    except Exception as exc:
        print(f"Telegram exception: {exc}")
        return False


def get_json(url, params=None):
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            r = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json()

            retryable = (
                r.status_code == 429
                or 500 <= r.status_code < 600
            )

            if retryable and attempt < REQUEST_RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue

            print(f"HTTP {r.status_code}: {url}")
            return None

        except requests.RequestException as exc:
            if attempt < REQUEST_RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            print(f"Request error: {exc}")
            return None

    return None


def fmt_price(price):
    p = float(price)
    if p >= 100:
        return "$" + f"{p:,.2f}"
    if p >= 1:
        return "$" + f"{p:.4f}"
    if p >= 0.01:
        return "$" + f"{p:.5f}"
    if p >= 0.0001:
        return "$" + f"{p:.7f}"
    return "$" + f"{p:.10f}".rstrip("0").rstrip(".")


def targets(entry):
    return " | ".join(
        f"+{p}% {fmt_price(entry * (1 + p / 100.0))}"
        for p in TARGETS
    )


def load_universe():
    exchange = get_json(
        f"{BINANCE_BASE}/api/v3/exchangeInfo"
    )
    tickers = get_json(
        f"{BINANCE_BASE}/api/v3/ticker/price"
    )

    if not isinstance(exchange, dict):
        raise RuntimeError("exchangeInfo failed")
    if not isinstance(tickers, list):
        raise RuntimeError("ticker/price failed")

    prices = {}

    for x in tickers:
        if not isinstance(x, dict):
            continue
        s = str(x.get("symbol", "")).upper()
        try:
            p = float(x.get("price", 0))
        except (TypeError, ValueError):
            continue
        if s and p > 0 and math.isfinite(p):
            prices[s] = p

    symbols = []

    for x in exchange.get("symbols", []):
        if not isinstance(x, dict):
            continue

        s = str(x.get("symbol", "")).upper()

        if not s.endswith("USDT"):
            continue
        if s in EXCLUDED:
            continue
        if x.get("status") != "TRADING":
            continue
        if x.get("isSpotTradingAllowed") is False:
            continue

        p = prices.get(s)
        if p is None or p > PRICE_LIMIT:
            continue

        symbols.append(s)

    return sorted(symbols), prices


def fetch_klines(symbol):
    data = get_json(
        f"{BINANCE_BASE}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": KLINE_LIMIT,
        },
    )

    if not isinstance(data, list):
        return symbol, None

    out = []

    for row in data:
        if not isinstance(row, list) or len(row) < 11:
            continue

        try:
            item = {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "close_time": int(row[6]),
                "base_volume": float(row[5]),
                "taker_buy_base": float(row[9]),
            }
        except (TypeError, ValueError):
            continue

        if min(
            item["open"],
            item["high"],
            item["low"],
            item["close"],
        ) <= 0:
            continue

        out.append(item)

    if len(out) < 180:
        return symbol, None

    return symbol, out


def closed_only(candles):
    now_ms = int(time.time() * 1000)
    return [
        x for x in candles
        if x["close_time"] < now_ms
    ]


def features_at(closed, index):
    if index < 145:
        return None

    prior = closed[:index]

    highs = np.array(
        [x["high"] for x in prior],
        dtype=float,
    )
    lows = np.array(
        [x["low"] for x in prior],
        dtype=float,
    )
    closes = np.array(
        [x["close"] for x in prior],
        dtype=float,
    )

    prev = closes[:-1]

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - prev),
            np.abs(lows[1:] - prev),
        ),
    )

    if len(tr) < 12:
        return None

    atr_1h = float(
        np.mean(tr[-12:])
    )

    close_now = float(closes[-1])

    atr_1h_pct = (
        atr_1h / close_now * 100.0
    )

    low_12h = float(
        np.min(lows[-144:])
    )
    high_12h = float(
        np.max(highs[-144:])
    )

    if low_12h <= 0:
        return None

    range_12h_pct = (
        (high_12h - low_12h)
        / low_12h
        * 100.0
    )

    resistance = float(
        np.max(highs[-48:])
    )

    base_vol = [
        x["base_volume"]
        for x in prior[-12:]
    ]
    taker_vol = [
        x["taker_buy_base"]
        for x in prior[-12:]
    ]

    total_base = float(
        np.sum(base_vol)
    )
    total_taker = float(
        np.sum(taker_vol)
    )

    taker_ratio = (
        total_taker / total_base
        if total_base > 0
        else 0.5
    )

    return {
        "close": close_now,
        "atr_1h_pct": atr_1h_pct,
        "range_12h_pct": range_12h_pct,
        "previous_4h_high": resistance,
        "taker_ratio": min(
            max(taker_ratio, 0.0),
            1.0,
        ),
    }


def distance(price, resistance):
    if resistance <= 0:
        return 999.0
    return (
        price / resistance - 1.0
    ) * 100.0


def qualifies(f, range_q, atr_q):
    return (
        f is not None
        and f["range_12h_pct"] >= range_q
        and f["atr_1h_pct"] >= atr_q
    )


def setup_from_current(
    candles,
    price,
    range_threshold,
    atr_threshold,
):
    closed = closed_only(candles)
    if len(closed) < 180:
        return None

    f = features_at(
        closed,
        len(closed),
    )
    if f is None:
        return None

    d = distance(
        price,
        f["previous_4h_high"],
    )

    if not (
        ZONE_MIN_PCT <= d <= ZONE_MAX_PCT
    ):
        return None

    if not qualifies(
        f,
        range_threshold,
        atr_threshold,
    ):
        return None

    return {
        "entry": f["previous_4h_high"],
        "distance_before": d,
        "features": f,
    }


def find_recovery(
    candles,
    live_price,
    range_threshold,
    atr_threshold,
    last_breakout,
):
    closed = closed_only(candles)

    if len(closed) < 180:
        return None

    start = max(
        145,
        len(closed) - RECENT_BARS,
    )

    found = None

    for i in range(
        start,
        len(closed),
    ):
        f = features_at(
            closed,
            i,
        )

        if f is None:
            continue

        R = f["previous_4h_high"]

        d = distance(
            f["close"],
            R,
        )

        if not (
            ZONE_MIN_PCT
            <= d
            <= ZONE_MAX_PCT
        ):
            continue

        if not qualifies(
            f,
            range_threshold,
            atr_threshold,
        ):
            continue

        # Candle i is the breakout/touch candle.
        if closed[i]["high"] < R:
            continue

        touch_dt = datetime.fromtimestamp(
            closed[i]["open_time"] / 1000,
            tz=timezone.utc,
        )

        if (
            last_breakout is not None
            and touch_dt <= last_breakout
        ):
            continue

        found = {
            "entry": R,
            "distance_before": d,
            "features": f,
            "touch_time": iso(touch_dt),
            "current_price": live_price,
        }

    return found


def build_watch(symbol, setup):
    f = setup["features"]
    entry = setup["entry"]
    return (
        f"👀 <b>WATCH SETUP</b>\n\n"
        f"<b>PAIR:</b> {html.escape(symbol)}\n"
        f"<b>SETUP:</b> MARKET SIGNAL\n\n"
        f"<b>CURRENT PRICE:</b> {fmt_price(f['close'])}\n"
        f"<b>PREVIOUS 4H HIGH:</b> {fmt_price(entry)}\n"
        f"<b>DISTANCE:</b> {setup['distance_before']:.2f}%\n\n"
        f"🎯 <b>TARGET ENTRY PRICE:</b> {fmt_price(entry)}\n"
        f"<b>TRIGGER:</b> FIRST TOUCH\n\n"
        f"<b>12H RANGE:</b> {f['range_12h_pct']:.2f}%\n"
        f"<b>1H ATR:</b> {f['atr_1h_pct']:.2f}%\n"
        f"<b>TAKER BUY:</b> {f['taker_ratio'] * 100:.1f}%\n\n"
        f"⚠️ <b>DO NOT BUY YET.</b>\n"
        f"Wait for {fmt_price(entry)}."
    )


def build_enter(
    symbol,
    item,
):
    entry = float(item["entry"])
    price = float(item["current_price"])
    f = item["features"]
    late = (
        price / entry - 1.0
    ) * 100.0

    return (
        f"🚀 <b>ENTER TRADE</b>\n\n"
        f"<b>PAIR:</b> {html.escape(symbol)}\n"
        f"<b>SETUP:</b> MARKET SIGNAL\n\n"
        f"🎯 <b>ENTRY PRICE:</b> {fmt_price(entry)}\n"
        f"<b>TRIGGER:</b> PREVIOUS 4H HIGH TOUCH\n"
        f"<b>CURRENT PRICE:</b> {fmt_price(price)}\n"
        f"<b>LATE FROM ENTRY:</b> {late:+.2f}%\n\n"
        f"<b>DISTANCE BEFORE BREAKOUT:</b> "
        f"{item['distance_before']:.2f}%\n"
        f"<b>12H RANGE:</b> {f['range_12h_pct']:.2f}%\n"
        f"<b>1H ATR:</b> {f['atr_1h_pct']:.2f}%\n"
        f"<b>TAKER BUY:</b> {f['taker_ratio'] * 100:.1f}%\n"
        f"<b>DETECTION:</b> {html.escape(item.get('source', 'LIVE'))}\n\n"
        f"<b>TARGET LADDER FROM ENTRY:</b>\n"
        f"{targets(entry)}\n\n"
        f"✅ <b>ENTER TRADE AT / NEAR THE TARGET ENTRY PRICE.</b>"
    )


def build_missed(
    symbol,
    item,
):
    entry = float(item["entry"])
    price = float(item["current_price"])
    late = (
        price / entry - 1.0
    ) * 100.0

    touch_time = item.get(
        "touch_time",
        iso(now()),
    )

    return (
        f"⚠️ <b>MISSED IDEAL ENTRY</b>\n\n"
        f"<b>PAIR:</b> {html.escape(symbol)}\n"
        f"<b>SETUP:</b> MARKET SIGNAL\n\n"
        f"🎯 <b>ORIGINAL ENTRY:</b> {fmt_price(entry)}\n"
        f"<b>CURRENT PRICE:</b> {fmt_price(price)}\n"
        f"<b>LATE BY:</b> +{late:.2f}%\n\n"
        f"<b>ORIGINAL DISTANCE:</b> "
        f"{item['distance_before']:.2f}%\n"
        f"<b>TOUCH TIME:</b> {html.escape(touch_time)}\n\n"
        f"<b>TARGET LADDER FROM ORIGINAL ENTRY:</b>\n"
        f"{targets(entry)}\n\n"
        f"STATUS: <b>MISSED ENTRY — DO NOT CHASE.</b>"
    )


def scan():
    current = now()

    state = load_json(
        STATE_FILE,
        {
            "watch": {},
            "cooldowns": {},
            "last_breakout": {},
            "last_missed": {},
            "events": [],
        },
    )

    for key in [
        "watch",
        "cooldowns",
        "last_breakout",
        "last_missed",
        "events",
    ]:
        if not isinstance(state.get(key), type({
            "watch": {},
            "cooldowns": {},
            "last_breakout": {},
            "last_missed": {},
            "events": [],
        })[key]):
            state[key] = (
                [] if key == "events"
                else {}
            )

    symbols, prices = load_universe()

    print(
        "Eligible Spot USDT markets:",
        len(symbols),
    )

    market = {}

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as pool:

        futures = {
            pool.submit(
                fetch_klines,
                s,
            ): s
            for s in symbols
        }

        for future in as_completed(futures):
            s = futures[future]
            try:
                symbol, candles = future.result()
            except Exception as exc:
                print(
                    f"Kline failure {s}: {exc}"
                )
                continue

            if candles:
                market[symbol] = candles

    if len(market) < 20:
        raise RuntimeError(
            f"Only {len(market)} markets returned candles."
        )

    # Cross-sectional adaptive thresholds.
    latest_features = []

    for candles in market.values():

        closed = closed_only(
            candles
        )

        f = features_at(
            closed,
            len(closed),
        )

        if f:
            latest_features.append(f)

    range_threshold = float(
        np.quantile(
            [
                f["range_12h_pct"]
                for f in latest_features
            ],
            RANGE_QUANTILE,
        )
    )

    atr_threshold = float(
        np.quantile(
            [
                f["atr_1h_pct"]
                for f in latest_features
            ],
            RANGE_QUANTILE,
        )
    )

    print(
        f"Adaptive thresholds: "
        f"range12h >= {range_threshold:.2f}% | "
        f"atr1h >= {atr_threshold:.2f}%"
    )

    enter = []
    missed = []
    watches = []

    # --------------------------------------------------------
    # Existing watches.
    # --------------------------------------------------------
    for symbol, watch in list(
        state["watch"].items()
    ):

        candles = market.get(symbol)

        if candles is None:
            state["watch"].pop(
                symbol,
                None,
            )
            continue

        created = parse_dt(
            watch.get("created_at")
        )

        if created is None:
            state["watch"].pop(
                symbol,
                None,
            )
            continue

        if (
            current - created
        ).total_seconds() > WATCH_EXPIRY_HOURS * 3600:

            state["watch"].pop(
                symbol,
                None,
            )
            continue

        try:
            entry_price = float(
                watch["entry_price"]
            )
        except (KeyError, TypeError, ValueError):
            state["watch"].pop(
                symbol,
                None,
            )
            continue

        price = prices.get(symbol)

        if not price:
            continue

        touched = any(
            x["high"] >= entry_price
            for x in candles[-3:]
        )

        if not touched:
            if distance(
                price,
                entry_price,
            ) < ZONE_MIN_PCT - 0.5:

                state["watch"].pop(
                    symbol,
                    None,
                )
            continue

        item = {
            "entry": entry_price,
            "current_price": price,
            "distance_before": float(
                watch.get(
                    "distance_before",
                    -1.0,
                )
            ),
            "features": (
                features_at(
                    closed_only(candles),
                    len(
                        closed_only(candles)
                    ),
                )
                or {
                    "range_12h_pct": 0,
                    "atr_1h_pct": 0,
                    "taker_ratio": 0.5,
                }
            ),
            "touch_time": iso(current),
            "source": "WATCH",
        }

        late = (
            price / entry_price - 1.0
        ) * 100.0

        if late <= MAX_ENTRY_DRIFT_PCT:
            enter.append(
                (symbol, item)
            )
        elif late <= MAX_REVIEW_DRIFT_PCT:
            missed.append(
                (symbol, item)
            )

        state["watch"].pop(
            symbol,
            None,
        )

    # --------------------------------------------------------
    # New setups + recovery.
    # --------------------------------------------------------
    for symbol, candles in market.items():

        price = prices.get(symbol)

        if not price:
            continue

        if cooldown_active(
            state,
            symbol,
            current,
        ):
            continue

        setup = setup_from_current(
            candles,
            price,
            range_threshold,
            atr_threshold,
        )

        if setup and symbol not in state["watch"]:

            state["watch"][symbol] = {
                "created_at":
                    iso(current),
                "entry_price":
                    setup["entry"],
                "distance_before":
                    setup["distance_before"],
            }

            watches.append(
                (symbol, setup)
            )

        last_breakout = parse_dt(
            state["last_breakout"].get(
                symbol
            )
        )

        recovery = find_recovery(
            candles,
            price,
            range_threshold,
            atr_threshold,
            last_breakout,
        )

        if recovery:

            late = (
                price / recovery["entry"]
                - 1.0
            ) * 100.0

            if late <= MAX_ENTRY_DRIFT_PCT:
                recovery["source"] = "RECOVERED"
                enter.append(
                    (symbol, recovery)
                )

            elif late <= MAX_REVIEW_DRIFT_PCT:
                missed.append(
                    (symbol, recovery)
                )

    # --------------------------------------------------------
    # Send WATCH.
    # --------------------------------------------------------
    for symbol, setup in watches:

        if tg(
            build_watch(
                symbol,
                setup,
            )
        ):

            record_event(
                state,
                "WATCH",
                symbol,
                setup["entry"],
                setup["features"]["close"],
                setup["distance_before"],
            )

    # --------------------------------------------------------
    # Send ENTER.
    # --------------------------------------------------------
    sent = set()

    for symbol, item in enter:

        if symbol in sent:
            continue

        if cooldown_active(
            state,
            symbol,
            current,
        ):
            continue

        message = build_enter(
            symbol,
            item,
        )

        if tg(message):

            entry = float(
                item["entry"]
            )

            state["cooldowns"][symbol] = iso(
                current
            )

            state["last_breakout"][symbol] = iso(
                current
            )

            state["watch"].pop(
                symbol,
                None,
            )

            record_event(
                state,
                "ENTER",
                symbol,
                entry,
                item["current_price"],
                item["distance_before"],
                {
                    "source":
                        item.get(
                            "source",
                            "LIVE",
                        ),
                },
            )

            sent.add(symbol)

    # --------------------------------------------------------
    # Send MISSED.
    # --------------------------------------------------------
    for symbol, item in missed:

        if symbol in sent:
            continue

        if cooldown_active(
            state,
            symbol,
            current,
        ):
            continue

        message = build_missed(
            symbol,
            item,
        )

        if tg(message):

            state["last_missed"][symbol] = iso(
                current
            )

            state["cooldowns"][symbol] = iso(
                current
            )

            state["watch"].pop(
                symbol,
                None,
            )

            record_event(
                state,
                "MISSED",
                symbol,
                item["entry"],
                item["current_price"],
                item["distance_before"],
                {
                    "touch_time":
                        item.get(
                            "touch_time"
                        ),
                },
            )

    # --------------------------------------------------------
    # Keep 30 days of event history.
    # --------------------------------------------------------
    cutoff = current - timedelta(days=30)

    cleaned = []

    for event in state["events"]:

        ts = parse_dt(
            event.get("ts")
        )

        if (
            ts is not None
            and ts >= cutoff
        ):
            cleaned.append(event)

    state["events"] = cleaned[-1000:]

    save_json(
        STATE_FILE,
        state,
    )

    save_json(
        HISTORY_FILE,
        state["events"],
    )

    print(
        f"Active watch: {len(state['watch'])}"
    )
    print(
        f"Watch alerts: {len(watches)}"
    )
    print(
        f"Enter alerts: {len(enter)}"
    )
    print(
        f"Missed alerts: {len(missed)}"
    )


def cooldown_active(state, symbol, current):
    ts = parse_dt(
        state["cooldowns"].get(symbol)
    )

    if ts is None:
        return False

    return (
        current - ts
    ).total_seconds() < COOLDOWN_HOURS * 3600


def record_event(
    state,
    event_type,
    symbol,
    entry,
    current_price,
    distance_before,
    metadata=None,
):
    event = {
        "ts": iso(now()),
        "type": event_type,
        "symbol": symbol,
        "entry": float(entry),
        "current_price": float(current_price),
        "distance_before": float(
            distance_before
        ),
    }

    if metadata:
        event.update(metadata)

    state["events"].append(
        event
    )


if __name__ == "__main__":
    started = time.time()

    print("=" * 80)
    print("BINANCE SPOT MARKET SIGNAL SCANNER")
    print("=" * 80)

    try:
        scan()
    except Exception as exc:
        print(
            f"FATAL: {type(exc).__name__}: {exc}"
        )
        tg(
            "🚨 <b>MARKET SCANNER ERROR</b>\n\n"
            + html.escape(
                str(exc)[:500]
            )
        )
        raise

    print(
        f"Completed in {time.time() - started:.1f}s"
    )
