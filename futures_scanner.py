import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SPOT_API = "https://data-api.binance.vision"
COINGECKO_API = "https://api.coingecko.com/api/v3"
COOLDOWN_FILE = Path("futures_cooldown.json")
REJECT_FILE = Path("futures_rejections.json")
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
    "RAVEUSDT", "SIRENUSDT", "AKEUSDT", "XPINUSDT",
    "BTRUSDT", "ANTHROPICUSDT", "SKHYNIXUSDT", "REUSDT", "SNDKUSDT",
    "XPLUSDT",
}

_SPOT_SYMBOLS = None

# ================= HELPERS =================

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

def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}

def save_json(path, data):
    try:
        path.write_text(json.dumps(data))
    except Exception as e:
        print(f"save error: {e}")

def load_cooldown():
    data = load_json(COOLDOWN_FILE)
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

def log_rejection(symbol, reasons, price, change_24h):
    try:
        data = load_json(REJECT_FILE)
        key = symbol
        if key not in data:
            data[key] = {"symbol": symbol, "count": 0, "price": price, "change_24h": change_24h}
        data[key]["count"] += 1
        data[key]["last_seen"] = datetime.utcnow().isoformat()
        data[key]["price"] = price
        data[key]["change_24h"] = change_24h
        data[key]["top_reason"] = reasons[0] if reasons else "unknown"
        if len(data) > 200:
            sorted_items = sorted(data.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
            data = dict(sorted_items[:200])
        save_json(REJECT_FILE, data)
    except Exception as e:
        print(f"reject log error: {e}")

def btc_is_healthy():
    try:
        url = f"{SPOT_API}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return True
        current_close = float(data[-1][4])
        prev_close = float(data[-2][4])
        if prev_close == 0:
            return True
        change = ((current_close - prev_close) / prev_close) * 100
        print(f"BTC 1h change: {change:.2f}%")
        return change > -2
    except Exception:
        return True

def get_spot_1h_change(symbol):
    try:
        url = f"{SPOT_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=2"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return 0
        current_close = float(data[-1][4])
        prev_close = float(data[-2][4])
        if prev_close == 0:
            return 0
        return ((current_close - prev_close) / prev_close) * 100
    except Exception:
        return 0

def get_spot_symbols():
    global _SPOT_SYMBOLS
    if _SPOT_SYMBOLS is not None:
        return _SPOT_SYMBOLS
    try:
        url = f"{SPOT_API}/api/v3/exchangeInfo"
        r = requests.get(url, timeout=20)
        data = r.json()
        symbols = set()
        for s in data.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                symbols.add(s["symbol"])
        _SPOT_SYMBOLS = symbols
        print(f"Loaded {len(symbols)} spot USDT symbols")
        return symbols
    except Exception as e:
        print(f"spot symbols error: {e}")
        _SPOT_SYMBOLS = set()
        return _SPOT_SYMBOLS

# ================= COINGECKO DERIVATIVES =================

def get_derivatives_data():
    """
    One API call to CoinGecko. Returns all Binance Futures perpetuals
    with funding rate, open interest, price, volume.
    """
    url = f"{COINGECKO_API}/derivatives?include_tickers=unexpired"
    try:
        r = requests.get(url, timeout=20, headers={"Accept": "application/json"})
        if r.status_code != 200:
            print(f"CoinGecko status: {r.status_code}")
            return []
        data = r.json()
        if not isinstance(data, list):
            print(f"CoinGecko unexpected type: {type(data)}")
            return []
        # Filter to Binance Futures only
        binance_data = [
            d for d in data
            if d.get("market") == "Binance (Futures)"
            and d.get("contract_type") == "perpetual"
        ]
        print(f"CoinGecko: {len(binance_data)} Binance perps")
        return binance_data
    except Exception as e:
        print(f"CoinGecko error: {e}")
        return []

# ================= CANDIDATES =================

def get_candidates_from_spot():
    url = f"{SPOT_API}/api/v3/ticker/24hr"
    r = requests.get(url, timeout=20)
    tickers = r.json()
    spot_symbols = get_spot_symbols()
    candidates = []
    rejected = {"vol_low": 0, "change_range": 0, "price_high": 0, "major": 0, "blacklist": 0}
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol not in spot_symbols:
            continue
        if symbol in MAJORS:
            rejected["major"] += 1
            continue
        if symbol in BLACKLIST:
            rejected["blacklist"] += 1
            continue
        if symbol.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSDT")):
            continue
        try:
            quote_vol = float(t.get("quoteVolume", 0))
            change = float(t.get("priceChangePercent", 0))
            price = float(t.get("lastPrice", 0))
        except (KeyError, ValueError, TypeError):
            continue
        if quote_vol < 10_000_000:
            rejected["vol_low"] += 1
            continue
        if price > 1.00 or price <= 0:
            rejected["price_high"] += 1
            continue
        if change < -3 or change > 10:
            rejected["change_range"] += 1
            continue
        candidates.append({
            "symbol": symbol,
            "price": price,
            "change_24h": change,
            "quote_vol": quote_vol,
        })
    print(f"Stage1 rejections: {rejected}")
    return candidates

# ================= SIGNAL CHECK =================

def check_pre_pump(candidate, derivatives_map):
    """
    Check candidate against CoinGecko derivatives data.
    Filters: OI > $10M, funding < 0, valid price.
    """
    symbol = candidate["symbol"]
    # CoinGecko uses format "BTCUSDT" or "BTC"
    # Try both symbol formats
    deriv = derivatives_map.get(symbol)
    if not deriv:
        # Try without USDT suffix
        base = symbol.replace("USDT", "")
        deriv = derivatives_map.get(base)
        if not deriv:
            return None, ["not_on_coingecko"]

    try:
        funding = float(deriv.get("funding_rate", 0))
        oi_usd = float(deriv.get("open_interest", 0) or 0)
    except (ValueError, TypeError):
        return None, ["parse_error"]

    # Funding must be negative (shorts trapped)
    if funding >= 0:
        return None, ["funding_pos"]

    # OI must be meaningful
    if oi_usd < 5_000_000:
        return None, ["oi_too_small"]

    # Spot 1h check
    spot_1h = get_spot_1h_change(symbol)
    if spot_1h > 10:
        return None, ["already_moved"]
    if spot_1h < -5:
        return None, ["dumping"]

    return {
        "funding": funding,
        "oi_value": oi_usd,
        "spot_1h": spot_1h,
    }, []

def get_session_label(hour, minute):
    if hour == 5 and minute >= 30:
        return "Asia"
    if 6 <= hour <= 10:
        return "Asia"
    if hour == 11 and minute < 30:
        return "Asia"
    if hour == 12 and minute >= 30:
        return "Europe"
    if 13 <= hour <= 14:
        return "Europe"
    if hour == 15 and minute < 30:
        return "Europe"
    if hour == 18 and minute >= 30:
        return "US"
    if 19 <= hour <= 20:
        return "US"
    if hour == 21 and minute < 30:
        return "US"
    return "Dead-Zone"

# ================= MAIN =================

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour, ist.minute)
    print(f"Futures Scanner (CoinGecko) starting at {ist} IST — Session: {session}")

    if not btc_is_healthy():
        print("BTC dumping >2%. Skipping.")
        return

    print("Fetching CoinGecko derivatives...")
    derivatives = get_derivatives_data()
    if not derivatives:
        print("No CoinGecko data. Exiting.")
        return

    # Build lookup: symbol -> derivative info
    deriv_map = {}
    for d in derivatives:
        sym = d.get("symbol", "").upper()
        if sym:
            deriv_map[sym] = d
            # Also strip "USDT" suffix
            if sym.endswith("USDT"):
                deriv_map[sym.replace("USDT", "")] = d

    candidates = get_candidates_from_spot()
    print(f"Candidates from spot: {len(candidates)}")
    if not candidates:
        print("No candidates. Exiting.")
        return

    candidates.sort(key=lambda x: x["quote_vol"], reverse=True)
    candidates = candidates[:30]
    print(f"Scanning top {len(candidates)} by volume...")

    cooldown = load_cooldown()
    print(f"Cooldown: {list(cooldown.keys())}")

    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(check_pre_pump, c, deriv_map): c for c in candidates}
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
                    top = reasons[0].split("_")[0]
                    rejection[top] = rejection.get(top, 0) + 1
                    log_rejection(c["symbol"], reasons, c["price"], c["change_24h"])

    print(f"Stage2 rejection: {rejection}")
    print(f"Found {len(hits)} pre-pump signals")

    now = datetime.utcnow()
    for h in hits:
        cooldown[h["symbol"]] = now.isoformat()
        stop = h["price"] * 0.97
        msg = (
            f"🔮 <b>PRE-PUMP DETECTED</b> [{session}]\n\n"
            f"<b>Source:</b> CoinGecko\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])} (flat)\n"
            f"<b>24h:</b> {h['change_24h']:.2f}%\n"
            f"<b>1h Spot:</b> {h['spot_1h']:+.2f}%\n"
            f"<b>OI Val:</b> ${h['oi_value']:,.0f}\n"
            f"<b>Funding:</b> {h['funding']*100:.4f}%\n"
            f"<b>24h Vol:</b> ${h['quote_vol']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"📋 <b>PLAN</b>\n"
            f"Entry: {format_price(h['price'])}\n"
            f"Stop: {format_price(stop)} (-3%)\n\n"
            f"⚠️ WAIT for Volume Breakout before entering."
        )
        send_telegram(msg)

    save_json(COOLDOWN_FILE, cooldown)

def safe_main():
    try:
        main()
    except Exception as e:
        err = str(e)[:300]
        send_telegram(f"🚨 <b>FUTURES SCANNER CRASHED</b>\n\n<b>Error:</b> {err}")
        raise

if __name__ == "__main__":
    safe_main()
