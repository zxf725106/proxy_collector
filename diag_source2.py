#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扩展测速源候选测试：找出直连可用、且能稳定下满数据的源。"""
import asyncio
import time

import aiohttp

URLS = [
    "http://speedtest.tele2.net/10MB.zip",
    "https://speed.cloudflare.com/__down?bytes=10000000",
    "http://speedtest.ftp.otenet.gr/files/test10Mb.db",
    "https://sgp.proof.ovh.net/files/10Mb.dat",
    "http://212.183.159.230/10MB.zip",
    "https://nbg1-speed.hetzner.com/100MB.bin",
    "https://ash-speed.hetzner.com/100MB.bin",
    "https://sin-speed.hetzner.com/100MB.bin",
    "http://speedtest.singapore.linode.com/100MB-singapore.bin",
    "http://speedtest.tokyo2.linode.com/100MB-tokyo2.bin",
    "https://download.microsoft.com/download/2/0/E/20E90413-712F-438C-988E-FDAA79A8AC3D/dotnetfx35.exe",
]

NBYTES = 3_000_000
TIMEOUT = 25


async def probe(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            clen = resp.headers.get("Content-Length", "?")
            if resp.status != 200:
                return (resp.status, clen, 0, 0.0, 0.0)
            t0 = time.time()
            dl = 0
            async for chunk in resp.content.iter_chunked(1 << 16):
                dl += len(chunk)
                if dl >= NBYTES:
                    break
            dt = time.time() - t0
            return (200, clen, dl, dt, (dl / (1024 * 1024)) / dt if dt > 0 else 0.0)
    except Exception as e:
        return (type(e).__name__[:18], "-", 0, 0.0, 0.0)


async def main():
    print("[*] 扩展测速源候选 —— 直连测试 (要求: 200 + 有Content-Length + 能下满3MB)\n")
    print(f"{'URL':60s} {'状态':>18s} {'ContentLen':>12s} {'下载KB':>9s} {'MB/s':>7s}")
    print("-" * 116)
    good = []
    async with aiohttp.ClientSession() as s:
        for url in URLS:
            st, clen, dl, dt, mbps = await probe(s, url)
            ok = (st == 200 and dl >= NBYTES and str(clen).isdigit())
            if ok:
                good.append((url, mbps))
            print(f"{url[:58]:60s} {str(st):>18s} {str(clen):>12s} {dl/1024:9.1f} "
                  f"{mbps:7.2f}  {'OK' if ok else 'BAD'}")
    print("-" * 116)
    print(f"\n[结论] 可用源 {len(good)}:")
    for u, m in good:
        print(f"    {m:6.2f} MB/s  {u}")


if __name__ == "__main__":
    asyncio.run(main())
