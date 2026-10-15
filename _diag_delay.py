#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 mihomo 自带 delay API 直接测每个节点是否可用（绕开 aiohttp SSL），判定节点真假死活。"""
import os, time, tempfile, subprocess, urllib.request, urllib.parse, json, socket, yaml, sys

BASE = r"C:\Users\zxf72\proxy_collector"
MIHOMO = r"C:\Program Files\Clash Verge\verge-mihomo.exe"
FAST = os.path.join(BASE, "output-filtered-fast.yaml")
SECRET = "verify"
PORT, API = 17900, 19099
TEST_URLS = [
    "http://www.gstatic.com/generate_204",
    "http://www.google.com/generate_204",
    "https://speed.cloudflare.com/__down?bytes=200000",
]

def port_up(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(("127.0.0.1", p)); return True
    except Exception:
        return False
    finally:
        s.close()

def delay(name, url):
    q = urllib.parse.urlencode({"timeout": 5000, "url": url})
    req = urllib.request.Request(f"http://127.0.0.1:{API}/proxies/{urllib.parse.quote(name, safe='')}/delay?{q}")
    req.add_header("Authorization", f"Bearer {SECRET}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"err": f"{type(e).__name__}:{str(e)[:80]}"}

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    d = yaml.safe_load(open(FAST, encoding="utf-8")) or {}
    nodes = d.get("proxies", [])[:n]
    cfg = {"mixed-port": PORT, "external-controller": f"127.0.0.1:{API}", "secret": SECRET,
           "allow-lan": False, "mode": "rule", "log-level": "warning",
           "proxies": nodes,
           "proxy-groups": [{"name": "__VERIFY__", "type": "select",
                             "proxies": ["DIRECT"] + [p["name"] for p in nodes]}],
           "rules": ["MATCH,__VERIFY__"]}
    fd, tmp = tempfile.mkstemp(suffix=".yaml"); os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    proc = subprocess.Popen([MIHOMO, "-f", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(25):
            if port_up(API): break
            time.sleep(1)
        else:
            print("[!] API 未就绪"); return
        print(f"nodes={len(nodes)}  (mihomo delay API 独立测每个节点)", flush=True)
        print(f"[DIRECT] " + str({u.split('/')[2]: delay('DIRECT', u) for u in TEST_URLS}), flush=True)
        alive = 0
        for nd in nodes:
            nm = nd["name"]
            res = {u.split('/')[2]: delay(nm, u) for u in TEST_URLS}
            ok = any("delay" in v for v in res.values())
            alive += ok
            print(f"[{'OK ' if ok else 'DEAD'}] {nm[:46]:46s} {res}", flush=True)
        print(f"== 汇总: {alive}/{len(nodes)} 节点在 mihomo 层面可用 ==", flush=True)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: os.remove(tmp)
        except OSError: pass

if __name__ == "__main__":
    main()
