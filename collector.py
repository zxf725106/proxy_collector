#!/usr/bin/env python3
"""Proxy Collector - 自动采集订阅节点并生成 Clash 配置"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import yaml

from atomic import write_text_atomic
from dedup import deduplicate, filter_nodes
from fetcher import fetch_all, fetch_all_pairs
import fetcher
from output import generate_clash_config
from parser import node_identity, parse_content
from tester import print_latency_table, remove_unreachable, sort_by_latency, test_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("collector")

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _label_from_url(url: str) -> str:
    """把订阅 URL 压缩成「源名」用于 legacy 映射：取 host + path 末两段。"""
    from urllib.parse import urlparse

    p = urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg][-2:]
    return (p.netloc + "/" + "/".join(parts)) if parts else p.netloc


def load_config() -> dict:
    """加载配置文件"""
    if not CONFIG_PATH.exists():
        logger.error("配置文件不存在: %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def main():
    config = load_config()

    # 1. 获取订阅链接
    urls = config.get("subscriptions", [])
    urls = [u for u in urls if u and u.strip()]
    if not urls:
        logger.error("未配置订阅链接，请编辑 config.yaml")
        sys.exit(1)

    logger.info("共 %d 个订阅源", len(urls))

    # 2. 抓取内容
    logger.info("开始抓取订阅内容...")
    # 同步保留「URL ↔ 内容」对齐关系，供来源质量打分（旧 config.yaml 订阅源）
    fetch_results = await fetcher.fetch_all_pairs(urls)
    contents = []
    for url, result in zip(urls, fetch_results):
        if isinstance(result, Exception):
            logger.error("抓取异常 [%s]: %s", url, result)
        elif result:
            contents.append(result)

    if not contents:
        logger.error("所有订阅源抓取失败")
        sys.exit(1)

    # 2.5 合并 aggregator 移植层的 URI 池（付费主源 / GitHub 发现 / crawlers）
    #     与上面 contents 等价，parser.parse_content 自动按行解析 URI
    agg_cfg = config.get("aggregator") or {}
    agg_uris_file = agg_cfg.get("uris_file") or "candidates/aggregator_uris.txt"
    agg_text_for_parse = ""
    if agg_cfg.get("enabled", True):
        agg_path = agg_uris_file
        if not os.path.isabs(agg_path):
            agg_path = os.path.join(os.path.dirname(__file__), agg_path)
        if os.path.exists(agg_path):
            with open(agg_path, "r", encoding="utf-8") as f:
                agg_text = f.read()
            if agg_text.strip():
                agg_text_for_parse = agg_text
                logger.info("已合并 aggregator URI 池: %s", agg_path)
            else:
                logger.info("aggregator URI 池为空, 跳过: %s", agg_path)
        else:
            logger.info("未发现 aggregator URI 池文件 %s (如未跑 collect_aggregator.py, 忽略)", agg_path)

    # 3. 解析节点：每个源只解析一次。
    #    原实现在 3.5 步为了写 legacy 来源映射又把所有内容重新 parse_content() 一遍，
    #    等于把最贵的解析做了两次（实测每轮多花约 1.5 分钟），这里改成一次解析两处复用。
    all_nodes = []
    per_url_nodes: list[tuple[str, list]] = []      # (url, nodes) 供来源追溯复用
    for url, result in zip(urls, fetch_results):
        if isinstance(result, Exception) or not result:
            continue
        nodes = parse_content(result)
        per_url_nodes.append((url, nodes))
        logger.info("解析到 %d 个节点 [%s]", len(nodes), _label_from_url(url))
        all_nodes.extend(nodes)

    if agg_text_for_parse:
        agg_nodes = parse_content(agg_text_for_parse)
        logger.info("解析到 %d 个节点 [aggregator 池]", len(agg_nodes))
        all_nodes.extend(agg_nodes)

    if not all_nodes:
        logger.error("未解析到任何节点")
        sys.exit(1)

    logger.info("共解析 %d 个节点", len(all_nodes))

    # 3.5 旧订阅源的「来源追溯」：仅当 quality.enabled 时写 legacy_sources.json
    qcfg = (agg_cfg.get("quality") or {})
    if qcfg.get("enabled", True):
        try:
            legacy_map: dict[str, list[str]] = {}
            for url, nodes in per_url_nodes:
                ids = [node_identity(n) for n in nodes]
                if ids:
                    legacy_map[_label_from_url(url)] = ids
            map_path = os.path.join(os.path.dirname(__file__), "candidates", "legacy_sources.json")
            os.makedirs(os.path.dirname(map_path), exist_ok=True)

            write_text_atomic(map_path, json.dumps(
                {"ts": time.time(), "sources": legacy_map}, ensure_ascii=False, indent=1))
            logger.info("已写 legacy 来源映射: %s (%d 个源)", map_path, len(legacy_map))
        except Exception as e:
            logger.warning("legacy 来源映射写入失败(非致命): %s", e)

    # 4. 去重
    all_nodes = deduplicate(all_nodes)

    # 5. 过滤
    filter_cfg = config.get("filter", {})
    include_kw = filter_cfg.get("include_keywords", [])
    exclude_kw = filter_cfg.get("exclude_keywords", [])
    all_nodes = filter_nodes(all_nodes, include_kw, exclude_kw)

    if not all_nodes:
        logger.error("过滤后无可用节点")
        sys.exit(1)

    # 6. 延迟测试
    latency_cfg = config.get("latency_test", {})
    # TCP 结果旁路文件（供 http_test 分级预筛；不是配置，勿手工编辑）
    latency_map_path = os.path.join(os.path.dirname(__file__), "output-latency.json")
    if latency_cfg.get("enabled", False):
        max_nodes = latency_cfg.get("max_nodes", 0)
        if max_nodes > 0 and len(all_nodes) > max_nodes:
            sampling_cfg = config.get("sampling", {}) or {}
            seed = str(sampling_cfg.get("seed", "proxy-collector-v1"))
            ranked = sorted(
                all_nodes,
                key=lambda n: hashlib.sha256(f"{seed}|{n.dedup_key}".encode("utf-8")).hexdigest(),
            )
            logger.info(
                "节点数 %d 超过上限 %d，使用稳定哈希抽样（seed=%s）",
                len(all_nodes), max_nodes, seed,
            )
            all_nodes = ranked[:max_nodes]

        results = await test_all(
            all_nodes,
            timeout=latency_cfg.get("timeout", 5),
            concurrency=latency_cfg.get("concurrency", 50),
        )
        print_latency_table(results)

        # 把 TCP 结果写成旁路文件，供 http_test 做「先测 TCP 连通节点」的分级预筛。
        # 实测：152 个 HTTP 可用节点里有 4 个（2.6%）TCP 探测超时但实际能代理，
        # 因此 http_test 采用两阶段（第一阶段存活数不足时补测剩余节点），见 http_test.py。
        try:
            lat_map = {f"{n.type}|{n.server}|{n.port}": d for n, d in results}
            tcp_alive = sum(1 for v in lat_map.values() if v is not None)
            write_text_atomic(latency_map_path, json.dumps(
                {"ts": time.time(), "key": "type|server|port", "latency": lat_map},
                ensure_ascii=False))
            logger.info("TCP 旁路文件已写: %s (%d 个节点, %d 个 TCP 连通)",
                        latency_map_path, len(lat_map), tcp_alive)
        except Exception as e:
            logger.warning("TCP 旁路文件写入失败(非致命): %s", e)

        if latency_cfg.get("remove_unreachable", True):
            filtered = remove_unreachable(results)
            logger.info("移除不可达节点后剩余 %d 个", len(filtered))
        else:
            filtered = [n for n, _ in results]

        if latency_cfg.get("sort_by_latency", True):
            # 从 filtered 构建带延迟的列表，再排序
            node_to_delay = {id(n): d for n, d in results}
            filtered_with_delay = [(n, node_to_delay[id(n)]) for n in filtered if id(n) in node_to_delay]
            all_nodes = sort_by_latency(filtered_with_delay)
        else:
            all_nodes = filtered

        # 按延迟阈值过滤
        max_latency = latency_cfg.get("max_latency", 0)
        if max_latency > 0:
            node_to_delay = {id(n): d for n, d in results}
            before = len(all_nodes)
            all_nodes = [
                n for n in all_nodes
                if id(n) in node_to_delay and node_to_delay[id(n)] is not None and node_to_delay[id(n)] <= max_latency
            ]
            logger.info("按延迟 <%dms 过滤: %d -> %d", max_latency, before, len(all_nodes))

        if not all_nodes:
            logger.error("所有节点均不可达或超过延迟阈值")
            sys.exit(1)

    # 7. 输出完整版
    output_path = config.get("output", "output.yaml")
    # 相对路径基于脚本目录
    if not Path(output_path).is_absolute():
        output_path = str(Path(__file__).parent / output_path)

    generate_clash_config(all_nodes, output_path, filter_unsupported_flow=False)
    logger.info("完成! 共 %d 个节点已写入 %s", len(all_nodes), output_path)

    # 8. 输出移动兼容版（过滤带 flow 的 vless 节点）
    mobile_output = config.get("mobile_output", "")
    if mobile_output:
        if not Path(mobile_output).is_absolute():
            mobile_output = str(Path(__file__).parent / mobile_output)
        mobile_count = generate_clash_config(all_nodes, mobile_output, filter_unsupported_flow=True)
        logger.info("移动兼容版已生成: %s (%d 个节点)", mobile_output, mobile_count)

    # 9. 复制完整版到 Clash 配置目录（默认给 Windows Clash Verge/mihomo 使用）
    clash_dir = Path(config.get("clash_config_dir", ""))
    if clash_dir.is_absolute() and clash_dir.exists():
        dest = clash_dir / "config.yaml"
        shutil.copy2(output_path, dest)
        logger.info("已复制到 Clash 配置目录: %s", dest)


if __name__ == "__main__":
    asyncio.run(main())
