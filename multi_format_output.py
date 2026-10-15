# -*- coding: utf-8 -*-
"""
multi_format_output.py — 多格式订阅输出（subconverter 封装）
================================================================================

移植自 aggregator 的 subscribe/subconverter.py 思路：
  原始节点池（通用 URI 列表） -> 经 subconverter -> 多目标格式。

支持目标：
  * clash      -> clash.yaml（你现在的 output.yaml）
  * singbox    -> singbox.json
  * v2ray      -> v2ray.txt (base64 / vmess 集合)
  * ss / ssr / trojan / vless -> 按 subconverter 能力

与 proxy_collector 的接缝：
  * 你现有 output.py 只出 Clash；本模块作为「附加输出层」。
  * 若本机没装 subconverter，自动降级：clash 直接由通用 URI 写 yaml，
    其余格式给出「需 subconverter」提示，不阻断主流程。
  * 保留你双端输出习惯：desktop / mobile 由调用方分别传 node 列表即可。

用法：
  from multi_format_output import build_config, convert
  nodes = [...]  # 通用 URI 列表（已验证可用的）
  convert(nodes, targets=["clash", "singbox"], outdir=".", tag="desktop")
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# 1. 通用 URI -> Clash proxies（无依赖的极简版，覆盖主流协议）
#    若你已有 output.py 的 clash 生成逻辑，可删掉本段直接用你的。
# ---------------------------------------------------------------------------
def _uri_to_clash_node(uri: str) -> Optional[dict]:
    """极简：把 ss:// trojan:// vless:// vmess:// 等转回 clash dict。
    注意：base64 类(ss/vmess/ssr)解析较脆弱，生产建议交给 subconverter。
    """
    if uri.startswith("ss://"):
        return _parse_ss(uri)
    if uri.startswith("trojan://"):
        return _parse_trojan(uri)
    if uri.startswith("vless://"):
        return _parse_vless(uri)
    if uri.startswith("vmess://"):
        return _parse_vmess(uri)
    if uri.startswith("socks5://") or uri.startswith("http://"):
        return _parse_socks(uri)
    # hysteria2 / snell / ssr 等交给 subconverter 更稳，这里返回 None
    return None


def _split_uri(uri: str):
    scheme, rest = uri.split("://", 1)
    rest, _, frag = rest.partition("#")
    rest, _, query = rest.partition("?")
    userinfo_host, _, port = rest.rpartition(":")
    if "@" in userinfo_host:
        userinfo, _, host = userinfo_host.rpartition("@")
    else:
        userinfo, host = "", userinfo_host
    return scheme, userinfo, host, port, query, frag


def _parse_ss(uri: str):
    scheme, ui, host, port, q, frag = _split_uri(uri)
    name = _url_unquote(frag)
    # SIP002: userinfo 是 base64(method:pass)
    try:
        method_pass = base64.urlsafe_b64decode(ui + "=" * (-len(ui) % 4)).decode()
        method, _, pwd = method_pass.partition(":")
    except Exception:
        method, pwd = "aes-256-gcm", ui
    node = {"name": name, "type": "ss", "server": host, "port": int(port),
            "cipher": method, "password": pwd}
    return node


def _parse_trojan(uri: str):
    scheme, ui, host, port, q, frag = _split_uri(uri)
    return {"name": _url_unquote(frag), "type": "trojan", "server": host,
            "port": int(port), "password": ui, **_parse_qs(q)}


def _parse_vless(uri: str):
    scheme, ui, host, port, q, frag = _split_uri(uri)
    return {"name": _url_unquote(frag), "type": "vless", "server": host,
            "port": int(port), "uuid": ui, **_parse_qs(q)}


def _parse_socks(uri: str):
    scheme, ui, host, port, q, frag = _split_uri(uri)
    t = "socks5" if scheme == "socks5" else "http"
    user = pwd = ""
    if "@" in ui:
        user, _, pwd = ui.partition(":")
    return {"name": _url_unquote(frag), "type": t, "server": host,
            "port": int(port), "username": user, "password": pwd}


def _parse_vmess(uri: str):
    b64 = uri[len("vmess://"):]
    try:
        vm = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode())
    except Exception:
        return None
    return {"name": vm.get("ps", ""), "type": "vmess", "server": vm.get("add", ""),
            "port": int(vm.get("port", 0) or 0), "uuid": vm.get("id", ""),
            "alterId": int(vm.get("aid", 0) or 0), "cipher": vm.get("scy", "auto"),
            "network": vm.get("net", "tcp"), "tls": vm.get("tls", ""),
            "servername": vm.get("sni", ""), "path": vm.get("path", "")}


def _parse_qs(q: str) -> dict:
    out = {}
    if not q:
        return out
    for kv in q.split("&"):
        k, _, v = kv.partition("=")
        out[k] = _url_unquote(v)
    return out


def _url_unquote(s: str) -> str:
    import urllib.parse
    return urllib.parse.unquote(s)


# ---------------------------------------------------------------------------
# 2. subconverter 封装
# ---------------------------------------------------------------------------
def find_subconverter() -> Optional[str]:
    return shutil.which("subconverter") or shutil.which("subconverter.exe")


def build_clash_yaml(nodes: Iterable[str], tag: str = "pool") -> str:
    """无依赖：通用 URI -> clash yaml 文本（仅覆盖能解析的协议）。"""
    prox = []
    for u in nodes:
        n = _uri_to_clash_node(u)
        if n:
            prox.append(n)
    doc = {"port": 7890, "socks-port": 7891, "allow-lan": False,
           "mode": "rule", "proxies": prox}
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    except Exception:
        return json.dumps(doc, ensure_ascii=False, indent=2)


def convert(
    nodes: Iterable[str],
    targets: Iterable[str] = ("clash",),
    outdir: str = ".",
    tag: str = "desktop",
    subconverter_bin: Optional[str] = None,
) -> dict[str, str]:
    """把通用 URI 节点池转成多格式并落盘，返回 {target: filepath}。

    优先走 subconverter（格式最全）；clash 无 subconverter 也能本地生成。
    """
    os.makedirs(outdir, exist_ok=True)
    nodes = list(nodes)
    result: dict[str, str] = {}
    targets = list(targets)

    sc = subconverter_bin or find_subconverter()

    if "clash" in targets or sc is None:
        # 本地生成 clash（始终可用）
        yml = build_clash_yaml(nodes, tag)
        clash_path = os.path.join(outdir, f"{tag}-clash.yaml")
        with open(clash_path, "w", encoding="utf-8") as f:
            f.write(yml)
        result["clash"] = clash_path

    if sc is not None:
        # 有 subconverter：写一份中间 clash 给 subconverter 读，再转其它格式
        base = os.path.join(outdir, f"{tag}-clash.yaml")
        if "clash" not in result:
            with open(base, "w", encoding="utf-8") as f:
                f.write(build_clash_yaml(nodes, tag))
        for t in targets:
            if t == "clash":
                continue
            out_path = os.path.join(outdir, f"{tag}-{t}.{_ext(t)}")
            cmd = [sc, "-f", base, "-t", t, "-o", out_path]
            try:
                subprocess.run(cmd, check=True, timeout=120,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result[t] = out_path
            except Exception as e:
                print(f"[multi_format_output] subconverter {t} failed: {e}")
    else:
        skipped = [t for t in targets if t != "clash"]
        if skipped:
            print(f"[multi_format_output] subconverter 未安装，跳过格式: {skipped}"
                  f"（clash 已生成；装 subconverter 后可出 {skipped}）")
    return result


def _ext(target: str) -> str:
    return {
        "singbox": "json",
        "v2ray": "txt",
        "ss": "txt",
        "ssr": "txt",
        "trojan": "txt",
        "vless": "txt",
    }.get(target, "txt")


if __name__ == "__main__":
    import sys

    sample = [
        "ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:8388#test-ss",
        "trojan://secret@5.6.7.8:443?sni=example.com#test-trojan",
    ]
    print(convert(sample, targets=["clash"], outdir=".", tag="demo"))
