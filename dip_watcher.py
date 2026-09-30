import os
import json
import html
import requests
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BINANCE_API = "https://data-api.binance.vision"
DIP_STATE_FILE = Path("dip_state.json")
DIP_CONFIRMED_FILE = Path("dip_confirmed.json")

WATCH_END_MIN = 90
MISSED_PUMP_PCT = 3.0
DIP_ENTRY_MIN = -3.5
DIP_ENTRY_MAX = -1.0


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
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram HTTP error: {r.status_code} - {r.text}")
            return
        body = r.json()
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


def parse_ts(s):
    try:
        t = datetime.fromisoformat(s)
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return t
    except Exception:
        return None


def get_price(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/ticker/price?symbol={symbol}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return 0.0
        return float(r.json().get("price", 0))
    except Exception:
        return 0.0


def load_pending_signals(state):
    sig_file = Path("monitor/monitor_signals.json")
    data = load_json(sig_file)
    entries = data.get("signals", [])
    if not isinstance(entries, list):
        return 0

    pending = state.get("pending", [])
    if not isinstance(pending, list):
        pending = []
    seen = state.get("seen", {})
    if not isinstance(seen, dict):
        seen = {}

    added = 0
    for s in entries:
        if not isinstance(s, dict):
            continue
        ts = s.get("ts")
        symbol = s.get("symbol")
        price = s.get("price")
        stage = s.get("stage", "BREAKOUT")
        if not ts or not symbol or not price:
            continue
        if stage != "BREAKOUT":
            continue
        try:
            sp = float(price)
        except (ValueError, TypeError):
            continue
        if sp <= 0:
            continue

        key = f"{ts}|{symbol}"
        if key in seen:
            continue

        pending.append({
            "key": key,
            "symbol": symbol,
            "signal_price": sp,
            "signal_ts": ts,
            "session": s.get("session", "?"),
            "change_1h": s.get("change_1h"),
            "change_6h": s.get("change_6h"),
            "vol_ratio": s.get("volume_ratio"),
            "quiet_ratio": s.get("quiet_ratio"),
            "buy_pressure": s.get("buy_pressure"),
            "bid_depth": s.get("bid_depth"),
            "ask_depth": s.get("ask_depth"),
            "bid_ask_ratio": s.get("bid_ask_ratio"),
            "lowest_price": sp,
            "lowest_ts": ts,
            "highest_price": sp,
            "highest_ts": ts,
            "status": "WAITING",
            "entered": False,
            "missed_notified": False,
            "failed_notified": False,
            "stale_notified": False,
        })
        seen[key] = True
        added += 1

    state["pending"] = pending
    state["seen"] = seen
    return added


def decide(p, current_price, now):
    sig = p["signal_price"]
    low = min(p["lowest_price"], current_price)
    high = max(p["highest_price"], current_price)

    dip_pct = (low - sig) / sig * 100
    up_pct = (high - sig) / sig * 100

    if dip_pct <= DIP_ENTRY_MIN and dip_pct >= DIP_ENTRY_MAX:
        return "ENTER", dip_pct
    if dip_pct < DIP_ENTRY_MIN:
        return "FAILED", dip_pct

    if up_pct >= MISSED_PUMP_PCT:
        return "MISSED", up_pct

    signal_dt = parse_ts(p["signal_ts"])
    if signal_dt:
        age_min = (now - signal_dt).total_seconds() / 60
        if age_min >= WATCH_END_MIN:
            return "STALE", dip_pct

    return None, None


def main():
    state = load_json(DIP_STATE_FILE)
    if "pending" not in state or not isinstance(state["pending"], list):
        state["pending"] = []
    if "seen" not in state or not isinstance(state["seen"], dict):
        state["seen"] = {}

    added = load_pending_signals(state)
    print(f"New signals to watch: {added}")

    pending = state["pending"]
    print(f"Currently watching: {len(pending)}")

    if not pending:
        save_json(DIP_STATE_FILE, state)
        return

    symbols = list({p["symbol"] for p in pending})
    prices = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(get_price, sym): sym for sym in symbols}
        for f in as_completed(futures):
            prices[futures[f]] = f.result()

    now = now_utc()
    confirmed = load_json(DIP_CONFIRMED_FILE)
    if not isinstance(confirmed.get("signals"), list):
        confirmed["signals"] = []

    still_waiting = []
    for p in pending:
        cp = prices.get(p["symbol"], 0)
        if cp <= 0:
            still_waiting.append(p)
            continue

        if cp < p["lowest_price"]:
            p["lowest_price"] = cp
            p["lowest_ts"] = now.isoformat()
        if cp > p["highest_price"]:
            p["highest_price"] = cp
            p["highest_ts"] = now.isoformat()

        decision, pct = decide(p, cp, now)

        if decision is None:
            still_waiting.append(p)
            continue

        sig = p["signal_price"]

        if decision == "ENTER":
            if p.get("entered"):
                continue
            p["entered"] = True

            entry_price = p["lowest_price"]
            stop_price = entry_price * 0.97
            dip_at_entry = (entry_price - sig) / sig * 100

            confirmed["signals"].append({
                "ts": now.isoformat(),
                "entry_ts": now.isoformat(),
                "signal_ts": p["signal_ts"],
                "session": p["session"],
                "symbol": p["symbol"],
                "stage": "BREAKOUT",
                "price": entry_price,
                "signal_price": sig,
                "dip_pct": dip_at_entry,
                "change_1h": p.get("change_1h"),
                "change_6h": p.get("change_6h"),
                "volume_ratio": p.get("vol_ratio"),
                "quiet_ratio": p.get("quiet_ratio"),
                "buy_pressure": p.get("buy_pressure"),
                "bid_depth": p.get("bid_depth"),
                "ask_depth": p.get("ask_depth"),
                "bid_ask_ratio": p.get("bid_ask_ratio"),
            })

            msg = (
                f"✅ <b>DIP CONFIRMED — ENTER NOW</b>\n\n"
                f"<b>Coin:</b> {p['symbol']}\n"
                f"<b>Signal Price:</b> {format_price(sig)}\n"
                f"<b>Entry (Dip):</b> {format_price(entry_price)}\n"
                f"<b>Dip:</b> {dip_at_entry:+.2f}%\n"
                f"<b>Stop Loss:</b> {format_price(stop_price)} (-3% from entry)\n"
                f"<b>Session:</b> {p.get('session', '?')}\n\n"
                f"<b>Buy now at market. Set SL at {format_price(stop_price)}.</b>"
            )
            send_telegram(msg)
            print(f"ENTER: {p['symbol']} at {entry_price} (dip {dip_at_entry:.2f}%)")

        elif decision == "MISSED":
            if p.get("missed_notified"):
                continue
            p["missed_notified"] = True

            msg = (
                f"⚠️ <b>MISSED — {p['symbol']}</b>\n\n"
                f"Signal: {format_price(sig)}\n"
                f"Pumped to: {format_price(p['highest_price'])} ({pct:+.2f}%)\n"
                f"No dip. Skipping entry.\n"
                f"<b>Do not chase.</b>"
            )
            send_telegram(msg)
            print(f"MISSED: {p['symbol']}")

        elif decision == "FAILED":
            if p.get("failed_notified"):
                continue
            p["failed_notified"] = True

            msg = (
                f"❌ <b>FAILED — {p['symbol']}</b>\n\n"
                f"Signal: {format_price(sig)}\n"
                f"Dropped to: {format_price(p['lowest_price'])} ({pct:+.2f}%)\n"
                f"Setup broke. No entry."
            )
            send_telegram(msg)
            print(f"FAILED: {p['symbol']}")

        elif decision == "STALE":
            if p.get("stale_notified"):
                continue
            p["stale_notified"] = True

            msg = (
                f"⏸️ <b>STALE — {p['symbol']}</b>\n\n"
                f"Signal: {format_price(sig)}\n"
                f"Current: {format_price(cp)} ({pct:+.2f}%)\n"
                f"90 min passed. No dip. Skipping."
            )
            send_telegram(msg)
            print(f"STALE: {p['symbol']}")

    state["pending"] = still_waiting
    save_json(DIP_STATE_FILE, state)

    if len(confirmed["signals"]) > 500:
        confirmed["signals"] = confirmed["signals"][-500:]
    save_json(DIP_CONFIRMED_FILE, confirmed)

    print(f"Still waiting: {len(still_waiting)}")
    print(f"Confirmed entries total: {len(confirmed['signals'])}")


def safe_main():
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 DIP WATCHER CRASHED\n\nError: {html.escape(str(e)[:300])}")
        raise


if __name__ == "__main__":
    safe_main()
