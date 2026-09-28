import os
import json
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BINANCE_API = "https://data-api.binance.vision"
TRACKER_FILE = Path("tracker_state.json")

STOP_PCT = -3.0
PUMP_TARGET = 30.0

SIGNAL_SOURCES = [
    ("SPIKE", Path("spike/signals.json")),
    ("GRIND", Path("grind/grind_signals.json")),
    ("MONITOR", Path("monitor/monitor_signals.json")),
    ("FUTURES", Path("futures/futures_signals.json")),
]


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
            print(f"Telegram error: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Telegram error: {e}")


def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_json(path, data):
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"save error: {e}")


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
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


def load_new_signals(state):
    seen = state.get("seen", {})
    signals = state.get("signals", [])
    added = 0

    for source, path in SIGNAL_SOURCES:
        data = load_json(path)
        for s in data.get("signals", []):
            ts = s.get("ts")
            symbol = s.get("symbol")
            price = s.get("price")
            if not ts or not symbol or not price:
                continue
            try:
                entry = float(price)
            except (ValueError, TypeError):
                continue
            if entry <= 0:
                continue

            key = f"{source}|{ts}|{symbol}"
            if key in seen:
                continue

            signals.append({
                "key": key,
                "source": source,
                "symbol": symbol,
                "entry": entry,
                "entry_ts": ts,
                "stage": s.get("stage", source),
                "peak": entry,
                "peak_pct": 0.0,
                "peak_ts": ts,
                "dip": entry,
                "dip_pct": 0.0,
                "dip_ts": ts,
                "current": entry,
                "current_pct": 0.0,
                "time_to_peak_min": 0,
                "time_to_dip_min": 0,
                "status": "ACTIVE",
                "last_update": ts,
                "closed_ts": None,
                "exit_pct": None,
            })
            seen[key] = True
            added += 1

    state["signals"] = signals
    state["seen"] = seen
    return added


def update_signals(state):
    signals = state.get("signals", [])
    trackable = [s for s in signals if s.get("status") in ("ACTIVE", "PUMPED")]
    if not trackable:
        return 0

    symbols = list({s["symbol"] for s in trackable})
    prices = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(get_price, sym): sym for sym in symbols}
        for f in as_completed(futures):
            prices[futures[f]] = f.result()

    now = datetime.utcnow()
    updated = 0
    for s in trackable:
        p = prices.get(s["symbol"], 0)
        if p <= 0:
            continue

        s["current"] = p
        s["current_pct"] = ((p - s["entry"]) / s["entry"]) * 100

        if p > s["peak"]:
            s["peak"] = p
            s["peak_pct"] = ((p - s["entry"]) / s["entry"]) * 100
            s["peak_ts"] = now.isoformat()
            t = parse_ts(s["entry_ts"])
            if t:
                s["time_to_peak_min"] = int((now - t).total_seconds() / 60)

        if p < s["dip"]:
            s["dip"] = p
            s["dip_pct"] = ((p - s["entry"]) / s["entry"]) * 100
            s["dip_ts"] = now.isoformat()
            t = parse_ts(s["entry_ts"])
            if t:
                s["time_to_dip_min"] = int((now - t).total_seconds() / 60)

        if s["status"] == "ACTIVE":
            if s["peak_pct"] >= PUMP_TARGET:
                s["status"] = "PUMPED"
            elif s["current_pct"] <= STOP_PCT:
                s["status"] = "STOPPED"
                s["exit_pct"] = s["current_pct"]
                s["closed_ts"] = now.isoformat()

        s["last_update"] = now.isoformat()
        updated += 1

    return updated


def build_report(state):
    signals = state.get("signals", [])
    total = len(signals)
    active = [s for s in signals if s.get("status") == "ACTIVE"]
    pumped = [s for s in signals if s.get("status") == "PUMPED"]
    stopped = [s for s in signals if s.get("status") == "STOPPED"]

    lines = ["📊 <b>SIGNAL TRACKER</b>", ""]
    lines.append(
        f"Total: <b>{total}</b> | "
        f"🟡 Active: <b>{len(active)}</b> | "
        f"🟢 Pumped: <b>{len(pumped)}</b> | "
        f"🔴 Stopped: <b>{len(stopped)}</b>"
    )

    if pumped:
        lines.append("")
        lines.append("🟢 <b>PUMPED (+30%+)</b>")
        for s in pumped[-5:]:
            lines.append(
                f"• <b>{s['symbol']}</b> [{s['source']}] "
                f"{format_price(s['entry'])} → +{s['peak_pct']:.2f}% "
                f"in {s['time_to_peak_min']}m"
            )

    if active:
        lines.append("")
        lines.append("🟡 <b>ACTIVE</b>")
        for s in active[-5:]:
            lines.append(
                f"• <b>{s['symbol']}</b> [{s['source']}] "
                f"{format_price(s['entry'])} → {s['current_pct']:+.2f}% "
                f"(peak {s['peak_pct']:+.2f}%, dip {s['dip_pct']:+.2f}%)"
            )

    if stopped:
        lines.append("")
        lines.append("🔴 <b>STOPPED (-3%)</b>")
        for s in stopped[-5:]:
            lines.append(
                f"• <b>{s['symbol']}</b> [{s['source']}] "
                f"{format_price(s['entry'])} → {s.get('exit_pct', 0):+.2f}%"
            )

    return "\n".join(lines)


def main():
    state = load_json(TRACKER_FILE)
    if "signals" not in state:
        state["signals"] = []
    if "seen" not in state:
        state["seen"] = {}

    added = load_new_signals(state)
    print(f"New signals: {added}")
    print(f"Total tracked: {len(state['signals'])}")

    updated = update_signals(state)
    print(f"Updated: {updated}")

    save_json(TRACKER_FILE, state)

    report = build_report(state)
    send_telegram(report)


if __name__ == "__main__":
    main()
