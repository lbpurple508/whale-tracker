import os
import json
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

STOP_PCT = -3.0
PUMP_TARGET = 30.0
MAX_SIGNAL_AGE_HOURS = 48

SIGNAL_SOURCES = [
    ("MONITOR", BASE_DIR / "monitor" / "monitor_signals.json"),
]

VALID_SOURCES = ("MONITOR",)


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def should_track(source, stage):
    return source in VALID_SOURCES and stage == "BREAKOUT"


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
        path.parent.mkdir(parents=True, exist_ok=True)
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
    """FIX: retry once on 429 / 5xx."""
    for attempt in range(2):
        try:
            url = f"{BINANCE_API}/api/v3/ticker/price?symbol={symbol}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return float(r.json().get("price", 0))
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return 0.0
            return 0.0
        except Exception:
            if attempt == 0:
                time.sleep(2)
                continue
            return 0.0
    return 0.0


def clean_state(state):
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        return 0
    before = len(signals)
    cleaned = []
    for s in signals:
        if not isinstance(s, dict):
            continue
        if not should_track(s.get("source", ""), s.get("stage", "")):
            continue
        if "dip_pct_tracked" not in s:
            s["dip_pct_tracked"] = 0.0
        if "entry_dip_pct" not in s and "dip_pct" in s:
            s["entry_dip_pct"] = s.pop("dip_pct")
        if "entry_dip_pct" not in s:
            s["entry_dip_pct"] = 0.0
        if "signal_price" not in s:
            s["signal_price"] = s.get("entry", 0)
        if "peak_pct" not in s:
            s["peak_pct"] = 0.0
        if "current_pct" not in s:
            s["current_pct"] = 0.0
        if "tier" not in s:
            s["tier"] = 2
        if "dead_hours" not in s:
            s["dead_hours"] = 0
        cleaned.append(s)
    state["signals"] = cleaned
    return before - len(cleaned)


def load_new_signals(state):
    seen = state.get("seen", {})
    if not isinstance(seen, dict):
        seen = {}
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        signals = []
    added = 0
    now = now_utc()

    for source, path in SIGNAL_SOURCES:
        data = load_json(path)
        entries = data.get("signals", [])
        if not isinstance(entries, list):
            continue
        for s in entries:
            if not isinstance(s, dict):
                continue
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

            stage = s.get("stage", "BREAKOUT")
            if not should_track(source, stage):
                seen[key] = True
                continue

            entry_dt = parse_ts(ts)
            if not entry_dt:
                continue
            age_hours = (now - entry_dt).total_seconds() / 3600
            if age_hours > MAX_SIGNAL_AGE_HOURS:
                seen[key] = True
                continue

            signals.append({
                "key": key,
                "source": source,
                "symbol": symbol,
                "entry": entry,
                "entry_ts": ts,
                "stage": stage,
                "tier": s.get("tier", 2),
                "dead_hours": s.get("dead_hours", 0),
                "signal_price": entry,
                "entry_dip_pct": 0.0,
                "peak": entry,
                "peak_pct": 0.0,
                "peak_ts": ts,
                "dip": entry,
                "dip_pct_tracked": 0.0,
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
    if not isinstance(signals, list):
        return 0
    trackable = [s for s in signals if isinstance(s, dict) and s.get("status") == "ACTIVE"]
    if not trackable:
        return 0

    symbols = list({s["symbol"] for s in trackable if s.get("symbol")})
    prices = {}
    if symbols:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(get_price, sym): sym for sym in symbols}
            for f in as_completed(futures):
                prices[futures[f]] = f.result()

    now = now_utc()
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
            s["dip_pct_tracked"] = ((p - s["entry"]) / s["entry"]) * 100
            s["dip_ts"] = now.isoformat()
            t = parse_ts(s["entry_ts"])
            if t:
                s["time_to_dip_min"] = int((now - t).total_seconds() / 60)

        # Exit logic: simulate actual exit at target or stop
        if s["peak_pct"] >= PUMP_TARGET:
            s["status"] = "TP30_HIT"
            s["exit_pct"] = PUMP_TARGET
            s["closed_ts"] = now.isoformat()
        elif s["current_pct"] <= STOP_PCT:
            s["status"] = "STOPPED"
            s["exit_pct"] = STOP_PCT
            s["closed_ts"] = now.isoformat()

        s["last_update"] = now.isoformat()
        updated += 1

    return updated


def build_report(state):
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        signals = []
    total = len(signals)
    active = [s for s in signals if s.get("status") == "ACTIVE"]
    tp30 = [s for s in signals if s.get("status") == "TP30_HIT"]
    stopped = [s for s in signals if s.get("status") == "STOPPED"]

    wins = len(tp30)
    losses = len(stopped)
    closed_total = wins + losses
    wr = (wins / closed_total * 100) if closed_total > 0 else 0
    lr = (losses / closed_total * 100) if closed_total > 0 else 0

    lines = ["📊 <b>SIGNAL TRACKER</b>", ""]
    lines.append(
        f"Total: <b>{total}</b> | "
        f"🟡 Active: <b>{len(active)}</b> | "
        f"🟢 TP30 Hit: <b>{len(tp30)}</b> | "
        f"🔴 Stopped: <b>{len(stopped)}</b>"
    )
    if closed_total > 0:
        lines.append(f"WR: <b>{wr:.0f}%</b> | LR: <b>{lr:.0f}%</b> ({closed_total} closed)")

    if tp30:
        lines.append("")
        lines.append("🟢 <b>TP30 HIT (+30%)</b>")
        for s in tp30[-5:]:
            peak = s.get("peak_pct", 0)
            ttp = s.get("time_to_peak_min", 0)
            lines.append(
                f"• <b>{s.get('symbol', '?')}</b> "
                f"{format_price(s.get('entry', 0))} → +{peak:.2f}% "
                f"in {ttp}m"
            )

    if active:
        lines.append("")
        lines.append("🟡 <b>ACTIVE</b>")
        for s in active[-5:]:
            peak = s.get("peak_pct", 0)
            dip_tr = s.get("dip_pct_tracked", 0)
            cur = s.get("current_pct", 0)
            lines.append(
                f"• <b>{s.get('symbol', '?')}</b> "
                f"{format_price(s.get('entry', 0))} → {cur:+.2f}% "
                f"(peak {peak:+.2f}%, dip {dip_tr:+.2f}%)"
            )

    if stopped:
        lines.append("")
        lines.append("🔴 <b>STOPPED (-3%)</b>")
        for s in stopped[-5:]:
            exit_pct = s.get("exit_pct", 0)
            lines.append(
                f"• <b>{s.get('symbol', '?')}</b> "
                f"{format_price(s.get('entry', 0))} → {exit_pct:+.2f}%"
            )

    return "\n".join(lines)


def main():
    state = load_json(TRACKER_FILE)
    if "signals" not in state or not isinstance(state["signals"], list):
        state["signals"] = []
    if "seen" not in state or not isinstance(state["seen"], dict):
        state["seen"] = {}

    cleaned = clean_state(state)
    print(f"Cleaned: {cleaned} invalid signals removed")

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
