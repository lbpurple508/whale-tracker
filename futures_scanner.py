import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BYBIT = "https://api.bybit.com"
COOLDOWN_FILE = Path("futures_cooldown.json")
COOLDOWN_MINUTES = 60

MAJORS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "AVAXUSDT", "DOTUSDT",
    "MATICUSDT", "LTCUSDT", "LINKUSDT", "TONUSDT", "SHIBUSDT",
    "BCHUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT",
    "APTUSDT", "NEARUSDT", "ICPUSDT", "VETUSDT", "OPUSDT",
    "ARBUSDT", "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT",
    "XLMUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT",
    "STXUSDT", "IMXUSDT", "RUNEUSDT", "AAVEUSDT", "MKRUSDT",
    "GRTUSDT", "SANDUSDT", "MANAUSDT", "AXSUSDT", "CRVUSDT",
    "ALGOUSDT", "EGLDUSDT", "FTMUSDT", "THETAUSDT", "FLOWUSDT",
    "HBARUSDT",
}

BLACKLIST = {
    "GPSUSDT", "SHELLUSDT", "ENAUSDT", "ZKJUSDT", "KOGEUSDT",
    "COAIUSDT", "SAITAMAUSDT", "ROBOUSDT", "VZZNUSDT", "LABUSDT",
    "RAVEUSDT", "BROCCOLIUSDT", "SIRENUSDT", "AKEUSDT", "XPINUSDT",
    "BTRUSDT", "ANTHROPICUSDT", "SKHYNIXUSDT", "REUSDT", "SNDKUSDT",
    "XPLUSDT",
}

def format_price(p):
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_cooldown():
    if COOLDOWN_FILE.exists():
        try:
            data = json.loads(COOLDOWN_FILE.read_text())
            now = datetime.utcnow()
            cleaned = {}
            for sym, ts in data.items():
                try:
                    t = datetime.fromisoformat(ts)
                    if (now - t).total_seconds() < COOLDOWN_MINUTES * 60:
                        cleaned[sym] = ts
                except Exception:
                    pass
            return cleaned
        except Exception:
            return {}
    return {}

def save_cooldown(data):
    try:
        COOLDOWN_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"cooldown save error: {e}")

def get_futures_candidates():
    """Get all USDT perpetual tickers from Bybit."""
    try:
        url = f"{BYBIT}/v5/market/tickers?category=linear"
        r = requests.get(url, timeout=20)
        data = r.json()
        tickers = data.get("result", {}).get("list", [])
        if not isinstance(tickers, list):
            print(f"Unexpected response: {data}")
            return []
        candidates = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            if symbol in MAJORS or symbol in BLACKLIST:
                continue
            if symbol.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSDT")):
                continue
            try:
                quote_vol = float(t.get("turnover24h", 0))
                change = float(t.get("price24hPcnt", 0)) * 100  # decimal to %
                price = float(t.get("lastPrice", 0))
            except (KeyError, ValueError, TypeError):
                continue
            if quote_vol < 10_000_000:
                continue
            if price > 1.00 or price <= 0:
                continue
            if abs(change) > 5:
                continue
            candidates.append({
                "symbol": symbol,
                "price": price,
                "change_24h": change,
                "quote_vol": quote_vol,
            })
        return candidates
    except Exception as e:
        print(f"get_futures_candidates error: {e}")
        return []

def get_oi_history(symbol, interval="5min", limit=13):
    """Bybit open interest history."""
    try:
        url = f"{BYBIT}/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime={interval}&limit={limit}"
        r = requests.get(url, timeout=10)
        data = r.json()
        result = data.get("result", {})
        oi_list = result.get("list", [])
        if not isinstance(oi_list, list):
            return []
        # Bybit returns newest first, reverse to oldest first
        return list(reversed(oi_list))
    except Exception:
        return []

def get_funding_rate(symbol):
    """Bybit funding rate."""
    try:
        url = f"{BYBIT}/v5/market/tickers?category=linear&symbol={symbol}"
        r = requests.get(url, timeout=10)
        data = r.json()
        tickers = data.get("result", {}).get("list", [])
        if not tickers:
            return 0
        return float(tickers[0].get("fundingRate", 0))
    except Exception:
        return 0

def get_top_trader_ratio(symbol):
    """Bybit long/short account ratio."""
    try:
        url = f"{BYBIT}/v5/market/account-ratio?category=linear&symbol={symbol}&period=5min&limit=1"
        r = requests.get(url, timeout=10)
        data = r.json()
        ratios = data.get("result", {}).get("list", [])
        if not ratios:
            return 1
        return float(ratios[0].get("buyRatio", 0.5)) / max(float(ratios[0].get("sellRatio", 0.5)), 0.01)
    except Exception:
        return 1

def check_pre_pump(symbol, price):
    reasons = []
    try:
        oi_data = get_oi_history(symbol, "5min", 13)
        if not oi_data or len(oi_data) < 6:
            return None, ["no oi data"]

        try:
            # Bybit OI value field: openInterest (in coins). Multiply by price for USD.
            current_oi_raw = float(oi_data[-1].get("openInterest", 0))
            oi_15m_ago_raw = float(oi_data[-4].get("openInterest", 0))
            oi_1h_ago_raw = float(oi_data[0].get("openInterest", 0))
        except (KeyError, ValueError, IndexError):
            return None, ["oi parse error"]

        if oi_15m_ago_raw == 0 or oi_1h_ago_raw == 0:
            return None, ["oi zero"]

        oi_15m_change = ((current_oi_raw - oi_15m_ago_raw) / oi_15m_ago_raw) * 100
        oi_1h_change = ((current_oi_raw - oi_1h_ago_raw) / oi_1h_ago_raw) * 100
        oi_value_usd = current_oi_raw * price

        if oi_15m_change < 5 and oi_1h_change < 10:
            reasons.append(f"oi_flat")

        funding = get_funding_rate(symbol)
        # Bybit funding rate is per 8h. Positive high = longs crowded. Negative = shorts trapped (good).
        if funding > 0.001:
            reasons.append(f"funding_high")

        ls_ratio = get_top_trader_ratio(symbol)
        if ls_ratio < 1.2:
            reasons.append(f"ls_low")

        if reasons:
            return None, reasons

        return {
            "oi_15m_change": oi_15m_change,
            "oi_1h_change": oi_1h_change,
            "oi_value": oi_value_usd,
            "funding": funding,
            "ls_ratio": ls_ratio,
        }, []
    except Exception as e:
        return None, [f"exception {e}"]

def is_active_session(hour, minute):
    if hour == 5:
        return True, "Asia"
    if 6 <= hour <= 10:
        return True, "Asia"
    if hour == 11 and minute < 30:
        return True, "Asia"
    if hour == 12:
        return True, "Europe"
    if 13 <= hour <= 14:
        return True, "Europe"
    if hour == 15 and minute < 30:
        return True, "Europe"
    if hour == 18:
        return True, "US"
    if 19 <= hour <= 20:
        return True, "US"
    if hour == 21 and minute < 30:
        return True, "US"
    return False, "Off-hours"

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    active, session = is_active_session(ist.hour, ist.minute)
    print(f"Futures Scanner starting at {ist} IST — Session: {session} — Active: {active}")

    if not active:
        print("Outside fresh cycle window. Skipping.")
        return

    candidates = get_futures_candidates()
    print(f"Futures candidates: {len(candidates)}")

    if not candidates:
        print("No candidates. Exiting cleanly.")
        return

    cooldown = load_cooldown()
    print(f"Cooldown: {list(cooldown.keys())}")

    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(check_pre_pump, c["symbol"], c["price"]): c for c in candidates}
        for future in as_completed(futures):
            c = futures[future]
            if c["symbol"] in cooldown:
                continue
            result, reasons = future.result()
            if result:
                c.update(result)
                hits.append(c)
            else:
                if reasons:
                    top = reasons[0].split()[0] if reasons[0] else "unknown"
                    rejection[top] = rejection.get(top, 0) + 1

    print(f"Rejection reasons: {rejection}")
    print(f"Found {len(hits)} pre-pump signals")

    now = datetime.utcnow()
    for h in hits:
        cooldown[h["symbol"]] = now.isoformat()
        msg = (
            f"🔮 <b>PRE-PUMP DETECTED</b> [{session}]\n\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])} (flat)\n"
            f"<b>24h Change:</b> {h['change_24h']:.2f}%\n"
            f"<b>OI 15m Change:</b> +{h['oi_15m_change']:.2f}%\n"
            f"<b>OI 1h Change:</b> +{h['oi_1h_change']:.2f}%\n"
            f"<b>OI Value:</b> ${h['oi_value']:,.0f}\n"
            f"<b>Funding Rate:</b> {h['funding']*100:.4f}%\n"
            f"<b>Top Trader L/S:</b> {h['ls_ratio']:.2f}\n"
            f"<b>24h Vol:</b> ${h['quote_vol']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"⚠️ <b>ACTION:</b> Watch spot chart. Enter when spot volume confirms."
        )
        send_telegram(msg)

    save_cooldown(cooldown)

if __name__ == "__main__":
    main()
