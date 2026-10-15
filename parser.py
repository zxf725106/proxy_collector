"""解析订阅内容，支持 vmess/vless/trojan/ss/ssr 协议"""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ProxyNode:
    """统一节点数据结构"""
    name: str = ""
    type: str = ""  # vmess, vless, trojan, ss, ssr
    server: str = ""
    port: int = 0
    # 通用可选字段
    uuid: str = ""
    alterId: int = 0
    cipher: str = ""
    password: str = ""
    udp: bool = True
    # TLS
    tls: bool = False
    sni: str = ""
    skip_cert_verify: bool = False
    # transport
    network: str = "ws"
    ws_opts: Dict = field(default_factory=dict)
    grpc_opts: Dict = field(default_factory=dict)
    h2_opts: Dict = field(default_factory=dict)
    # vless / reality
    flow: str = ""
    client_fingerprint: str = ""
    reality_opts: Dict = field(default_factory=dict)

    protocol: str = ""
    protocol_param: str = ""
    obfs: str = ""
    obfs_param: str = ""

    @property
    def dedup_key(self) -> str:
        """返回包含凭据和传输参数的稳定身份指纹，不暴露敏感值。"""
        return node_identity(self)


def _secret_digest(*values: object) -> str:
    payload = "|".join(str(v or "") for v in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _json_digest(value: object) -> str:
    try:
        payload = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 身份指纹版本。与旧数据不兼容时递增，便于一眼看出"需要重建学习状态"。
# v2 -> v3：统一 node_identity / proxy_identity（旧数据里 233,159 条来源身份只有
#           11 条能与部署产物对上，见 CHANGELOG_改进记录_20261015.md）。
IDENTITY_VERSION = "v3"

# 这些 cipher 值对 vmess/vless/trojan 等价于"未指定"：output.py 会把空值写成
# "auto"(vmess) / "none"(vless)，两侧不归一化就会算出两个指纹。
_NEUTRAL_CIPHERS = frozenset({"", "none", "auto", "null"})


def _norm_cipher(value: object, proxy_type: str) -> str:
    """按 output.py 的写法归一化 cipher，使两侧指纹一致。"""
    v = str(value if value is not None else "").strip()
    if proxy_type in ("vmess", "vless", "trojan") and v.lower() in _NEUTRAL_CIPHERS:
        return ""
    return v


def _norm_flow(value: object) -> str:
    """镜像 output.py 对 vless flow 的拼写修正，否则两侧指纹不一致。"""
    flow = str(value if value is not None else "")
    if flow.startswith("xtls-rprx-visionn"):
        flow = "xtls-rprx-vision"
    return flow


def _norm_short_id(value: object) -> str:
    """镜像 output.py 对 reality short-id 的清洗：非法 hex 清空，空值兜底 3e58。"""
    sid = str(value if value is not None else "")
    if sid:
        try:
            int(sid, 16)
        except ValueError:
            sid = ""
    return sid or "3e58"


def _transport_fields(fields: dict) -> dict:
    """只保留会真正写进 Clash 配置的传输层字段（顺序固定，便于哈希稳定）。"""
    return {
        "tls": bool(fields.get("tls")),
        "sni": fields.get("sni", "") or "",
        "network": fields.get("network", "") or "",
        "flow": _norm_flow(fields.get("flow", "")),
        "reality": fields.get("reality-opts") or {},
        "ws": fields.get("ws-opts") or {},
        "grpc": fields.get("grpc-opts") or {},
        "h2": fields.get("h2-opts") or {},
        "plugin": fields.get("plugin", "") or "",
        "plugin_opts": fields.get("plugin-opts") or {},
    }


def _identity_from_fields(fields: dict) -> str:
    proxy_type = str(fields.get("type", "")).lower()
    credential = _secret_digest(
        proxy_type,
        fields.get("uuid", ""),
        fields.get("password", ""),
        _norm_cipher(fields.get("cipher", ""), proxy_type),
        fields.get("protocol", ""),
        fields.get("obfs", ""),
    )
    transport = _json_digest(_transport_fields(fields))
    return (f"{IDENTITY_VERSION}:{proxy_type}:{fields.get('server', '')}:"
            f"{fields.get('port', '')}:{credential}:{transport}")


def canonical_proxy_fields(node: ProxyNode) -> dict:
    """把 ProxyNode 归一化成「与 output.py 实际写出的 Clash dict 等价」的字段集。

    这是身份统一的关键：指纹必须只依赖最终写进配置的东西。此前 node_identity 与
    proxy_identity 各写一套默认值（network 默认 tcp/ws、cipher 默认 空/auto/none、
    short-id 清洗时机不同），同一节点在「解析阶段」与「配置阶段」会算出不同指纹，
    导致来源质量统计整体失效。
    """
    proxy_type = str(node.type or "").lower()
    tls = bool(node.tls) or proxy_type == "trojan"

    # sni 的写入条件镜像 output.py：trojan 恒写 node.sni or server；
    # vmess/vless 仅在 tls 且有 sni 时写；ss/ssr 不写。
    if proxy_type == "trojan":
        sni = node.sni or node.server
    elif proxy_type in ("vmess", "vless"):
        sni = node.sni if (node.tls and node.sni) else ""
    else:
        sni = ""

    fields: dict = {
        "type": proxy_type,
        "server": node.server,
        "port": node.port,
        "uuid": node.uuid,
        "password": node.password,
        "cipher": node.cipher,
        "protocol": node.protocol,
        "obfs": node.obfs,
        "tls": tls,
        "sni": sni,
        "flow": node.flow,
        "network": "",
    }
    # 与 output.py 一致：只有 ws/grpc/h2 且 opts 非空才输出 network 字段
    if node.network == "ws" and node.ws_opts:
        fields["network"] = "ws"
        fields["ws-opts"] = node.ws_opts
    elif node.network == "grpc" and node.grpc_opts:
        fields["network"] = "grpc"
        fields["grpc-opts"] = node.grpc_opts
    elif node.network == "h2" and node.h2_opts:
        fields["network"] = "h2"
        fields["h2-opts"] = node.h2_opts
    if node.reality_opts:
        ro = dict(node.reality_opts)
        if ro.get("short-id"):
            ro["short-id"] = _norm_short_id(ro.get("short-id"))
        fields["reality-opts"] = ro
    plugin = getattr(node, "plugin", "")
    if plugin:
        fields["plugin"] = plugin
        fields["plugin-opts"] = getattr(node, "plugin_opts", {}) or {}
    return fields


def canonical_fields_from_clash(proxy: dict) -> dict:
    """从 Clash proxy dict 提取同一套字段，供 proxy_identity 使用。"""
    proxy_type = str(proxy.get("type", "")).lower()
    sni = proxy.get("sni", "")
    if proxy_type == "trojan":
        sni = sni or proxy.get("server", "")
    fields: dict = {
        "type": proxy_type,
        "server": proxy.get("server", ""),
        "port": proxy.get("port", ""),
        "uuid": proxy.get("uuid", ""),
        "password": proxy.get("password", ""),
        "cipher": proxy.get("cipher", ""),
        "protocol": proxy.get("protocol", ""),
        "obfs": proxy.get("obfs", ""),
        "tls": bool(proxy.get("tls", False)) or proxy_type == "trojan",
        "sni": sni,
        "flow": proxy.get("flow", ""),
        "network": proxy.get("network", "") or "",
    }
    for key in ("ws-opts", "grpc-opts", "h2-opts", "reality-opts", "plugin", "plugin-opts"):
        if proxy.get(key):
            fields[key] = proxy[key]
    ro = fields.get("reality-opts")
    if isinstance(ro, dict) and ro.get("short-id"):
        ro = dict(ro)
        ro["short-id"] = _norm_short_id(ro.get("short-id"))
        fields["reality-opts"] = ro
    return fields


def node_identity(node: ProxyNode) -> str:
    """生成跨来源、跨验证阶段可复用的节点身份。

    server/port 不是充分的身份：同一端口可能对应不同 UUID、密码或 Reality 参数。
    这里仅保存摘要，不把凭据写入日志或质量状态文件。
    """
    return _identity_from_fields(canonical_proxy_fields(node))


def proxy_identity(proxy: dict) -> str:
    """对 Clash proxy 字典生成身份指纹（与 node_identity 同源同构）。"""
    return _identity_from_fields(canonical_fields_from_clash(proxy))


def parse_content(raw: str) -> List[ProxyNode]:
    """自动识别格式并解析"""
    raw = raw.strip()
    if not raw:
        return []

    # 尝试 Clash YAML 格式
    # 兼容两类：① 以 proxies: 开头的纯节点段；② 完整 Clash 配置（port:/mixed-port: 等在前，proxies 不在首行）
    if raw.startswith("proxies:") or re.search(r"^proxies:\s*$", raw, re.MULTILINE):
        return _parse_clash_yaml(raw)

    # 按行解析 URI 列表
    nodes = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        node = _parse_uri(line)
        if node:
            nodes.append(node)
    return nodes


def _to_int(value: object, default: int = 0) -> int:
    """宽松转 int：兼容 '443?' / '443 ' / '443.0' 等脏值（部分源端口带杂字符）"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        m = re.search(r"\d+", str(value or ""))
        return int(m.group()) if m else default


def _parse_clash_yaml(text: str) -> List[ProxyNode]:
    """解析 Clash YAML 格式的订阅"""
    proxies = None
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            proxies = data.get("proxies") or []
    except Exception as e:
        logger.warning("Clash YAML 整体解析失败(%s)，启用逐行容错解析", e)

    # 整体 YAML 非法时逐行抢救：部分源单行 name 含未转义引号会让整份 YAML 报废，
    # 但它们只占极少数行，逐行解析可跳过破损行、保留其余绝大部分节点
    if proxies is None:
        proxies = _parse_clash_flow_lines(text)
        if proxies:
            logger.info("逐行容错解析恢复 %d 个节点", len(proxies))

    if not proxies:
        return []

    nodes = []
    for p in proxies:
        try:
            cipher = p.get("cipher", "")
            password = p.get("password", "")

            # 修复: 某些订阅源 password 格式为 //base64(cipher:real_password)
            # 需要从中解码真正的 cipher 和 password
            if p.get("type", "").lower() == "ss" and password.startswith("//"):
                try:
                    encoded = password[2:]
                    padded = encoded + "=" * (-len(encoded) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8")
                    if ":" in decoded:
                        cipher, password = decoded.split(":", 1)
                except Exception:
                    pass  # 解码失败则保持原值

            # 修复: cipher 为 "ss" 是非法值，尝试从 password 中恢复
            # 某些订阅源把真实 cipher:password 通过 base64 编码塞进了 password 字段
            if cipher.lower() == "ss" and password:
                try:
                    padded = password + "=" * (-len(password) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8")
                    if ":" in decoded:
                        real_cipher, real_password = decoded.split(":", 1)
                        if real_cipher.lower() != "ss":
                            cipher, password = real_cipher, real_password
                            logger.info("从 password 字段恢复了 cipher: %s", cipher)
                except Exception:
                    pass

            node = ProxyNode(
                name=p.get("name", ""),
                type=p.get("type", "").lower(),
                server=p.get("server", ""),
                port=_to_int(p.get("port", 0)),
                uuid=p.get("uuid", ""),
                alterId=_to_int(p.get("alterId", 0)),
                cipher=cipher,
                password=password,
                udp=p.get("udp", True),
                tls=p.get("tls", False),
                sni=p.get("sni", ""),
                skip_cert_verify=p.get("skip-cert-verify", False),
                network=p.get("network", "ws"),
                ws_opts=p.get("ws-opts", {}),
                grpc_opts=p.get("grpc-opts", {}),
                h2_opts=p.get("h2-opts", {}),
            )
            # vless 专有字段：Clash dict 里是 flow / client-fingerprint / reality-opts。
            # 原实现漏读这三个字段 —— 「Clash 格式订阅」里的 REALITY 节点被重新写出时会
            # 丢掉 public-key（节点彻底不可用），flow 也被抹平；同时使同一节点在
            # 「解析阶段」与「配置阶段」的身份指纹不一致（见 CHANGELOG_改进记录_20261015.md）。
            if node.type in ("vless", "vmess"):
                if p.get("flow"):
                    node.flow = str(p.get("flow"))
                if p.get("client-fingerprint"):
                    node.client_fingerprint = str(p.get("client-fingerprint"))
                reality = p.get("reality-opts")
                if isinstance(reality, dict) and reality:
                    node.reality_opts = dict(reality)
            # trojan 专用字段
            if node.type == "trojan":
                node.sni = p.get("sni", "") or node.sni
            # ssr 专用字段
            if node.type == "ssr":
                node.protocol = p.get("protocol", "")
                node.protocol_param = p.get("protocol-param", "")
                node.obfs = p.get("obfs", "")
                node.obfs_param = p.get("obfs-param", "")
            if node.server and node.port:
                nodes.append(node)
        except Exception as e:
            logger.warning("解析 Clash 节点失败: %s", e)
    return nodes


def _parse_clash_flow_lines(text: str) -> List[dict]:
    """逐行抢救 flow mapping 形式的 Clash 节点行（- {k: v, ...}）。

    用于整份 YAML 因个别非法行（如 name 内含未转义引号）无法解析的场景。
    破损行直接跳过，其余节点正常保留；块格式（多行缩进的 - name:）不做抢救。
    """
    items: List[dict] = []
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- {"):
            continue
        try:
            obj = yaml.safe_load(line[2:].strip())
        except Exception:
            skipped += 1
            continue
        if isinstance(obj, dict):
            items.append(obj)
    if skipped:
        logger.warning("逐行容错解析跳过 %d 个破损节点行", skipped)
    return items


def _parse_uri(uri: str) -> Optional[ProxyNode]:
    """解析单个协议 URI"""
    # 部分源从 HTML 页面抓取，参数分隔符 & 被转义成 &amp;，会让 pbk/sid 等参数名变成 "amp;pbk"
    # 导致 REALITY 节点被误判为「缺少 public-key」而丢弃
    uri = uri.replace("&amp;", "&").replace("&#38;", "&")
    if uri.startswith("vmess://"):
        return _parse_vmess(uri)
    elif uri.startswith("vless://"):
        return _parse_vless(uri)
    elif uri.startswith("trojan://"):
        return _parse_trojan(uri)
    elif uri.startswith("ss://"):
        return _parse_ss(uri)
    elif uri.startswith("ssr://"):
        return _parse_ssr(uri)
    else:
        logger.debug("未知协议: %s", uri[:20])
        return None


def _parse_vmess(uri: str) -> Optional[ProxyNode]:
    """解析 vmess://链接"""
    try:
        encoded = uri[len("vmess://"):]
        padded = encoded + "=" * (-len(encoded) % 4)
        data = json.loads(base64.b64decode(padded))
        node = ProxyNode(
            name=data.get("ps", ""),
            type="vmess",
            server=data.get("add", ""),
            port=_to_int(data.get("port", 0)),
            uuid=data.get("id", ""),
            alterId=_to_int(data.get("aid", 0)),
            cipher=data.get("scy", "auto"),
            network=data.get("net", "ws"),
            tls=data.get("tls", "") == "tls",
        )
        # 传输层选项
        if node.network == "ws":
            path = data.get("path", "/")
            host = data.get("host", "")
            node.ws_opts = {"path": path}
            if host:
                node.ws_opts["headers"] = {"Host": host}
        elif node.network == "grpc":
            node.grpc_opts = {"grpc-service-name": data.get("path", "")}
        elif node.network == "h2":
            node.h2_opts = {"path": data.get("path", "/")}
            hosts = data.get("host", "")
            if hosts:
                node.h2_opts["host"] = [h.strip() for h in hosts.split(",")]
        # SNI
        sni = data.get("sni", "")
        if sni:
            node.sni = sni
        return node if node.server and node.port else None
    except Exception as e:
        logger.warning("vmess 解析失败: %s", e)
        return None


def _parse_vless(uri: str) -> Optional[ProxyNode]:
    """解析 vless://链接"""
    try:
        # vless://uuid@host:port?params#name
        parsed = urlparse(uri)
        name = unquote(parsed.fragment) if parsed.fragment else ""
        uuid = parsed.username or ""
        server = parsed.hostname or ""
        port = parsed.port or 0
        params = parse_qs(parsed.query)

        node = ProxyNode(
            name=name,
            type="vless",
            server=server,
            port=port,
            uuid=uuid,
            cipher=params.get("encryption", ["none"])[0],
            network=params.get("type", ["ws"])[0],
            tls=params.get("security", [""])[0] in ("tls", "reality"),
            udp=True,
        )
        # flow
        if "flow" in params:
            flow = params["flow"][0]
            # 归一化常见乱码/拼写错误
            flow_norm = flow.replace("xtxts", "xtls").replace("visio", "vision")
            # 仅保留已知合法的 xtls-rprx-vision 变体
            if "xtls-rprx-vision" in flow_norm:
                node.flow = flow_norm
            else:
                logger.warning("忽略未知 vless flow 值: %s (%s)", flow, node.name)

        if "sni" in params:
            node.sni = params["sni"][0]
        if "fp" in params:
            node.client_fingerprint = params["fp"][0]
        # 传输层
        if node.network == "ws":
            node.ws_opts = {
                "path": params.get("path", ["/"])[0],
            }
            if "host" in params:
                node.ws_opts["headers"] = {"Host": params["host"][0]}
        elif node.network == "grpc":
            node.grpc_opts = {"grpc-service-name": params.get("serviceName", [""])[0]}
        elif node.network == "h2":
            node.h2_opts = {"path": params.get("path", ["/"])[0]}
            if "host" in params:
                node.h2_opts["host"] = [params["host"][0]]
        # reality
        if params.get("security", [""])[0] == "reality":
            node.reality_opts = {}
            if "pbk" not in params:
                # REALITY 必须 public-key，缺少则无法握手，直接丢弃
                logger.warning("vless REALITY 缺少 public-key (pbk)，丢弃: %s", uri[:120])
                return None
            node.reality_opts["public-key"] = params["pbk"][0]
            node.reality_opts["short-id"] = params["sid"][0] if "sid" in params else "3e58"

        return node if node.server and node.port else None
    except Exception as e:
        logger.warning("vless 解析失败: %s", e)
        return None


def _parse_trojan(uri: str) -> Optional[ProxyNode]:
    """解析 trojan://链接"""
    try:
        # trojan://password@host:port?params#name
        parsed = urlparse(uri)
        name = unquote(parsed.fragment) if parsed.fragment else ""
        password = unquote(parsed.password or "") or (parsed.username or "")
        server = parsed.hostname or ""
        port = parsed.port or 443
        params = parse_qs(parsed.query)

        node = ProxyNode(
            name=name,
            type="trojan",
            server=server,
            port=port,
            password=password,
            udp=True,
            sni=params.get("sni", [server])[0],
            skip_cert_verify=params.get("allowInsecure", ["0"])[0] == "1",
        )
        network = params.get("type", ["ws"])[0]
        node.network = network
        node.tls = True  # trojan 默认 TLS
        if network == "ws":
            node.ws_opts = {"path": params.get("path", ["/"])[0]}
            if "host" in params:
                node.ws_opts["headers"] = {"Host": params["host"][0]}
        elif network == "grpc":
            node.grpc_opts = {"grpc-service-name": params.get("serviceName", [""])[0]}

        return node if node.server and node.port else None
    except Exception as e:
        logger.warning("trojan 解析失败: %s", e)
        return None


def _parse_ss(uri: str) -> Optional[ProxyNode]:
    """解析 ss://链接"""
    try:
        # 格式1: ss://base64(method:password)@host:port#name
        # 格式2: ss://base64(method:password@host:port)#name (SIP002)
        body = uri[len("ss://"):]
        name = ""
        if "#" in body:
            body, name = body.rsplit("#", 1)
            name = unquote(name)

        # SIP002 格式
        if "@" in body:
            userinfo_host = body
            # 尝试 base64 解码 userinfo
            if ":" not in userinfo_host.split("@")[0]:
                encoded_userinfo = userinfo_host.split("@")[0]
                padded = encoded_userinfo + "=" * (-len(encoded_userinfo) % 4)
                userinfo = base64.b64decode(padded).decode("utf-8")
                host_port = userinfo_host.split("@", 1)[1]
            else:
                userinfo, host_port = userinfo_host.rsplit("@", 1)
                if "%" in userinfo or "+" in userinfo:
                    # URL 编码的 userinfo
                    padded = unquote(userinfo) + "=" * (-len(unquote(userinfo)) % 4)
                    try:
                        userinfo = base64.b64decode(padded).decode("utf-8")
                    except Exception:
                        pass

            # 去掉 SIP002 中可能携带的 ?plugin=... 查询串，避免端口解析失败
            host_port = host_port.split("?")[0]
            method, password = userinfo.split(":", 1)
            if ":" in host_port:
                server, port_str = host_port.rsplit(":", 1)
            else:
                # 可能是 IPv6
                match = re.match(r"\[([^\]]+)\]:(\d+)", host_port)
                if match:
                    server, port_str = match.group(1), match.group(2)
                else:
                    server, port_str = host_port, "0"

            # 解析插件参数
            params = parse_qs(f"plugin={unquote(uri.split('?')[1])}" if "?" in uri else "")
            plugin = params.get("plugin", [""])[0]
            plugin_opts = {}
            if plugin:
                for opt in params.get("plugin-opts", []):
                    if "=" in opt:
                        k, v = opt.split("=", 1)
                        plugin_opts[k] = v

        else:
            # 旧格式: base64(method:password@host:port)
            padded = body + "=" * (-len(body) % 4)
            decoded = base64.b64decode(padded).decode("utf-8")
            method, rest = decoded.split(":", 1)
            password, host_port = rest.rsplit("@", 1)
            server, port_str = host_port.rsplit(":", 1)
            plugin = ""
            plugin_opts = {}

        node = ProxyNode(
            name=name or server,
            type="ss",
            server=server,
            port=_to_int(port_str),
            cipher=method,
            password=password,
            udp=True,
        )
        if plugin:
            node.plugin = plugin
            node.plugin_opts = plugin_opts

        return node if node.server and node.port else None
    except Exception as e:
        logger.warning("ss 解析失败: %s", e)
        return None


def _parse_ssr(uri: str) -> Optional[ProxyNode]:
    """解析 ssr://链接"""
    try:
        encoded = uri[len("ssr://"):]
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        # server:port:protocol:method:obfs:base64password/?params
        main_parts, _, params_str = decoded.partition("/?")
        parts = main_parts.split(":")
        if len(parts) < 6:
            logger.warning("ssr 格式不完整: %s", decoded[:50])
            return None

        server = parts[0]
        port = _to_int(parts[1])
        protocol = parts[2]
        method = parts[3]
        obfs = parts[4]
        password_b64 = parts[5]
        padded_pw = password_b64 + "=" * (-len(password_b64) % 4)
        try:
            password = base64.b64decode(padded_pw).decode("utf-8")
        except Exception:
            password = password_b64

        # 解析参数
        params = parse_qs(params_str)
        name = ""
        if "remarks" in params:
            try:
                name_b64 = params["remarks"][0]
                padded_name = name_b64 + "=" * (-len(name_b64) % 4)
                name = base64.b64decode(padded_name).decode("utf-8")
            except Exception:
                name = params["remarks"][0]

        node = ProxyNode(
            name=name or server,
            type="ssr",
            server=server,
            port=port,
            cipher=method,
            password=password,
            protocol=protocol,
            obfs=obfs,
            udp=True,
        )
        if "protoparam" in params:
            try:
                pp = params["protoparam"][0]
                node.protocol_param = base64.b64decode(pp + "=" * (-len(pp) % 4)).decode("utf-8")
            except Exception:
                node.protocol_param = params["protoparam"][0]
        if "obfsparam" in params:
            try:
                op = params["obfsparam"][0]
                node.obfs_param = base64.b64decode(op + "=" * (-len(op) % 4)).decode("utf-8")
            except Exception:
                node.obfs_param = params["obfsparam"][0]

        return node if node.server and node.port else None
    except Exception as e:
        logger.warning("ssr 解析失败: %s", e)
        return None
