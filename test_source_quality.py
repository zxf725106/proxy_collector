# -*- coding: utf-8 -*-
"""source_quality 本地自测（不触网，纯合成数据验证打分/稀有度/自动降权/持久化）。

运行： python test_source_quality.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import source_quality as sq

PASS = FAIL = 0


def check(c, m):
    global PASS, FAIL
    if c:
        PASS += 1
        print(f"  ok  - {m}")
    else:
        FAIL += 1
        print(f"  FAIL- {m}")


def _tmp_state():
    fd, p = tempfile.mkstemp(suffix=".json", prefix="sq_test_")
    os.close(fd)
    os.remove(p)  # 让 tracker 自己建
    return p


# 1. 样本不足时权重中性(=1.0)，不误杀
st = _tmp_state()
t = sq.SourceQualityTracker(st)
t.begin_run()
t.add_source_batch("newsr", ["vmess://a", "vmess://b"])
t.add_verification("newsr", ["vmess://a"], 200)
t.finish_run()
check(abs(t.weight("newsr") - 1.0) < 1e-9, "样本不足时权重中性=1.0")
check(t.is_enabled("newsr"), "样本不足时源仍启用")

# 2. 高质量源：高通过率 -> 权重高且启用
st = _tmp_state()
t = sq.SourceQualityTracker(st)
t.begin_run()
t.add_source_batch("good", [f"vmess://n{i}" for i in range(250)])
t.add_verification("good", [f"vmess://n{i}" for i in range(200)], 250)
t.finish_run()
w = t.weight("good")
check(0.5 < w <= 1.0, f"高质量源权重较高 ({w:.3f})")
check(t.is_enabled("good"), "高质量源启用")
check(t.sources["good"]["pass_rate_ema"] > 0.7, "通过率 EMA 反映高通过率")

# 3. 垃圾源：低通过率 + 样本充足 -> 自动停用(权重=0)
st = _tmp_state()
t = sq.SourceQualityTracker(st)
t.begin_run()
t.add_source_batch("junk", [f"vmess://j{i}" for i in range(250)])
t.add_verification("junk", [f"vmess://j{i}" for i in range(5)], 1400)  # 通过率 2%
t.finish_run()
check(t.weight("junk") == 0.0, "垃圾源权重=0(自动停用)")
check(not t.is_enabled("junk"), "垃圾源被停用")
check("junk" in t.disabled_sources(), "disabled_sources() 含 junk")
check(t.sources["junk"]["pass_rate_ema"] < 0.05, "垃圾源通过率 EMA 极低")

# 4. 稀有度(新鲜度)信号：共享多的节点 uniqueness 低
st = _tmp_state()
t = sq.SourceQualityTracker(st)
t.begin_run()
# 3 个源都含 u1 -> u1 出现在 3 个源，不稀有；u2/u3/u4 仅各自源独有 -> 稀有
t.add_source_batch("A", ["vmess://u1", "vmess://u2"])
t.add_source_batch("B", ["vmess://u1", "vmess://u3"])
t.add_source_batch("C", ["vmess://u1", "vmess://u4"])
t.finish_run()
uniq_a = t.sources["A"]["uniqueness_ema"]
check(abs(uniq_a - 0.5) < 1e-6, f"A 的稀有度=0.5 (u1共享,u2独有) 实际={uniq_a:.3f}")

# 5. 持久化：跨实例学习
st = _tmp_state()
t1 = sq.SourceQualityTracker(st)
t1.begin_run()
t1.add_source_batch("persist", [f"vmess://p{i}" for i in range(250)])
t1.add_verification("persist", [f"vmess://p{i}" for i in range(180)], 300)
t1.finish_run()
t2 = sq.SourceQualityTracker(st)  # 重新加载
check("persist" in t2.sources, "状态文件已落盘并重新加载")
check(t2.sources["persist"]["passed_total"] >= 180, "重新加载后累计统计保留")

# 6. report() 返回可读文本
out = sq.SourceQualityTracker(st).report()
check(isinstance(out, str) and "WEIGHT" in out, "report() 输出质量榜文本")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
