import json
import sys

if len(sys.argv) < 2:
    print("usage: python dump.py <file>")
    sys.exit(1)

filename = sys.argv[1]
try:
    data = json.load(open(filename))
except Exception as e:
    print(f"error loading {filename}: {e}")
    sys.exit(1)

items = sorted(data.items(), key=lambda x: x[1].get("count", 0), reverse=True)
print(f"{len(data)} unique coins rejected")
print()
print(f"{'COIN':<15} {'COUNT':>6} {'24h%':>8} {'PRICE':>12}  REASON")
print("-" * 70)

for sym, info in items[:30]:
    count = info.get("count", 0)
    change = info.get("change_24h", 0)
    price = info.get("price", 0)
    reason = info.get("top_reason", "")
    print(f"{sym:<15} {count:>6} {change:>7.2f}% {price:>12.6f}  {reason}")
