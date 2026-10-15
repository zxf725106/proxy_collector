# -*- coding: utf-8 -*-
"""
subscription_parser.py — 从 wzdnzd/aggregator 移植的「订阅解析 / 机场订阅接入」层
================================================================================

移植目标（对照 aggregator 的 subscribe/collect.py + subscribe/crawl.py）：
  1. 抓取订阅 URL（base64 体 / 纯 YAML / 每行一个链接 三种形态都能解）
  2. 解析 Clash YAML 的 proxies: 列表，按协议归一化
  3. 把结构化节点转成「通用代理 URI」（ss://, vmess://, vless://, trojan://,
     ssr://, hysteria2://, snell://, socks5://, http://）
     —— 这样产物能直接喂进 proxy_collector 现有的 parser.py（不再改解析逻辑）
  4. 解析 aggregator 风格的机场域名配置（address@#@#coupon@#@#invite@#@#api_prefix）

设计原则：
  * 零强依赖：仅用标准库 + 可选 PyYAML。无 PyYAML 时自动回退到极简解析器。
  * 纯函数化：所有解析函数不触网，便于单测；触网只在 fetch_subscription()。
  * 与 proxy_collector 现有 pipeline 的接缝：
        aggregator 解析产物 --> 通用 URI 列表 --> proxy_collector 的
        去重 --> TCP 预筛 --> mihomo HTTP 验证(http_test.py) --> output.py
    即：aggregator 只作为「一个新的输入源」，不替换你的验证/输出链路。

本文件可直接拷到 proxy_collector/ 根目录使用。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# 依赖：PyYAML 可选
# ---------------------------------------------------------------------------
try:
    import yaml  # type: ignore

    _HAS_YAML = True
except Exception:  # pragma: no cover - 运行环境可能未装
    yaml = None
    _HAS_YAML = False


# ===========================================================================
# 1. 网络抓取（唯一触网点）
# ===========================================================================
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def fetch_subscription(
    url: str,
    timeout: int = 20,
    headers: Optional[dict] = None,
    retries: int = 2,
) -> str:
    """抓取订阅 URL，返回原始文本（可能是 base64 / yaml / 纯链接）。

    与 aggregator 的 utils.http_get 等价，但纯标准库实现，不依赖 requests。
    """
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            # 有些订阅返回 gzip，标准库 urlopen 会自动解；这里直接解码
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
    raise RuntimeError(f"fetch_subscription failed: {url} -> {last_err}")


# ===========================================================================
# 2. 解码：base64 / 原文
# ===========================================================================
_LINK_RE = re.compile(r"^(ss|ssr|vmess|vless|trojan|snell|hysteria2|hysteria|socks5?|http)://", re.I)


def _try_b64_decode(text: str) -> Optional[str]:
    """尝试把文本当 base64 解码；失败返回 None。处理 padding / URL-safe。"""
    s = text.strip()
    if not s or "\n" in s and len(s) < 200:
        # 太短或含换行的明文一般不可能是 base64 订阅体
        pass
    # 去掉可能的空白/回车
    compact = re.sub(r"\s+", "", s)
    if not compact:
        return None
    # 补 padding
    pad = (-len(compact)) % 4
    cand = compact + ("=" * pad)
    for alphabet in (None, base64.urlsafe_b64decode):
        try:
            if alphabet is None:
                dec = base64.b64decode(cand, validate=True)
            else:
                dec = alphabet(cand)
            out = dec.decode("utf-8")
            if out and (":" in out or "\n" in out or "proxies" in out):
                return out
        except Exception:
            continue
    return None


def decode_body(text: str) -> str:
    """把订阅体解码为「可读文本」。优先 base64，否则原样返回。"""
    decoded = _try_b64_decode(text)
    return decoded if decoded is not None else text


# ===========================================================================
# 3. 解析 Clash YAML
# ===========================================================================
def parse_clash_yaml(text: str) -> list[dict]:
    """从 Clash 配置文本中提取 proxies: 列表。无 PyYAML 时回退极简解析。"""
    if _HAS_YAML and yaml is not None:
        try:
            doc = yaml.safe_load(text)
            if isinstance(doc, dict):
                prox = doc.get("proxies")
                if isinstance(prox, list):
                    return [p for p in prox if isinstance(p, dict)]
        except Exception:
            pass
    # 回退：正则抓 'proxies:' 之后、下一个顶层 key 之前的内容，逐块塞进字典
    return _fallback_yaml_proxies(text)


def _fallback_yaml_proxies(text: str) -> list[dict]:
    """极简 YAML proxies 解析（仅覆盖扁平字段 + name/server/port/type）。

    生产环境强烈建议装 PyYAML；这里是兜底，避免无依赖时直接崩。
    """
    m = re.search(r"^proxies:\s*$", text, re.M)
    if not m:
        return []
    body = text[m.end():]
    # 截到下一个「无缩进的顶层 key:」
    body = re.split(r"\n[a-zA-Z_][\w-]*:\s*$", body)[0]
    nodes: list[dict] = []
    cur: Optional[dict] = None
    indent_re = re.compile(r"^(\s+)-\s+(\w[\w-]*):\s*(.*)$")
    flat_re = re.compile(r"^(\s+)(\w[\w-]*):\s*(.*)$")
    for line in body.splitlines():
        if not line.strip():
            continue
        mi = indent_re.match(line)
        if mi and re.match(r"^\s+", line) and not line.strip().startswith("- "):
            pass
        if re.match(r"^\s+-\s", line):
            if cur:
                nodes.append(cur)
            cur = {}
            # 形如 "- name: xxx" 或 "- {json}" - 这里只处理 "- key: val"
            inner = line.strip()[2:]
            kv = re.match(r"(\w[\w-]*):\s*(.*)$", inner)
            if kv and cur is not None:
                cur[kv.group(1)] = _strip(kv.group(2))
            continue
        fm = flat_re.match(line)
        if fm and cur is not None:
            cur[fm.group(2)] = _strip(fm.group(3))
    if cur:
        nodes.append(cur)
    return nodes


def _strip(v: str):
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v


# ===========================================================================
# 4. 结构化节点 -> 通用 URI
# ===========================================================================
def _b64u(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _name_tag(name: str) -> str:
    return "#" + urllib.parse.quote(name or "node", safe="")


def node_to_uri(node: dict) -> Optional[str]:
    """把一个 Clash 节点 dict 转成通用代理 URI。不支持的协议返回 None。"""
    t = (node.get("type") or "").lower()
    name = node.get("name", "")
    server = node.get("server")
    port = node.get("port")
    if not server or not port:
        return None
    tag = _name_tag(name)

    if t == "ss":
        method = node.get("cipher") or node.get("method") or "aes-256-gcm"
        pwd = node.get("password", "")
        plugin = node.get("plugin")
        # SIP002: ss://base64(method:pass)@host:port#name
        userinfo = _b64u(f"{method}:{pwd}")
        uri = f"ss://{userinfo}@{server}:{port}{tag}"
        if plugin:
            uri += f"?plugin={urllib.parse.quote(plugin)}"
        return uri

    if t == "trojan":
        pwd = node.get("password", "")
        params = _kv_params(node, ["sni", "alpn", "allowInsecure", "peer"])
        return f"trojan://{urllib.parse.quote(pwd)}@{server}:{port}{params}{tag}"

    if t == "vless":
        uid = node.get("uuid", "")
        params = _kv_params(
            node, ["flow", "encryption", "sni", "alpn", "allowInsecure", "fp", "pbk", "sid"]
        )
        return f"vless://{uid}@{server}:{port}{params}{tag}"

    if t in ("hysteria2", "hysteria", "hy2"):
        pwd = node.get("password", "")
        params = _kv_params(node, ["sni", "alpn", "allowInsecure", "obfs", "obfs-password"])
        scheme = "hysteria2" if t != "hysteria" else "hysteria"
        return f"{scheme}://{urllib.parse.quote(pwd)}@{server}:{port}{params}{tag}"

    if t == "snell":
        pwd = node.get("password", "")
        params = _kv_params(node, ["obfs"])
        return f"snell://{urllib.parse.quote(pwd)}@{server}:{port}{params}{tag}"

    if t in ("socks5", "socks"):
        uid = node.get("username", "")
        pwd = node.get("password", "")
        auth = f"{urllib.parse.quote(uid)}:{urllib.parse.quote(pwd)}@" if uid else ""
        return f"socks5://{auth}{server}:{port}{tag}"

    if t in ("http", "https"):
        uid = node.get("username", "")
        pwd = node.get("password", "")
        auth = f"{urllib.parse.quote(uid)}:{urllib.parse.quote(pwd)}@" if uid else ""
        return f"http://{auth}{server}:{port}{tag}"

    if t == "ssr":
        return _ssr_uri(node, tag)

    if t == "vmess":
        return _vmess_uri(node, tag)

    return None


def _kv_params(node: dict, keys: Iterable[str]) -> str:
    out = []
    for k in keys:
        v = node.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, list):
            v = ",".join(map(str, v))
        out.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
    return ("?" + "&".join(out)) if out else ""


def _ssr_uri(node: dict, tag: str) -> str:
    """ssr://base64(host:port:protocol:method:obfs:base64pass/?params)"""
    server = node.get("server", "")
    port = node.get("port", "")
    proto = node.get("protocol", "origin")
    method = node.get("cipher") or node.get("method", "aes-256-cfb")
    obfs = node.get("obfs", "plain")
    pwd = _b64u(node.get("password", ""))
    remarks = _b64u(node.get("name", ""))
    params = node.get("protocol-param", "")
    obfs_param = node.get("obfs-param", "")
    query = ""
    if params:
        query += f"&protoparam={_b64u(params)}"
    if obfs_param:
        query += f"&obfsparam={_b64u(obfs_param)}"
    if remarks:
        query += f"&remarks={remarks}"
    head = f"{server}:{port}:{proto}:{method}:{obfs}:{pwd}/"
    body = _b64u(head + query.lstrip("&"))
    return f"ssr://{body}"


def _vmess_uri(node: dict, tag: str) -> str:
    """vmess://base64(json) — 标准 vmess 链接格式。"""
    vm = {
        "v": "2",
        "ps": node.get("name", ""),
        "add": node.get("server", ""),
        "port": str(node.get("port", "")),
        "id": node.get("uuid", ""),
        "aid": str(node.get("alterId", node.get("aid", "0"))),
        "scy": node.get("cipher") or node.get("security", "auto"),
        "net": node.get("network", "tcp"),
        "type": node.get("type", "none"),
        "host": node.get("servername") or node.get("host", ""),
        "path": node.get("ws-opts", {}).get("path") if isinstance(node.get("ws-opts"), dict) else (node.get("path", "")),
        "tls": "tls" if node.get("tls") else "",
        "sni": node.get("servername") or node.get("sni", ""),
    }
    return "vmess://" + _b64u(json.dumps(vm, ensure_ascii=False))


# ===========================================================================
# 5. 统一入口
# ===========================================================================
def parse_subscription(
    url: str,
    timeout: int = 20,
    headers: Optional[dict] = None,
    retries: int = 2,
) -> list[str]:
    """抓取并解析一个订阅 URL，返回去重后的通用代理 URI 列表。

    这是替换 aggregator collect.py 的「单订阅」入口，产物可直接 append 进
    proxy_collector 的全量节点池（交给现有去重/TCP/mihomo 链路）。
    """
    raw = fetch_subscription(url, timeout=timeout, headers=headers, retries=retries)
    decoded = decode_body(raw)
    uris: list[str] = []

    # 情况 A：解码后是 Clash YAML
    nodes = parse_clash_yaml(decoded)
    for n in nodes:
        uri = node_to_uri(n)
        if uri:
            uris.append(uri)

    # 情况 B：解码后是每行一个通用链接（订阅本身就是链接集合）
    if not uris:
        for line in decoded.splitlines():
            line = line.strip()
            if _LINK_RE.match(line):
                uris.append(line)

    # 去重保序
    seen = set()
    out = []
    for u in uris:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ===========================================================================
# 6. 机场域名配置解析（aggregator domains.txt 风格）
# ===========================================================================
@dataclass
class AirportDomain:
    address: str
    coupon: str = ""
    invite_code: str = ""
    api_prefix: str = ""
    extra: dict = field(default_factory=dict)


def parse_domains(content: str) -> list[AirportDomain]:
    """解析 aggregator 风格机场域名配置。

    每行: address@#@#coupon@#@#invite_code@#@#api_prefix
    也兼容简单每行一个 URL 的格式。
    """
    records: list[AirportDomain] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "@#@#" in line:
            parts = line.split("@#@#")
            rec = AirportDomain(
                address=parts[0],
                coupon=parts[1] if len(parts) > 1 else "",
                invite_code=parts[2] if len(parts) > 2 else "",
                api_prefix=parts[3] if len(parts) > 3 else "",
            )
        else:
            rec = AirportDomain(address=line)
        records.append(rec)
    return records


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        for u in parse_subscription(sys.argv[1]):
            print(u)
