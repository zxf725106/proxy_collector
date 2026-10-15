#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：对比 re-test 与 speed_test 两种 mihomo 路由方式，找出复测全 DEAD 的真因。
- 用当前 output-filtered-fast.yaml 的前 N 个节点
- 起一个 mihomo，mode 可切换(rule/global)，同一 select 组
- 先测 DIRECT 基线(验证 mihomo 代理链路本身可用)
- 再逐个切节点下载，打印每次的 URL/状态/字节/异常
"""
import os, sys, time, tempfile, subprocess, asyncio, yaml, socket
import aiohttp

BASE = r"C:\Users\zxf72\proxy_collector"
MIHOMO = r"C:\Program Files\Clash Verge\verge-mihomo.exe"
FAST = os.path.join(BASE, "output-filtered-fast.yaml")
SECRET = "verify"
URLS = [
    "https://www.gstatic.com/generate_204",
    "https://speed.cloudflare.com/__down?bytes=300000",
    "https://sgp.proof.ovh.net/files/1Mb.dat",
    "http://212.183.159.230/1MB.zip",
]

def port_up(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(("127.0.0.1", p)); return True
    except Exception:
        return False
    finally:
        s.close()

async def run(mode, nodes, port=17900, api=19099):
    cfg = {
        "mixed-port": port,
        "external-controller": f"127.0.0.1:{api}",
        "secret": SECRET,
        "allow-lan": False,
        "mode": mode,
        "log-level": "warning",
        "proxies": nodes,
        "proxy-groups": [{"name": "__VERIFY__", "type": "select",
                          "proxies": ["DIRECT"] + [p["name"] for p in nodes]}],
        "rules": ["MATCH,__VERIFY__"],
    }
    fd, tmp = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"\n===== mode={mode} ports={port}/{api} nodes={len(nodes)} =====", flush=True)
    proc = subprocess.Popen([MIHOMO, "-f", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        ready = False
        for _ in range(25):
            if port_up(api):
                ready = True; break
            await asyncio.sleep(1)
        if not ready:
            print("[!] mihomo API 未就绪", flush=True); return
        base = f"http://127.0.0.1:{api}"
        hdr = {"Authorization": f"Bearer {SECRET}"}
        proxy = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as s:
            # 组内成员
            async with s.get(f"{base}/proxies/__VERIFY__", headers=hdr, timeout=aiohttp.ClientTimeout(total=5)) as r:
                j = await r.json()
                allm = j.get("all", [])
                print(f"[group] members={len(allm)} first3={allm[:3]}", flush=True)
            # DIRECT 基线
            await s.put(f"{base}/proxies/__VERIFY__", json={"name": "DIRECT"}, headers=hdr,
                        timeout=aiohttp.ClientTimeout(total=5))
            await asyncio.sleep(0.3)
            ok, det = await try_dl(s, proxy, URLS[0])
            print(f"[DIRECT] gstatic204 -> {det}", flush=True)
            # 逐节点
            for node in nodes:
                name = node["name"]
                try:
                    async with s.put(f"{base}/proxies/__VERIFY__", json={"name": name}, headers=hdr,
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status not in (200, 204):
                            print(f"[node] {name[:40]:40s} PUT_FAIL status={r.status}", flush=True); continue
                except Exception as e:
                    print(f"[node] {name[:40]:40s} PUT_EXC {type(e).__name__}: {e}", flush=True); continue
                await asyncio.sleep(0.3)
                res = []
                for u in URLS:
                    ok, det = await try_dl(s, proxy, u)
                    res.append(det)
                    if ok:
                        break
                print(f"[node] {name[:40]:40s} {'OK' if ok else 'DEAD'} | {res}", flush=True)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: os.remove(tmp)
        except OSError: pass

async def try_dl(session, proxy, url):
    try:
        async with session.get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            n = 0
            async for ch in resp.content.iter_chunked(32768):
                n += len(ch)
                if n >= 200000: break
            return (resp.status == 200 and n >= 1024), f"http{resp.status} bytes={n}"
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}"

async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "rule"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    d = yaml.safe_load(open(FAST, encoding="utf-8")) or {}
    nodes = d.get("proxies", [])[:n]
    print(f"FAST nodes total={len(d.get('proxies', []))}, testing first {len(nodes)}", flush=True)
    for i, nd in enumerate(nodes):
        print(f"  #{i} type={nd.get('type')} name={nd.get('name','')[:50]}", flush=True)
    await run(mode, nodes)

if __name__ == "__main__":
    asyncio.run(main())
