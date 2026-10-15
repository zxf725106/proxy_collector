#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
瓶颈定位：同一节点 × 多个测速源交叉测试。

判读：
  - 各源速度都接近 -> 瓶颈在节点/国际链路(换测速源无用, 需换线路)
  - 某源明显更快   -> 之前的测速源是瓶颈(测速结果被低估)
同时对照 DIRECT 直连，量化"代理相对直连"的增益或损耗。
"""
import asyncio
import time

import aiohttp
import yaml
import os
import subprocess

from output import _SafeDumper, _defuse_shortid

API_PORT = 19099
MIXED_PORT = 17904
API_SECRET = "bneck123"
MIHOMO = r"C:/Program Files/Clash Verge/verge-mihomo.exe"

SOURCES = [
    ("sgp.ovh", "https://sgp.proof.ovh.net/files/10Mb.dat"),
    ("linode-sg", "http://speedtest.singapore.linode.com/100MB-singapore.bin"),
    ("linode-tk", "http://speedtest.tokyo2.linode.com/100MB-tokyo2.bin"),
    ("ms-cdn", "https://download.microsoft.com/download/2/0/E/20E90413-712F-438C-988E-FDAA79A8AC3D/dotnetfx35.exe"),
]

NBYTES = 2_000_000
TIMEOUT = 15


def port_up():
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", API_PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


async def dl(session, url, proxy):
    try:
        async with session.get(url, proxy=proxy,
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200:
                return 0.0
            clen = r.headers.get("Content-Length", "")
            if not str(clen).isdigit() or int(clen) < NBYTES:
                return 0.0
            t0 = time.time()
            n = 0
            async for c in r.content.iter_chunked(1 << 16):
                n += len(c)
                if n >= NBYTES:
                    break
            dt = time.time() - t0
            if n < 256 * 1024 or dt < 0.3:
                return 0.0
            return (n / (1024 * 1024)) / dt
    except Exception:
        return 0.0


async def main():
    d = yaml.safe_load(open("output-filtered-fast.yaml", encoding="utf-8"))
    nodes = d["proxies"][:3]

    print("[*] 第一步：DIRECT 直连基准（不走代理）\n")
    base = {}
    async with aiohttp.ClientSession() as s:
        for tag, url in SOURCES:
            v = await dl(s, url, None)
            base[tag] = v
            print(f"    {tag:12s} {v:6.2f} MB/s")

    cdata = dict(d)
    cdata["mixed-port"] = MIXED_PORT
    cdata["external-controller"] = f"127.0.0.1:{API_PORT}"
    cdata["secret"] = API_SECRET
    cdata["mode"] = "global"
    cdata["proxy-groups"] = [{"name": "__BN__", "type": "select",
                              "proxies": [p["name"] for p in d["proxies"]]}]
    cdata["rules"] = ["MATCH,__BN__"]
    for p in cdata.get("proxies", []):
        p.pop("_http_delay", None)
        if p.get("reality-opts"):
            p["reality-opts"] = _defuse_shortid(p["reality-opts"])
    tmp = "_bn_tmp.yaml"
    yaml.dump(cdata, open(tmp, "w", encoding="utf-8"), allow_unicode=True,
              default_flow_style=False, sort_keys=False, Dumper=_SafeDumper)

    proc = subprocess.Popen([MIHOMO, "-f", tmp], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(45):
            if port_up():
                break
            await asyncio.sleep(1)
        else:
            print("[!] mihomo 未就绪")
            return

        api = f"http://127.0.0.1:{API_PORT}"
        hdr = {"Authorization": f"Bearer {API_SECRET}"}
        proxy = f"http://127.0.0.1:{MIXED_PORT}"

        print(f"\n[*] 第二步：Top3 节点 × {len(SOURCES)} 个测速源交叉测试\n")
        head = "节点".ljust(34) + "".join(t.rjust(12) for t, _ in SOURCES) + "最佳".rjust(10)
        print(head)
        print("-" * len(head))
        async with aiohttp.ClientSession() as s:
            for p in nodes:
                name = p["name"]
                try:
                    async with s.put(f"{api}/proxies/__BN__", json={"name": name},
                                     headers=hdr,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status not in (200, 204):
                            continue
                except Exception:
                    continue
                await asyncio.sleep(0.4)
                row = []
                for tag, url in SOURCES:
                    row.append(await dl(s, url, proxy))
                print(name[:32].ljust(34)
                      + "".join(f"{v:12.2f}" for v in row)
                      + f"{max(row):10.2f}")

        print("\n[*] 直连基准对照：")
        for tag, _ in SOURCES:
            print(f"    {tag:12s} {base[tag]:6.2f} MB/s")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    asyncio.run(main())
