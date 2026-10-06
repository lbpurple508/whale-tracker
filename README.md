# 🐋 Whale Tracker

A free, GitHub-hosted Binance Spot scanner that looks for unusual market expansion and breakout setups and sends actionable Telegram alerts.

## 🚀 Current System

This repository contains two separate scanners:

### 1. Existing Monitoring Scanner

Tracks the original 32 monitored symbols independently.

### 2. Extreme Pump Scanner

The new research-driven scanner searches the Binance Spot USDT market for the recurring setup discovered from 12 months of historical 5-minute data:

```
EXTREME volatility expansion
        +
price 0% to 1% below previous 4H high
        ↓
previous 4H high becomes trigger
        ↓
FIRST TOUCH
        ↓
🚀 ENTER TRADE
```

The scanner intentionally does **not** require a candle close, 2x/3x volume spike, or large breakout candle. Those filters were tested historically and generally made entry worse.

## 📡 Telegram Alerts

### Watch

```
👀 WATCH SETUP

PAIR: XYZUSDT
SETUP: EXTREME

CURRENT PRICE: $0.18291
PREVIOUS 4H HIGH: $0.18420
DISTANCE: -0.70%

🎯 TARGET ENTRY PRICE: $0.18420
TRIGGER: FIRST TOUCH
```

### Entry

```
🚀 ENTER TRADE

PAIR: XYZUSDT
SETUP: EXTREME

🎯 ENTRY PRICE: $0.18420
TRIGGER: PREVIOUS 4H HIGH TOUCH
CURRENT PRICE: $0.18420

TARGET LADDER:
+5%  $0.19341
+10% $0.20262
+20% $0.22104
+50% $0.27630
+100% $0.36840
```

### Missed

```
⚠️ MISSED IDEAL ENTRY

PAIR: XYZUSDT

🎯 ORIGINAL ENTRY: $0.18420
CURRENT PRICE: $0.18605
LATE BY: +1.01%

STATUS: MISSED ENTRY — DO NOT CHASE.
```

The original trigger price is preserved so a missed move can still be reviewed.

## 🧪 Historical Research

Research dataset:

- 12 months of Binance Spot 5m candles
- ~32.8 million candles
- 328-symbol research universe
- Existing 32 monitored symbols excluded
- Current price filter: <= $2

Observed breakout outcomes for the validated setup:

| Target | Historical hit rate |
|---|---:|
| +5% | 63.58% |
| +10% | 44.35% |
| +20% | 24.65% |
| +50% | 6.99% |
| +100% | 2.04% |

The research also showed that chasing the breakout gets progressively worse as entry moves farther above the original trigger.

## 💸 Cost

The intended deployment is **$0**:

- Binance public Spot market-data endpoints
- GitHub Actions
- Telegram Bot API
- No VPS
- No paid market-data subscription
- No futures / F&O

GitHub scheduled workflows support a minimum 5-minute interval. Public-repository standard GitHub-hosted runners are free. See the official GitHub Actions documentation for scheduling and billing details.

## 🔐 Secrets

Add these GitHub Actions repository secrets:

```
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
```

Do not commit bot tokens or credentials to the repository.

## 🏗️ Files

| File | Purpose |
|---|---|
| `monitoring_scanner.py` | Existing 32-symbol scanner |
| `pump_scanner.py` | New research-driven Spot pump scanner |
| `.github/workflows/pipeline.yml` | Existing scanner pipeline |
| `.github/workflows/pump_scanner.yml` | New 5-minute pump scanner |
| `tracker.py` | Existing signal tracking |
| `distribution_scanner.py` | Existing distribution logic |

## ⚠️ Status

The pump scanner is now in live-canary stage.

The **entry pattern is validated historically**. Exit management is still being refined around the large retracements seen before some +20%/+50%/+100% moves.

That means this project is intentionally focused on:

**find the setup → give the exact entry price → detect the touch → tell me ENTER TRADE → preserve the target ladder.**

---

### Built for experimentation

Binance Spot • 5m data • Python • GitHub Actions • Telegram
