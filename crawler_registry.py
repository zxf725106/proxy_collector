# -*- coding: utf-8 -*-
"""
crawler_registry.py — 配置/目录驱动的 crawler 自动注册
================================================================================

移植自 aggregator 的「配置驱动 pipeline」思路，替换 proxy_collector 里
crawlers/__init__.py 的硬编码 ALL_CRAWLERS。

约定（每个 crawler 模块只需满足其一）：
  A) 模块级暴露 __crawler__ 元数据 + crawl() 函数：
        __crawler__ = {"name": "proxifly", "source": "free", "enabled": True}
        def crawl() -> list[str]:   # 返回通用代理 URI 列表
            ...
  B) 没写 __crawler__ 时，用文件名当 name，默认 enabled=True、source="free"。

注册行为：
  * 扫描 crawlers/ 目录下所有 *.py（排除 __init__ / 以 _ 开头）
  * 读取 __crawler__ 元数据；enabled=False 的跳过
  * 按 config.json 里 crawler 开关二次过滤（config 优先）
  * 返回 name -> module 的有序字典，供调度器调用
"""

from __future__ import annotations

import importlib.util
import json
import os
from typing import Callable, Optional


def discover_crawlers(
    crawlers_dir: str,
    config: Optional[dict] = None,
) -> dict[str, dict]:
    """扫描 crawlers_dir，返回 {name: meta}。

    meta = {
        "name", "source", "enabled",
        "module_path", "crawl_fn": Callable[[], list[str]] | None
    }
    config: 可选，形如 {"crawlers": {"proxifly": {"enabled": true}, ...}}
            当 config 显式给出 enabled 时覆盖模块自身默认值。
    """
    if not os.path.isdir(crawlers_dir):
        raise FileNotFoundError(f"crawlers_dir not found: {crawlers_dir}")

    found: dict[str, dict] = {}
    for fname in sorted(os.listdir(crawlers_dir)):
        if not fname.endswith(".py"):
            continue
        if fname == "__init__.py" or fname.startswith("_"):
            continue
        base = fname[:-3]
        path = os.path.join(crawlers_dir, fname)
        spec = importlib.util.spec_from_file_location(f"pc_crawler_{base}", path)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # 单个 crawler 坏不掉整个注册
            print(f"[crawler_registry] skip {base}: {e}")
            continue

        meta = getattr(mod, "__crawler__", None)
        if isinstance(meta, dict):
            name = meta.get("name", base)
            source = meta.get("source", "free")
            enabled = bool(meta.get("enabled", True))
        else:
            name, source, enabled = base, "free", True

        # config 二次覆盖
        if isinstance(config, dict):
            c = (config.get("crawlers") or {}).get(name)
            if isinstance(c, dict) and "enabled" in c:
                enabled = bool(c["enabled"])

        crawl_fn = getattr(mod, "crawl", None)
        found[name] = {
            "name": name,
            "source": source,
            "enabled": enabled,
            "module_path": path,
            "crawl_fn": crawl_fn if callable(crawl_fn) else None,
        }
    return found


def run_crawler(name: str, meta: dict) -> list[str]:
    """执行单个 crawler 的 crawl()，返回 URI 列表；异常时返回 [] 不中断全局。"""
    fn = meta.get("crawl_fn")
    if fn is None:
        print(f"[crawler_registry] {name}: no crawl() function, skipped")
        return []
    try:
        result = fn()
        if not isinstance(result, list):
            result = list(result)
        return [u for u in result if isinstance(u, str)]
    except Exception as e:
        print(f"[crawler_registry] {name} crawl failed: {e}")
        return []


def run_all(
    crawlers_dir: str,
    config: Optional[dict] = None,
) -> list[str]:
    """发现 + 过滤 + 全部执行，返回合并去重后的 URI 列表。"""
    metas = discover_crawlers(crawlers_dir, config)
    out: list[str] = []
    for name, meta in metas.items():
        if not meta["enabled"]:
            print(f"[crawler_registry] {name}: disabled, skip")
            continue
        out.extend(run_crawler(name, meta))
    # 去重保序
    seen = set()
    dedup = []
    for u in out:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    return dedup


if __name__ == "__main__":
    import sys

    d = sys.argv[1] if len(sys.argv) > 1 else "crawlers"
    cfg = None
    if len(sys.argv) > 2:
        with open(sys.argv[2], "r", encoding="utf-8") as f:
            cfg = json.load(f)
    for uri in run_all(d, cfg):
        print(uri)
