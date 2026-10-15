# -*- coding: utf-8 -*-
"""示例：GitHub 免费源发现 crawler（crawler_registry 模板）。

注意：发现源产出的是「订阅 URL / 代理链接」，不是最终 URI。
本例的 crawl() 直接把发现的 URL 经 subscription_parser 解析成 URI 返回，
这样能无缝并入 proxy_collector 主链路（去重/TCP/mihomo）。

改名 github_discovery.py 即可被自动注册（去掉下划线）。
"""

__crawler__ = {"name": "github_discovery", "source": "free", "enabled": True}

from source_discovery import GitHubDiscoverer, discover_all
import json
import os


def crawl() -> list[str]:
    cfg = {"discovery": {"github": {
        "enabled": True,
        "repos": ["proxifly/free-proxy-list"],
        "topics": ["proxypool", "free-proxy"],
        "queries": ["free proxy subscription"],
    }}}
    # 1) 发现 URL
    urls = discover_all(cfg)
    # 2) 解析成通用 URI
    from subscription_parser import parse_subscription
    out: list[str] = []
    for u in urls:
        try:
            out += parse_subscription(u)
        except Exception:
            continue
    return out
