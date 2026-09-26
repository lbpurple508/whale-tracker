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
COOLDOWN_FILE = Path("futures_cooldown.json")
REJECT_FILE = Path("futures_rejections.json")
COOLDOWN_MINUTES = 60

# ================= HARDCODED JAPAN PROXY POOL =================
# Fresh Japan proxies (not US-blocked, works with Binance Futures)
JAPAN_PROXIES = [
    "http://3.113.38.132:443",
    "https://3.113.38.132:443",
    "http://211.128.96.206:80",
    "https://140.238.50.134:1234",
    "http://8.209.255.13:3128",
    "http://45.43.60.220:8080",
    "http://47.74.46.81:11310",
    "https://14.137.237.91:443",
    "http://172.237.11.129:3128",
    "http://8.221.138.111:6379",
    "http://47.91.29.151:4145",
    "http://56.155.73.159:27549",
    "http://138.3.218.141:54261",
    "http://213.165.43.73:46650",
    "https://153.126.214.29:443",
    "http://47.91.29.151:9200",
    "http://101.36.104.46:10808",
    "https://210.236.6.167:443",
    "http://140.238.32.108:3128",
    "http://8.221.138.111:18080",
    "http://35.78.212.217:35679",
    "http://47.91.29.151:194",
    "https://56.155.73.159:29393",
    "https://210.236.6.162:443",
    "http://47.74.46.81:1080",
    "http://47.79.86.137:1080",
    "https://56.155.73.159:28082",
    "http://43.167.166.56:1080",
    "https://64.176.51.83:8443",
    "http://35.78.212.217:50469",
    "http://56.155.73.159:29191",
    "http://52.195.147.51:8082",
    "http://8.221.139.222:31433",
    "http://8.221.138.111:46691",
    "http://56.155.73.159:35512",
    "http://35.78.212.217:58837",
    "https://35.78.212.217:8443",
    "https://160.16.144.120:443",
    "http://8.221.138.111:100",
    "http://47.91.29.151:7890",
    "http://103.75.118.84:1080",
    "http://47.91.29.151:6666",
    "http://56.155.73.159:7280",
    "https://175.134.18.237:443",
    "http://35.78.212.217:8585",
    "https://52.195.147.51:20341",
    "http://45.146.163.31:80",
    "http://35.78.212.217:44573",
    "http://35.78.252.142:33946",
]

# Fallback sources if Japan pool fails
PROXY_LIST_URL_1 = "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/all/data.json"
PROXY_LIST_URL_2 = "https://raw.githubusercontent.com/mohammedcha/ProxRipper/main/full_proxies/http.txt"

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

# ================= STATE =================
_SPOT_SYMBOLS = None
_FUTURES_SYMBOLS = None
_VERIFIED_PROXIES = []
_PROXY_DEAD = set()

# ================= PROXY VERIFICATION =================

def test_proxy_oi(proxy_str):
    """Test against OI endpoint. Returns proxy or None."""
    try:
        proxies = {"http": proxy_str, "https": proxy_str}
        url = f"{FAPI}/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=5"
        r = requests.get(url, proxies=proxies, timeout=6)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) >= 3:
                return proxy_str
    except Exception:
        pass
    return None

def load_external_proxies():
    """Fetch external proxy list as backup."""
    proxies = []
    try:
        r = requests.get(PROXY_LIST_URL_1, timeout=10)
        data = r.json()
        if isinstance(data, list):
            for p in data:
                if p.get("protocol") == "http" and p.get("ssl") is True:
                    ip = p.get("ip")
                    port = p.get("port")
                    if ip and port:
                        proxies.append(f"http://{ip}:{port}")
    except Exception:
        pass
    try:
        r = requests.get(PROXY_LIST_URL_2, timeout=10)
        for line in r.text.splitlines():
            line = line.strip()
            if line and ":" in line and not line.startswith("#"):
                if not line.startswith("http"):
                    line = f"http://{line}"
                proxies.append(line)
    except Exception:
        pass
    return proxies

def build_verified_pool():
    """Test Japan proxies first. If too few, add external."""
    global _VERIFIED_PROXIES
    if _VERIFIED_PROXIES:
        return _VERIFIED_PROXIES

    print(f"Testing {len(JAPAN_PROXIES)} Japan proxies (parallel)...")
    verified = []

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(test_proxy_oi, p): p for p in JAPAN_PROXIES}
        try:
            for future in as_completed(futures, timeout=30):
                try:
                    result = future.result()
                    if result:
                        verified.append(result)
                        print(f"JP VERIFIED ({len(verified)}): {result}")
                except Exception:
                    continue
        except Exception:
            pass

    print(f"Japan proxies verified: {len(verified)}")

    # If we have enough Japan proxies, use them
    if len(verified) >= 5:
        _VERIFIED_PROXIES = verified
        print(f"Using {len(verified)} Japan proxies")
        return _VERIFIED_PROXIES

    # Otherwise, add external
    print("Not enough Japan proxies. Adding external...")
    external = load_external_proxies()
    random.shuffle(external)
    test_list = external[:200]

    with ThreadPoolExecutor(max_workers=80) as executor:
        futures = {executor.submit(test_proxy_oi, p): p for p in test_list}
        try:
            for future in as_completed(futures, timeout=40):
                try:
                    result = future.result()
                    if result:
                        verified.append(result)
                        print(f"EXT VERIFIED ({len(verified)}): {result}")
                        if len(verified) >= 25:
                            for f in futures:
                                f.cancel()
                            break
                except Exception:
                    continue
        except Exception:
            pass

    _VERIFIED_PROXIES = verified
    print(f"Final verified pool: {len(verified)} proxies")
    return _VERIFIED_PROXIES

def rotate_get_small(url, timeout=8, max_attempts=30):
    """Rotate through verified proxies on every retry."""
    global _VERIFIED_PROXIES
    if not _VERIFIED_PROXIES:
        return None
    pool = [p for p in _VERIFIED_PROXIES if p not in _PROXY_DEAD]
    if not pool:
        pool = list(_VERIFIED_PROXIES)
    for _ in range(max_attempts):
        if not pool:
            break
        proxy = random.choice(pool)
        try:
            proxies = {"http": proxy, "https": proxy}
            r = requests.get(url, proxies=proxies, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    pass
            elif r.status_code == 451:
                pass
        except Exception:
            _PROXY_DEAD.add(proxy)
            pool = [p for p in pool if p not in _PROXY_DEAD]
            continue
    return None

# ================= TELEGRAM =================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ================= HELPERS =================
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

def get_futures_symbols():
    global _FUTURES_SYMBOLS
    if _FUTURES_SYMBOLS is not None:
        return _FUTURES_SYMBOLS
    url = f"{FAPI}/fapi/v1/exchangeInfo"
    data = rotate_get_small(url, timeout=15, max_attempts=50)
    if not isinstance(data, dict):
        print("Failed to fetch futures exchange info")
        _FUTURES_SYMBOLS = set()
        return _FUTURES_SYMBOLS
    symbols = set()
    for s in data.get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            symbols.add(s["symbol"])
    _FUTURES_SYMBOLS = symbols
    print(f"Loaded {len(symbols)} futures USDT symbols")
    return symbols

def get_candidates_from_spot():
    url = f"{SPOT_API}/api/v3/ticker/24hr"
    r = requests.get(url, timeout=20)
    tickers = r.json()
    spot_symbols = get_spot_symbols()
    futures_symbols = get_futures_symbols()
    if not futures_symbols:
        print("Cannot fetch futures symbols, abort")
        return []
    candidates = []
    rejected = {"vol_low": 0, "change_range": 0, "price_high": 0, "major": 0, "blacklist": 0, "not_futures": 0}
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol not in spot_symbols:
            continue
        if symbol not in futures_symbols:
            rejected["not_futures"] += 1
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

def get_oi_history(symbol, period="5m", limit=13):
    url = f"{FAPI}/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}"
    data = rotate_get_small(url, timeout=10, max_attempts=30)
    if not isinstance(data, list):
        return []
    return data

def get_funding_rate(symbol):
    url = f"{FAPI}/fapi/v1/premiumIndex?symbol={symbol}"
    data = rotate_get_small(url, timeout=10, max_attempts=30)
    if not isinstance(data, dict):
        return None
    try:
        return float(data.get("lastFundingRate", 0))
    except Exception:
        return None

def get_top_trader_ratio(symbol):
    url = f"{FAPI}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
    data = rotate_get_small(url, timeout=10, max_attempts=30)
    if not isinstance(data, list) or not data:
        return None
    try:
        return float(data[-1].get("longShortRatio", 1))
    except Exception:
        return None

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
        if funding is None:
            return None, ["funding_fail"]
        if funding >= 0:
            reasons.append(f"funding_pos_{funding*100:.4f}%")
        elif funding > -0.0001:
            reasons.append(f"funding_weak_{funding*100:.4f}%")

        ls_ratio = get_top_trader_ratio(symbol)
        if ls_ratio is None:
            return None, ["ls_fail"]
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

    print("Building verified proxy pool...")
    verified = build_verified_pool()
    if not verified:
        print("No verified proxies. Exiting.")
        return
    print(f"Proceeding with {len(verified)} proxies")

    candidates = get_candidates_from_spot()
    print(f"Candidates from spot: {len(candidates)}")

    if not candidates:
        print("No candidates. Exiting.")
        return

    candidates.sort(key=lambda x: x["quote_vol"], reverse=True)
    candidates = candidates[:15]
    print(f"Scanning top {len(candidates)} by volume...")

    cooldown = load_cooldown()
    print(f"Cooldown: {list(cooldown.keys())}")

    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
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
