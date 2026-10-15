"""节点真实可用性测试 - 通过 mihomo 核心的延迟测试 API 做 HTTP 实测

原理：启动一个独立的 mihomo 实例加载所有节点，然后调用
GET /proxies/{name}/delay?url=...&timeout=...
让 mihomo 真正通过每个节点发起 HTTP 请求，验证其能否实际代理流量。
这比单纯 TCP 连通性测试准确得多——能过滤掉"端口开着但协议层失败"的节点。
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
import yaml

# 复用 output.py 的 _SafeDumper 和 _defuse_shortid，确保 short-id 等字段被正确加引号
from atomic import dump_yaml_atomic, remove_quiet, tempdir_for_run, write_text_atomic
from output import _SafeDumper, _defuse_shortid

logger = logging.getLogger(__name__)

# mihomo 核心路径（Clash Verge 自带）
MIHOMO_PATHS = [
    r"C:\Program Files\Clash Verge\verge-mihomo.exe",
    r"C:\Program Files\Clash Verge Rev\verge-mihomo.exe",
    r"C:\Users\zxf72\AppData\Local\Programs\Clash Verge\verge-mihomo.exe",
]

# 测试用 URL（生成 204 响应的轻量端点）
TEST_URL = "http://www.gstatic.com/generate_204"
# 测试超时（毫秒）：超过这个时间认为节点不可用
TEST_TIMEOUT_MS = 8000
# API 控制端口
API_PORT = 9095
API_SECRET = "proxycollector"
# 并发数
CONCURRENCY = 100

# ---- TCP 预筛（分级实测）--------------------------------------------------
# 用 collector 写下的 output-latency.json 先只测「TCP 连通」的节点，能省掉约 40% 的
# 实测时间（19830 个节点里约 58% TCP 可达）。但实测在 152 个 HTTP 可用节点中有
# 4 个（2.6%）是 TCP 探测超时、却能真正代理的，所以不是无损 —— 因此做成两阶段：
# 第一阶段存活数少于 PREFILTER_MIN_ALIVE 时，自动补测剩余节点，避免漏掉好节点。
# 环境变量 PCOLL_PREFILTER=0 可关闭预筛，PCOLL_PREFILTER_MIN_ALIVE 可调阈值。
PREFILTER_MIN_ALIVE = 120
LATENCY_FILE = "output-latency.json"
DELAYS_FILE = "outputs/http_delays.json"
# 旁路文件可信度门槛：collector 会先写旁路(22:59:01)再写 output.yaml(22:59:12)，
# 所以"比 output.yaml 新"是错的判据（实测会被自己拒掉）。改用语义判据：
#   * 超过 LATENCY_TTL_SEC 的旧文件不认
#   * 对当前节点池的覆盖率低于 LATENCY_MIN_COVERAGE 说明是别的节点集的遗留，不认
LATENCY_TTL_SEC = 24 * 3600
LATENCY_MIN_COVERAGE = 0.5


def find_mihomo() -> str | None:
    for p in MIHOMO_PATHS:
        if os.path.exists(p):
            return p
    # 尝试从 PATH 找
    found = shutil.which("verge-mihomo") or shutil.which("mihomo") or shutil.which("clash")
    return found


def prepare_test_config(src_yaml: str, dst_yaml: str, drop_names: set = None) -> int:
    """复制配置并加上 external-controller / secret，供 API 测试使用。
    去掉 proxy-groups / rules（HTTP 实测只需 proxies 列表逐个测试）。
    剔除：① SS-2022 系列 cipher（本机 mihomo 版本不支持，加载即 fatal）；
          ② 调用方指定的 drop_names（上一轮导致 mihomo 崩溃的坏节点）。
    返回写入的 proxies 数量。"""
    drop_names = drop_names or set()
    with open(src_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["external-controller"] = f"127.0.0.1:{API_PORT}"
    data["secret"] = API_SECRET
    data["allow-lan"] = False
    data["mode"] = "direct"
    # 只保留 proxies 用于逐个延迟测试，移除组/规则避免加载依赖
    data.pop("proxy-groups", None)
    data.pop("rules", None)
    data.pop("rule-providers", None)
    # 确保有 mixed-port 供 API 工作（不影响延迟测试）
    if "mixed-port" not in data and "port" not in data:
        data["mixed-port"] = 7895
    kept = []
    ss2022_dropped = 0
    for p in data.get("proxies", []):
        n = p.get("name")
        if n in drop_names:
            continue
        # 本机 mihomo 版本不支持 Shadowsocks-2022 系列 cipher，加载即崩
        if p.get("type") == "ss" and str(p.get("cipher", "")).lower().startswith("2022-"):
            ss2022_dropped += 1
            continue
        # 重新应用 short-id 引号（safe_load 后 _QuotedStr 标记已丢失，否则 mihomo 报错）
        if p.get("reality-opts"):
            p["reality-opts"] = _defuse_shortid(p["reality-opts"])
        kept.append(p)
    data["proxies"] = kept
    dump_yaml_atomic(dst_yaml, data, dumper=_SafeDumper)
    if ss2022_dropped:
        logger.warning("prepare_test_config: 剔除 %d 个不支持的 SS-2022 节点", ss2022_dropped)
    return len(kept)


async def _wait_for_api(session: aiohttp.ClientSession, base: str, headers: dict) -> bool:
    """等待 mihomo API 就绪（15k+ 节点启动较慢，最多等 180 秒）"""
    for _ in range(180):  # 最多等 180 秒
        try:
            async with session.get(f"{base}/version", headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def _test_one_node(
    session: aiohttp.ClientSession,
    base: str,
    headers: dict,
    name: str,
    semaphore: asyncio.Semaphore,
    timeout_ms: int = TEST_TIMEOUT_MS,
) -> tuple[str, float | None]:
    """对单个节点调用 mihomo 延迟测试 API（超时显式传入，避免改全局变量）"""
    async with semaphore:
        encoded = quote(name, safe="")
        url = f"{base}/proxies/{encoded}/delay?url={quote(TEST_URL, safe='')}&timeout={timeout_ms}"
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=(timeout_ms / 1000) + 5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    delay = data.get("delay")
                    if isinstance(delay, (int, float)) and delay > 0:
                        return name, float(delay)
                return name, None
        except Exception:
            return name, None


def _kill_proc(proc) -> None:
    """终止 mihomo 进程。

    原实现用 send_signal(CTRL_C_EVENT)，但 Windows 上只有以
    CREATE_NEW_PROCESS_GROUP 启动的子进程才收得到，实测经常无效 -> 改为
    terminate 失败再 kill 的兜底序列。
    """
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


async def http_test_nodes(
    config_yaml: str,
    names: list[str],
    concurrency: int = CONCURRENCY,
    timeout_ms: int = TEST_TIMEOUT_MS,
    label: str = "",
) -> dict[str, float]:
    """启动 mihomo，对给定节点做 HTTP 实测，返回 {节点名: 延迟ms}（仅成功的）。
    容错：若 mihomo 因某个坏节点（如不支持的 cipher、坏字段）启动即崩，
    根据 stderr 中的 'proxy NNNN:' 定位并剔除该节点后重试，最多 10 次。"""
    tag = f"[{label}] " if label else ""

    mihomo = find_mihomo()
    if not mihomo:
        logger.error("未找到 mihomo 核心，无法做 HTTP 实测")
        return {}

    # 临时文件放系统临时区：被中断时不再把配置/日志漏在项目根
    # （2026-09-12、2026-10-15 各留下 8 个 tmp*.tmp 与 6MB 的 _http_test_config.yaml）
    work_dir = tempdir_for_run("http")
    test_cfg = os.path.join(work_dir, "_http_test_config.yaml")
    mihomo_log = os.path.join(work_dir, "_mihomo_start.log")
    drop_names: set = set()
    results: dict[str, float] = {}

    for attempt in range(1, 11):
        n_loaded = prepare_test_config(config_yaml, test_cfg, drop_names)
        logger.info("%s启动 mihomo 核心 (尝试 %d/10, 加载 %d 节点): %s", tag, attempt, n_loaded, mihomo)
        # mihomo 的启动/解析错误写到 stdout（不是 stderr），必须把 stdout 也捕获，
        # 否则下面的"定位坏节点并重试"逻辑读不到错误信息，会直接以"未知原因"退出。
        try:
            _mh_logf = open(mihomo_log, "w", encoding="utf-8", errors="ignore")
        except Exception:
            _mh_logf = None
        proc = subprocess.Popen(
            [mihomo, "-f", test_cfg],
            stdout=_mh_logf if _mh_logf else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        base = f"http://127.0.0.1:{API_PORT}"
        headers = {"Authorization": f"Bearer {API_SECRET}"}

        # 等待 API 就绪 或 进程退出（坏节点导致 fatal）
        ready = False
        async with aiohttp.ClientSession() as session:
            for _ in range(60):
                if proc.poll() is not None:
                    break
                try:
                    async with session.get(f"{base}/version", headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    pass
                await asyncio.sleep(1)

            if proc.poll() is not None or not ready:
                # mihomo 退出/未就绪 -> 从日志文件读错误定位坏节点
                err_txt = ""
                try:
                    if _mh_logf is not None:
                        _mh_logf.flush()
                        _mh_logf.close()
                    with open(mihomo_log, "r", encoding="utf-8", errors="ignore") as _lf:
                        err_txt = _lf.read()
                except Exception:
                    err_txt = ""
                last = err_txt.strip().splitlines()[-1] if err_txt.strip() else "未知原因"
                logger.error("%smihomo 启动失败 (尝试 %d/10): %s", tag, attempt, last)
                bad_name = None
                if "proxy " in err_txt and "Parse config error" in err_txt:
                    try:
                        idx = int(err_txt.split("proxy ")[1].split(":")[0])
                        td = yaml.safe_load(open(test_cfg, encoding="utf-8"))
                        plist = td.get("proxies", [])
                        if 0 <= idx < len(plist):
                            bad_name = plist[idx].get("name")
                    except Exception:
                        bad_name = None
                _kill_proc(proc)
                if bad_name and bad_name not in drop_names and attempt < 10:
                    drop_names.add(bad_name)
                    logger.warning("剔除导致崩溃的节点: %s", bad_name)
                    continue
                logger.error("mihomo 无法启动且无法定位坏节点，终止 HTTP 实测")
                remove_quiet(test_cfg, mihomo_log, log=logger.info)
                return {}

            # API 就绪，开始实测（仅测仍在的节点）
            async with session.get(f"{base}/proxies", headers=headers) as resp:
                if resp.status == 200:
                    pdata = await resp.json()
                    logger.info("mihomo 已加载 %d 个代理", len(pdata.get("proxies", {})))
            alive_names = [n for n in names if n not in drop_names]
            semaphore = asyncio.Semaphore(concurrency)
            tasks = [_test_one_node(session, base, headers, name, semaphore, timeout_ms)
                     for name in alive_names]
            done = 0
            ok = 0
            for coro in asyncio.as_completed(tasks):
                name, delay = await coro
                done += 1
                if delay is not None:
                    results[name] = delay
                    ok += 1
                if done % 1000 == 0:
                    logger.info("HTTP 实测进度: %d/%d, 成功 %d", done, len(alive_names), ok)
            break  # 成功，跳出重试循环

    _kill_proc(proc)
    remove_quiet(test_cfg, mihomo_log, log=logger.info)
    if drop_names:
        logger.warning("本轮共剔除 %d 个导致 mihomo 崩溃的节点", len(drop_names))
    logger.info("%sHTTP 实测完成: %d/%d 节点可用", tag, len(results), len(names))
    return results


def filter_output_by_http_test(
    src_yaml: str,
    dst_yaml: str,
    delay_map: dict[str, float],
    max_delay: int = 0,
) -> int:
    """根据 HTTP 实测结果过滤 output.yaml，只保留通过测试的节点。

    HTTP 实测延迟不再写进 Clash 配置（原先会留下非标准的 `_http_delay` 字段，
    实测 output-filtered-http.yaml 里有 199 处），改为旁路写到
    outputs/http_delays.json（按节点身份指纹索引），供来源质量打分使用。
    """
    import time as _t

    from parser import proxy_identity

    with open(src_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    proxies = data.get("proxies", [])
    kept = []
    delays: dict[str, float] = {}
    for p in proxies:
        name = p.get("name")
        if name in delay_map:
            if max_delay > 0 and delay_map[name] > max_delay:
                continue
            try:
                delays[proxy_identity(p)] = delay_map[name]
            except Exception:                       # 身份算不出来不影响过滤
                pass
            # 重新应用 short-id 引号（safe_load 后 _QuotedStr 标记已丢失）
            if p.get("reality-opts"):
                p["reality-opts"] = _defuse_shortid(p["reality-opts"])
            kept.append(p)

    data["proxies"] = kept
    # 关键：同步裁剪 proxy-groups 里的节点引用，否则会指向已删除节点（悬空引用）。
    # 注意必须保留 DIRECT/REJECT 与其它代理组名：原实现只保留节点名，已把 PROXY 组的
    # 首成员 AUTO 误删（实测 output-filtered-http.yaml 的 PROXY 组丢了 AUTO）。
    kept_names = {p["name"] for p in kept}
    group_names = {g.get("name") for g in data.get("proxy-groups", []) if g.get("name")}
    special = {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY", "AUTO"}
    for g in data.get("proxy-groups", []):
        if g.get("proxies"):
            g["proxies"] = [n for n in g["proxies"]
                            if n in kept_names or n in group_names or n in special]

    dump_yaml_atomic(dst_yaml, data, dumper=_SafeDumper)

    try:
        delays_path = os.path.join(os.path.dirname(os.path.abspath(dst_yaml)), DELAYS_FILE)
        write_text_atomic(delays_path, json.dumps(
            {"ts": _t.time(), "delays": delays}, ensure_ascii=False))
        logger.info("HTTP 实测延迟旁路已写: %s (%d 条)", delays_path, len(delays))
    except Exception as e:                          # 旁路文件不是关键路径
        logger.warning("HTTP 延迟旁路写入失败(非致命): %s", e)

    logger.info("过滤完成并原子写入: %d -> %d 节点", len(proxies), len(kept))
    return len(kept)


def load_tcp_alive(latency_path: str, proxies: list[dict]) -> tuple[set, str]:
    """读取 collector 写下的 output-latency.json，返回 (TCP 连通节点名集合, 说明)。

    做了两道可信度门槛（见 LATENCY_TTL_SEC / LATENCY_MIN_COVERAGE），不达标就返回空集，
    调用方会自动退回"全量实测"，绝不会因为一份不可信的旁路而漏测节点。
    """
    if not os.path.exists(latency_path):
        return set(), f"旁路文件不存在({latency_path})"
    try:
        age = time.time() - os.path.getmtime(latency_path)
    except OSError:
        return set(), "旁路文件无法读取修改时间"
    if age > LATENCY_TTL_SEC:
        return set(), f"旁路文件过期({age / 3600:.1f} 小时前，上限 {LATENCY_TTL_SEC / 3600:.0f} 小时)"
    try:
        with open(latency_path, "r", encoding="utf-8") as f:
            lat = (json.load(f) or {}).get("latency") or {}
    except Exception as e:
        return set(), f"旁路文件读取失败: {e}"
    if not isinstance(lat, dict):
        return set(), "旁路文件格式异常"

    total = 0
    covered = 0
    alive: set = set()
    for p in proxies:
        name = p.get("name")
        if not name:
            continue
        total += 1
        key = f"{p.get('type')}|{p.get('server')}|{p.get('port')}"
        if key in lat:                      # 值可能是 None（TCP 不通），也算"有结论"
            covered += 1
            if lat[key] is not None:
                alive.add(name)
    coverage = covered / max(1, total)
    if coverage < LATENCY_MIN_COVERAGE:
        return set(), (f"旁路只覆盖 {coverage * 100:.0f}% 的当前节点池"
                       f"（门槛 {LATENCY_MIN_COVERAGE * 100:.0f}%），疑似上一轮的遗留文件")
    return alive, (f"{len(alive)}/{total} 个节点 TCP 连通，覆盖 {coverage * 100:.0f}%")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    src = sys.argv[1] if len(sys.argv) > 1 else "output.yaml"
    dst = sys.argv[2] if len(sys.argv) > 2 else "output-filtered-http.yaml"
    # 第三个参数（可选）：TCP 旁路文件路径，默认取 src 同目录的 output-latency.json
    latency_path = (sys.argv[3] if len(sys.argv) > 3
                    else os.path.join(os.path.dirname(os.path.abspath(src)), LATENCY_FILE))

    with open(src, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    proxies = [p for p in data.get("proxies", []) if p.get("name")]
    names = [p["name"] for p in proxies]

    # ---- 分级实测：先测 TCP 连通的节点，第一阶段产出不足时再补测其余节点 ----
    prefilter_on = os.environ.get("PCOLL_PREFILTER", "1") not in ("0", "false", "False")
    min_alive = int(os.environ.get("PCOLL_PREFILTER_MIN_ALIVE", PREFILTER_MIN_ALIVE))
    # 旁路文件的可信度由 load_tcp_alive 内部的 TTL + 覆盖率门槛把关（不用 mtime 比较：
    # collector 是先写旁路、后写 output.yaml，比较 mtime 会把同一轮的正常文件误判为旧）
    stage1, stage2 = names, []
    if prefilter_on:
        tcp_alive, why = load_tcp_alive(latency_path, proxies)
        if tcp_alive:
            stage1 = [n for n in names if n in tcp_alive]
            stage2 = [n for n in names if n not in tcp_alive]
            logger.info("TCP 预筛: 先测 %d 个连通节点, 保留 %d 个待补测 (%s)",
                        len(stage1), len(stage2), why)
        else:
            logger.info("TCP 预筛跳过（%s），全量实测 %d 个节点", why, len(names))
    else:
        logger.info("TCP 预筛已关闭（PCOLL_PREFILTER=0），全量实测 %d 个节点", len(names))

    delay_map = asyncio.run(http_test_nodes(src, stage1, label="stage1"))
    if stage2:
        if len(delay_map) < min_alive:
            logger.info("第一阶段仅 %d 个可用 < 阈值 %d，补测剩余 %d 个 TCP 未连通节点",
                        len(delay_map), min_alive, len(stage2))
            delay_map.update(asyncio.run(http_test_nodes(src, stage2, label="stage2")))
        else:
            logger.info("第一阶段已得 %d 个可用（>= 阈值 %d），跳过 %d 个 TCP 未连通节点",
                        len(delay_map), min_alive, len(stage2))

    filter_output_by_http_test(src, dst, delay_map)
    logger.info("结果已写入: %s (共 %d 个可用节点)", dst, len(delay_map))
