import yaml, json
sys_path = r"C:\Users\zxf72\proxy_collector"
import sys; sys.path.insert(0, sys_path)
from output import _SafeDumper, _defuse_shortid

alive = json.load(open("candidates/ruk1ng.http.json", encoding="utf-8"))
alive_names = set(alive.keys())
print("alive count:", len(alive_names))

src = yaml.safe_load(open("candidates/ruk1ng-clash.yaml", encoding="utf-8"))
proxies = [p for p in src.get("proxies", []) if p.get("name") in alive_names]
print("matched proxies:", len(proxies))

# 干净化：删除非 proxies 段落
out = {"proxies": []}
for p in proxies:
    p.pop("_http_delay", None)
    if p.get("reality-opts"):
        p["reality-opts"] = _defuse_shortid(p["reality-opts"])
    out["proxies"].append(p)
yaml.dump(out, open("candidates/ruk1ng_alive.yaml", "w", encoding="utf-8"),
          allow_unicode=True, default_flow_style=False, sort_keys=False, Dumper=_SafeDumper)
print("written candidates/ruk1ng_alive.yaml")
