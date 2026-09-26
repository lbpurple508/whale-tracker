import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import random

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FAPI = "https://fapi.binance.com"
SPOT_API = "https://data-api.binance.vision"
PROXY_LIST_URL_1 = "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/all/data.json"
PROXY_LIST_URL_2 = "https://raw.githubusercontent.com/mohammedcha/ProxRipper/main/full_proxies/http.txt"
COOLDOWN_FILE = Path("futures_cooldown.json")
REJECT_FILE = Path("futures_rejections.json")
PROXY_POOL_FILE = Path("proxy_pool.json")
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
_PROXY_POOL = []

# ---------- PROXY POOL ----------

def load_proxy_pool():
    """Fetch fresh proxy list and build in-memory pool."""
    global _PROXY_POOL
    all_proxies = []
    
    # Source 1: ProxyScrape
    try:
        r = requests.get(PROXY_LIST_URL_1, timeout=15)
        data = r.json()
        if isinstance(data, list):
            for p in data:
                if p.get("protocol") == "http" and p.get("ssl") is True:
                    ip = p.get("ip")
                    port = p.get("port")
                    if ip and port:
                        all_proxies.append(f"http://{ip}:{port}")
    except Exception as e:
        print(f"ProxyScrape error: {e}")

    # Source 2: ProxRipper
    try:
        r = requests.get(PROXY_LIST_URL_2, timeout=15)
        for line in r.text.splitlines():
            line = line.strip()
            if line and ":" in line and not line.startswith("#"):
                if not line.startswith("http"):
                    line = f"http://{line}"
                all_proxies.append(line)
    except Exception as e:
        print(f"ProxRipper error: {e}")

    # Dedupe + shuffle
    all_proxies = list(set(all_proxies))
    random.shuffle(all_proxies)
    _PROXY_POOL = all_proxies
    print(f"Proxy pool loaded: {len(_PROXY_POOL)} proxies")
    return _PROXY_POOL

def get_random_proxy():
    """Get random proxy from pool."""
    if not _PROXY_POOL:
        load_proxy_pool()
    if not _PROXY_POOL:
        return None
    return random.choice(_PROXY_POOL)

def rotate_get(url, timeout=10, max_attempts=8):
    """
    Try up to max_attempts proxies. Each request uses a DIFFERENT proxy.
    Returns parsed JSON or None.
    """
    for attempt in range(max_attempts):
        proxy = get_random_proxy()
        if not proxy:
            break
        try:
            r = requests.get(
                url,
                proxies={"http": proxy, "https": proxy},
                timeout=timeout
            )
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    continue
            elif r.status_code == 451:
                # Blocked - try different proxy
                continue
        except Exception:
            # Timeout/error - try different proxy
            continue
    return None

# ---------- TELEGRAM ----------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ---------- HELPERS ----------

def format_price(p):
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"

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

# ---------- FUTURES DATA (VIA ROTATING PROXIES) ----------

def get_futures_candidates():
    """Get ticker data via rotating proxies."""
    # First, get all futures tickers with ONE good proxy
    data = None
    for attempt in range(15):
        proxy = get_random_proxy()
        if not proxy:
            break
        try:
            r = requests.get(
                f"{FAPI}/fapi/v1/ticker/24hr",
                proxies={"http": proxy, "https": proxy},
                timeout=15
            )
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, list) and len(d) > 100:
                    data = d
                    print(f"Got ticker data on attempt {attempt+1}")
                    break
        except Exception:
            continue
    
    if not data:
        print("Failed to get ticker data after 15 attempts")
        return []
    
    spot_symbols = get_spot_symbols()
    candidates = []
    rejected_stage1 = {
        "vol_low": 0, "change_range": 0, "price_high": 0,
        "major": 0, "blacklist": 0, "not_on_spot": 0
    }
    for t in data:
        if not isinstance(t, dict):
            continue
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol in MAJORS:
            rejected_stage1["major"] += 1
            continue
        if symbol in BLACKLIST:
            rejected_stage1["blacklist"] += 1
            continue
        if symbol.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSDT")):
            continue
        if spot_symbols and symbol not in spot_symbols:
            rejected_stage1["not_on_spot"] += 1
            continue
        try:
            quote_vol = float(t.get("quoteVolume", 0))
            change = float(t.get("priceChangePercent", 0))
            price = float(t.get("lastPrice", 0))
        except (KeyError, ValueError, TypeError):
            continue
        if quote_vol < 10_000_000:
            rejected_stage1["vol_low"] += 1
            continue
        if price > 1.00 or price <= 0:
            rejected_stage1["price_high"] += 1
            continue
        if abs(change) > 5:
            rejected_stage1["change_range"] += 1
            continue
        candidates.append({
            "symbol": symbol,
            "price": price,
            "change_24h": change,
            "quote_vol": quote_vol,
        })
    print(f"Stage1 rejections: {rejected_stage1}")
    return candidates

def get_oi_history(symbol, period="5m", limit=13):
    url = f"{FAPI}/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}"
    data = rotate_get(url, timeout=8, max_attempts=6)
    if not isinstance(data, list):
        return []
    return data

def get_funding_rate(symbol):
    url = f"{FAPI}/fapi/v1/premiumIndex?symbol={symbol}"
    data = rotate_get(url, timeout=8, max_attempts=6)
    if not isinstance(data, dict):
        return 0
    try:
        return float(data.get("lastFundingRate", 0))
    except Exception:
        return 0

def get_top_trader_ratio(symbol):
    url = f"{FAPI}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
    data = rotate_get(url, timeout=8, max_attempts=6)
    if not isinstance(data, list) or not data:
        return 1
    try:
        return float(data[-1].get("longShortRatio", 1))
    except Exception:
        return 1

def check_pre_pump(symbol, price):
    reasons = []
    try:
        spot_1h = get_spot_1h_change(symbol)
        if spot_1h > 10:
            return None, [f"already_moved_{spot_1h:.1f}%"]
        if spot_1h < -5:
            return None, [f"dumping_{spot_1h:.1f}%"]

        oi_data = get_oi_history(symbol, "5m", 13)
        if not oi_data or len(oi_data) < 6:
            return None, ["no_oi"]

        try:
            current_oi = float(oi_data[-1]["sumOpenInterestValue"])
            oi_15m_ago = float(oi_data[-4]["sumOpenInterestValue"])
            oi_1h_ago = float(oi_data[0]["sumOpenInterestValue"])
        except (KeyError, ValueError, IndexError):
            return None, ["oi_parse_error"]

        if oi_15m_ago == 0 or oi_1h_ago == 0:
            return None, ["oi_zero"]

        oi_15m_change = ((current_oi - oi_15m_ago) / oi_15m_ago) * 100
        oi_1h_change = ((current_oi - oi_1h_ago) / oi_1h_ago) * 100

        if oi_15m_change < 5 and oi_1h_change < 10:
            reasons.append(f"oi_flat_{oi_15m_change:.1f}%_{oi_1h_change:.1f}%")

        funding = get_funding_rate(symbol)
        if funding >= 0:
            reasons.append(f"funding_pos_{funding*100:.4f}%")
        elif funding > -0.0001:
            reasons.append(f"funding_weak_{funding*100:.4f}%")

        ls_ratio = get_top_trader_ratio(symbol)
        if ls_ratio < 1.2:
            reasons.append(f"ls_low_{ls_ratio:.2f}")

        if reasons:
            return None, reasons

        return {
            "oi_15m_change": oi_15m_change,
            "oi_1h_change": oi_1h_change,
            "oi_value": current_oi,
            "funding": funding,
            "ls_ratio": ls_ratio,
            "spot_1h": spot_1h,
        }, []
    except Exception as e:
        return None, [f"exception_{e}"]

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

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour, ist.minute)
    print(f"Futures Scanner starting at {ist} IST — Session: {session}")

    if not btc_is_healthy():
        print("BTC dumping >2%. Skipping.")
        return

    print("Loading proxy pool...")
    load_proxy_pool()
    if not _PROXY_POOL:
        print("No proxies available. Exiting.")
        return

    candidates = get_futures_candidates()
    print(f"Futures candidates (spot-verified): {len(candidates)}")

    if not candidates:
        print("No candidates. Exiting.")
        return

    cooldown = load_cooldown()
    print(f"Cooldown: {list(cooldown.keys())}")

    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
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
            f"<b>Type:</b> WHALE LOADING\n"
            f"<b>Coin:</b> {h['symbol']}\n"
            f"<b>Price:</b> {format_price(h['price'])} (flat)\n"
            f"<b>24h:</b> {h['change_24h']:.2f}%\n"
            f"<b>1h Spot:</b> {h['spot_1h']:+.2f}%\n"
            f"<b>OI 15m:</b> +{h['oi_15m_change']:.2f}%\n"
            f"<b>OI 1h:</b> +{h['oi_1h_change']:.2f}%\n"
            f"<b>OI Val:</b> ${h['oi_value']:,.0f}\n"
            f"<b>Funding:</b> {h['funding']*100:.4f}%\n"
            f"<b>L/S:</b> {h['ls_ratio']:.2f}\n"
            f"<b>24h Vol:</b> ${h['quote_vol']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"📋 <b>IF ENTERED</b>\n"
            f"Entry: {format_price(h['price'])}\n"
            f"Stop: {format_price(stop)} (-3%)\n\n"
            f"⚠️ WAIT for Volume Breakout or Grind before entering."
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
