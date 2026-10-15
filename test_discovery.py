# -*- coding: utf-8 -*-
"""source_discovery 本地自测（不触网，仅验证 URL 提取正则与去重）。

运行： python test_discovery.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import source_discovery as sd

PASS = FAIL = 0


def check(c, m):
    global PASS, FAIL
    if c:
        PASS += 1; print(f"  ok  - {m}")
    else:
        FAIL += 1; print(f"  FAIL- {m}")


# 1. GitHub README 文本提取
readme = """
# 免费节点
订阅1: https://example.com/clash.yaml
订阅2: https://example.com/proxies.txt?token=abc
频道: https://t.me/proxypool
vmess://abc123==#node1
查看更多 https://blog.example.org/list
"""
urls = sd.GitHubDiscoverer._extract_from_text(readme)
check(any(u.endswith("clash.yaml") for u in urls), "提取 clash.yaml 订阅链接")
check(any("proxies.txt?token=abc" in u for u in urls), "提取带参数的 txt 链接")
check(any("t.me/proxypool" in u for u in urls), "提取 t.me 频道链接")
check(any(u.startswith("vmess://") for u in urls), "提取裸代理 URI")

# 2. Telegram 文本提取
tg_text = "新节点 https://sub.example.com/api/v1/sub?token=x trojan://p@1.1.1.1:443#t"
tg_urls = sd.TelegramDiscoverer._extract(tg_text)
check(any("sub.example.com" in u for u in tg_urls), "TG 提取订阅 URL")
check(any(u.startswith("trojan://") for u in tg_urls), "TG 提取 trojan URI")

# 3. 去重
dup = sd._dedupe(["https://a.com/x", "https://a.com/x", " https://a.com/y "])
check(len(dup) == 2 and dup[1] == "https://a.com/y", f"去重+去空白 ({dup})")

# 4. discover_all 路由（twitter/github 用 mock，telegram 关）
class _FakeGH:
    def __init__(self, *a, **k): pass
    def discover(self): return ["https://x.com/a.yaml"]

sd.GitHubDiscoverer = _FakeGH  # mock
cfg = {"discovery": {"github": {"enabled": True}, "telegram": {"enabled": False},
                     "twitter": {"enabled": False}}}
res = sd.discover_all(cfg)
check(res == ["https://x.com/a.yaml"], "discover_all 路由+合并正确")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
