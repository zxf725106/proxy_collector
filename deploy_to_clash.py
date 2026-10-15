#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 output-filtered-fast.yaml 部署为 Clash Verge 的 local profile 并设为当前。

改进点（相对旧版）：
- 用 YAML 正确解析 profiles.yaml，避免文本拼接出错
- 部署前删除旧的同名自动部署条目（name==output-filtered-fast.yaml），防止越堆越多
- AUTO 默认选中节点动态取 Top1（proxies[0]），避免写死节点导致的悬空引用
- 同步清理被删除条目对应的 profiles/<uid>.yaml 孤儿文件
- **全部逻辑收进 main() + __main__ 守卫**：旧版在 import 期就执行部署，任何工具只要
  import 这个模块就会真的改写并切换你的 Clash 配置
- 路径可用环境变量覆盖（PCOLL_FAST / PCOLL_CLASH_BASE），便于在临时目录里自测
- profiles.yaml 改为原子写 + 占用重试
"""
import os
import secrets
import shutil
import string
import sys
import time

import yaml

from atomic import dump_yaml_atomic

DEFAULT_SRC = r"C:\Users\zxf72\proxy_collector\output-filtered-fast.yaml"
DEFAULT_BASE = r"C:\Users\zxf72\AppData\Roaming\io.github.clash-verge-rev.clash-verge-rev"
AUTO_NAME = "output-filtered-fast.yaml"   # 自动部署条目标记


def _local_missing(item: dict, profile_dir: str) -> bool:
    """local 条目的文件是否已缺失（用于悬空条目自愈）。"""
    if item.get("type") != "local":
        return False
    fn = item.get("file")
    return bool(fn) and not os.path.exists(os.path.join(profile_dir, fn))


def main() -> int:
    src = os.environ.get("PCOLL_FAST", DEFAULT_SRC)
    base = os.environ.get("PCOLL_CLASH_BASE", DEFAULT_BASE)
    profile_dir = os.path.join(base, "profiles")
    profiles_yaml = os.path.join(base, "profiles.yaml")

    # 0. 读取源，取 Top1 节点名
    if not os.path.exists(src):
        print(f"[ERROR] 找不到高速精选 {src}，拒绝部署", flush=True)
        return 2
    with open(src, encoding="utf-8") as f:
        src_doc = yaml.safe_load(f) or {}
    proxies = src_doc.get("proxies", []) or []
    # 质量门：部署脚本不再把未经过速度筛选的 HTTP 候选冒充高速精选，
    # 也不生成空 profile。失败时由 run_pipeline 负责回滚上一版。
    if not proxies:
        print("[ERROR] 高速精选为空，拒绝部署；请检查测速结果或上一版备份", flush=True)
        return 2
    top1 = proxies[0]["name"]
    n_proxies = len(proxies)

    if not os.path.isdir(profile_dir) or not os.path.exists(profiles_yaml):
        print(f"[ERROR] Clash Verge 配置目录不可用（profiles/ 或 profiles.yaml 缺失）: {base}",
              flush=True)
        return 3

    # 1. 备份（失败不致命：备份只是兜底）
    try:
        shutil.copy(profiles_yaml, profiles_yaml + ".bak")
    except Exception as e:
        print(f"[WARN] profiles.yaml 备份失败(继续): {e}", flush=True)

    # 2. 读取现有 profiles.yaml
    with open(profiles_yaml, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        print(f"[ERROR] profiles.yaml 结构异常（不是映射），拒绝部署: {profiles_yaml}", flush=True)
        return 3

    items = doc.get("items", []) or []

    # 复用已存在的自动部署条目 uid（关键修复）：
    #   旧逻辑每次生成新 uid 并删除旧文件；而 Clash Verge 会用其内存里的旧状态重写
    #   profiles.yaml，于是留下「条目在、文件没了」的悬空引用，点该 profile 即报
    #   "failed to read current profile <uid>"。改为同一条目名沿用同一 uid、原地覆盖
    #   文件，并做一次悬空条目自愈。
    _auto = [it for it in items if it.get("name") == AUTO_NAME]
    if _auto:
        uid = _auto[0]["uid"]
    else:
        uid = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))

    # 悬空条目自愈：local 条目若其文件已缺失，直接移除（避免下次读取报错 / 被 Clash Verge 顶成死引用）
    _orphans = [it for it in items if _local_missing(it, profile_dir) and it.get("uid") != uid]
    if _orphans:
        print("SELFHEAL_REMOVED=" + str([it.get("name") or it.get("uid") for it in _orphans]))
    kept = [it for it in items
            if it.get("name") != AUTO_NAME and not _local_missing(it, profile_dir)]

    # 3. 复制配置（沿用同一 uid -> 原地覆盖，不再产生孤儿文件，也不会留下悬空引用）
    shutil.copy(src, os.path.join(profile_dir, uid + ".yaml"))

    # 4. 追加新条目
    ts = int(time.time())
    new_item = {
        "uid": uid,
        "type": "local",
        "name": AUTO_NAME,
        "file": uid + ".yaml",
        "desc": f"Auto-deployed high-speed nodes ({n_proxies}, {time.strftime('%Y-%m-%d')})",
        "selected": [
            {"name": "PROXY", "now": "AUTO"},
            {"name": "AUTO", "now": top1},
        ],
        "updated": ts,
    }
    kept.append(new_item)
    doc["items"] = kept
    doc["current"] = uid

    dump_yaml_atomic(profiles_yaml, doc, dumper=yaml.SafeDumper)

    # 5. 不再删除任何 profile 文件（旧逻辑删除会导致悬空引用）；
    #    孤儿文件由上面的「条目侧自愈」处理，且本轮起已不再生成新的孤儿文件。

    print("DEPLOY_UID=" + uid)
    print("TOP1=" + top1)
    print("ORPHANS_HEALED=" + str([it.get("name") or it.get("uid") for it in _orphans]))
    print("BACKUP=" + profiles_yaml + ".bak")
    print("COPIED=" + os.path.join(profile_dir, uid + ".yaml"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
