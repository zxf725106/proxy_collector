#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性测试：在 re-test 的 mihomo(mode=rule) 上，直接调用 speed_test.measure() 测节点，
看是否与 speed_test 相同方法能成功（若成功 -> 复测门改用 measure 即可）。"""
import os, time, tempfile, subprocess, socket, asyncio, yaml, sys
import aiohttp
sys.path.insert(0, r"C:\Users\zxf72\proxy_collector")
from speed_test import measure, SPEED_URLS
from urllib.parse import quote

BASE = r"C:\Users\zxf72\proxy_collector"
MIHOMO = r"C:\Program Files\Clash Verge\verge-mihomo.exe"
FAST = os.path.join(BASE, "output-filtered-fast.yaml")
SECRET = "verify"
PORT, API = 17900, 19099

def port_up(p):
    s = socket.socket(); s.settimeout(1)
    try: s.connect(("127.0.0.1", p)); return True
    except Exception: return False
    finally: s.close()

async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    mode = sys.argv[2] if len(sys.argv) > 2 else "rule"
    d = yaml.safe_load(open(FAST, encoding="utf-8")) or {}
    nodes = d.get("proxies", [])[:n]
    cfg = {"mixed-port": PORT, "external-controller": f"127.0.0.1:{API}", "secret": SECRET,
           "allow-lan": False, "mode": mode, "log-level": "warning", "proxies": nodes,
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
        print(f"mode={mode} nodes={len(nodes)} SPEED_URLS={len(SPEED_URLS)}", flush=True)
        base = f"http://127.0.0.1:{API}"
        proxy = f"http://127.0.0.1:{PORT}"
        hdr = {"Authorization": f"Bearer {SECRET}"}
        async with aiohttp.ClientSession() as s:
            for name in ["DIRECT"] + [nd["name"] for nd in nodes]:
                try:
                    async with s.put(f"{base}/proxies/__VERIFY__", json={"name": name}, headers=hdr,
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        st = r.status
                except Exception as e:
                    print(f"  PUT_EXC {name[:30]} {e}"); continue
                await asyncio.sleep(0.4)
                mbps = await measure(name, s, proxy, 300000, 15)
                print(f"  [{'OK ' if mbps>0 else 'DEAD'}] {name[:46]:46s} {mbps:.2f} MB/s (PUT {st})", flush=True)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: os.remove(tmp)
        except OSError: pass

if __name__ == "__main__":
    asyncio.run(main())
