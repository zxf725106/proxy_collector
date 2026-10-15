#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐个测试 SPEED_URLS 直连(不走代理)是否可用，定位测速源当前状态。"""
import asyncio, sys, aiohttp
sys.path.insert(0, r"C:\Users\zxf72\proxy_collector")
from speed_test import SPEED_URLS

async def main():
    async with aiohttp.ClientSession() as s:
        for u in SPEED_URLS:
            try:
                async with s.get(u, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    n = 0
                    async for ch in r.content.iter_chunked(32768):
                        n += len(ch)
                        if n >= 300000:
                            break
                    print(f"  http{r.status} bytes={n:>7d}  {u[:70]}", flush=True)
            except Exception as e:
                print(f"  ERR {type(e).__name__}:{str(e)[:60]}  {u[:70]}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
