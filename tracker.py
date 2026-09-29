import os
import json
import requests
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BINANCE_API = "https://data-api.binance.vision"
TRACKER_FILE = Path("tracker_state.json")

STOP_PCT = -3.0
PUMP_TARGET = 30.0
MAX_SIGNAL_AGE_HOURS = 48
MAX_PUMPED_AGE_HOURS = 72

SIGNAL_SOURCES = [
    ("MOMENTUM", Path("momentum/momentum_signals.json")),
    ("MONITOR", Path("monitor/monitor_signals.json")),
    ("FUTURES", Path("futures/futures_signals.json")),
    ("TREND", Path("trend/trend_signals.json")),
]


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def should_track(source, stage):
    if source == "FUTURES":
        return False
    if source == "MONITOR" and stage != "BREAKOUT":
        return False
    return True


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


def clean_state(state):
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        return 0
    before = len(signals)
    state["signals"] = [
        s for s in signals
        if isinstance(s, dict) and should_track(s.get("source", ""), s.get("stage", ""))
    ]
    return before - len(state["signals"])


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

            stage = s.get("stage", source)
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
    if not isinstance(signals, list):
        return 0
    trackable = [s for s in signals if isinstance(s, dict) and s.get("status") in ("ACTIVE", "PUMPED")]
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
                s["exit_pct"] = STOP_PCT
                s["closed_ts"] = now.isoformat()
        elif s["status"] == "PUMPED":
            entry_dt = parse_ts(s["entry_ts"])
            if entry_dt:
                age_hours = (now - entry_dt).total_seconds() / 3600
                if age_hours > MAX_PUMPED_AGE_HOURS:
                    s["status"] = "CLOSED"
                    s["exit_pct"] = s["current_pct"]
                    s["closed_ts"] = now.isoformat()

        s["last_update"] = now.isoformat()
        updated += 1

    return updated


def build_source_stats(signals):
    sources = ["MOMENTUM", "MONITOR", "TREND"]
    stats = {}
    for src in sources:
        items = [s for s in signals if s.get("source") == src]
        total = len(items)
        wins = len([s for s in items if s.get("status") in ("PUMPED", "CLOSED") and s.get("peak_pct", 0) >= PUMP_TARGET])
        losses = len([s for s in items if s.get("status") == "STOPPED"])
        active = len([s for s in items if s.get("status") == "ACTIVE"])
        closed = wins + losses
        win_rate = (wins / closed * 100) if closed > 0 else 0
        loss_rate = (losses / closed * 100) if closed > 0 else 0
        stats[src] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "active": active,
            "closed": closed,
            "win_rate": win_rate,
            "loss_rate": loss_rate,
        }
    return stats


def build_report(state):
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        signals = []
    total = len(signals)
    active = [s for s in signals if s.get("status") == "ACTIVE"]
    pumped = [s for s in signals if s.get("status") == "PUMPED"]
    stopped = [s for s in signals if s.get("status") == "STOPPED"]
    closed = [s for s in signals if s.get("status") == "CLOSED"]

    lines = ["📊 <b>SIGNAL TRACKER</b>", ""]
    lines.append(
        f"Total: <b>{total}</b> | "
        f"🟡 Active: <b>{len(active)}</b> | "
        f"🟢 Pumped: <b>{len(pumped)}</b> | "
        f"🔴 Stopped: <b>{len(stopped)}</b> | "
        f"⚫ Closed: <b>{len(closed)}</b>"
    )

    stats = build_source_stats(signals)
    lines.append("")
    lines.append("📈 <b>BY SOURCE</b>")
    for src in ["MOMENTUM", "MONITOR", "TREND"]:
        st = stats[src]
        if st["total"] == 0:
            continue
        if st["closed"] > 0:
            wr = f"{st['win_rate']:.0f}%"
            lr = f"{st['loss_rate']:.0f}%"
        else:
            wr = "—"
            lr = "—"
        lines.append(
            f"• <b>{src}</b>: {st['total']} total | "
            f"✅ {st['wins']} | ❌ {st['losses']} | 🟡 {st['active']}"
        )
        lines.append(f"   WR: {wr} | LR: {lr}")

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
