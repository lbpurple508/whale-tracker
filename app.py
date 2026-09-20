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
        txs = data if isinstance(data, list) else [data]
        for tx in txs:
            process_transaction(tx)
        return "OK", 200
    except Exception as e:
        print(f"Webhook error: {e}")
        return f"Error: {str(e)}", 500

def process_transaction(tx):
    description = tx.get("description", "Unknown transaction")
    signature = tx.get("signature", "Unknown")
    fee_payer = tx.get("feePayer", "Unknown")
    timestamp = tx.get("timestamp", 0)
    token_transfers = tx.get("tokenTransfers", [])
    native_transfers = tx.get("nativeTransfers", [])

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

    for nt in native_transfers:
        amount = nt.get("amount", 0) / 1_000_000_000
        if amount > 0.1:
            lines.append(f"<b>SOL:</b> {amount:.4f}")

    lines.append(f"<b>Tx:</b> <code>{signature[:20]}...</code>")
    if timestamp:
        lines.append(f"<b>Time:</b> {datetime.fromtimestamp(timestamp)}")

    message = "\n".join(lines)
    send_telegram(message)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
