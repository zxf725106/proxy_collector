#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quality_report.py — 验证后按来源结算质量分
============================================================
把 collect_aggregator.py 写下的来源映射，与本轮 mihomo/speed 验证通过
的节点对齐，按源统计通过率 / 稀有度 / 时延，喂给 SourceQualityTracker，
并落盘到 source_quality.json。下一轮 collect_aggregator 会按
is_enabled() 自动跳过被降权的源。

输入：
  candidates/aggregator_sources.json   （新：aggregator 源）
  candidates/legacy_sources.json       （可选：旧 config.yaml 订阅源，按 host 命名）
  output-filtered-fast.yaml            （本轮实测通过的节点集）

输出：
  source_quality.json                  （累积状态）
  终端打印质量榜 + 降权建议
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Dict, List, Set

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_identities_for_alive(fast_path: str) -> Set[str]:
    """从 FAST clash yaml 读取「实测可用节点」的身份串集合。"""
    import yaml

    with open(fast_path, "r", encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    from parser import proxy_identity

    ids: Set[str] = set()
    for p in d.get("proxies", []) or []:
        if p.get("type") and p.get("server") and p.get("port"):
            ids.add(proxy_identity(p))
    return ids


def load_source_map(path: str) -> Dict[str, List[str]]:
    """读取「来源 -> 节点身份列表」映射。

    必须校验值类型：本模块的状态文件 source_quality.json 顶层同样是 "sources" 键，
    但值是统计 dict。误把它当映射传入会得到 {源: [统计字段名...]}，随后在
    verified = {x for x in s if x in alive} 处抛 TypeError 崩掉结算步骤。
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, dict) and "sources" in d:
        d = d["sources"]
    out: Dict[str, List[str]] = {}
    skipped: List[str] = []
    for k, v in (d or {}).items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[k] = list(v)
        else:
            skipped.append(str(k))
    if skipped:
        log(f"!! {path} 中 {len(skipped)} 个条目不是「字符串列表」(疑似把状态文件当映射)，已跳过: "
            + ", ".join(skipped[:3]))
    return out


def load_latency_by_identity(path: str) -> Dict[str, float]:
    """读取 http_test 写下的 HTTP 实测延迟旁路文件 {身份指纹: 毫秒}。

    原先没有任何调用方给 tracker.add_verification 传时延，导致 avg_latency_ema 恒为 0、
    lat_factor 恒为 1.0（每个源白拿 0.1 权重），实测速率信号被完全丢弃。
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception as e:
        log(f"!! 延迟旁路读取失败: {e}")
        return {}
    delays = d.get("delays") or {}
    return {k: float(v) for k, v in delays.items() if isinstance(v, (int, float))}


def derive_legacy_map(collector_mapping_path: str) -> Dict[str, List[str]]:
    """如果 collector 写了 legacy_sources.json（按 URL host 命名的源），合并进来。"""
    if not os.path.exists(collector_mapping_path):
        return {}
    try:
        with open(collector_mapping_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "sources" in d:
            d = d["sources"]
        return {k: list(v) for k, v in (d or {}).items()}
    except Exception as e:
        log(f"legacy 映射读取失败: {e}")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="按来源结算质量分")
    ap.add_argument("--fast", default="output-filtered-fast.yaml")
    ap.add_argument("--aggregator-map", default="candidates/aggregator_sources.json")
    ap.add_argument("--legacy-map", default="candidates/legacy_sources.json")
    ap.add_argument("--state", default="source_quality.json")
    ap.add_argument("--no-save", action="store_true", help="只读模式，不结算/不落盘")
    args = ap.parse_args()

    if not os.path.exists(args.fast):
        log(f"!! 找不到验证结果 {args.fast}，跳过打分")
        return 1

    alive = load_identities_for_alive(args.fast)
    log(f"实测可用节点: {len(alive)} 个")

    if args.no_save:
        # 只读模式：先尝试加载 tracker 并展示状态，无论有无来源映射
        try:
            from source_quality import SourceQualityTracker

            tracker = SourceQualityTracker(os.path.join(HERE, args.state))
            print(tracker.report())
        except Exception as e:
            log(f"!! 加载 SourceQualityTracker 失败: {e}")
        return 0

    by_source: Dict[str, List[str]] = {}
    by_source.update(load_source_map(args.aggregator_map))
    by_source.update(derive_legacy_map(args.legacy_map))
    if not by_source:
        log("!! 无来源映射（未跑 collect_aggregator / collector 旧源未记映射）")
        return 1
    log(f"来源映射共 {len(by_source)} 个源: " + ", ".join(f"{k}={len(v)}" for k, v in by_source.items()))

    try:
        from source_quality import SourceQualityTracker
    except Exception as e:
        log(f"!! 导入 SourceQualityTracker 失败: {e}")
        return 1

    tracker = SourceQualityTracker(os.path.join(HERE, args.state))

    tracker.begin_run()
    delays = load_latency_by_identity(os.path.join(HERE, "outputs", "http_delays.json"))
    if delays:
        log(f"HTTP 实测延迟旁路: {len(delays)} 条（用于来源时延评分）")
    for name, ids in by_source.items():
        s = {x for x in ids if x}
        if not s:
            continue
        verified = {x for x in s if x in alive}
        lats = [delays[x] for x in verified if x in delays]
        avg_lat = (sum(lats) / len(lats)) if lats else None
        tracker.add_source_batch(name, s)
        tracker.add_verification(name, verified, avg_lat)
    tracker.finish_run()

    print("\n" + tracker.report())
    log(f"质量状态已更新: {args.state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
