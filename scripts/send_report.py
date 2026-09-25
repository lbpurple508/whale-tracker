import os
import requests

token = os.environ.get('TELEGRAM_TOKEN')
chat = os.environ.get('TELEGRAM_CHAT_ID')

if not token or not chat:
    print("missing telegram env vars")
    exit(0)

if not os.path.exists('report.txt'):
    print("no report.txt")
    exit(0)

text = open('report.txt').read()
max_len = 4000
chunks = [text[i:i+max_len] for i in range(0, len(text), max_len)]

for c in chunks:
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': c},
            timeout=15
        )
    except Exception as e:
        print(f"send error: {e}")

print(f"sent {len(chunks)} message(s)")
