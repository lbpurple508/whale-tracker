import json
import sys

if len(sys.argv) < 2:
    print("usage: python dump.py <file1> [file2] [file3]")
    sys.exit(1)

labels = {
    "rejections": "SPIKE",
    "grind_rejections": "GRIND",
    "futures_rejections": "FUTURES",
}

for filename in sys.argv[1:]:
    print(f"\n=== {labels.get(filename.replace('.json','').split('/')[-1], filename)} ===")
    try:
        data = json.load(open(filename))
    except Exception:
        print("no data")
        continue
    items = sorted(data.items(), key=lambda x: x[1].get("count", 0), reverse=True)
    print(f"{len(data)} coins rejected\n")
    for sym, info in items[:15]:
        count = info.get("count", 0)
        change = info.get("change_24h", 0)
        reason = info.get("top_reason", "")
        print(f"{sym:<12} {count:>4}x {change:>+7.2f}% {reason}")
