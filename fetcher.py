"""异步抓取订阅内容"""

import asyncio
import base64
import gzip
import logging
from typing import List

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 2

# GitHub 加速镜像（中国大陆可访问）
GITHUB_MIRRORS = [
    "https://ghfast.top/",
    "https://gh.con.sh/",
    "https://mirror.ghproxy.com/",
]


def _apply_mirror(url: str) -> str:
    """对 raw.githubusercontent.com 链接尝试镜像加速"""
    if "raw.githubusercontent.com" not in url:
        return url
    # 直接返回原 URL，抓取失败时再尝试镜像
    return url


def _get_mirror_urls(url: str) -> List[str]:
    """为 GitHub 原始链接生成镜像 URL 列表"""
    urls = [url]  # 先试原始地址
    if "raw.githubusercontent.com" in url:
        for mirror in GITHUB_MIRRORS:
            urls.append(mirror + url)
    return urls


async def fetch_one(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """抓取单个订阅链接，返回原始文本内容。自动尝试 GitHub 镜像。"""
    urls_to_try = _get_mirror_urls(url)

    for current_url in urls_to_try:
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                async with session.get(current_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    resp.raise_for_status()
                    raw = await resp.read()
                    # 尝试 gzip 解压
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                    text = raw.decode("utf-8", errors="ignore").strip()
                    # 如果内容看起来是 base64，尝试解码
                    decoded = _try_base64_decode(text)
                    result = decoded if decoded else text
                    if result:
                        return result
            except Exception as e:
                last_error = e
                logger.debug("抓取失败 [%s] 第 %d 次: %s", current_url, attempt, e)
        if len(urls_to_try) > 1:
            logger.debug("原始地址失败，尝试镜像: %s", current_url)

    logger.error("抓取最终失败 [%s]（含镜像）: %s", url, last_error)
    return ""


async def fetch_all(urls: List[str], timeout: int = DEFAULT_TIMEOUT) -> List[str]:
    """并发抓取所有订阅链接"""
    results = await fetch_all_pairs(urls, timeout)
    contents = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            logger.error("抓取异常 [%s]: %s", url, result)
        elif result:
            contents.append(result)
    return contents


async def fetch_all_pairs(
    urls: List[str], timeout: int = DEFAULT_TIMEOUT,
) -> List[object]:
    """并发抓取，返回与 urls 等长的「结果列表」（失败位置是 Exception 对象）。

    与 fetch_all 的区别：保留 url-result 对齐关系，供调用方做「来源追溯」、
    按订阅源分组记录。gather 用 return_exceptions=True 防止单源失败中断整批。
    """
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_one(session, url, timeout) for url in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)


def _try_base64_decode(text: str) -> str | None:
    """尝试 base64 解码，失败返回 None"""
    # 简单启发式：如果文本不含协议前缀且看起来像 base64
    prefixes = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "proxies:")
    if any(text.startswith(p) for p in prefixes):
        return None
    try:
        # 补齐 padding
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        # 验证解码结果看起来像节点列表
        if any(p in decoded for p in prefixes):
            return decoded
    except Exception:
        pass
    return None
