# -*- coding: utf-8 -*-
"""原子写 + 抗文件占用的替换重试 + 运行期临时目录。

为什么需要：
1) Windows 上 ``os.replace`` 会因目标文件正被其他进程打开（Clash 客户端、编辑器、
   杀软扫描）抛 ``PermissionError``。原先各脚本让异常直接冒泡，``run_pipeline`` 只能
   以 ``SystemExit(1)`` 结束整轮（日志里已真实发生过：make_mobile 因
   output-filtered-fast-mobile.yaml 被占用而中止整条流水线）。
2) ``speed_test``/``http_test`` 原先用 ``tempfile.mkstemp(dir=项目根)``，被中断时会把
   ``tmp*.tmp`` 漏在项目根（2026-09-12、2026-10-15 各留下 8 个，mtime 与中断时刻吻合）。

这里统一为「重试 + 退避」与「临时文件放系统临时区」，把瞬时占用降级成可恢复的等待。
"""
from __future__ import annotations

import os
import tempfile
import time

REPLACE_ATTEMPTS = 8
REPLACE_DELAY = 0.4


def replace_with_retry(src: str, dst: str, attempts: int = REPLACE_ATTEMPTS,
                       delay: float = REPLACE_DELAY, log=None) -> None:
    """``os.replace`` 的占用容忍版本；重试用尽后仍抛 PermissionError（不静默丢数据）。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            if i > 1 and log:
                log(f"   (第 {i} 次尝试替换成功: {os.path.basename(dst)})")
            return
        except PermissionError as e:      # 文件被占用
            last = e
            if i < attempts:
                if log and i == 1:
                    log(f"   {os.path.basename(dst)} 被其他进程占用，等待重试…")
                time.sleep(delay * i)
    raise last


def write_text_atomic(path: str, text: str, log=None,
                      attempts: int = REPLACE_ATTEMPTS) -> None:
    """同目录临时文件 + fsync + 带重试的 ``os.replace``（保留原有换行行为）。"""
    path = os.path.abspath(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, path, attempts=attempts, log=log)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def dump_yaml_atomic(path: str, data, dumper=None, log=None,
                     sort_keys: bool = False) -> None:
    """yaml 序列化后原子写入（与 ``output._SafeDumper`` 用法兼容）。"""
    import yaml

    text = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                     sort_keys=sort_keys, Dumper=dumper)
    write_text_atomic(path, text, log=log)


def remove_quiet(*paths: str, log=None) -> int:
    """尽力删除（清理临时文件用）。失败只记一行，绝不抛异常。

    注意：在受管沙箱里 os.remove 可能被 safe-delete 拦截而失败，这里必须容忍。
    """
    removed = 0
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
                removed += 1
        except Exception as e:                      # noqa: BLE001 - 清理不应影响主流程
            if log:
                log(f"   (清理失败，忽略: {os.path.basename(p)}: {e})")
    return removed


def tempdir_for_run(sub: str = "") -> str:
    """运行期工作目录：放系统临时区，避免中断时把临时文件漏在项目根。"""
    base = os.path.join(tempfile.gettempdir(), "proxy_collector")
    if sub:
        base = os.path.join(base, sub)
    os.makedirs(base, exist_ok=True)
    return base


def cleanup_stale_temp(project_dir: str, log=None) -> int:
    """清理历史遗留的项目根临时文件（speed_test/http_test 旧版本漏下的）。"""
    import glob

    patterns = ["tmp*.tmp", "_http_test_config.yaml", "_mihomo_start.log",
                "_speed_tmp.yaml", "_diag_tmp.yaml", ".*.yaml.*.tmp"]
    victims = []
    for pat in patterns:
        victims.extend(glob.glob(os.path.join(project_dir, pat)))
    # 只删上次运行留下的（>10 分钟），避免删掉当前正在跑的实例的临时文件
    cutoff = time.time() - 600
    stale = [p for p in victims if os.path.isfile(p) and os.path.getmtime(p) < cutoff]
    n = remove_quiet(*stale, log=log)
    if n and log:
        log(f"   已清理 {n} 个历史遗留临时文件")
    return n
