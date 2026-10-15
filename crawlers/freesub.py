# -*- coding: utf-8 -*-
"""FreeSub 自动更新 crawler。

FreeSub (ovmvo/FreeSub) 的订阅是「完整 Clash/Mihomo 配置」(含 proxies / proxy-groups / rules)，
而 proxy_collector 的 parser 只认 URI 风格订阅 (vmess:// vless:// ...)，直接喂进去会解析为 0 节点。

本 crawler 做一层适配：
  1. 拉取 sub/permanent/mihomo.yaml —— 文件名固定、仓库每次更新在原地刷新内容，天然支持「自动更新」；
     （sub/latest/<userid>.yaml 会随每次生成轮换文件名，无法稳定自动更新，故不用）
  2. 提取 proxies 数组；
  3. 过滤无用的 type（http 型明文代理对翻墙没价值），保留 trojan/vless/vmess/ss/socks5/hysteria* /tuic；
  4. 把每个 proxy 反序列化成通用 URI，交回 collector 现有的去重 / TCP / mihomo 链路。

注意：当前 FreeSub 生成内容里 http 型占比极高（约 7 成），过滤后可用节点通常只有几十个，
作为「免费聚合备份源」足够，不要指望替代付费主源。
"""
from __future__ import annotations

__crawler__ = {"name": "freesub", "source": "free", "enabled": True}

import asyncio
import base64
import json
import os
import urllib.parse
import urllib.request

import yaml

# 复用项目自带的 fetcher：自带 GitHub 镜像回退（ghfast.top / gh.con.sh / mirror.ghproxy.com），
# 在国内不依赖 Clash 代理也能拉到订阅。
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys as _sys
if _PROJ_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJ_ROOT)
try:
    from fetcher import fetch_one as _fetch_one
    _HAVE_FETCHER = True
except Exception:
    _HAVE_FETCHER = False

# 自动更新入口（文件名固定，仓库原地刷新）
FREESUB_URL = "https://raw.githubusercontent.com/ovmvo/FreeSub/main/sub/permanent/mihomo.yaml"

# 翻墙场景下无用的协议（http 明文代理基本没价值）
DROP_TYPES = {"http"}

# proxy_collector 的 parser 仅支持 vmess/vless/trojan/ss/ssr（其余类型转成 URI 也会被丢弃）
# 这里只输出 parser 能消化的协议，避免产出一堆被静默丢弃的 URI。
PARSER_SUPPORTED = {"trojan", "vless", "vmess", "ss", "ssr"}


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _q(params: dict) -> str:
    """把 dict 拼成 URL query（过滤空值，值做 percent-encode）。"""
    kv = []
    for k, v in params.items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            v = "1" if v else "0"
        kv.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
    return "&".join(kv)


def _vmess(p: dict) -> str:
    obj = {
        "v": "2",
        "ps": p.get("name", ""),
        "add": p.get("server", ""),
        "port": p.get("port", ""),
        "id": p.get("uuid", ""),
        "aid": p.get("alterId", 0),
        "scy": p.get("cipher", "auto"),
        "net": p.get("network", "tcp"),
        "type": p.get("type", "none"),
        "host": "",
        "path": "",
        "tls": "",
        "sni": "",
    }
    net = p.get("network", "tcp")
    if net == "ws":
        obj["host"] = (p.get("ws-opts", {}).get("headers", {}) or {}).get("Host", "")
        obj["path"] = p.get("ws-opts", {}).get("path", "")
    elif net == "grpc":
        obj["path"] = p.get("grpc-opts", {}).get("grpc-service-name", "")
    if p.get("tls") or p.get("tls") is True:
        obj["tls"] = "tls"
        obj["sni"] = p.get("sni", "")
    elif p.get("servername"):
        obj["sni"] = p.get("servername", "")
    return "vmess://" + _b64(json.dumps(obj, ensure_ascii=False))


def _vless(p: dict) -> str:
    uuid = p.get("uuid", "")
    server = p.get("server", "")
    port = p.get("port", "")
    net = p.get("network", "tcp")
    fp = p.get("client-fingerprint", "")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    params = {}
    # 传输层
    if net == "ws":
        params["type"] = "ws"
        params["path"] = p.get("ws-opts", {}).get("path", "")
        params["host"] = (p.get("ws-opts", {}).get("headers", {}) or {}).get("Host", "")
    elif net == "grpc":
        params["type"] = "grpc"
        params["serviceName"] = p.get("grpc-opts", {}).get("grpc-service-name", "")
    else:
        params["type"] = net  # tcp / raw / h2 等
    # 安全层
    real = p.get("reality-opts") or {}
    if real:
        params["security"] = "reality"
        params["pbk"] = real.get("public-key", "")
        params["sid"] = real.get("short-id", "")
        params["spx"] = real.get("spiderX", "")
        params["sni"] = real.get("servername", p.get("servername", ""))
        if fp:
            params["fp"] = fp
    elif p.get("tls"):
        params["security"] = "tls"
        params["sni"] = p.get("sni", p.get("servername", ""))
        if fp:
            params["fp"] = fp
    else:
        params["security"] = "none"
    q = _q(params)
    return f"vless://{uuid}@{server}:{port}?{q}#{name}"


def _trojan(p: dict) -> str:
    pw = p.get("password", "")
    server = p.get("server", "")
    port = p.get("port", "")
    net = p.get("network", "tcp")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    params = {"security": "tls"}
    if p.get("sni"):
        params["sni"] = p.get("sni")
    if net == "ws":
        params["type"] = "ws"
        params["path"] = p.get("ws-opts", {}).get("path", "")
        params["host"] = (p.get("ws-opts", {}).get("headers", {}) or {}).get("Host", "")
    q = _q(params)
    return f"trojan://{urllib.parse.quote(pw, safe='')}@{server}:{port}?{q}#{name}"


def _ss(p: dict) -> str:
    method = p.get("cipher", "")
    pw = p.get("password", "")
    server = p.get("server", "")
    port = p.get("port", "")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    userinfo = _b64(f"{method}:{pw}")
    plugin = p.get("plugin", "")
    if plugin:
        # 简单透传 plugin 参数（如 obfs=...）
        return f"ss://{userinfo}@{server}:{port}?plugin={urllib.parse.quote(plugin, safe='')}#{name}"
    return f"ss://{userinfo}@{server}:{port}#{name}"


def _socks5(p: dict) -> str:
    server = p.get("server", "")
    port = p.get("port", "")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    user = p.get("username", "")
    pw = p.get("password", "")
    if user:
        auth = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}@"
    else:
        auth = ""
    return f"socks5://{auth}{server}:{port}#{name}"


def _hysteria2(p: dict) -> str:
    server = p.get("server", "")
    port = p.get("port", "")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    pw = p.get("password", p.get("auth-str", ""))
    params = {}
    if p.get("sni") or p.get("servername"):
        params["sni"] = p.get("sni", p.get("servername", ""))
    if p.get("insecure"):
        params["insecure"] = "1"
    q = _q(params)
    if pw:
        return f"hysteria2://{urllib.parse.quote(pw, safe='')}@{server}:{port}?{q}#{name}"
    return f"hysteria2://{server}:{port}?{q}#{name}"


def _hysteria(p: dict) -> str:
    server = p.get("server", "")
    port = p.get("port", "")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    params = {}
    if p.get("protocol"):
        params["protocol"] = p.get("protocol")
    if p.get("auth-str") or p.get("auth"):
        params["auth"] = p.get("auth-str", p.get("auth", ""))
    if p.get("peer"):
        params["peer"] = p.get("peer")
    q = _q(params)
    return f"hysteria://{server}:{port}?{q}#{name}"


def _tuic(p: dict) -> str:
    server = p.get("server", "")
    port = p.get("port", "")
    name = urllib.parse.quote(p.get("name", ""), safe="")
    uuid = p.get("uuid", "")
    pw = p.get("password", "")
    params = {}
    if p.get("sni") or p.get("servername"):
        params["sni"] = p.get("sni", p.get("servername", ""))
    if p.get("congestion-controller") or p.get("congestion-control"):
        params["congestion_control"] = p.get("congestion-controller", p.get("congestion-control", ""))
    q = _q(params)
    return f"tuic://{uuid}:{urllib.parse.quote(pw, safe='')}@{server}:{port}?{q}#{name}"


_DISPATCH = {
    "vmess": _vmess,
    "vless": _vless,
    "trojan": _trojan,
    "ss": _ss,
    "socks5": _socks5,
    "hysteria": _hysteria,
    "hysteria2": _hysteria2,
    "tuic": _tuic,
}


def _to_uri(p: dict) -> str | None:
    t = p.get("type", "")
    if t in DROP_TYPES:
        return None
    if t not in PARSER_SUPPORTED:
        return None
    fn = _DISPATCH.get(t)
    if fn is None:
        return None
    try:
        return fn(p)
    except Exception:
        return None


def _fetch_text(url: str) -> str:
    """优先用项目 fetcher（带 GitHub 镜像回退）；失败再裸 urllib 直连兜底。"""
    if _HAVE_FETCHER:
        try:
            async def _go():
                import aiohttp
                async with aiohttp.ClientSession() as s:
                    return await _fetch_one(s, url, timeout=30, retries=2)
            txt = asyncio.run(_go())
            if txt:
                return txt
        except Exception as e:
            print(f"[freesub] fetcher 失败, 回退直连: {e}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "proxy-collector/freesub"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[freesub] fetch failed: {e}")
        return ""


def crawl() -> list[str]:
    raw = _fetch_text(FREESUB_URL)
    if not raw:
        return []
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:
        print(f"[freesub] yaml parse failed: {e}")
        return []
    proxies = doc.get("proxies", []) if isinstance(doc, dict) else []
    out = []
    dropped = 0
    for p in proxies:
        if not isinstance(p, dict):
            continue
        uri = _to_uri(p)
        if uri:
            out.append(uri)
        else:
            dropped += 1
    print(f"[freesub] 提取 {len(proxies)} 节点 -> 转 URI {len(out)} 个 (过滤 {dropped} 个无效/垃圾)")
    return out


if __name__ == "__main__":
    for u in crawl():
        print(u)
