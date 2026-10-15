# -*- coding: utf-8 -*-
"""
source_quality.py — 来源质量打分 / 自学习降权
================================================================================

解决「源质量都不咋地」的病根：不是爬更多同质性来源，而是让 pipeline 自己学会
**哪个源真的产出可用节点**，动态加权、自动降权垃圾源。

为什么有效
----------
免费公网代理是「必输游戏」：大家都爬那几个知名仓库，IP 几小时内被用烂，
通过率天然低。破局点在于——把采集来的节点按「来源」打标签，喂给现有
去重/TCP/mihomo 验证链路，事后统计每个来源的：

  * pass_rate  : 历史通过率（验证后存活比例），EMA 平滑 —— 核心信号
  * uniqueness  : 稀有度 = 该源节点中「只在 <=2 个源出现」的比例（新鲜度代理）
                  （一个节点在 10 个源都出现 = 已锈；只在 1~2 个源出现 = 偏新）
  * latency     : 平均时延（越低越好），EMA 平滑
  * samples     : 累计样本量（样本不足时不急着惩罚，维持中性权重）

质量权重（0~1）：
    weight = 0.6 * pass_rate + 0.3 * uniqueness + 0.1 * latency_factor
  * 样本不足(<MIN_SAMPLES)时 weight 维持 1.0（中性，避免误杀新源）
  * pass_rate 极低且样本充足时自动 disabled（权重置 0，建议停用）

状态持久化到 JSON，跨多次运行累积学习。

集成方式（在你主链路「验证后」调用）
------------------------------------
    from source_quality import SourceQualityTracker

    tracker = SourceQualityTracker("source_quality.json")
    tracker.begin_run()
    for name, uris in collected_by_source.items():   # 原始采集（含付费订阅名）
        tracker.add_source_batch(name, uris)
    # ... 跑现有 去重 -> TCP 预筛 -> mihomo HTTP 验证 ...
    for name, (verified, avg_lat) in verified_by_source.items():
        tracker.add_verification(name, verified, avg_lat)
    tracker.finish_run()
    print(tracker.report())                          # 质量榜 + 降权建议

    # 下次调度前：用权重决定要不要跑某源
    if tracker.is_enabled("proxifly"):
        ...

依赖：仅标准库。不触网、可单测。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable, Optional


# ----------------------------- 可调参数 --------------------------------------
MIN_SAMPLES = 200          # 累计采集样本达到这个数才敢惩罚/奖励（之前维持中性）
RARITY_MAX_SOURCES = 2     # 出现在 <= 该数量的源里，算「稀有/新鲜」
PASS_ALPHA = 0.3           # 通过率 EMA 系数（越大越跟近期）
UNIQ_ALPHA = 0.3          # 稀有度 EMA 系数
LAT_ALPHA = 0.3           # 时延 EMA 系数
DISABLE_PASS_RATE = 0.05  # 历史通过率低于此且样本充足 -> 自动停用
REENABLE_AFTER_RUNS = 8   # 被自动停用满这么多轮后，重新放行一次做观察（避免误杀永久化）
GOOD_LATENCY_MS = 300     # 时延因子=1 的阈值（优秀）
BAD_LATENCY_MS = 1500     # 时延因子=0 的阈值（不可接受）
WEIGHT_DISABLE_THRESHOLD = 0.05  # 权重低于此视为实际禁用（建议调度时跳过）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class SourceQualityTracker:
    """跨运行累积每个来源的采集/验证统计，计算质量权重并自动降权。"""

    def __init__(self, state_path: str = "source_quality.json"):
        self.state_path = state_path
        self.sources: dict[str, dict] = {}
        self._load()

        # 单次运行内的缓冲区
        self._run_collected: dict[str, set] = {}
        self._run_verified: dict[str, set] = {}
        self._run_latency: dict[str, float] = {}
        self._run_active = False
        self._reenabled: list[str] = []

    # --------------------------- 持久化 ------------------------------------
    def _load(self) -> None:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.sources = data.get("sources", {})
            except Exception:
                self.sources = {}
        # 确保结构
        for st in self.sources.values():
            st.setdefault("collected_total", 0)
            st.setdefault("passed_total", 0)
            st.setdefault("pass_rate_ema", 0.0)
            st.setdefault("uniqueness_ema", 0.0)
            st.setdefault("avg_latency_ema", 0.0)
            st.setdefault("runs", 0)
            st.setdefault("enabled", True)
            st.setdefault("disabled_at_run", 0)
            st.setdefault("probation", False)
            st.setdefault("last_seen", "")

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": _now(), "sources": self.sources}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)  # 原子写，避免半截文件

    # --------------------------- 单次运行接口 ------------------------------
    def begin_run(self) -> None:
        self._run_collected = {}
        self._run_verified = {}
        self._run_latency = {}
        self._run_active = True
        self._reenabled = []

    def add_source_batch(self, source_name: str, uris: Iterable[str]) -> None:
        """记录某来源本次「原始采集」到的节点 URI（用于统计通过率分母 + 稀有度）。"""
        s = set(u for u in uris if isinstance(u, str))
        self._run_collected.setdefault(source_name, set()).update(s)
        if source_name not in self.sources:
            self._init_source(source_name)

    def add_verification(self, source_name: str, verified_uris: Iterable[str],
                         avg_latency_ms: Optional[float] = None) -> None:
        """记录某来源本次「验证通过」的节点 URI 与平均时延。"""
        s = set(u for u in verified_uris if isinstance(u, str))
        self._run_verified.setdefault(source_name, set()).update(s)
        if avg_latency_ms is not None:
            self._run_latency[source_name] = float(avg_latency_ms)
        if source_name not in self.sources:
            self._init_source(source_name)

    def finish_run(self) -> None:
        """结算单次运行：算稀有度、更新 EMA、判断是否自动停用、落盘。"""
        if not self._run_active:
            raise RuntimeError("finish_run() 前需先 begin_run()")

        # 1) 跨源稀有度：uri -> 出现的来源集合
        uri_to_sources: dict[str, set] = {}
        for name, uris in self._run_collected.items():
            for u in uris:
                uri_to_sources.setdefault(u, set()).add(name)

        total_sources = max(1, len(self._run_collected))

        # 2) 逐源更新（仅此一份逻辑，避免 EMA/样本被重复计算）
        for name in set(self._run_collected) | set(self._run_verified):
            st = self.sources.setdefault(name, self._init_source(name))
            collected = self._run_collected.get(name, set())
            verified = self._run_verified.get(name, set())

            n_col = len(collected)
            n_pass = len(verified)
            first = st["runs"] == 0

            # 通过率（首次出现直接用观测值，否则 EMA 平滑）
            if n_col > 0:
                pass_obs = n_pass / n_col
                st["pass_rate_ema"] = pass_obs if first else \
                    (1 - PASS_ALPHA) * st["pass_rate_ema"] + PASS_ALPHA * pass_obs
                st["collected_total"] += n_col
                st["passed_total"] += n_pass

            # 稀有度：该源节点中「出现在 <=RARITY_MAX_SOURCES 个源」的比例（新鲜度代理）
            if n_col > 0:
                rare = sum(1 for u in collected if len(uri_to_sources.get(u, {name})) <= RARITY_MAX_SOURCES)
                uniq_obs = rare / n_col
                st["uniqueness_ema"] = uniq_obs if first else \
                    (1 - UNIQ_ALPHA) * st["uniqueness_ema"] + UNIQ_ALPHA * uniq_obs

            # 时延（首次出现直接用观测值；后续 EMA）
            if name in self._run_latency:
                lat = self._run_latency[name]
                if st["avg_latency_ema"] <= 0:
                    st["avg_latency_ema"] = lat
                else:
                    st["avg_latency_ema"] = (1 - LAT_ALPHA) * st["avg_latency_ema"] + LAT_ALPHA * lat

            st["runs"] += 1
            st["last_seen"] = _now()

            # 4) 恢复观察期：被停用满 REENABLE_AFTER_RUNS 轮的源重新放行一次。
            #    原实现只会写 enabled=False，全项目没有任何代码路径能改回 True ——
            #    一旦某轮网络异常把源误判成垃圾就永久停用（实测 41 个源里 31 个已被停用）。
            if not st.get("enabled", True):
                if st["runs"] - st.get("disabled_at_run", 0) >= REENABLE_AFTER_RUNS:
                    st["enabled"] = True
                    st["probation"] = True
                    st["collected_total"] = 0   # 重新累计样本，避免用同一批旧数据立刻再停用
                    self._reenabled.append(name)

            # 5) 自动停用：样本充足且通过率极低
            if (st.get("enabled", True) and st["collected_total"] >= MIN_SAMPLES
                    and st["pass_rate_ema"] < DISABLE_PASS_RATE):
                st["enabled"] = False
                st["disabled_at_run"] = st["runs"]
                st["probation"] = False

        self._save()
        self._run_active = False

    # --------------------------- 查询/报告 ----------------------------------
    def weight(self, source_name: str) -> float:
        """返回某来源当前质量权重（0~1）。样本不足返回 1.0（中性）。"""
        st = self.sources.get(source_name)
        if st is None:
            return 1.0
        if not st.get("enabled", True):
            return 0.0
        if st.get("collected_total", 0) < MIN_SAMPLES:
            return 1.0
        lat = st.get("avg_latency_ema", 0.0)
        if lat <= 0:
            lat_factor = 1.0
        else:
            lat_factor = _clamp(1.0 - (lat - GOOD_LATENCY_MS) / (BAD_LATENCY_MS - GOOD_LATENCY_MS))
        w = (0.6 * st["pass_rate_ema"]
             + 0.3 * st["uniqueness_ema"]
             + 0.1 * lat_factor)
        return _clamp(w)

    def is_enabled(self, source_name: str) -> bool:
        """调度时判断：是否还要跑这个源。"""
        st = self.sources.get(source_name)
        if st is None:
            return True
        if not st.get("enabled", True):
            return False
        return self.weight(source_name) >= WEIGHT_DISABLE_THRESHOLD

    def score(self) -> list[dict]:
        """返回按权重降序排列的来源质量榜。"""
        rows = []
        for name, st in self.sources.items():
            rows.append({
                "name": name,
                "weight": round(self.weight(name), 3),
                "pass_rate": round(st.get("pass_rate_ema", 0.0), 3),
                "uniqueness": round(st.get("uniqueness_ema", 0.0), 3),
                "avg_latency_ms": round(st.get("avg_latency_ema", 0.0), 1),
                "collected_total": st.get("collected_total", 0),
                "passed_total": st.get("passed_total", 0),
                "runs": st.get("runs", 0),
                "enabled": st.get("enabled", True),
                "auto_disabled": (not st.get("enabled", True)),
                "probation": st.get("probation", False),
            })
        rows.sort(key=lambda r: (r["weight"], r["pass_rate"]), reverse=True)
        return rows

    def disabled_sources(self) -> list[str]:
        """返回建议停用的来源名（自动降权命中）。"""
        return [r["name"] for r in self.score() if r["auto_disabled"] or r["weight"] < WEIGHT_DISABLE_THRESHOLD]

    def report(self) -> str:
        """生成可读的质量榜 + 降权建议文本。"""
        rows = self.score()
        if not rows:
            return "（暂无来源统计，先跑一次 pipeline 再调用 report）"
        line = f"{'SOURCE':<22}{'WEIGHT':>7}{'PASS%':>7}{'RARE%':>7}{'LAT_ms':>9}{'SAMPLES':>9}  STATE"
        out = [line, "-" * len(line)]
        for r in rows:
            state = "OFF" if r["auto_disabled"] else ("试用" if r["probation"] else "on")
            out.append(f"{r['name']:<22}{r['weight']:>7.2f}{r['pass_rate']*100:>6.1f}%"
                       f"{r['uniqueness']*100:>6.1f}%{r['avg_latency_ms']:>9.0f}"
                       f"{r['collected_total']:>9}  {state}")
        dis = self.disabled_sources()
        out.append("")
        if getattr(self, "_reenabled", None):
            out.append(f"本轮恢复观察期（停用满 {REENABLE_AFTER_RUNS} 轮）: "
                       + ", ".join(self._reenabled))
        if dis:
            out.append("建议停用（自动降权）：" + ", ".join(dis))
        else:
            out.append("无来源触发自动停用阈值。")
        return "\n".join(out)

    # --------------------------- 内部 --------------------------------------
    def _init_source(self, name: str) -> dict:
        st = self.sources.get(name)
        if st is None:
            st = {
                "collected_total": 0, "passed_total": 0,
                "pass_rate_ema": 0.0, "uniqueness_ema": 0.0,
                "avg_latency_ema": 0.0, "runs": 0,
                "enabled": True, "last_seen": _now(),
            }
            self.sources[name] = st
        return st


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "source_quality.json"
    t = SourceQualityTracker(path)
    print(t.report())
