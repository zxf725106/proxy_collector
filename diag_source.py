#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测速源对照诊断：先确认测速 URL 本身是否可靠（排除"源挂了"导致误判节点）。
直连(DIRECT) 逐个访问各测速源，打印状态码/Content-Length/实际下载量。
"""
import asyncio
import time

import aiohttp

URLS = [
    "http://www.ovh.net/files/10Mio.dat",
    "https://speed.hetzner.de/100MB.bin",
    "http://ipv4.download.thinkbroadband.com/10MB.zip",
    "https://mirror.rackspace.com/centos/7/isos/x86_64/0_README.txt",
    "http://speedtest.tele2.net/10MB.zip",
    "https://proof.ovh.net/files/10Mb.dat",
    "http://cachefly.cachefly.net/10mb.test",
]

NBYTES = 3_000_000
TIMEOUT = 20


async def probe(session, url):
    try:
        t_start = time.time()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            clen = resp.headers.get("Content-Length", "?")
            ctype = resp.headers.get("Content-Type", "?")[:24]
            if resp.status != 200:
                return (resp.status, clen, ctype, 0, 0.0, 0.0)
            t0 = time.time()
            dl = 0
            async for chunk in resp.content.iter_chunked(1 << 16):
                dl += len(chunk)
                if dl >= NBYTES:
                    break
            dt = time.time() - t0
            mbps = (dl / (1024 * 1024)) / dt if dt > 0 else 0.0
            return (200, clen, ctype, dl, dt, mbps)
    except Exception as e:
        return (type(e).__name__[:16], "-", "-", 0, 0.0, 0.0)


async def main():
    print("[*] 直连(不走代理)测试各测速源可靠性\n")
    print(f"{'URL':52s} {'状态':>14s} {'ContentLen':>13s} {'下载KB':>9s} {'耗时s':>7s} {'MB/s':>7s}")
    print("-" * 112)
    good = []
    async with aiohttp.ClientSession() as s:
        for url in URLS:
            st, clen, ctype, dl, dt, mbps = await probe(s, url)
            ok = (st == 200 and dl >= NBYTES)
            if ok:
                good.append(url)
            flag = "OK " if ok else "BAD"
            print(f"{url[:50]:52s} {str(st):>14s} {str(clen):>13s} {dl/1024:9.1f} "
                  f"{dt:7.3f} {mbps:7.2f}  {flag}")
    print("-" * 112)
    print(f"\n[结论] 可靠测速源 {len(good)}/{len(URLS)}:")
    for u in good:
        print("   ", u)
    if not good:
        print("    无 —— 本机直连所有测速源都失败(可能被墙/限速)，需换国内可达源做基准")


if __name__ == "__main__":
    asyncio.run(main())
