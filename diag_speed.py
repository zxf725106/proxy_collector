#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测速真实性诊断：验证 speed_test.py 报出的高 MB/s 是否真实。

对 Top N 节点逐个检测三项：
  1. 实际下载字节数 / 耗时 / 算出的 MB/s  -> 判断是否"秒回小响应"导致虚高
  2. 是否真正下满目标字节 (full=True/False)
  3. 真实可用性：能否访问 google generate_204 与 youtube

用法: python diag_speed.py [input.yaml] [--n 12]
"""
import argparse
import asyncio
import os
import subprocess
import time

import aiohttp
import yaml

from output import _SafeDumper, _defuse_shortid

API_PORT = 19098
MIXED_PORT = 17903
API_SECRET = "diag123"
MIHOMO = r"C:/Program Files/Clash Verge/verge-mihomo.exe"

DL_URL = "https://sgp.proof.ovh.net/files/10Mb.dat"                  # 已验证可靠(新加坡)
DL_URL2 = "http://speedtest.singapore.linode.com/100MB-singapore.bin"  # 已验证可靠
CHECK_204 = "https://www.gstatic.com/generate_204"  # 真实连通性
CHECK_YT = "https://www.youtube.com/generate_204"


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


async def probe_download(session, proxy_url, url, nbytes, timeout):
    """返回 (status, downloaded, dt, mbps, full)"""
    try:
        t_conn = time.time()
        async with session.get(
            url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                return (resp.status, 0, 0.0, 0.0, False)
            t0 = time.time()
            downloaded = 0
            async for chunk in resp.content.iter_chunked(1 << 16):
                downloaded += len(chunk)
                if downloaded >= nbytes:
                    break
            dt = time.time() - t0
            mbps = (downloaded / (1024 * 1024)) / dt if dt > 0 else 0.0
            return (200, downloaded, dt, mbps, downloaded >= nbytes)
    except Exception as e:
        return (type(e).__name__, 0, 0.0, 0.0, False)


async def probe_204(session, proxy_url, url, timeout=8):
    try:
        async with session.get(
            url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            return resp.status
    except Exception as e:
        return type(e).__name__[:12]


async def main(input_yaml, n, nbytes, timeout):
    data = yaml.safe_load(open(input_yaml, encoding="utf-8"))
    nodes = data.get("proxies", [])[:n]
    print(f"[*] 诊断 {len(nodes)} 个节点 (来自 {input_yaml} 的 Top{n})\n", flush=True)

    cdata = dict(data)
    cdata["mixed-port"] = MIXED_PORT
    cdata["external-controller"] = f"127.0.0.1:{API_PORT}"
    cdata["secret"] = API_SECRET
    cdata["allow-lan"] = False
    cdata["mode"] = "global"
    cdata["proxy-groups"] = [{
        "name": "__DIAG__", "type": "select",
        "proxies": ["DIRECT"] + [p["name"] for p in data.get("proxies", [])],
    }]
    cdata["rules"] = ["MATCH,__DIAG__"]
    for p in cdata.get("proxies", []):
        p.pop("_http_delay", None)
        if p.get("reality-opts"):
            p["reality-opts"] = _defuse_shortid(p["reality-opts"])

    tmp = os.path.join(os.path.dirname(os.path.abspath(input_yaml)), "_diag_tmp.yaml")
    yaml.dump(cdata, open(tmp, "w", encoding="utf-8"),
              allow_unicode=True, default_flow_style=False, sort_keys=False, Dumper=_SafeDumper)

    proc = subprocess.Popen([MIHOMO, "-f", tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(45):
            if port_up():
                break
            await asyncio.sleep(1)
        else:
            print("[!] mihomo API 未就绪")
            return
        print("[*] mihomo 就绪，开始诊断\n", flush=True)

        base = f"http://127.0.0.1:{API_PORT}"
        hdr = {"Authorization": f"Bearer {API_SECRET}"}
        proxy_url = f"http://127.0.0.1:{MIXED_PORT}"

        print(f"{'#':>3} {'节点':38s} {'状态':>10s} {'下载KB':>9s} {'耗时s':>7s} "
              f"{'报告MB/s':>9s} {'满量':>5s} {'gstatic':>8s} {'youtube':>9s}")
        print("-" * 108, flush=True)

        full_ok = 0
        fake = 0
        async with aiohttp.ClientSession() as s:
            for i, p in enumerate(nodes, 1):
                name = p["name"]
                try:
                    async with s.put(f"{base}/proxies/__DIAG__", json={"name": name},
                                     headers=hdr, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status not in (200, 204):
                            print(f"{i:3d} {name[:36]:38s} 切换失败")
                            continue
                except Exception:
                    print(f"{i:3d} {name[:36]:38s} 切换异常")
                    continue
                await asyncio.sleep(0.4)

                st, dl, dt, mbps, full = await probe_download(s, proxy_url, DL_URL, nbytes, timeout)
                if st != 200 or dl == 0:
                    st2, dl2, dt2, mbps2, full2 = await probe_download(s, proxy_url, DL_URL2, nbytes, timeout)
                    if dl2 > dl:
                        st, dl, dt, mbps, full = st2, dl2, dt2, mbps2, full2

                g = await probe_204(s, proxy_url, CHECK_204)
                y = await probe_204(s, proxy_url, CHECK_YT)

                if full:
                    full_ok += 1
                if (not full) and mbps > 20:
                    fake += 1

                print(f"{i:3d} {name[:36]:38s} {str(st):>10s} {dl/1024:9.1f} {dt:7.3f} "
                      f"{mbps:9.2f} {'YES' if full else 'NO':>5s} {str(g):>8s} {str(y):>9s}", flush=True)

        print("-" * 108)
        print(f"\n[结论] 下满 {nbytes//1000}KB 的节点: {full_ok}/{len(nodes)}")
        print(f"[结论] 虚高节点(没下满却报 >20MB/s): {fake}/{len(nodes)}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", default="output-filtered-fast.yaml")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--bytes", type=int, default=3000000)
    ap.add_argument("--timeout", type=int, default=15)
    a = ap.parse_args()
    asyncio.run(main(a.input, a.n, a.bytes, a.timeout))
