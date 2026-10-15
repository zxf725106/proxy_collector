import yaml, subprocess, sys, time, os
sys.path.insert(0, r"C:\Users\zxf72\proxy_collector")
from output import _SafeDumper, _defuse_shortid

MIH = r"C:\Program Files\Clash Verge\verge-mihomo.exe"

def make_clean(src, dst):
    d = yaml.safe_load(open(src, encoding="utf-8"))
    d["external-controller"] = "127.0.0.1:9096"
    d["secret"] = "test"
    d["allow-lan"] = False
    d["mode"] = "direct"
    d.pop("proxy-groups", None)
    d.pop("rules", None)
    d.pop("rule-providers", None)
    # disable geo auto-download attempts
    d["geo-auto-update"] = False
    kept = []
    for p in d.get("proxies", []):
        if p.get("type") == "ss" and str(p.get("cipher", "")).lower().startswith("2022-"):
            continue
        if p.get("reality-opts"):
            p["reality-opts"] = _defuse_shortid(p["reality-opts"])
        kept.append(p)
    d["proxies"] = kept
    yaml.dump(d, open(dst, "w", encoding="utf-8"), allow_unicode=True, default_flow_style=False, sort_keys=False, Dumper=_SafeDumper)
    return len(kept)

for name, src in [("ruk1ng","candidates/ruk1ng-clash.yaml"),("hyqzj","candidates/hyqzj-clash.yaml")]:
    dst = f"candidates/_clean_{name}.yaml"
    n = make_clean(src, dst)
    print(f"=== {name}: {n} proxies, testing mihomo startup ===", flush=True)
    t0=time.time()
    proc = subprocess.Popen([MIH,"-f",dst], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0)
    try:
        out,err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill(); out,err = b"",b"TIMEOUT"
    dt=time.time()-t0
    print(f"    exit={proc.returncode} dt={dt:.1f}s")
    print("    STDOUT:", (out or b"").decode(errors="ignore")[-800:])
    print("    STDERR:", (err or b"").decode(errors="ignore")[-800:])
    print()
