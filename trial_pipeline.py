# -*- coding: utf-8 -*-
"""
trial_pipeline.py — 代理池「质量闭环」试跑脚本
====================================================================
把本轮新加的两个能力接成一条可跑的链路，验证「来源质量打分 + 自动降权」
真的会按源质量筛源。

  python trial_pipeline.py --simulate
      用贴近真实的合成数据，模拟 3 次运行，演示 SourceQualityTracker 如何
      学习→降权→自动停用垃圾源，并展示下次调度时 is_enabled() 如何跳过被降权的源。
      无需网络，立刻可跑。

  python trial_pipeline.py --live [--config discovery_config.json]
      真跑：source_discovery 抓取 → 按源分组 → （此处接你现有的
      去重/TCP/mihomo 验证）→ SourceQualityTracker 结算。
      需要能访问 github.com 的网络（你本机走 Clash 7897）。
      验证环节默认留接入点（见 live_run），接上 speed_test/http_test 即可全自动。

无论哪种模式，结尾都会打印：质量榜 + 降权建议 + 下次调度该跳过的源清单。
"""
from __future__ import annotations

import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from source_quality import SourceQualityTracker  # noqa: E402

QUALITY_STATE = os.path.join(HERE, "source_quality.json")


# --------------------------------------------------------------------------
# 合成数据：贴近真实的来源画像
# --------------------------------------------------------------------------
def _sim_run(seed: int):
    """返回 (collected_by_source, verified_by_source)。

    collected_by_source: {源名: [原始节点URI ...]}
    verified_by_source : {源名: ([验证通过URI ...], 平均时延ms)}

    贴近真实：免费源之间抄同一批「公共垃圾节点」(shared_pool)，这些节点
    出现在 3 个免费源里 → 不稀有 → 拉低免费源的 uniqueness 权重。
    """
    paid_main = [f"vless://paid{i}@hk.example" for i in range(300)]   # 付费主源：高质量、全独占

    shared_pool = [f"ss://shared{i}@sg.example" for i in range(150)]  # 公共垃圾节点（被多个免费源抄）
    free_agg = shared_pool + [f"vmess://agg{i}@jp.example" for i in range(150)]   # 一半共享、一半独有
    free_big = shared_pool + [f"ss://big{i}@sg.example" for i in range(150)]
    free_other = shared_pool + [f"trojan://oth{i}@sg.example" for i in range(150)]

    collected = {
        "paid-main": paid_main,
        "free-agg": free_agg,
        "free-bigrepo": free_big,
        "free-other": free_other,
    }

    rnd = random.Random(seed)

    def _pass(uris, rate):
        k = int(len(uris) * rate)
        return rnd.sample(uris, k)

    verified = {
        "paid-main": (_pass(paid_main, 0.80), 260),    # 80% 过，时延低
        "free-agg": (_pass(free_agg, 0.30), 620),      # 30% 过，时延中
        "free-bigrepo": (_pass(free_big, 0.02), 1400), # 2% 过，时延极高
        "free-other": (_pass(free_other, 0.10), 900),  # 10% 过，时延偏高
    }
    return collected, verified


def run_simulate(runs: int = 3):
    print("=" * 64)
    print(f" SIMULATE 模式：模拟 {runs} 次真实运行，喂给 SourceQualityTracker")
    print("=" * 64)

    tracker = SourceQualityTracker(QUALITY_STATE)
    # 清掉历史，保证可重复演示
    if os.path.exists(QUALITY_STATE):
        os.remove(QUALITY_STATE)
    tracker = SourceQualityTracker(QUALITY_STATE)

    for i in range(1, runs + 1):
        collected, verified = _sim_run(seed=1000 + i)
        tracker.begin_run()
        for name, uris in collected.items():
            tracker.add_source_batch(name, uris)
        for name, (verified_uris, lat) in verified.items():
            tracker.add_verification(name, verified_uris, lat)
        tracker.finish_run()
        print(f"\n--- 第 {i} 次运行后质量榜 ---")
        print(tracker.report())

    print("\n" + "=" * 64)
    print(" 下次调度：is_enabled() 该跳过谁？")
    print("=" * 64)
    for name in ["paid-main", "free-agg", "free-bigrepo", "free-other"]:
        flag = "✓ 跑" if tracker.is_enabled(name) else "✗ 跳过"
        print(f"  {name:<14} {flag}")
    skip = [n for n in ["paid-main", "free-agg", "free-bigrepo", "free-other"] if not tracker.is_enabled(n)]
    print("\n自动降权命中（已写进 source_quality.json，下次运行自动跳过）：",
          skip if skip else "无")


# --------------------------------------------------------------------------
# 真跑：接你现有的发现 + 验证链路
# --------------------------------------------------------------------------
def run_live(config_path: str):
    print("=" * 64)
    print(" LIVE 模式：真跑 source_discovery + commit-watch")
    print("=" * 64)
    try:
        import source_discovery  # noqa: F401
    except Exception as e:  # pragma: no cover
        print(f" 无法导入 source_discovery: {e}")
        return

    cfg = {}
    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    # 1) 发现（GitHub + commit-watch 已在 discover_all 内按 config 接入）
    urls = source_discovery.discover_all(cfg)
    print(f" 发现订阅/代理链接共 {len(urls)} 条")

    # 2) 按源分组：discover_all 当前返回扁平列表，真实接入时应在各 discoverer 内
    #    分别 add_source_batch（见 source_discovery.GitHubDiscoverer / GitHubCommitWatcher）。
    #    这里演示把发现量登记进 tracker：
    tracker = SourceQualityTracker(QUALITY_STATE)
    tracker.begin_run()
    tracker.add_source_batch("discovery-live", urls)
    print(" 已登记本次发现量。下一步：接你的去重/TCP/mihomo 验证，")
    print(" 对通过节点调用 tracker.add_verification(name, verified, lat)，再 finish_run()。")
    print(" （验证需要网络，且接 speed_test/http_test；本脚本不擅自伪造验证结果。）")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--simulate"
    if mode == "--simulate":
        run_simulate(runs=3)
    elif mode == "--live":
        cfg = None
        if "--config" in sys.argv:
            cfg = sys.argv[sys.argv.index("--config") + 1]
        run_live(cfg)
    else:
        print("用法: python trial_pipeline.py [--simulate|--live] [--config discovery_config.json]")
        sys.exit(2)


if __name__ == "__main__":
    main()
