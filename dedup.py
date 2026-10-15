"""节点去重"""

import logging
from typing import List

from parser import ProxyNode

logger = logging.getLogger(__name__)


def deduplicate(nodes: List[ProxyNode]) -> List[ProxyNode]:
    """按 server+port+type 去重，保留名称信息更丰富的节点"""
    seen: dict[str, ProxyNode] = {}

    for node in nodes:
        key = node.dedup_key
        if key not in seen:
            seen[key] = node
        else:
            # 保留名称更长的（信息更丰富）
            existing = seen[key]
            if len(node.name) > len(existing.name):
                seen[key] = node

    total = len(nodes)
    unique = len(seen)
    removed = total - unique
    if removed > 0:
        logger.info("去重: %d -> %d (移除 %d 个重复节点)", total, unique, removed)

    return list(seen.values())


def filter_nodes(
    nodes: List[ProxyNode],
    include_keywords: List[str] | None = None,
    exclude_keywords: List[str] | None = None,
) -> List[ProxyNode]:
    """按关键词过滤节点"""
    result = nodes

    if include_keywords:
        kw = [k.lower() for k in include_keywords]
        result = [n for n in result if any(k in n.name.lower() for k in kw)]
        logger.info("关键词包含过滤: %d -> %d", len(nodes), len(result))

    if exclude_keywords:
        kw = [k.lower() for k in exclude_keywords]
        before = len(result)
        result = [n for n in result if not any(k in n.name.lower() for k in kw)]
        logger.info("关键词排除过滤: %d -> %d", before, len(result))

    return result
