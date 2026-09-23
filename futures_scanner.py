import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FAPI = "https://fapi.binance.com"
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
    url = f"{FAPI}/fapi/v1/ticker/24hr"
    r = requests.get(url, timeout=20)
    tickers = r.json()
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
            quote_vol = float(t["quoteVolume"])
            change = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
        except (KeyError, ValueError):
            continue
        if quote_vol < 10_000_000:
            continue
        if price > 1.00:
            continue
        # Pre-pump: price should be FLAT (under 5% move)
        if abs(change) > 5:
            continue
        candidates.append({
            "symbol": symbol,
            "price": price,
            "change_24h": change,
            "quote_vol": quote_vol,
        })
    return candidates

def get_oi_history(symbol, period="5m", limit=13):
    try:
        url = f"{FAPI}/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}"
        r = requests.get(url, timeout=10)
        return r.json()
    except Exception:
        return []

def get_funding_rate(symbol):
    try:
        url = f"{FAPI}/fapi/v1/premiumIndex?symbol={symbol}"
        r = requests.get(url, timeout=10)
        data = r.json()
        return float(data.get("lastFundingRate", 0))
    except Exception:
        return 0

def get_top_trader_ratio(symbol):
    try:
        url = f"{FAPI}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data and len(data) > 0:
            return float(data[-1].get("longShortRatio", 1))
    except Exception:
        pass
    return 1

def check_pre_pump(symbol, price):
    reasons = []
    try:
        oi_data = get_oi_history(symbol, "5m", 13)
        if not oi_data or len(oi_data) < 6:
            return None, ["no oi data"]

        current_oi = float(oi_data[-1]["sumOpenInterestValue"])
        oi_15m_ago = float(oi_data[-4]["sumOpenInterestValue"])
        oi_1h_ago = float(oi_data[0]["sumOpenInterestValue"])

        if oi_15m_ago == 0 or oi_1h_ago == 0:
            return None, ["oi zero"]

        oi_15m_change = ((current_oi - oi_15m_ago) / oi_15m_ago) * 100
        oi_1h_change = ((current_oi - oi_1h_ago) / oi_1h_ago) * 100

        # Require OI spike
        if oi_15m_change < 5 and oi_1h_change < 10:
            reasons.append(f"oi15m{oi_15m_change:.1f}%1h{oi_1h_change:.1f}%")

        # Funding rate (negative or low = shorts trapped)
        funding = get_funding_rate(symbol)
        if funding > 0.001:
            reasons.append(f"funding{funding*100:.3f}%")

        # Top trader ratio
        ls_ratio = get_top_trader_ratio(symbol)
        if ls_ratio < 1.2:
            reasons.append(f"ls{ls_ratio:.2f}")

        if reasons:
            return None, reasons

        return {
            "oi_15m_change": oi_15m_change,
            "oi_1h_change": oi_1h_change,
            "oi_value": current_oi,
            "funding": funding,
            "ls_ratio": ls_ratio,
        }, []
    except Exception as e:
        return None, [f"exception {e}"]

def is_active_session(hour, minute):
    # Pre-pump detection: run a bit earlier than spot scanner
    # Asia: 5:00-11:30 AM
    if hour == 5:
        return True, "Asia"
    if 6 <= hour <= 10:
        return True, "Asia"
    if hour == 11 and minute < 30:
        return True, "Asia"
    # Europe: 12:00-3:30 PM
    if hour == 12:
        return True, "Europe"
    if 13 <= hour <= 14:
        return True, "Europe"
    if hour == 15 and minute < 30:
        return True, "Europe"
    # US: 6:00-9:30 PM
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
                    top = reasons[0].split()[0]
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
