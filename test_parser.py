# -*- coding: utf-8 -*-
"""subscription_parser / crawler_registry 本地自测（不触网）。

运行：
  python test_parser.py
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import subscription_parser as sp

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  - {msg}")
    else:
        FAIL += 1
        print(f"  FAIL- {msg}")


# ---- 1. base64 解码 ----
sample_yaml = """
proxies:
  - name: "JP-SS"
    type: ss
    server: 1.2.3.4
    port: 8388
    cipher: aes-256-gcm
    password: mypass
  - name: "US-Trojan"
    type: trojan
    server: 5.6.7.8
    port: 443
    password: trojanpass
    sni: example.com
  - name: "HK-VMess"
    type: vmess
    server: 9.9.9.9
    port: 443
    uuid: 11111111-2222-3333-4444-555555555555
    alterId: 0
    cipher: auto
    network: ws
    tls: true
    servername: hk.example.com
    path: /ws
"""
b64 = base64.b64encode(sample_yaml.encode("utf-8")).decode()
decoded = sp.decode_body(b64)
check("proxies" in decoded, "base64 订阅体可解码")

# ---- 2. 解析 Clash YAML ----
nodes = sp.parse_clash_yaml(decoded)
check(len(nodes) == 3, f"解析出 3 个节点（实际 {len(nodes)}）")

# ---- 3. node_to_uri 各协议 ----
by_name = {n["name"]: n for n in nodes}
ss_uri = sp.node_to_uri(by_name["JP-SS"])
trojan_uri = sp.node_to_uri(by_name["US-Trojan"])
vmess_uri = sp.node_to_uri(by_name["HK-VMess"])

check(ss_uri and ss_uri.startswith("ss://"), f"ss -> {ss_uri}")
check(trojan_uri and trojan_uri.startswith("trojan://"), f"trojan -> {trojan_uri}")
check("sni=example.com" in trojan_uri, "trojan 参数(sni)保留")
check(vmess_uri and vmess_uri.startswith("vmess://"), "vmess -> uri 生成")
# vmess uri 应能回解
vdec = base64.urlsafe_b64decode(vmess_uri[len("vmess://"):] + "==").decode()
check("9.9.9.9" in vdec and "hk.example.com" in vdec, "vmess uri 可回解")

# ---- 4. parse_domains ----
dom = sp.parse_domains("a.com@#@#cp@#@#inv@#@#api\n# comment\nb.com\n")
check(len(dom) == 2, f"domains 解析出 2 条（实际 {len(dom)}）")
check(dom[0].coupon == "cp" and dom[0].invite_code == "inv", "domains 字段解析正确")

# ---- 5. parse_subscription（mock 网络） ----
orig_fetch = sp.fetch_subscription
sp.fetch_subscription = lambda *a, **k: b64  # 返回上面的 base64 体
uris = sp.parse_subscription("https://fake/sub")
sp.fetch_subscription = orig_fetch
check(len(uris) == 3, f"parse_subscription 端到端产出 3 个 URI（实际 {len(uris)}）")
check(all(sp._LINK_RE.match(u) for u in uris), "产出均为通用代理 URI")

# ---- 6. crawler_registry 自动发现（示例 crawler 以下划线开头被跳过） ----
import crawler_registry as cr

metas = cr.discover_crawlers(os.path.join(os.path.dirname(__file__), "crawlers"))
# 示例文件带 _ 前缀，应不被注册
check("proxifly" not in metas and "my-airport" not in metas,
      "下划线示例 crawler 不会被自动注册（符合预期）")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
