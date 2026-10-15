"""临时脚本：单独验证新增订阅源的抓取与解析贡献"""
import asyncio
import sys
from collections import Counter

sys.path.insert(0, ".")

from dedup import deduplicate
from fetcher import fetch_all_pairs
from parser import parse_content

DEFAULT_SOURCES = [
    "https://raw.githubusercontent.com/awesome-vpn/awesome-vpn/master/clash.yaml",
    "https://raw.githubusercontent.com/mfuu/FreeProxies/master/sub.yaml",
    "https://raw.githubusercontent.com/ishalumi/proxy-node-collector/main/output/nodes_base64.txt",
    "https://www.xrayvip.com/free.txt",
]

# 支持命令行传入待测源：py -u _verify_new_sources.py <url1> <url2> ...
NEW_SOURCES = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SOURCES


async def main():
    print(f"测试 {len(NEW_SOURCES)} 个新增源...\n")
    results = await fetch_all_pairs(NEW_SOURCES, timeout=45)
    all_nodes = []
    for url, r in zip(NEW_SOURCES, results):
        tag = url.split("/")[-1] or url
        if isinstance(r, Exception):
            print(f"[FAIL] {tag}: 抓取异常 {r}")
            continue
        if not r or not r.strip():
            print(f"[EMPTY] {tag}: 抓取内容为空")
            continue
        nodes = parse_content(r)
        types = Counter(getattr(n, "type", "?") for n in nodes)
        print(f"[OK] {tag}")
        print(f"     内容 {len(r):,} 字符 -> 解析 {len(nodes):,} 节点")
        print(f"     类型: {dict(types.most_common())}")
        all_nodes.extend(nodes)

    print(f"\n合计解析: {len(all_nodes):,}")
    deduped = deduplicate(all_nodes)
    print(f"去重后: {len(deduped):,} (移除 {len(all_nodes) - len(deduped):,})")
    types = Counter(getattr(n, "type", "?") for n in deduped)
    print(f"类型: {dict(types.most_common())}")


asyncio.run(main())
