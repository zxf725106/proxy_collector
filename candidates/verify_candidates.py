import asyncio, sys, json, time
sys.path.insert(0, r"C:\Users\zxf72\proxy_collector")
from http_test import http_test_nodes
import yaml

CAND = [
    ("candidates/rico-cell.yaml", "rico-cell"),
    ("candidates/ruk1ng-clash.yaml", "ruk1ng"),
    ("candidates/hyqzj-clash.yaml", "hyqzj"),
]

summary = {}
for path, key in CAND:
    data = yaml.safe_load(open(path, encoding="utf-8"))
    names = [p.get("name") for p in data.get("proxies", []) if p.get("name")]
    print(f"=== {key}: {len(names)} nodes loaded, HTTP testing...", flush=True)
    t0 = time.time()
    dm = asyncio.run(http_test_nodes(path, names, timeout_ms=8000))
    dt = time.time() - t0
    print(f"=== {key}: ALIVE {len(dm)}/{len(names)} in {dt:.1f}s", flush=True)
    json.dump(dm, open(f"candidates/{key}.http.json", "w"), ensure_ascii=False)
    summary[key] = {"total": len(names), "alive": len(dm), "secs": round(dt, 1)}

print("SUMMARY:", json.dumps(summary, ensure_ascii=False))
