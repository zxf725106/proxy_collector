#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心回归测试 —— 锁住 2026-10-15 那批修复。

跑法（不需要 pytest）:
    py -3 test_core_regressions.py

也兼容 pytest（每个 test_* 都是普通断言函数）。

覆盖：
1. 身份指纹统一（P0-2）：URI -> Clash dict -> 重新解析，三段指纹必须一致
2. Clash 格式来源的 REALITY 节点不再丢 public-key / flow / client-fingerprint
3. 数字型 short-id（如 3e58）在流水线重写后仍是字符串，不会被 YAML 吃成浮点数
4. AUTO(url-test) 组收敛到 Top30（P0-3）
5. http_test 裁剪组引用时保留 AUTO/DIRECT（不再误删）
6. client-fingerprint 白名单（脏值不再进配置）
7. 来源质量：极低通过率会被停用，但停用满 N 轮后能恢复（不再是单向棘轮）
"""
import base64
import json
import os
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import yaml  # noqa: E402

import output  # noqa: E402
import run_pipeline  # noqa: E402
from atomic import dump_yaml_atomic  # noqa: E402
from http_test import filter_output_by_http_test, load_tcp_alive  # noqa: E402
from parser import node_identity, parse_content, proxy_identity  # noqa: E402
from source_quality import REENABLE_AFTER_RUNS, SourceQualityTracker  # noqa: E402

PBK = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
UUID1 = "11111111-2222-3333-4444-555555555555"


def _vmess(**kw):
    base = {"ps": "vm", "add": "1.2.3.4", "port": "443", "id": UUID1, "aid": "0",
            "scy": "auto", "net": "ws", "type": "none", "host": "a.com", "path": "/p",
            "tls": "tls", "sni": "a.com"}
    base.update(kw)
    return "vmess://" + base64.b64encode(json.dumps(base).encode()).decode()


SAMPLE_URIS = [
    _vmess(),
    _vmess(net="tcp", tls="", host="", path="", sni=""),
    f"vless://{UUID1}@1.2.3.4:443?encryption=none&security=reality&sni=x.com&fp=chrome&pbk={PBK}&sid=3e58&type=tcp&flow=xtls-rprx-vision",
    f"vless://{UUID1}@1.2.3.4:443?encryption=none&security=reality&sni=x.com&pbk={PBK}&sid=zzz&type=tcp",
    f"vless://{UUID1}@1.2.3.4:443?encryption=none&security=tls&sni=y.com&type=tcp&flow=xtls-rprx-visionn",
    f"vless://{UUID1}@9.9.9.9:80?type=ws&path=%2Fw&host=h.com",
    f"vless://{UUID1}@5.6.7.8:8443?type=grpc&serviceName=gs&security=tls&sni=g.com",
    "trojan://pw123@t.com:443?sni=t.com&type=ws&path=%2Ft",
    "trojan://pw123@t2.com:8443",
    "ss://" + base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=") + "@1.1.1.1:8388#n1",
    "ss://" + base64.b64encode(b"chacha20-ietf-poly1305:pw@2.2.2.2:8389").decode().rstrip("=") + "#n2",
]


def _tmp(name):
    return os.path.join(tempfile.gettempdir(), "pcoll_test_" + name)


# ---------------------------------------------------------------- 1. 身份指纹统一
def test_identity_survives_uri_dict_reparse():
    for uri in SAMPLE_URIS:
        nodes = parse_content(uri)
        assert nodes, f"解析失败: {uri[:60]}"
        node = nodes[0]
        d = output._node_to_clash(node)
        assert d is not None, f"_node_to_clash 返回 None: {uri[:60]}"
        assert node_identity(node) == proxy_identity(d), f"dict 指纹不一致: {uri[:60]}"
        text = yaml.dump({"proxies": [d]}, allow_unicode=True, default_flow_style=False,
                         sort_keys=False, Dumper=output._SafeDumper)
        again = parse_content(text)
        assert again, "重新解析失败"
        assert node_identity(node) == node_identity(again[0]), f"重解析指纹不一致: {uri[:60]}"


def test_clash_source_keeps_reality_flow_and_fingerprint():
    clash = {"proxies": [{
        "name": "reality-node", "type": "vless", "server": "1.2.3.4", "port": 443,
        "uuid": UUID1, "tls": True, "sni": "x.com", "network": "tcp",
        "flow": "xtls-rprx-vision", "client-fingerprint": "chrome",
        "reality-opts": {"public-key": PBK, "short-id": "3e58"},
    }]}
    text = yaml.dump(clash, allow_unicode=True, default_flow_style=False, sort_keys=False,
                     Dumper=output._SafeDumper)
    node = parse_content(text)[0]
    # 旧实现漏读这三个字段：REALITY 节点会丢掉 public-key 而彻底不可用
    assert node.reality_opts.get("public-key") == PBK, "Clash 来源丢了 reality public-key"
    assert node.flow == "xtls-rprx-vision", "Clash 来源丢了 flow"
    assert node.client_fingerprint == "chrome", "Clash 来源丢了 client-fingerprint"


# ---------------------------------------------------------------- 3. short-id 引号
def test_numeric_short_id_is_quoted_for_mihomo():
    """short-id 必须带引号，否则 mihomo 会把它读成浮点数。

    机制（实测确认）：
      * PyYAML 的 float 解析要求小数点，所以 `3e58` 在 PyYAML 眼里是字符串，
        plain `yaml.safe_dump` 会原样输出不带引号的 `short-id: 3e58`；
      * 但 mihomo 用 go-yaml，其数字解析走 Go 的 strconv.ParseFloat ——
        `ParseFloat("3e58")` 成功，于是变成 3e+58 浮点数，mihomo 报
        "invalid REALITY short ID"。
      * parser.py 在源未提供 sid 时的默认值恰好就是 "3e58"，所以这条路径很容易踩到。
    因此所有重写配置的环节都必须显式加引号（_defuse_shortid / defuse_reality_short_ids）。
    """
    raw = {"proxies": [{"reality-opts": {"short-id": "3e58"}}]}
    plain_text = yaml.safe_dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=False)
    assert "short-id: 3e58" in plain_text, "前提失效：plain dump 本应输出不带引号的 3e58"

    cfg = {"proxies": [{"name": "n", "type": "vless", "server": "1.2.3.4", "port": 443,
                        "uuid": UUID1, "reality-opts": {"public-key": PBK, "short-id": "3e58"}}]}
    output.defuse_reality_short_ids(cfg)
    path = _tmp("shortid.yaml")
    dump_yaml_atomic(path, cfg, dumper=output._SafeDumper)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "short-id: '3e58'" in text, "short-id 没有被加引号，mihomo 会解析成 3e+58"
    assert isinstance(yaml.safe_load(text)["proxies"][0]["reality-opts"]["short-id"], str)


# ---------------------------------------------------------------- 4. AUTO 组收敛
def _cfg_with_groups(n=50):
    names = [f"n{i}" for i in range(n)]
    return {
        "proxies": [{"name": x, "type": "ss", "server": "1.1.1.1", "port": 8388,
                     "cipher": "aes-256-gcm", "password": "p"} for x in names],
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": ["AUTO"] + names},
            {"name": "AUTO", "type": "url-test", "proxies": names},
        ],
    }, names


def test_enforce_auto_cap_trims_url_test_group():
    cfg, names = _cfg_with_groups(50)
    path = _tmp("autocap.yaml")
    dump_yaml_atomic(path, cfg, dumper=output._SafeDumper)
    run_pipeline.enforce_auto_cap(path, cap=30)
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    groups = {g["name"]: g for g in d["proxy-groups"]}
    assert len(groups["AUTO"]["proxies"]) == 30, "AUTO 组没有被收敛到 30"
    assert groups["AUTO"]["proxies"] == names[:30]
    assert groups["PROXY"]["proxies"][0] == "AUTO", "PROXY 组丢了 AUTO 引用"
    assert len(groups["PROXY"]["proxies"]) == 51


# ---------------------------------------------------------------- 5. 组引用裁剪
def test_http_filter_keeps_group_references():
    cfg, names = _cfg_with_groups(4)
    src = _tmp("http_src.yaml")
    dst = _tmp("http_dst.yaml")
    dump_yaml_atomic(src, cfg, dumper=output._SafeDumper)
    delay_map = {names[0]: 100.0, names[1]: 200.0}
    kept = filter_output_by_http_test(src, dst, delay_map)
    assert kept == 2
    with open(dst, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    groups = {g["name"]: g for g in d["proxy-groups"]}
    assert groups["PROXY"]["proxies"][0] == "AUTO", "PROXY 组丢了 AUTO（旧实现会误删）"
    assert groups["AUTO"]["proxies"] == names[:2]
    # 非标准字段不应再出现在配置里
    for p in d["proxies"]:
        assert "_http_delay" not in p, "配置里仍残留 _http_delay 字段"


# ---------------------------------------------------------------- 6. 指纹白名单
def test_client_fingerprint_whitelist():
    uri = (f"vless://{UUID1}@1.2.3.4:443?encryption=none&security=tls&sni=y.com"
           "&type=tcp&fp=unsafe")
    node = parse_content(uri)[0]
    d = output._node_to_clash(node)
    assert d["client-fingerprint"] == "chrome", "脏指纹没有被归一化"
    ok, _ = output._validate_proxy_for_clash(d)
    assert ok

    bad = dict(d)
    bad["client-fingerprint"] = "unsafe"
    ok2, reason = output._validate_proxy_for_clash(bad)
    assert not ok2 and "fingerprint" in reason, "校验器没有拦住非法指纹"


# ---------------------------------------------------------------- 7. 来源质量可恢复
def test_prefilter_sidecar_key_format_matches():
    """collector 写出的旁路 key 必须能被 http_test 读回（否则预筛永远命中 0 个）。

    这是本次新增的唯一跨模块接口：key 格式为 "{type}|{server}|{port}"，
    latency 值为 None 表示 TCP 连不上（不参与第一阶段）。
    """
    proxies = [
        {"name": "a", "type": "vless", "server": "1.1.1.1", "port": 443},
        {"name": "b", "type": "trojan", "server": "t.com", "port": 8443},
        {"name": "c", "type": "ss", "server": "2.2.2.2", "port": 8388},
    ]
    sidecar = {"ts": 0, "key": "type|server|port",
               "latency": {"vless|1.1.1.1|443": 120.0,
                           "trojan|t.com|8443": None,
                           "ss|2.2.2.2|8388": 45.0}}
    path = _tmp("latency.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f)
    alive, why = load_tcp_alive(path, proxies)
    assert alive == {"a", "c"}, f"预筛命中集合错误: {alive} ({why})"
    assert "2/3" in why, f"说明文本异常: {why}"
    # 文件缺失时必须优雅退化（返回空集 -> 调用方全量实测）
    alive2, why2 = load_tcp_alive(_tmp("no_such_latency.json"), proxies)
    assert alive2 == set() and why2

    # 覆盖率不足（旁路只覆盖 1/3）必须被拒 —— 否则会漏测大量节点
    thin = _tmp("latency_thin.json")
    with open(thin, "w", encoding="utf-8") as f:
        json.dump({"latency": {"vless|1.1.1.1|443": 10.0}}, f)
    alive3, why3 = load_tcp_alive(thin, proxies)
    assert alive3 == set(), f"覆盖率不足的旁路没有被拒: {alive3}"
    assert "覆盖" in why3

    # 过期旁路（TTL 之外）必须被拒
    old = _tmp("latency_old.json")
    with open(old, "w", encoding="utf-8") as f:
        json.dump({"latency": {"vless|1.1.1.1|443": 10.0,
                               "trojan|t.com|8443": 10.0,
                               "ss|2.2.2.2|8388": 10.0}}, f)
    stale = time.time() - 48 * 3600
    os.utime(old, (stale, stale))
    alive4, why4 = load_tcp_alive(old, proxies)
    assert alive4 == set(), f"过期旁路没有被拒: {alive4}"
    assert "过期" in why4


# ---------------------------------------------------------------- 8. 重名（mihomo 整份拒收）
def test_ensure_unique_proxy_names_fixes_duplicates():
    """同名不同凭据的节点必须被改名，否则 mihomo 报 duplicate name 整份拒收。

    2026-10-16 05:28 真实事故：Clash Verge 校验失败并把变更回滚，而流水线自己 rc=0。
    """
    proxies = [
        {"name": "dup", "type": "trojan", "server": "a.com", "port": 443, "password": "P1"},
        {"name": "dup", "type": "trojan", "server": "a.com", "port": 443, "password": "P2"},
        {"name": "dup", "type": "trojan", "server": "a.com", "port": 443, "password": "P3"},
        {"name": "other", "type": "ss", "server": "b.com", "port": 8388, "password": "P4"},
    ]
    # 前提：它们的指纹不同，所以按指纹去重的 merge_previous_fast 折叠不掉
    assert len({proxy_identity(p) for p in proxies[:3]}) == 3, "前提失效：指纹应各不相同"

    renamed = output.ensure_unique_proxy_names(proxies)
    assert [p["name"] for p in proxies] == ["dup", "dup 2", "dup 3", "other"], \
        [p["name"] for p in proxies]
    assert renamed == ["dup 2", "dup 3"]
    assert output.ensure_unique_proxy_names(proxies) == [], "去重应幂等"


def test_finalize_deploy_config_repairs_and_validates():
    """finalize_deploy_config: 名称唯一 + 重建组引用 + mihomo 整份校验。"""
    cfg = {
        "proxies": [
            {"name": "dup", "type": "trojan", "server": "a.com", "port": 443, "password": "P1"},
            {"name": "dup", "type": "trojan", "server": "a.com", "port": 443, "password": "P2"},
        ],
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": ["AUTO"]},
            {"name": "AUTO", "type": "url-test", "proxies": ["dup"],
             "url": "https://www.gstatic.com/generate_204", "interval": 300},
        ],
        "rules": ["MATCH,PROXY"],
    }
    path = _tmp("finalize.yaml")
    dump_yaml_atomic(path, cfg, dumper=output._SafeDumper)
    assert run_pipeline.finalize_deploy_config(path), "finalize 校验未通过（应有 mihomo 时才算严格）"
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    names = [p["name"] for p in d["proxies"]]
    assert len(names) == len(set(names)), f"仍有重名: {names}"
    groups = {g["name"]: g for g in d["proxy-groups"]}
    assert groups["PROXY"]["proxies"] == ["AUTO"] + names, groups["PROXY"]["proxies"]
    assert groups["AUTO"]["proxies"] == names


# ---------------------------------------------------------------- 9. AUTO 该装"今天的"节点
def test_strip_speed_suffix_and_order_by_today():
    """AUTO 必须装今天实测最快的节点，而不是池子里最靠前的旧节点。

    2026-10-16 早上实测：AUTO 的 30 个全是上一轮合并进来的旧节点（名字带 | x.xMB/s），
    一夜之间全部 Error，而池子里有大量当天验证过的活节点。
    """
    proxies = [
        {"name": "old1 | 1.7MB/s", "type": "trojan", "server": "o1.com", "port": 443, "password": "a"},
        {"name": "old2 | 0.9MB/s", "type": "trojan", "server": "o2.com", "port": 443, "password": "b"},
        {"name": "fresh1", "type": "trojan", "server": "f1.com", "port": 443, "password": "c"},
        {"name": "fresh2", "type": "trojan", "server": "f2.com", "port": 443, "password": "d"},
    ]
    assert run_pipeline.strip_speed_suffixes(proxies) == 2
    assert [p["name"] for p in proxies] == ["old1", "old2", "fresh1", "fresh2"]

    # 今天的实测速率：fresh2 比 fresh1 快；旧节点没有今天的记录
    speed = {proxy_identity(proxies[2]): 1.0, proxy_identity(proxies[3]): 5.0}
    ordered = run_pipeline.order_proxies_by_today(proxies, speed)
    assert [p["name"] for p in ordered] == ["fresh2", "fresh1", "old1", "old2"], \
        [p["name"] for p in ordered]
    # 关键：没有任何节点被丢掉（旧节点仍然全部保留）
    assert len(ordered) == len(proxies)

    # 无速率数据时保持原顺序（安全退化）
    assert [p["name"] for p in run_pipeline.order_proxies_by_today(proxies, {})] == \
        ["old1", "old2", "fresh1", "fresh2"]


def test_order_merged_nodes_puts_today_first():
    old = [{"name": "A", "type": "trojan", "server": "a.com", "port": 1, "password": "pa"},
           {"name": "B", "type": "trojan", "server": "b.com", "port": 1, "password": "pb"}]
    new = [{"name": "C", "type": "trojan", "server": "c.com", "port": 1, "password": "pc"},
           {"name": "B", "type": "trojan", "server": "b.com", "port": 1, "password": "pb"}]
    merged = run_pipeline.order_merged_nodes(old, new)
    assert [p["name"] for p in merged] == ["C", "B", "A"], [p["name"] for p in merged]
    assert len(merged) == 3, "去重应按指纹生效，且不能丢节点"
    # 旧字典优先保留的语义不变（B 取的是旧对象）
    assert merged[1] is old[1]


def test_source_quality_can_recover_after_disable():
    path = _tmp("quality.json")
    if os.path.exists(path):
        os.remove(path)
    t = SourceQualityTracker(path)
    t.begin_run()
    t.add_source_batch("bad", [f"id{i}" for i in range(300)])   # >= MIN_SAMPLES
    t.add_verification("bad", [])                               # 0 通过
    t.finish_run()
    assert t.sources["bad"]["enabled"] is False, "低通过率源没有被停用"

    for _ in range(REENABLE_AFTER_RUNS):
        t.begin_run()
        t.add_source_batch("bad", ["id0"])
        t.add_verification("bad", [])
        t.finish_run()
    assert t.sources["bad"]["enabled"] is True, "停用后无法恢复（单向棘轮）"
    assert t.sources["bad"]["probation"] is True


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed.append((name, e))
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
