import os
import requests
from flask import Flask, request
from datetime import datetime

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

@app.route("/")
def home():
    return "Whale Tracker Bot is running."

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if isinstance(data, dict) and data.get("type") == "ADDRESS_ACTIVITY":
            event = data.get("event", {})
            activities = event.get("activity", [])
            for act in activities:
                process_alchemy_activity(act)
        else:
            txs = data if isinstance(data, list) else [data]
            for tx in txs:
                process_transaction(tx)
        return "OK", 200
    except Exception as e:
        print(f"Webhook error: {e}")
        return f"Error: {str(e)}", 500

def process_alchemy_activity(act):
    from_addr = act.get("fromAddress", "Unknown")
    to_addr = act.get("toAddress", "Unknown")
    value = act.get("value", 0)
    asset = act.get("asset", "Unknown")
    tx_hash = act.get("hash", "Unknown")
    category = act.get("category", "unknown")
    raw = act.get("rawContract", {})
    token_address = raw.get("address") if raw else None

    lines = [
        "🐋 <b>WHALE ALERT</b>",
        "",
        f"<b>From:</b> <code>{from_addr[:12]}...</code>",
        f"<b>To:</b> <code>{to_addr[:12]}...</code>",
        f"<b>Asset:</b> {asset}",
        f"<b>Amount:</b> {value}",
    ]
    if token_address:
        lines.append(f"<b>Token:</b> <code>{token_address[:12]}...</code>")
    lines.append(f"<b>Category:</b> {category}")
    lines.append(f"<b>Tx:</b> <code>{tx_hash[:24]}...</code>")

    message = "\n".join(lines)
    send_telegram(message)

def process_transaction(tx):
    description = tx.get("description", "Unknown transaction")
    signature = tx.get("signature", "Unknown")
    fee_payer = tx.get("feePayer", "Unknown")
    timestamp = tx.get("timestamp", 0)
    token_transfers = tx.get("tokenTransfers", [])

    lines = [
        "🐋 <b>WHALE ALERT</b>",
        "",
        f"<b>Wallet:</b> <code>{fee_payer[:8]}...</code>",
        f"<b>Action:</b> {description}",
    ]
    for tt in token_transfers:
        mint = tt.get("mint", "Unknown")
        amount = tt.get("tokenAmount", 0)
        to_addr = tt.get("toUserAccount", "")
        direction = "BUY" if to_addr == fee_payer else "SELL"
        lines.append(f"<b>Token:</b> {mint[:8]}...")
        lines.append(f"<b>Amount:</b> {amount}")
        lines.append(f"<b>Direction:</b> {direction}")

    lines.append(f"<b>Tx:</b> <code>{signature[:20]}...</code>")
    if timestamp:
        lines.append(f"<b>Time:</b> {datetime.fromtimestamp(timestamp)}")

    message = "\n".join(lines)
    send_telegram(message)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
