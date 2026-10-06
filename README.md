# Market Signal Monitor

A lightweight Python market-monitoring project using public exchange data and Telegram notifications.

## What it does

The repository contains independent monitoring components for spotting unusual market conditions and tracking signals.

The public implementation intentionally keeps the exact research methodology private. Runtime code uses adaptive thresholds and price-level logic rather than publishing the underlying research recipe.

## Alerts

The notification flow is designed around three states:

```
WATCH SETUP
    ↓
TARGET ENTRY PRICE
    ↓
ENTER TRADE
    ↓
MISSED IDEAL ENTRY (when the move has already passed)
```

Example:

```
👀 WATCH SETUP

PAIR: XYZUSDT

🎯 TARGET ENTRY PRICE: $0.18420
STATUS: WAITING
```

Then:

```
🚀 ENTER TRADE

PAIR: XYZUSDT

🎯 ENTRY PRICE: $0.18420
CURRENT PRICE: $0.18420

TARGET LADDER:
+5% / +10% / +20% / +50% / +100%
```

And when the level has already been passed:

```
⚠️ MISSED IDEAL ENTRY

PAIR: XYZUSDT

🎯 ORIGINAL ENTRY: $0.18420
CURRENT PRICE: $0.18605
STATUS: MISSED ENTRY
```

## Technology

- Python
- Public exchange market-data endpoints
- Telegram Bot API
- GitHub Actions
- 5-minute scheduled execution

## Cost

Designed to run without a paid server or paid market-data subscription.

## Secrets

The workflows expect repository secrets named:

```
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
```

Credentials should never be committed to source control.

## Project files

| File | Purpose |
|---|---|
| `monitoring_scanner.py` | Existing monitoring component |
| `market_scanner.py` | New market-signal component |
| `tracker.py` | Signal tracking |
| `distribution_scanner.py` | Distribution logic |
| `.github/workflows/pipeline.yml` | Existing workflow |
| `.github/workflows/market_scanner.yml` | Market-signal workflow |

## Notes

This repository is for personal experimentation and automated market monitoring. It does not publish the full internal research process or decision thresholds.
