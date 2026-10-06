import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_DIR = Path(__file__).resolve().parent

BINANCE_API = "https://data-api.binance.vision"
TRACKER_FILE = BASE_DIR / "tracker_state.json"

STOP_PCT = -3.0
PUMP_TARGET = 30.0
MAX_SIGNAL_AGE_HOURS = 48
MAX_FUTURE_SKEW_MINUTES = 5
REQUEST_TIMEOUT = 10
PRICE_RETRIES = 2
TRACK_INTERVAL_MS = 5 * 60 * 1000
KLINE_LIMIT = 1000

RESET_CUTOFF = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)

SIGNAL_SOURCES = [
    ("MONITOR", BASE_DIR / "monitor_signals.json"),
]

VALID_SOURCES = ("MONITOR",)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def should_track(source: str, stage: str) -> bool:
    return source in VALID_SOURCES and str(stage).upper() == "BREAKOUT"


def format_price(value: Any) -> str:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return "$0.00000000"

    if not math.isfinite(p):
        return "$0.00000000"
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars missing")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            print(f"Telegram HTTP error: {response.status_code} - {response.text}")
            return

        try:
            body = response.json()
        except ValueError:
            print("Telegram returned non-JSON response")
            return

        if not body.get("ok"):
            print(f"Telegram API error: {body}")
    except requests.RequestException as exc:
        print(f"Telegram network error: {exc}")
    except Exception as exc:
        print(f"Telegram error: {exc}")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"load error for {path}: {exc}")
        return {}


def save_json(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except OSError as exc:
        print(f"save error: {exc}")
        return False


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def get_price(symbol: str) -> float:
    symbol = str(symbol).strip().upper()
    if not symbol:
        return 0.0

    url = f"{BINANCE_API}/api/v3/ticker/price"
    for attempt in range(PRICE_RETRIES):
        try:
            response = requests.get(
                url,
                params={"symbol": symbol},
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:
                payload = response.json()
                price = float(payload.get("price", 0))
                return price if math.isfinite(price) and price > 0 else 0.0

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt + 1 < PRICE_RETRIES:
                time.sleep(2)
                continue
            return 0.0
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            if attempt + 1 < PRICE_RETRIES:
                time.sleep(2)
                continue
            return 0.0
        except Exception:
            if attempt + 1 < PRICE_RETRIES:
                time.sleep(2)
                continue
            return 0.0

    return 0.0



def fetch_post_entry_klines(symbol: str, entry_dt: datetime, end_dt: datetime) -> list:
    """Fetch 5m bars beginning with the first full bar after the signal."""
    start_ms = (
        int(entry_dt.timestamp() * 1000) // TRACK_INTERVAL_MS + 1
    ) * TRACK_INTERVAL_MS
    end_ms = int(end_dt.timestamp() * 1000)

    if start_ms > end_ms:
        return []

    url = f"{BINANCE_API}/api/v3/klines"
    for attempt in range(PRICE_RETRIES):
        try:
            response = requests.get(
                url,
                params={
                    "symbol": symbol,
                    "interval": "5m",
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": KLINE_LIMIT,
                },
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:
                payload = response.json()
                return payload if isinstance(payload, list) else []

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt + 1 < PRICE_RETRIES:
                time.sleep(2)
                continue
            return []
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            if attempt + 1 < PRICE_RETRIES:
                time.sleep(2)
                continue
            return []
        except Exception:
            if attempt + 1 < PRICE_RETRIES:
                time.sleep(2)
                continue
            return []

    return []

def _as_finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _signal_key(source: str, ts: str, symbol: str) -> str:
    return f"{source}|{ts}|{symbol}"


def normalize_signal(signal: dict) -> dict | None:
    source = signal.get("source", "")
    stage = signal.get("stage", "")
    symbol = str(signal.get("symbol", "")).strip().upper()
    entry = _as_finite_float(signal.get("entry"), 0.0)
    entry_dt = parse_ts(signal.get("entry_ts"))

    if not should_track(source, stage) or not symbol or entry <= 0:
        return None
    if entry_dt is None or entry_dt < RESET_CUTOFF:
        return None

    normalized = dict(signal)
    normalized["source"] = source
    normalized["stage"] = str(stage).upper()
    normalized["symbol"] = symbol
    normalized["entry"] = entry
    normalized["entry_ts"] = entry_dt.isoformat()
    normalized["key"] = normalized.get("key") or _signal_key(source, normalized["entry_ts"], symbol)

    if "dip_pct_tracked" not in normalized and "dip_pct" in normalized:
        normalized["dip_pct_tracked"] = _as_finite_float(normalized.pop("dip_pct"), 0.0)
    else:
        normalized["dip_pct_tracked"] = _as_finite_float(
            normalized.get("dip_pct_tracked"), 0.0
        )

    normalized["entry_dip_pct"] = _as_finite_float(normalized.get("entry_dip_pct"), 0.0)
    normalized["signal_price"] = _as_finite_float(normalized.get("signal_price"), entry) or entry
    normalized["peak"] = _as_finite_float(normalized.get("peak"), entry)
    normalized["peak_pct"] = _as_finite_float(normalized.get("peak_pct"), 0.0)
    normalized["peak_ts"] = normalized.get("peak_ts") or normalized["entry_ts"]
    normalized["dip"] = _as_finite_float(normalized.get("dip"), entry)
    normalized["dip_ts"] = normalized.get("dip_ts") or normalized["entry_ts"]
    normalized["current"] = _as_finite_float(normalized.get("current"), entry)
    normalized["current_pct"] = _as_finite_float(normalized.get("current_pct"), 0.0)
    normalized["time_to_peak_min"] = max(0, int(_as_finite_float(normalized.get("time_to_peak_min"), 0)))
    normalized["time_to_dip_min"] = max(0, int(_as_finite_float(normalized.get("time_to_dip_min"), 0)))
    normalized["tier"] = normalized.get("tier", 2)
    normalized["dead_hours"] = _as_finite_float(normalized.get("dead_hours"), 0.0)
    normalized["status"] = normalized.get("status") or "ACTIVE"
    normalized["closed_ts"] = normalized.get("closed_ts")
    normalized["exit_pct"] = (
        None
        if normalized.get("exit_pct") is None
        else _as_finite_float(normalized.get("exit_pct"), 0.0)
    )
    normalized["last_update"] = normalized.get("last_update") or normalized["entry_ts"]

    return normalized


def clean_state(state: dict) -> int:
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        state["signals"] = []
        return 0

    before = len(signals)
    cleaned = []
    seen_keys = set()

    for raw_signal in signals:
        if not isinstance(raw_signal, dict):
            continue

        signal = normalize_signal(raw_signal)
        if signal is None:
            continue

        key = signal["key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cleaned.append(signal)

    state["signals"] = cleaned
    return before - len(cleaned)


def load_new_signals(state: dict) -> int:
    seen = state.get("seen", {})
    if not isinstance(seen, dict):
        seen = {}

    signals = state.get("signals", [])
    if not isinstance(signals, list):
        signals = []

    existing_keys = {
        s.get("key") for s in signals if isinstance(s, dict) and s.get("key")
    }
    added = 0
    now = now_utc()

    for source, path in SIGNAL_SOURCES:
        data = load_json(path)
        entries = data.get("signals", [])
        if not isinstance(entries, list):
            continue

        for raw in entries:
            if not isinstance(raw, dict):
                continue

            ts = raw.get("ts")
            symbol = str(raw.get("symbol", "")).strip().upper()
            price = raw.get("price")
            if not ts or not symbol or price is None:
                continue

            key = _signal_key(source, str(ts), symbol)
            if key in seen or key in existing_keys:
                continue

            ts_dt = parse_ts(ts)
            if ts_dt is None or ts_dt < RESET_CUTOFF:
                seen[key] = True
                continue

            age_hours = (now - ts_dt).total_seconds() / 3600.0
            if age_hours < -MAX_FUTURE_SKEW_MINUTES / 60.0:
                print(f"Skipping future signal: {key}")
                seen[key] = True
                continue
            if age_hours > MAX_SIGNAL_AGE_HOURS:
                seen[key] = True
                continue

            try:
                entry = float(price)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(entry) or entry <= 0:
                continue

            stage = str(raw.get("stage", "BREAKOUT")).upper()
            if not should_track(source, stage):
                seen[key] = True
                continue

            entry_ts = ts_dt.isoformat()
            signals.append(
                {
                    "key": key,
                    "source": source,
                    "symbol": symbol,
                    "entry": entry,
                    "entry_ts": entry_ts,
                    "stage": stage,
                    "tier": raw.get("tier", 2),
                    "dead_hours": _as_finite_float(raw.get("dead_hours"), 0.0),
                    "signal_price": entry,
                    "entry_dip_pct": 0.0,
                    "peak": entry,
                    "peak_pct": 0.0,
                    "peak_ts": entry_ts,
                    "dip": entry,
                    "dip_pct_tracked": 0.0,
                    "dip_ts": entry_ts,
                    "current": entry,
                    "current_pct": 0.0,
                    "time_to_peak_min": 0,
                    "time_to_dip_min": 0,
                    "status": "ACTIVE",
                    "last_update": now.isoformat(),
                    "closed_ts": None,
                    "exit_pct": None,
                }
            )
            seen[key] = True
            existing_keys.add(key)
            added += 1

    state["signals"] = signals
    state["seen"] = seen
    return added


def update_signals(state: dict) -> int:
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        return 0

    trackable = [
        signal
        for signal in signals
        if isinstance(signal, dict) and signal.get("status") == "ACTIVE"
    ]
    if not trackable:
        return 0

    now = now_utc()
    updated = 0

    def _update_one(signal):
        symbol = str(signal.get("symbol", "")).strip().upper()
        entry = _as_finite_float(signal.get("entry"), 0.0)
        entry_dt = parse_ts(signal.get("entry_ts"))

        if not symbol or entry <= 0 or entry_dt is None:
            return False, "invalid_signal"

        age_hours = (now - entry_dt).total_seconds() / 3600.0
        if age_hours < -MAX_FUTURE_SKEW_MINUTES / 60.0:
            return False, "future_signal"

        candles = fetch_post_entry_klines(symbol, entry_dt, now)
        if not candles:
            # Never manufacture a TP/SL result from missing history.
            return False, "no_klines"

        peak = _as_finite_float(signal.get("peak"), entry)
        dip = _as_finite_float(signal.get("dip"), entry)
        peak_ts = parse_ts(signal.get("peak_ts")) or entry_dt
        dip_ts = parse_ts(signal.get("dip_ts")) or entry_dt

        first_hit = None
        first_hit_pct = None
        first_hit_ts = None

        for candle in candles:
            if not isinstance(candle, list) or len(candle) < 7:
                continue

            try:
                open_ms = int(candle[0])
                high = float(candle[2])
                low = float(candle[3])
            except (TypeError, ValueError, IndexError):
                continue

            if high <= 0 or low <= 0 or high < low:
                continue

            bar_dt = datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc)

            if high > peak:
                peak = high
                peak_ts = bar_dt

            if low < dip:
                dip = low
                dip_ts = bar_dt

            tp_hit = high >= entry * (1.0 + PUMP_TARGET / 100.0)
            sl_hit = low <= entry * (1.0 + STOP_PCT / 100.0)

            if first_hit is None and (tp_hit or sl_hit):
                first_hit_ts = bar_dt.isoformat()

                if tp_hit and sl_hit:
                    # OHLC cannot reveal intrabar order, so resolve conservatively as loss.
                    first_hit = "STOPPED"
                    first_hit_pct = STOP_PCT
                elif tp_hit:
                    first_hit = "TP30_HIT"
                    first_hit_pct = PUMP_TARGET
                else:
                    first_hit = "STOPPED"
                    first_hit_pct = STOP_PCT

        signal["peak"] = peak
        signal["peak_pct"] = ((peak - entry) / entry) * 100.0
        signal["peak_ts"] = peak_ts.isoformat()
        signal["dip"] = dip
        signal["dip_pct_tracked"] = ((dip - entry) / entry) * 100.0
        signal["dip_ts"] = dip_ts.isoformat()
        signal["time_to_peak_min"] = max(
            0, int((peak_ts - entry_dt).total_seconds() / 60)
        )
        signal["time_to_dip_min"] = max(
            0, int((dip_ts - entry_dt).total_seconds() / 60)
        )

        if first_hit:
            signal["status"] = first_hit
            signal["exit_pct"] = first_hit_pct
            signal["closed_ts"] = first_hit_ts or now.isoformat()
        elif age_hours >= MAX_SIGNAL_AGE_HOURS:
            signal["status"] = "EXPIRED"
            signal["exit_pct"] = None
            signal["closed_ts"] = now.isoformat()

        current_price = get_price(symbol)
        if current_price > 0:
            current_pct = ((current_price - entry) / entry) * 100.0
            if math.isfinite(current_pct):
                signal["current"] = current_price
                signal["current_pct"] = current_pct

        signal["last_update"] = now.isoformat()
        return True, "updated"

    max_workers = min(8, len(trackable))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_update_one, signal) for signal in trackable]

        for future in as_completed(futures):
            try:
                did_update, reason = future.result()
                if did_update:
                    updated += 1
                elif reason == "no_klines":
                    print("Tracker skipped a signal because Binance history was unavailable")
            except Exception as exc:
                print(f"Tracker worker failure: {exc}")

    return updated


def build_report(state: dict) -> str:
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        signals = []

    valid_signals = [signal for signal in signals if isinstance(signal, dict)]
    total = len(valid_signals)
    active = [signal for signal in valid_signals if signal.get("status") == "ACTIVE"]
    tp30 = [signal for signal in valid_signals if signal.get("status") == "TP30_HIT"]
    stopped = [signal for signal in valid_signals if signal.get("status") == "STOPPED"]
    expired = [signal for signal in valid_signals if signal.get("status") == "EXPIRED"]

    wins = len(tp30)
    losses = len(stopped)
    closed_total = wins + losses
    wr = wins / closed_total * 100 if closed_total else 0
    lr = losses / closed_total * 100 if closed_total else 0

    lines = ["📊 <b>SIGNAL TRACKER</b>", ""]
    lines.append(
        f"Total: <b>{total}</b> | "
        f"🟡 Active: <b>{len(active)}</b> | "
        f"🟢 TP30 Hit: <b>{len(tp30)}</b> | "
        f"🔴 Stopped: <b>{len(stopped)}</b> | "
        f"⚪ Expired: <b>{len(expired)}</b>"
    )

    if closed_total:
        lines.append(f"WR: <b>{wr:.0f}%</b> | LR: <b>{lr:.0f}%</b> ({closed_total} resolved; expired excluded)")

    if tp30:
        lines.extend(["", "🟢 <b>TP30 HIT (+30%)</b>"])
        for signal in tp30[-5:]:
            peak = _as_finite_float(signal.get("peak_pct"), 0.0)
            ttp = max(0, int(_as_finite_float(signal.get("time_to_peak_min"), 0)))
            lines.append(
                f"• <b>{signal.get('symbol', '?')}</b> "
                f"{format_price(signal.get('entry', 0))} → +{peak:.2f}% "
                f"in {ttp}m"
            )

    if active:
        lines.extend(["", "🟡 <b>ACTIVE</b>"])
        for signal in active[-5:]:
            peak = _as_finite_float(signal.get("peak_pct"), 0.0)
            dip_tracked = _as_finite_float(signal.get("dip_pct_tracked"), 0.0)
            current = _as_finite_float(signal.get("current_pct"), 0.0)
            lines.append(
                f"• <b>{signal.get('symbol', '?')}</b> "
                f"{format_price(signal.get('entry', 0))} → {current:+.2f}% "
                f"(peak {peak:+.2f}%, dip {dip_tracked:+.2f}%)"
            )

    if stopped:
        lines.extend(["", "🔴 <b>STOPPED (-3%)</b>"])
        for signal in stopped[-5:]:
            exit_pct = _as_finite_float(signal.get("exit_pct"), STOP_PCT)
            lines.append(
                f"• <b>{signal.get('symbol', '?')}</b> "
                f"{format_price(signal.get('entry', 0))} → {exit_pct:+.2f}%"
            )

    return "\n".join(lines)


def main() -> None:
    state = load_json(TRACKER_FILE)
    if not isinstance(state, dict):
        state = {}

    if "signals" not in state or not isinstance(state["signals"], list):
        state["signals"] = []
    if "seen" not in state or not isinstance(state["seen"], dict):
        state["seen"] = {}

    cleaned = clean_state(state)
    print(f"Cleaned: {cleaned} invalid/duplicate signals removed")

    added = load_new_signals(state)
    print(f"New signals: {added}")
    print(f"Total tracked: {len(state['signals'])}")

    updated = update_signals(state)
    print(f"Updated: {updated}")

    if not save_json(TRACKER_FILE, state):
        print("State was not saved successfully")
        return

    # Only send Telegram when at least one ACTIVE signal exists.
    # Prevents 288 daily reports for the same closed signals.
    has_active = any(
        isinstance(s, dict) and s.get("status") == "ACTIVE"
        for s in state.get("signals", [])
    )

    if has_active:
        report = build_report(state)
        send_telegram(report)
    else:
        print("No active signals. Skipping report.")


if __name__ == "__main__":
    main()
