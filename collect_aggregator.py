#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_aggregator.py — aggregator 移植层的统一采集入口
================================================================================
把移植过来的三块能力（付费订阅解析 / 免费源自动发现 / crawler 自动注册）收拢成
一个脚本，产出「通用代理 URI」，直接并入 proxy_collector 主链路：

    付费订阅 ─┐
    GitHub/TG 发现 ─┼─> 通用 URI ─> candidates/aggregator_uris.txt
    crawlers/*.py ─┘                        │
                                            ▼
    collector.py(合并) -> 去重 -> TCP/mihomo 验证 -> speed_test -> 输出

设计要点
--------
* 代理自适应：先探测 Clash 7897，再退到环境变量代理，最后直连，避免
  「Clash 没开就全源失败」。
* 质量闭环：读 source_quality.json，被自动降权的源本轮直接跳过。
* 每个源独立 try/except，单源挂掉不影响其它源。
* 产出同时写「URI 池」和「来源映射」，供 quality_report.py 赛后结算打分。

用法
----
    python collect_aggregator.py --check      # 只解析不落盘，打印各源产量
    python collect_aggregator.py              # 正常采集并落盘
    python collect_aggregator.py --config aggregator_config.json --out xxx.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CRAWLERS_DIR = os.path.join(HERE, "crawlers")
DEFAULT_CONFIG = os.path.join(HERE, "aggregator_config.json")

# 裸代理链接（发现结果里可能直接就是节点，不需要再 fetch）
_PROXY_LINK_RE = re.compile(
    r"^(?:ss|ssr|vmess|vless|trojan|snell|hysteria2?|socks5?|http)://", re.I
)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# ---------------------------------------------------------------------------
# 1. 网络通道自适应
# ---------------------------------------------------------------------------
def setup_network(cfg: dict) -> Optional[str]:
    """探测可用网络通道并 install_opener（影响后续所有 urllib 调用）。"""
    net = cfg.get("network", {}) or {}
    probe_url = net.get("probe_url", "https://api.github.com/rate_limit")
    timeout = int(net.get("timeout", 20))

    candidates: List[Optional[str]] = []
    if net.get("proxy"):
        candidates.append(net["proxy"])
    env_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") \
        or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        candidates.append(env_proxy)
    candidates.append(None)  # 直连兜底

    for p in candidates:
        handler = urllib.request.ProxyHandler({"http": p, "https": p}) if p \
            else urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        try:
            r = opener.open(probe_url, timeout=min(timeout, 8))
            r.read(64)
            urllib.request.install_opener(opener)
            log(f"网络通道: {p or '直连'} (探测 {probe_url.split('//')[1][:28]} OK)")
            return p
        except Exception as e:
            log(f"网络通道 {p or '直连'} 不可用: {type(e).__name__}")

    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    log("!! 所有网络通道探测失败，本轮大概率采集不到东西")
    return None


# ---------------------------------------------------------------------------
# 2. 节点身份（供质量打分跨阶段对齐）
# ---------------------------------------------------------------------------
def uri_identity(uri: str) -> str:
    """把 URI 归一成 type:server:port，作为跨阶段对齐的身份串。"""
    try:
        from parser import parse_content

        nodes = parse_content(uri)
        if nodes:
            n = nodes[0]
            return n.dedup_key
    except Exception:
        pass
    return uri


# ---------------------------------------------------------------------------
# 3. 采集各源
# ---------------------------------------------------------------------------
def collect_paid(cfg: dict, tracker) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    paid = (cfg.get("sources", {}) or {}).get("paid_subscriptions", {}) or {}
    if not paid.get("enabled"):
        return out
    from subscription_parser import parse_subscription

    timeout = int((cfg.get("network", {}) or {}).get("timeout", 20))
    for item in paid.get("items", []) or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        name = item.get("name") or url
        if not tracker.is_enabled(name):
            log(f"  付费源 {name}: 已被质量打分降权，跳过")
            continue
        try:
            uris = parse_subscription(url, timeout=timeout)
            out[name] = uris
            log(f"  付费源 {name}: {len(uris)} 个节点")
        except Exception as e:
            log(f"  付费源 {name}: 抓取/解析失败 {type(e).__name__} {e}")
    return out


def collect_discovery(cfg: dict, tracker) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    disc = cfg.get("discovery", {}) or {}
    if not disc.get("enabled"):
        return out

    source_name = "discovery"
    if not tracker.is_enabled(source_name):
        log(f"  发现源 {source_name}: 已被质量打分降权，跳过")
        return out

    try:
        import source_discovery
        from subscription_parser import parse_subscription
    except Exception as e:
        log(f"  发现源: 模块导入失败 {e}")
        return out

    max_urls = int(disc.get("github", {}).get("max_urls", 40))
    budget = int(disc.get("budget_sec", 90))

    # 硬性墙钟：发现走 urllib 同步，没法优雅中断；用 daemon 线程 + join 抢时间。
    # 真跑超时则放弃该源（线程随主进程退出而被丢弃），不让它卡住全流程。
    box: Dict[str, object] = {}

    def _do() -> None:
        try:
            found = source_discovery.discover_all({"discovery": disc})
            uris: List[str] = []
            for u in found[:max_urls]:
                if _PROXY_LINK_RE.match(u or ""):
                    uris.append(u)
                    continue
                try:
                    uris += parse_subscription(u)
                except Exception:
                    continue
            box["result"] = {source_name: uris}
        except Exception as e:
            box["error"] = e

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    th.join(timeout=budget)
    if th.is_alive():
        log(f"  发现源: 超时 {budget}s 未完成，跳过（线程将被丢弃）")
        return out
    if "error" in box:
        log(f"  发现源: discover_all 失败 {type(box['error']).__name__} {box['error']}")
        return out
    res = box.get("result", {}) or {}
    if res:
        out.update(res)
        log(f"  发现源 {source_name}: {len(out[source_name])} 个节点")
    return out


def collect_crawlers(cfg: dict, tracker) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    free = (cfg.get("sources", {}) or {}).get("free_crawlers", {}) or {}
    if not free.get("enabled"):
        return out
    try:
        from crawler_registry import discover_crawlers, run_crawler
    except Exception as e:
        log(f"  crawler: 模块导入失败 {e}")
        return out
    if not os.path.isdir(CRAWLERS_DIR):
        return out

    try:
        metas = discover_crawlers(CRAWLERS_DIR, cfg)
    except Exception as e:
        log(f"  crawler: 注册失败 {e}")
        return out
    if not metas:
        log("  crawler: crawlers/ 目录无活跃 crawler（模板文件以 _ 开头不注册）")
        return out

    for name, meta in metas.items():
        if not meta.get("enabled"):
            log(f"  crawler {name}: 配置关闭，跳过")
            continue
        if not tracker.is_enabled(name):
            log(f"  crawler {name}: 已被质量打分降权，跳过")
            continue
        uris = run_crawler(name, meta)
        out[name] = uris
        log(f"  crawler {name}: {len(uris)} 个节点")
    return out


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="aggregator 移植层统一采集入口")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--out", default=None, help="URI 输出文件（默认取 config.output.uris_file）")
    ap.add_argument("--check", action="store_true", help="只解析统计，不落盘")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        log(f"!! 配置文件不存在: {args.config}")
        return 2
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    log("==== aggregator 采集启动 ====")
    setup_network(cfg)

    # 质量追踪器（只读：决定跳过谁；写入在 quality_report.py 赛后结算）
    try:
        from source_quality import SourceQualityTracker

        qcfg = cfg.get("quality", {}) or {}
        tracker = SourceQualityTracker(os.path.join(HERE, qcfg.get("state", "source_quality.json")))
    except Exception as e:
        log(f"!! 质量追踪器不可用({e})，本轮不跳过任何源")
        tracker = None
    if tracker is None:
        class _Null:
            def is_enabled(self, _n):
                return True
        tracker = _Null()

    by_source: Dict[str, List[str]] = {}
    by_source.update(collect_paid(cfg, tracker))
    by_source.update(collect_discovery(cfg, tracker))
    by_source.update(collect_crawlers(cfg, tracker))

    # 全局去重（保序：付费 > 发现 > crawler）
    seen = set()
    all_uris: List[str] = []
    for name in by_source:
        for u in by_source[name]:
            if u and u not in seen:
                seen.add(u)
                all_uris.append(u)

    total_raw = sum(len(v) for v in by_source.values())
    log(f"各源产量: " + (" | ".join(f"{k}={len(v)}" for k, v in by_source.items()) or "无"))
    log(f"合计采集 {total_raw} 个，去重后 {len(all_uris)} 个")

    if args.check:
        log("--check 模式：不落盘")
        return 0 if all_uris else 1

    out_cfg = cfg.get("output", {}) or {}
    uris_file = args.out or os.path.join(HERE, out_cfg.get("uris_file", "candidates/aggregator_uris.txt"))
    map_file = os.path.join(HERE, out_cfg.get("map_file", "candidates/aggregator_sources.json"))
    os.makedirs(os.path.dirname(uris_file), exist_ok=True)

    # 只有采集到东西才覆盖，避免把上一轮好结果洗空
    if all_uris:
        with open(uris_file, "w", encoding="utf-8") as f:
            f.write("\n".join(all_uris))
        log(f"已写入 URI 池: {uris_file}")
    else:
        log("!! 本轮未采集到任何节点，保留上一轮 URI 池")

    # 来源映射：存身份串，供 quality_report.py 与验证结果对齐
    mapping = {name: [uri_identity(u) for u in uris] for name, uris in by_source.items()}
    with open(map_file, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sources": mapping}, f, ensure_ascii=False, indent=1)
    log(f"已写入来源映射: {map_file}")
    log("==== aggregator 采集完成 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
