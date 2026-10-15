"""生成 Clash YAML 配置文件"""

import logging
import os
import tempfile
from typing import List

import yaml

from atomic import dump_yaml_atomic
from parser import ProxyNode

logger = logging.getLogger(__name__)


class _QuotedStr(str):
    """标记需要 YAML 强制加单引号输出的字符串。
    用于 reality-opts.short-id 等容易被 YAML 解析为科学计数法的纯 hex 短码
    （如 3e58、1e5 会被当成 3e58 / 1e5 浮点数，导致 mihomo 报 invalid short ID）。"""


class _SafeDumper(yaml.SafeDumper):
    pass


def _represent_quoted_str(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


_SafeDumper.add_representer(_QuotedStr, _represent_quoted_str)


def _defuse_shortid(reality_opts: dict) -> dict:
    """复制 reality_opts 并将 short-id 包装为带引号字符串，避免被 YAML 解析为科学计数法。"""
    if not reality_opts:
        return reality_opts
    out = dict(reality_opts)
    sid = out.get("short-id")
    if isinstance(sid, str) and sid and not sid.startswith("'") and not sid.startswith('"'):
        out["short-id"] = _QuotedStr(sid)
    return out


def ensure_unique_proxy_names(proxies: list) -> list:
    """确保所有代理名称唯一（重名追加 " 2"/" 3"…），返回被改名的名称列表。

    **为什么必须在部署前做**：mihomo 遇到重名会**整份配置拒收**
    （`level=error msg="proxy ... is the duplicate name"`），Clash Verge 随即把变更整体
    回滚 —— 表现为"流水线 rc=0，但客户端其实没有更新"。2026-10-16 05:28 真实发生过。

    重名从哪来：同一个 server:port 上不同凭据的节点、显示名却完全相同（免费源常把一个
    显示名复用到多个账号）。它们的身份指纹不同，所以按指纹去重的 merge_previous_fast
    不会把它们合并 —— 必须单独按名称去重。
    同一个重名还会让 `_verify_nodes_real` 的临时配置直接被 mihomo 拒收，
    于是走到"未就绪，保守保留全部节点"的保底分支。
    """
    seen: set = set()
    renamed: list = []
    for proxy in proxies or []:
        name = proxy.get("name")
        if not name:
            continue
        if name in seen:
            idx = 2
            while f"{name} {idx}" in seen:
                idx += 1
            proxy["name"] = f"{name} {idx}"
            renamed.append(proxy["name"])
        seen.add(proxy["name"])
    return renamed


def defuse_reality_short_ids(config: dict) -> int:
    """把整份配置里所有 reality-opts.short-id 重新包成加引号字符串，返回处理节点数。

    yaml.safe_load 之后 _QuotedStr 标记会丢失，所以任何用 yaml.dump/safe_dump 重新写出的
    环节都必须调用本函数 —— 否则像 `3e58` 这样的 short-id 会被 YAML 解析成浮点数
    (3e+58)，mihomo 报 "invalid REALITY short ID"。实测部署文件
    output-filtered-fast.yaml 里的 short-id 就是未加引号的（因为 run_pipeline 的
    apply_stability / pre_deploy_verify / merge_previous_fast 三处重写用了 plain yaml.dump），
    而 http_test 写出的中间产物是加引号的。
    """
    n = 0
    for p in (config or {}).get("proxies", []) or []:
        if isinstance(p, dict) and p.get("reality-opts"):
            p["reality-opts"] = _defuse_shortid(p["reality-opts"])
            n += 1
    return n


# Clash Meta 支持的 SS 加密方法（白名单；任何不在集合内的 cipher 都会被丢弃节点）
_VALID_SS_CIPHERS = frozenset({
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "aes-128-ofb", "aes-192-ofb", "aes-256-ofb",
    "aes-128-cfb8", "aes-192-cfb8", "aes-256-cfb8",
    "aes-128-cfb1", "aes-192-cfb1", "aes-256-cfb1",
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20", "chacha20-ietf", "chacha20-ietf-poly1305",
    "xchacha20", "xchacha20-ietf-poly1305",
    "rc4-md5", "none",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
})

_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", __import__("re").I
)
_HEX_RE = __import__("re").compile(r"^[0-9a-fA-F]+$")

# mihomo 支持的 uTLS 指纹。源里常见的脏值（unsafe / chrom / HTML 抓取残留）会让 mihomo
# 打印 "wrong clientFingerprint" 并回落默认值 —— 实测 output.yaml 里有 38 个 unsafe，
# 另有 'fi…</div>'、'chrom' 这类垃圾值。这里统一归一化，不把脏值写进配置。
_VALID_CLIENT_FINGERPRINTS = frozenset({
    "chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq",
    "random", "randomized",
})
_DEFAULT_CLIENT_FINGERPRINT = "chrome"

# AUTO(url-test) 组的成员上限。url-test 是按**延迟**自动选，若把上万个节点全塞进去，
# 它会挑中"低延迟但拥塞/低速"的节点（speed_test.py 里有同样的设计与结论）。
# proxies 已按延迟排序，因此取前 N 个即可。
AUTO_TOP = 30


def _normalize_client_fingerprint(value: object) -> str:
    """把源里的指纹值归一化到 mihomo 认识的白名单，非法值回落默认。"""
    fp = str(value or "").strip().lower()
    return fp if fp in _VALID_CLIENT_FINGERPRINTS else _DEFAULT_CLIENT_FINGERPRINT


def _validate_proxy_for_clash(proxy: dict) -> tuple[bool, str]:
    """校验 Clash 代理字典的合法性。任何让手机 Clash 拒绝导入/握手的字段问题都返回 (False, reason)。

    覆盖：
    - 缺 server/port 或 port 越界
    - vmess 缺 uuid / alterId 异常
    - vless 缺 uuid；vless flow 拼写错误
    - trojan 缺 password
    - ss cipher 不在白名单、或 cipher 形如 UUID/明显是 password 被误填
    - ss password 缺失
    - vless reality short-id 不是合法 hex 或长度非法
    - vless reality 缺少 public-key（mihomo 报 reality-opts has unset fields: public-key）
    """
    if not proxy.get("server"):
        return False, "missing_server"
    port = proxy.get("port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return False, f"bad_port:{port!r}"

    t = proxy.get("type")
    if t == "vmess":
        if not proxy.get("uuid"):
            return False, "vmess_no_uuid"
        alter = proxy.get("alterId", 0)
        if not isinstance(alter, int) or alter < 0:
            return False, f"vmess_bad_alterId:{alter!r}"
    elif t == "vless":
        if not proxy.get("uuid"):
            return False, "vless_no_uuid"
        flow = proxy.get("flow", "")
        if flow and not flow.startswith("xtls-rprx-vision"):
            return False, f"vless_bad_flow:{flow!r}"
        cf = proxy.get("client-fingerprint")
        if cf is not None and cf not in _VALID_CLIENT_FINGERPRINTS:
            return False, f"vless_bad_client_fingerprint:{cf!r}"
        ro = proxy.get("reality-opts")
        if ro:
            sid = ro.get("short-id", "")
            if not (isinstance(sid, str) and sid and len(sid) % 2 == 0
                    and len(sid) <= 16 and _HEX_RE.match(sid)):
                return False, f"vless_bad_sid:{sid!r}"
            pk = ro.get("public-key", "")
            if not (isinstance(pk, str) and pk.strip()):
                return False, f"vless_reality_missing_public_key:{ro!r}"
    elif t == "trojan":
        if not proxy.get("password"):
            return False, "trojan_no_password"
    elif t == "ss":
        cipher = (proxy.get("cipher") or "").lower()
        if not cipher or cipher not in _VALID_SS_CIPHERS or _UUID_RE.match(cipher):
            return False, f"ss_bad_cipher:{proxy.get('cipher')!r}"
        if not proxy.get("password"):
            return False, "ss_no_password"
    else:
        # 未知类型也丢弃，避免下游解析失败
        return False, f"unknown_type:{t!r}"

    return True, ""


def _node_to_clash(node: ProxyNode) -> dict:
    """将 ProxyNode 转换为 Clash 代理字典"""
    proxy = {
        "name": node.name or f"{node.type}-{node.server}",
        "type": node.type,
        "server": node.server,
        "port": node.port,
        "udp": node.udp,
    }

    if node.type == "vmess":
        proxy.update({
            "uuid": node.uuid,
            "alterId": node.alterId,
            "cipher": node.cipher or "auto",
        })
    elif node.type == "vless":
        proxy.update({
            "uuid": node.uuid,
            "cipher": node.cipher or "none",
        })
        if node.flow:
            flow = node.flow
            # 修正源里常见的 xtls-rprx-vision 拼写错误（多一个 n）
            if flow.startswith("xtls-rprx-visionn"):
                flow = "xtls-rprx-vision"
            proxy["flow"] = flow
        if node.client_fingerprint:
            proxy["client-fingerprint"] = _normalize_client_fingerprint(node.client_fingerprint)
        if node.reality_opts:
            # REALITY 必须 public-key；parser 层已丢弃缺 pbk 的节点，这里做二次防御
            pk = node.reality_opts.get("public-key", "")
            if not (isinstance(pk, str) and pk.strip()):
                logger.warning("vless REALITY 缺少 public-key，丢弃: %s", node.name)
                return None
            # 校验 public-key：mihomo 使用 base64.RawURLEncoding.Decode（不接受 '=' 填充）
            # 合法的 X25519 公钥 = 32 字节 raw base64url 编码（通常 43 字符，无填充）
            # 必须能被 RawURLEncoding 成功解码为 32 字节，否则 mihomo 报 "invalid REALITY public key"
            import base64 as _b64
            try:
                decoded = _b64.urlsafe_b64decode(pk + "=" * (-len(pk) % 4))  # 临时补齐用于验证
                if len(decoded) != 32:
                    logger.warning("vless REALITY public-key 解码后非 32 字节(%d)，丢弃: %s", len(decoded), node.name)
                    return None
                # 验证通过：保留原始 pk（不补填充符，mihomo 用 RawURLEncoding）
            except Exception:
                logger.warning("vless REALITY public-key 非合法 base64url，丢弃: %s (pk=%s)", node.name, pk[:20])
                return None
            # 清理/补齐 short-id：必须是非空合法 hex 字符串（Clash 手机端不接受缺失）
            sid = node.reality_opts.get("short-id", "")
            if sid:
                try:
                    int(sid, 16)
                except ValueError:
                    sid = ""
            if not sid:
                sid = "3e58"
            node.reality_opts["short-id"] = sid
            proxy["reality-opts"] = _defuse_shortid(node.reality_opts)
    elif node.type == "trojan":
        proxy.update({
            "password": node.password,
            "sni": node.sni or node.server,
        })
        if node.skip_cert_verify:
            proxy["skip-cert-verify"] = True
    elif node.type == "ss":
        # cipher 名称映射：修正订阅源中不符合 Clash 规范的加密方法名
        _CIPHER_MAP = {
            "chacha20-poly1305": "chacha20-ietf-poly1305",
        }
        if node.cipher.lower() in _CIPHER_MAP:
            logger.warning("修正 cipher: %s -> %s (%s)", node.cipher, _CIPHER_MAP[node.cipher.lower()], node.name)
            node.cipher = _CIPHER_MAP[node.cipher.lower()]

        # 安全性检查: cipher 不能是 "ss"（非法值）
        if node.cipher.lower() == "ss" and node.password:
            import base64
            try:
                padded = node.password + "=" * (-len(node.password) % 4)
                decoded = base64.b64decode(padded).decode("utf-8")
                if ":" in decoded:
                    real_cipher, real_password = decoded.split(":", 1)
                    if real_cipher.lower() != "ss":
                        node.cipher = real_cipher
                        node.password = real_password
                        logger.warning("输出阶段修复了非法 cipher: %s", node.name)
            except Exception:
                pass
        proxy.update({
            "cipher": node.cipher,
            "password": node.password,
        })
        if hasattr(node, "plugin") and node.plugin:
            proxy["plugin"] = node.plugin
        if hasattr(node, "plugin_opts") and node.plugin_opts:
            proxy["plugin-opts"] = node.plugin_opts
    elif node.type == "ssr":
        proxy.update({
            "cipher": node.cipher,
            "password": node.password,
            "protocol": node.protocol,
            "protocol-param": node.protocol_param,
            "obfs": node.obfs,
            "obfs-param": node.obfs_param,
        })

    # TLS 和传输层（vmess/vless）
    if node.type in ("vmess", "vless"):
        if node.tls:
            proxy["tls"] = True
            if node.sni:
                proxy["sni"] = node.sni
        if node.skip_cert_verify:
            proxy["skip-cert-verify"] = True
        # 只有非 tcp 才输出 network 字段
        if node.network == "ws" and node.ws_opts:
            proxy["network"] = "ws"
            proxy["ws-opts"] = node.ws_opts
        elif node.network == "grpc" and node.grpc_opts:
            proxy["network"] = "grpc"
            proxy["grpc-opts"] = node.grpc_opts
        elif node.network == "h2" and node.h2_opts:
            proxy["network"] = "h2"
            proxy["h2-opts"] = node.h2_opts

    # trojan 传输层
    if node.type == "trojan":
        if node.network == "ws" and node.ws_opts:
            proxy["network"] = "ws"
            proxy["ws-opts"] = node.ws_opts
        elif node.network == "grpc" and node.grpc_opts:
            proxy["network"] = "grpc"
            proxy["grpc-opts"] = node.grpc_opts

    return proxy


def generate_clash_config(nodes: List[ProxyNode], output_path: str, *, filter_unsupported_flow: bool = False) -> int:
    """生成完整的 Clash 配置文件

    Args:
        nodes: 节点列表
        output_path: 输出 YAML 路径
        filter_unsupported_flow: 为 True 时移除带 xtls-rprx-vision 的 vless 节点
                                 （旧版/原版 Clash 不支持该 flow）
    Returns:
        实际写入的节点数
    """
    if filter_unsupported_flow:
        before = len(nodes)
        nodes = [n for n in nodes if not (n.type == "vless" and n.flow)]
        logger.info("兼容模式过滤: 移除 %d 个带 flow 的 vless 节点, 剩余 %d", before - len(nodes), len(nodes))

    proxies = [p for p in (_node_to_clash(n) for n in nodes) if p is not None]

    # 通用 Clash 兼容性校验：丢弃任何让手机 Clash 拒绝导入的节点（非法 cipher、缺密码、短码未加引号等）
    before_validate = len(proxies)
    valid_proxies: list[dict] = []
    for p in proxies:
        ok, reason = _validate_proxy_for_clash(p)
        if ok:
            valid_proxies.append(p)
        else:
            logger.warning("丢弃非法 Clash 节点 [%s] %s: %s", p.get("type"), p.get("name", ""), reason)
    if before_validate - len(valid_proxies):
        logger.info("兼容性校验: 移除 %d 个非法节点, 保留 %d", before_validate - len(valid_proxies), len(valid_proxies))
    proxies = valid_proxies

    # 修复重名：同名节点追加序号，确保最终名称唯一（否则 mihomo 整份拒收）
    renamed = ensure_unique_proxy_names(proxies)
    if renamed:
        logger.info("重名修正: %d 个节点被追加序号", len(renamed))

    proxy_names = [p["name"] for p in proxies]
    # AUTO 只放前 AUTO_TOP 个（proxies 已按延迟排序），避免 url-test 选中"低延迟但慢"的节点
    auto_members = proxy_names[:AUTO_TOP]

    config = {
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "PROXY",
                "type": "select",
                "proxies": ["AUTO"] + proxy_names,
            },
            {
                "name": "AUTO",
                "type": "url-test",
                "proxies": auto_members,
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 50,
                "timeout": 8000,
            },
        ],
        "rules": [
            "MATCH,PROXY",
        ],
    }

    dump_yaml_atomic(output_path, config, dumper=_SafeDumper)

    logger.info("已原子生成 Clash 配置: %s (%d 个节点, AUTO 组 %d 个)",
                output_path, len(proxies), len(auto_members))
    return len(proxies)
