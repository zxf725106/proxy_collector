# -*- coding: utf-8 -*-
"""示例：免费 crawler 模板（文件名不带下划线才会被自动注册）。

把它改名为 proxifly.py 即可接入 crawler_registry.run_all()。
约定：
  * __crawler__ 元数据（name/source/enabled）
  * crawl() 返回「通用代理 URI」列表（ss://, vmess:// ...）
    这样产物能直接进 proxy_collector 现有的去重/TCP/mihomo 链路。
"""

__crawler__ = {"name": "proxifly", "source": "free", "enabled": True}

import json
import urllib.request

# 这里只演示结构；真实实现请照搬你现有的 crawlers/proxifly.py 抓取逻辑，
# 最后把节点转成通用 URI 返回即可。


def crawl() -> list[str]:
    # demo：从 jsDelivr CDN 抓 proxifly 全量，转成 URI（伪代码示意）
    url = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list/proxies/all/data.json"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    out = []
    for item in data.get("proxies", []):
        host = item.get("ip")
        port = item.get("port")
        if host and port:
            out.append(f"socks5://{host}:{port}#proxifly")
    return out
