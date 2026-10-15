"""节点延迟测试 - TCP 连通性测试"""

import asyncio
import logging
import socket
import struct
import sys
import time
from typing import List, Tuple

from parser import ProxyNode

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
DEFAULT_CONCURRENCY = 50
TEST_URL = "http://www.gstatic.com/generate_204"


async def _tcp_ping(server: str, port: int, timeout: float) -> float | None:
    """TCP 连接测试，返回延迟(ms)，超时/失败返回 None"""
    try:
        start = time.monotonic()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(server, port),
            timeout=timeout,
        )
        delay = (time.monotonic() - start) * 1000
        writer.close()
        await writer.wait_closed()
        return round(delay, 1)
    except Exception:
        return None


async def _http_test_via_node(
    node: ProxyNode,
    timeout: float,
    local_port: int = 0,
) -> Tuple[float | None, str]:
    """
    通过节点发起 HTTP 测试（需要本地代理端口）。
    如果无法建立代理连接则回退到 TCP ping。
    返回 (延迟ms, 状态描述)
    """
    # 直接 TCP ping（不需要本地代理）
    delay = await _tcp_ping(node.server, node.port, timeout)
    if delay is not None:
        return delay, "TCP OK"
    return None, "TIMEOUT"


async def test_node(
    node: ProxyNode,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 1,
) -> Tuple[ProxyNode, float | None]:
    """测试单个节点延迟，带重试"""
    delay = None
    for _ in range(retries + 1):
        delay, _ = await _http_test_via_node(node, timeout)
        if delay is not None:
            break
    return node, delay


async def test_all(
    nodes: List[ProxyNode],
    timeout: float = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> List[Tuple[ProxyNode, float | None]]:
    """并发测试所有节点延迟"""
    semaphore = asyncio.Semaphore(concurrency)

    async def _limited_test(node: ProxyNode):
        async with semaphore:
            return await test_node(node, timeout)

    logger.info("开始延迟测试: %d 个节点, 并发 %d, 超时 %ds", len(nodes), concurrency, timeout)

    tasks = [_limited_test(n) for n in nodes]
    results = await asyncio.gather(*tasks)

    alive = sum(1 for _, d in results if d is not None)
    dead = len(results) - alive
    logger.info("延迟测试完成: %d 可用, %d 超时", alive, dead)

    return list(results)


def sort_by_latency(
    nodes_with_latency: List[Tuple[ProxyNode, float | None]],
) -> List[ProxyNode]:
    """按延迟排序，可用节点在前，延迟低的排前面"""
    def sort_key(item):
        node, delay = item
        if delay is None:
            return (1, float("inf"))  # 不可用排最后
        return (0, delay)

    sorted_items = sorted(nodes_with_latency, key=sort_key)
    return [node for node, _ in sorted_items]


def remove_unreachable(
    nodes_with_latency: List[Tuple[ProxyNode, float | None]],
) -> List[ProxyNode]:
    """移除不可达节点"""
    return [node for node, delay in nodes_with_latency if delay is not None]


def print_latency_table(nodes_with_latency: List[Tuple[ProxyNode, float | None]]) -> None:
    """打印延迟结果表格"""
    if not nodes_with_latency:
        return

    # 按延迟排序用于展示
    sorted_results = sorted(
        nodes_with_latency,
        key=lambda x: x[1] if x[1] is not None else float("inf"),
    )

    # 安全编码输出：替换无法编码的字符
    def safe_str(s: str) -> str:
        try:
            s.encode(sys.stdout.encoding or "utf-8")
            return s
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")

    name_width = min(max(len(n.name) for n, _ in sorted_results), 40)
    print(f"\n{'节点名称':<{name_width}}  {'类型':<6}  {'延迟':>8}")
    print("-" * (name_width + 22))

    for node, delay in sorted_results:
        delay_str = f"{delay:.0f}ms" if delay is not None else "TIMEOUT"
        name = safe_str(node.name[:name_width])
        print(f"{name:<{name_width}}  {node.type:<6}  {delay_str:>8}")
