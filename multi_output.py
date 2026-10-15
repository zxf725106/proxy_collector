#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi_output.py — subconverter 多格式输出（clash/singbox/v2ray …）
=================================================================
本机没装 subconverter 时，clash 由 proxy_collector 现有 output.py 出，
这里只补「clash 之外」的格式，没装就安静跳过，不阻断主流程。

用法：
  python multi_output.py --fast output-filtered-fast.yaml --tag desktop
  python multi_output.py --fast output-filtered-fast.yaml --targets clash,singbox,v2ray
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description="多格式输出（subconverter 封装）")
    ap.add_argument("--fast", default="output-filtered-fast.yaml")
    ap.add_argument("--tag", default="multi")
    ap.add_argument("--outdir", default=os.path.join(HERE, "candidates"))
    ap.add_argument("--targets", default="singbox,v2ray",
                    help="需要额外生成的格式（逗号分隔）。clash 总是由 FAST 文件本体承担。")
    ap.add_argument("--proxy", default="http://127.0.0.1:7897",
                    help="subconverter 自检 / 下载用代理（默认 7897）")
    args = ap.parse_args()

    if not os.path.exists(args.fast):
        print(f"!! 找不到 {args.fast}，跳过")
        return 1

    import yaml
    with open(args.fast, "r", encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    proxies = d.get("proxies", []) or []
    if not proxies:
        print("!! FAST 节点为空，跳过多格式输出")
        return 0

    # clash 节点 dict -> 通用 URI
    try:
        from subscription_parser import node_to_uri
    except Exception as e:
        print(f"!! 导入 subscription_parser 失败: {e}")
        return 1

    uris = []
    for p in proxies:
        try:
            u = node_to_uri(p)
        except Exception:
            u = None
        if u:
            uris.append(u)
    print(f"FAST 节点 {len(proxies)} -> 可转 URI {len(uris)}")

    try:
        from multi_format_output import find_subconverter, convert
    except Exception as e:
        print(f"!! 导入 multi_format_output 失败: {e}")
        return 1

    sc = find_subconverter()
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    if sc is None:
        print(f"!! 本机未找到 subconverter，跳过 {targets}（仅 clash 已由 FAST 文件提供）")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    out = convert(uris, targets=targets, outdir=args.outdir, tag=args.tag, subconverter_bin=sc)
    for k, v in out.items():
        print(f"  {k:8s} -> {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
