import asyncio, sys, json, time, yaml
sys.path.insert(0, r"C:\Users\zxf72\proxy_collector")
from http_test import http_test_nodes, prepare_test_config
from output import _SafeDumper, _defuse_shortid

# 修复：prepare_test_config 没剥掉新版 mihomo 的 rule-set/dns 等顶层段，
# 导致 mihomo 因 'not found rule-set' 启动即崩。这里用更彻底的清理。
def make_clean(src, dst):
    d = yaml.safe_load(open(src, encoding="utf-8"))
    d["external-controller"] = "127.0.0.1:9097"
    d["secret"] = "proxycollector"
    d["allow-lan"] = False
    d["mode"] = "direct"
    for k in ("proxy-groups","rules","rule-providers","rule-set","dns","profile","proxy-providers","listeners","tun","script"):
        d.pop(k, None)
    d["geo-auto-update"] = False
    if "mixed-port" not in d and "port" not in d:
        d["mixed-port"] = 7897
    kept=[]
    for p in d.get("proxies",[]):
        if p.get("type")=="ss" and str(p.get("cipher","")).lower().startswith("2022-"):
            continue
        if p.get("reality-opts"):
            p["reality-opts"]=_defuse_shortid(p["reality-opts"])
        kept.append(p)
    d["proxies"]=kept
    yaml.dump(d, open(dst,"w",encoding="utf-8"), allow_unicode=True, default_flow_style=False, sort_keys=False, Dumper=_SafeDumper)
    return len(kept)

CAND=[("ruk1ng","candidates/ruk1ng-clash.yaml"),("hyqzj","candidates/hyqzj-clash.yaml")]
summary={}
for key,src in CAND:
    dst=f"candidates/_clean_{key}.yaml"
    n=make_clean(src,dst)
    data=yaml.safe_load(open(dst,encoding="utf-8"))
    names=[p.get("name") for p in data.get("proxies",[]) if p.get("name")]
    print(f"=== {key}: {len(names)} clean nodes, HTTP testing...", flush=True)
    t0=time.time()
    dm=asyncio.run(http_test_nodes(dst, names, timeout_ms=8000))
    dt=time.time()-t0
    print(f"=== {key}: ALIVE {len(dm)}/{len(names)} in {dt:.1f}s", flush=True)
    json.dump(dm, open(f"candidates/{key}.http.json","w"), ensure_ascii=False)
    summary[key]={"total":len(names),"alive":len(dm),"secs":round(dt,1)}
print("SUMMARY:", json.dumps(summary, ensure_ascii=False))
