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
SIGNALS_FILE = Path("monitor/monitor_signals.json")  # fixed path

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
    """Return True if this stage is blocked by existing cooldown."""
    info = cooldown.get(symbol)
    if not isinstance(info, dict):
        return False

    ts = parse_ts_safe(info.get("ts", ""))
    if ts is None:
        return False

    age_min = (now_utc() - ts).total_seconds() / 60
    prev_stage = info.get("stage", "")

    # A recent BREAKOUT blocks everything
    if prev_stage == "BREAKOUT":
        return age_min < COOLDOWN_BREAKOUT_MINUTES

    # A WATCHLIST cooldown only blocks another WATCHLIST
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


def count_dead_base_hours(k15):
    dead = 0
    # only completed candles
    completed = k15[:-1]
    for k in reversed(completed):
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


def check_pre_pump(k15, k5):
    """Detect tight base + volume waking up. Only completed candles."""
    try:
        closed15 = k15[:-1]
        closes = [float(k[4]) for k in closed15]
        highs = [float(k[2]) for k in closed15]
        lows = [float(k[3]) for k in closed15]

        current_price = closes[-1]

        recent_high = max(highs[-16:])
        recent_low = min(lows[-16:])
        if recent_low <= 0:
            return None

        base_range_pct = ((recent_high - recent_low) / recent_low) * 100
        if base_range_pct > 4.0:
            return None

        # volume: use completed 5m candles
        closed5 = k5[:-1]
        current_5m_vol = float(closed5[-1][5])
        baseline = [float(k[5]) for k in closed5[-22:-1]]
        if not baseline:
            return None
        avg_5m_vol = sum(baseline) / len(baseline)
        if avg_5m_vol <= 0:
            return None
        vol_ratio = current_5m_vol / avg_5m_vol
        if vol_ratio < 1.5:
            return None

        dist_to_base_high = ((recent_high - current_price) / current_price) * 100
        if dist_to_base_high < 0.5:
            return None

        return {
            "price": current_price,
            "base_range_pct": base_range_pct,
            "vol_ratio": vol_ratio,
            "dist_to_base_high": dist_to_base_high,
            "base_high": recent_high,
            "base_low": recent_low,
        }
    except Exception as e:
        print(f"pre_pump error: {e}")
        return None


def detect_stage(symbol, ist_hour):
    reasons = []
    try:
        url_15m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        r_15m = requests.get(url_15m, timeout=15)
        k15 = r_15m.json()
        if not isinstance(k15, list) or len(k15) < 50:
            return None, ["not_enough_15m"]

        url_5m = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=5m&limit=30"
        r_5m = requests.get(url_5m, timeout=10)
        k5 = r_5m.json()
        if not isinstance(k5, list) or len(k5) < 15:
            return None, ["not_enough_5m"]

        # WATCHLIST check first
        pre_pump = check_pre_pump(k15, k5)
        if pre_pump:
            return "WATCHLIST", {
                "price": pre_pump["price"],
                "base_range_pct": pre_pump["base_range_pct"],
                "vol_ratio": pre_pump["vol_ratio"],
                "dist_to_base_high": pre_pump["dist_to_base_high"],
                "base_high": pre_pump["base_high"],
                "tier": 1 if symbol in OWN_CHAIN else 2,
                "dead_hours": 0,
                "change_5m": 0,
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

        # BREAKOUT logic
        completed_5m = k5[-2]
        prev_completed_5m = k5[-3]

        c_open = float(completed_5m[1])
        c_high = float(completed_5m[2])
        c_low = float(completed_5m[3])
        c_close = float(completed_5m[4])
        c_vol = float(completed_5m[5])

        if c_close <= 0 or c_low <= 0:
            return None, ["bad_price"]

        prev_close = float(prev_completed_5m[4])
        if prev_close <= 0:
            return None, ["bad_prev"]
        change_5m = ((c_close - prev_close) / prev_close) * 100

        price_1h_ago = float(k15[-6][4])
        change_1h = ((c_close - price_1h_ago) / price_1h_ago) * 100

        price_6h_ago = float(k15[-26][4])
        change_6h = ((c_close - price_6h_ago) / price_6h_ago) * 100

        quiet_vols_5m = [float(k[5]) for k in k5[-22:-2]]
        quiet_avg_5m = sum(quiet_vols_5m) / len(quiet_vols_5m) if quiet_vols_5m else 0
        if quiet_avg_5m <= 0:
            return None, ["quiet_avg_zero"]
        explosion_ratio = c_vol / quiet_avg_5m

        rng = c_high - c_low
        if rng <= 0:
            return None, ["zero_range"]
        upper_wick = (c_high - max(c_open, c_close)) / rng
        clv = (c_close - c_low) / rng

        dead_hours = count_dead_base_hours(k15)
        closes_15m = [float(k[4]) for k in k15[:-1]]
        rsi_15m = compute_rsi(closes_15m, 14)

        recent_taker = sum(float(k[9]) for k in k5[-5:-1])
        recent_total = sum(float(k[5]) for k in k5[-5:-1])
        buy_pressure = recent_taker / recent_total if recent_total > 0 else 0

        bid_depth, ask_depth = check_depth(symbol, c_close)
        bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 0

        tier = 1 if symbol in OWN_CHAIN else 2
        if tier == 1:
            min_5m = 1.2
            min_vol = 2.0
        else:
            min_5m = 1.5
            min_vol = 2.5

        prime = PRIME_START_H <= ist_hour < PRIME_END_H
        if prime:
            min_5m -= 0.3
            min_vol -= 0.5

        if c_close <= c_open:
            reasons.append("red_candle")
        if change_5m < min_5m:
            reasons.append(f"5m_{change_5m:.1f}%")
        if change_5m > 6.0:
            reasons.append(f"5m_high_{change_5m:.1f}%")
        if explosion_ratio < min_vol:
            reasons.append(f"vol_{explosion_ratio:.1f}x")
        if upper_wick > 0.35:
            reasons.append(f"wick_{upper_wick*100:.0f}%")
        if clv < 0.70:
            reasons.append(f"clv_{clv:.2f}")
        if buy_pressure < 0.55:
            reasons.append(f"buy_{buy_pressure*100:.0f}%")
        if bid_ask_ratio < 0.9:
            reasons.append(f"ratio_{bid_ask_ratio:.2f}")
        if bid_depth < 15_000:
            reasons.append(f"bid_{bid_depth:.0f}")
        if ask_depth < 15_000:
            reasons.append(f"ask_{ask_depth:.0f}")

        if reasons:
            return None, reasons

        return "BREAKOUT", {
            "price": c_close,
            "tier": tier,
            "dead_hours": dead_hours,
            "change_5m": change_5m,
            "change_1h": change_1h,
            "change_6h": change_6h,
            "quiet_ratio": 1.0,
            "explosion_ratio": explosion_ratio,
            "vol_5m_ratio": explosion_ratio,
            "rsi": rsi_15m,
            "buy_pressure": buy_pressure * 100,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "bid_ask_ratio": bid_ask_ratio,
            "prime": prime,
        }
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
                f"<b>Distance to Base High:</b> {h['dist_to_base_high']:.2f}%\n\n"
                f"⚠️ <b>DO NOT BUY YET.</b> Watch for a breakout above {format_price(h['base_high'])}."
            )
            send_telegram(msg)
            record_alert(h["symbol"], "WATCHLIST")
            log_signal(h["symbol"], "WATCHLIST", h, session)
            continue

        if stage == "BREAKOUT":
            tier = h.get("tier", 2)
            tier_label = "Tier 1 — Own Chain" if tier == 1 else "Tier 2 — Token"
            prime_flag = " ⚡PRIME" if h.get("prime") else ""
            header = f"🚀 BREAKOUT [{tier_label}]{prime_flag} [{session}]"

            record_alert(h["symbol"], "BREAKOUT")
            log_signal(h["symbol"], "BREAKOUT", h, session)
            fired_symbols.add(h["symbol"])

            stop = h["price"] * 0.97
            target_5 = h["price"] * 1.05
            target_10 = h["price"] * 1.10
            target_25 = h["price"] * 1.25

            msg = (
                f"{header}\n\n"
                f"<b>Coin:</b> {h['symbol']}\n"
                f"<b>Price:</b> {format_price(h['price'])}\n"
                f"<b>Dead Base:</b> {h['dead_hours']:.1f}h\n"
                f"<b>5m Change:</b> {h['change_5m']:+.2f}%\n"
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
            send_telegram(msg)

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
