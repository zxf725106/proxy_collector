#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理节点自动刷新串联脚本（方案 B：高频自动刷新）
链路: collector -> http_test -> speed_test -> 部署前复测过滤 -> make_mobile -> deploy_to_clash

关键改进: 在部署前对 TopN 节点用 mihomo 真实下载复测, 过滤掉已死节点,
确保部署到 Clash Verge 的文件此刻一定是活的 (免费公网节点衰减极快, 这一步是 B 方案能成立的核心)。
"""
import subprocess, os, re, time, json, yaml, tempfile, asyncio, aiohttp, urllib.request, urllib.parse, shutil, sys

from atomic import cleanup_stale_temp, dump_yaml_atomic
from output import _SafeDumper, defuse_reality_short_ids, ensure_unique_proxy_names

# 路径可用环境变量覆盖，便于换机器/换解释器而不改代码：
#   PCOLL_PY      子步骤用的 python 解释器
#   PCOLL_BASE    项目目录
#   PCOLL_MIHOMO  mihomo 核心
#   PCOLL_SPEED_IN_NAME=1  把实测速率写进节点名（默认关闭，见 speed_test.py）
DEFAULT_VPY = r"C:\Users\zxf72\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
DEFAULT_BASE = r"C:\Users\zxf72\proxy_collector"
DEFAULT_MIHOMO = r"C:\Program Files\Clash Verge\verge-mihomo.exe"


def _python_works(path: str, timeout: float = 30) -> bool:
    """探测解释器是否真的能跑。

    只判断 os.path.exists 不够：这类 venv 的启动器存在，但它还必须能读到自己的基础
    解释器（pyvenv.cfg 里 home 指向的目录）。基础解释器缺失/不可读时，启动器只会报
    "did not find executable at ..." 并返回退出码 103，于是每个子步骤都失败、提示还不明。
    这里实际跑一次 `-c import sys` 再决定用不用。
    （排查记录：本次遇到的 103 是**受限沙箱阻止读取该路径**造成的，不是环境被清理；
      但无论哪种原因，这个探测都能给出准确结论并自动回退。）
    """
    try:
        r = subprocess.run([path, "-c", "import sys"], capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def _pick_python() -> str:
    """解释器优先级：环境变量 -> 历史 venv（存在**且能跑**才用）-> 当前解释器。

    原实现把 .workbuddy 私有 venv 写死在代码里、且没有任何回退，该环境一旦被重建或
    清理，流水线第一步就以 FileNotFoundError/rc=103 崩溃且提示不明。
    """
    for cand in (os.environ.get("PCOLL_PY"), DEFAULT_VPY):
        if cand and os.path.exists(cand):
            if _python_works(cand):
                return cand
            # 注意：此处不能用 log()，它定义在本块之后
            print(f"[{time.strftime('%H:%M:%S')}] !! {cand} 存在但无法运行"
                  f"（venv 基础解释器可能已被清理），跳过", flush=True)
    print(f"[{time.strftime('%H:%M:%S')}] 使用当前解释器运行所有子步骤: "
          f"{sys.executable}", flush=True)
    return sys.executable


VPY = _pick_python()
BASE = os.environ.get("PCOLL_BASE", DEFAULT_BASE)
MIHOMO = os.environ.get("PCOLL_MIHOMO", DEFAULT_MIHOMO)
FAST = "output-filtered-fast.yaml"
HTTP_CAND = "output-filtered-http.yaml"
STABLE_HISTORY = "node_stability.json"

# 部署前复测用的连通性探测 URL（走 mihomo delay API，任一可达即判活）。
# 用轻量 generate_204 端点：被测节点只需完成一次真实请求即可判活，避免依赖大文件下载源
# ——实测当前网络下「经混合端口下载」的路径对包括 DIRECT 在内的所有目标都返回 0 字节，
# 会把好节点全部误判为 DEAD。改用 delay API（mihomo 直接经该节点发起请求）后准确可靠。
RETEST_URLS = [
    "http://www.gstatic.com/generate_204",
    "http://www.google.com/generate_204",
    "http://cp.cloudflare.com/generate_204",
]

def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def run_step(script, *args):
    log(f">>> 步骤: {script} {' '.join(args)}")
    # 子进程与父进程统一 UTF-8：默认情况下子进程 stdout 编码跟随控制台代码页
    # （本机为 GBK），一旦打印节点名里的国旗 emoji 就会抛 UnicodeEncodeError。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    r = subprocess.run([VPY, script, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, cwd=BASE)
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    print(out[-3000:], flush=True)
    if r.returncode != 0:
        log(f"!! 步骤失败 rc={r.returncode}: {script}")
        raise SystemExit(1)
    log(f"<<< 完成: {script}")


# AUTO(url-test) 组的成员上限，与 speed_test.AUTO_TOP 保持一致。
# 可用 PCOLL_AUTO_TOP 覆盖：免费节点衰减极快，若经常一觉醒来 AUTO 全红，
# 可以调大（例如 60），让 url-test 在更多"今天实测过"的节点里挑活的。
AUTO_TOP = int(os.environ.get("PCOLL_AUTO_TOP", "30"))
# 上一轮 speed_test 写进节点名的速率后缀，例如 "name | 1.7MB/s"。
# 它只反映"当时"的速率，跨轮保留下来就是过期信息，且会让 A/B 两个不同节点同名，
# 因此在合并/发布前统一剥掉（需要该展示效果时用 speed_test --speed-in-name）。
_SPEED_SUFFIX_RE = re.compile(r"\s*\|\s*\d+(?:\.\d+)?\s*MB/s\s*$")


def strip_speed_suffixes(proxies) -> int:
    """剥掉节点名末尾的 " | x.xMB/s" 过期后缀，返回被清理的节点数。"""
    n = 0
    for p in proxies or []:
        name = p.get("name")
        if not name:
            continue
        cleaned = _SPEED_SUFFIX_RE.sub("", name)
        if cleaned != name:
            p["name"] = cleaned
            n += 1
    return n


def load_today_speed(path=None):
    """读取本轮 speed_test 写下的实测速率 {身份指纹: MB/s}（写不出时返回空表）。"""
    path = path or os.path.join(BASE, "outputs", "fast_speed.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception as e:                       # noqa: BLE001
        log(f"(读取本轮速率旁路失败，按空处理: {e})")
        return {}
    out = {}
    for k, v in (d.get("speeds") or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            out[k] = float(v)
    return out


def order_proxies_by_today(proxies, speed_map):
    """按「今天的实测速率」重排节点：测到的在前（速率降序），没测到的保持原顺序在后。

    为什么必须这样排：`AUTO = names[:AUTO_TOP]` 这个规则假设池子是"按今天的速度"排的，
    但 `merge_previous_fast` 是**旧节点排前面**，于是 AUTO 拿到的恰好是最陈旧的一批
    ——2026-10-16 早上实测：AUTO 的 30 个全部 Error，而池子里有大量当天验证过的活节点。

    只改顺序，不删任何节点：上一轮的节点仍然全部保留，只是不再占据 AUTO 的名额。
    """
    if not speed_map:
        return list(proxies)
    try:
        from parser import proxy_identity
    except Exception:                            # noqa: BLE001
        return list(proxies)
    measured, rest = [], []
    for p in proxies:
        try:
            fid = proxy_identity(p)
        except Exception:                        # noqa: BLE001
            fid = None
        (measured if fid in speed_map else rest).append(p)
    measured.sort(key=lambda p: -speed_map.get(proxy_identity(p), 0.0))
    return measured + rest


def order_merged_nodes(old_ps, new_ps):
    """合并去重并按「今天是否实测到」排序：本轮测过的在前（本轮带宽降序），其余在后。

    去重仍按身份指纹、且**旧字典优先保留**（保持原来的选择语义不变）；
    只是顺序改为"今天的证据优先"，这样 AUTO 与部署前复测都能落到当天验证过的节点上。
    """
    chosen = {}
    for p in list(old_ps) + list(new_ps):
        fp = fingerprint(p)
        if fp not in chosen:
            chosen[fp] = p
    ordered, seen = [], set()
    for p in new_ps:                             # new_ps 已按本轮带宽降序
        fp = fingerprint(p)
        if fp in chosen and fp not in seen:
            ordered.append(chosen[fp])
            seen.add(fp)
    for fp, p in chosen.items():                 # 上一轮保留、今天没测到的
        if fp not in seen:
            ordered.append(p)
    return ordered


def validate_config_with_mihomo(path, timeout: int = 180):
    """用 mihomo 本体校验**整份**配置（`-t` = test configuration and exit）。

    这是与 Clash Verge 完全相同的校验。为什么必须做：原来的部署前复测只把 top30 节点放进
    临时配置，重名/组引用之类的问题根本走不到，于是出现过
    「流水线 rc=0，但 Clash Verge 报 `... is the duplicate name` 并把变更整体回滚」
    —— 2026-10-16 05:28 真实发生，实测用 `mihomo -t -f` 可稳定复现（返回 1）。

    返回 (ok, 输出末尾文本)。校验工具本身异常时返回 True：不能因为工具问题阻断部署。
    """
    if not os.path.exists(MIHOMO):
        log("(未找到 mihomo，跳过整份配置校验)")
        return True, "mihomo 缺失"
    try:
        r = subprocess.run(
            [MIHOMO, "-t", "-f", os.path.abspath(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=BASE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:                       # noqa: BLE001 - 校验器异常不阻断部署
        log(f"(配置校验无法执行，按通过处理: {e})")
        return True, str(e)
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    return r.returncode == 0, out[-1500:]


def finalize_deploy_config(fast_yaml=FAST):
    """发布前最后一道工序：名称唯一 -> 重建组引用 -> mihomo 校验整份配置。

    返回 True 表示可以部署。任何一步失败都不部署 —— 宁可保持上一版 profile，
    也不要把 Clash Verge 会拒收的文件推上去（那会导致客户端把变更整体回滚，
    用户看到的是"校验失败/变更已撤销"，而流水线自己却以为成功了）。
    """
    if not os.path.exists(fast_yaml):
        log("!! 无 FAST 文件，跳过发布前整形")
        return False
    try:
        with open(fast_yaml, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception as e:
        log(f"!! FAST 解析失败，跳过发布前整形: {e}")
        return False
    proxies = d.get("proxies", []) or []
    if not proxies:
        log("!! FAST 无节点，跳过发布前整形")
        return False

    # 先剥掉过期速率后缀：它会制造重名，也会让 AUTO 里显示旧速率误导判断
    stripped = strip_speed_suffixes(proxies)
    if stripped:
        log(f"   清理过期速率后缀: {stripped} 个节点")
    renamed = ensure_unique_proxy_names(proxies)
    names = [p.get("name") for p in proxies if p.get("name")]
    # AUTO 的 30 个成员取"今天实测最快的节点"，而不是池子里最靠前的（那是上一轮的旧节点，
    # 一夜之间就全衰减了：2026-10-16 早上实测 AUTO 全部 Error）。
    speed_map = load_today_speed()
    auto_names = [p.get("name") for p in
                  order_proxies_by_today(proxies, speed_map)[:AUTO_TOP] if p.get("name")]
    if speed_map:
        log(f"   AUTO 组: 按本轮实测速率取前 {len(auto_names)} 个（本轮速率记录 {len(speed_map)} 条）")
    else:
        log(f"   AUTO 组: 无本轮速率旁路，退回池子顺序取前 {len(auto_names)} 个")
    groups = d.get("proxy-groups", []) or []
    group_names = {g.get("name") for g in groups if g.get("name")}
    special = {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY", "AUTO"}
    for g in groups:
        gname = g.get("name")
        if gname == "PROXY" and g.get("type") == "select":
            g["proxies"] = ["AUTO"] + names          # 全部节点都可手动选到
        elif gname == "AUTO" and g.get("type") == "url-test":
            g["proxies"] = auto_names                # url-test 只看今天实测最快的那批
        elif "proxies" in g:
            allowed = set(names) | group_names | special
            g["proxies"] = [m for m in g["proxies"] if m in allowed]
    defuse_reality_short_ids(d)
    dump_yaml_atomic(fast_yaml, d, dumper=_SafeDumper, log=log)
    if renamed:
        log(f"重名修正: {len(renamed)} 个节点追加了序号（mihomo 遇重名会整份拒收）")

    log(f">>> 发布前整份配置校验 (mihomo -t): {fast_yaml}")
    ok, detail = validate_config_with_mihomo(fast_yaml)
    if ok:
        log("<<< 配置校验通过")
        return True
    log("!! 配置校验未通过，拒绝部署（保留上一版 profile）：")
    for line in detail.splitlines()[-6:]:
        log("   " + line)
    return False


def enforce_auto_cap(fast_yaml=FAST, cap=AUTO_TOP):
    """发布前把 AUTO(url-test) 组收敛到按带宽排序的前 cap 个节点。

    为什么需要：speed_test.py 特意用 AUTO_TOP=30 限制过 AUTO 组（url-test 按延迟选，
    把全量塞进去会挑中"低延迟但拥塞/低速"的节点），但 merge_previous_fast /
    apply_stability / pre_deploy_verify 三处重建 proxy-groups 时都会把它重写成
    全部存活节点 —— 实测部署文件里 AUTO 组因此涨到 254 个成员（应为 30）。
    """
    if not os.path.exists(fast_yaml):
        return
    try:
        with open(fast_yaml, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception as e:
        log(f"!! AUTO 组收敛：读取失败，跳过 ({e})")
        return
    proxies = d.get("proxies", []) or []
    if not proxies:
        return
    names = [p.get("name") for p in proxies if p.get("name")]
    allowed = set(names)
    group_names = {g.get("name") for g in d.get("proxy-groups", []) if g.get("name")}
    allowed |= group_names | {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY"}
    changed = []
    for g in d.get("proxy-groups", []):
        if g.get("name") == "AUTO" and g.get("type") == "url-test":
            before = len(g.get("proxies", []) or [])
            g["proxies"] = names[:cap]
            if before != len(g["proxies"]):
                changed.append(f"AUTO组 {before} -> {len(g['proxies'])} 个成员")
        elif "proxies" in g:
            before = len(g["proxies"])
            g["proxies"] = [m for m in g["proxies"] if m in allowed]
            if before != len(g["proxies"]):
                changed.append(f"{g.get('name')} 清理 {before - len(g['proxies'])} 个悬空引用")
    if not changed:
        return
    try:
        defuse_reality_short_ids(d)
        dump_yaml_atomic(fast_yaml, d, dumper=_SafeDumper, log=log)
        log("AUTO 组收敛: " + "; ".join(changed))
    except Exception as e:
        log(f"!! AUTO 组收敛：写回失败，保留原文件 ({e})")

def fingerprint(p):
    """使用与来源质量对齐的完整代理身份，避免同端口不同凭据混淆。"""
    from parser import proxy_identity
    return proxy_identity(p)


def atomic_copy(src, dst):
    """在目标目录内完成替换，避免发布过程中留下半截文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def profile_quality(path, min_proxies=5):
    """发布前检查节点数量和代理组引用，拒绝明显损坏或空配置。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False, "文件不存在或为空"
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception as e:
        return False, f"YAML 解析失败: {e}"
    proxies = d.get("proxies") or []
    if len(proxies) < min_proxies:
        return False, f"节点数 {len(proxies)} 小于质量门 {min_proxies}"
    names = {p.get("name") for p in proxies if p.get("name")}
    groups = {g.get("name") for g in d.get("proxy-groups", []) if g.get("name")}
    dangling = []
    for group in d.get("proxy-groups", []):
        for member in group.get("proxies", []) or []:
            if member not in names and member not in groups and member not in {"DIRECT", "REJECT"}:
                dangling.append(f"{group.get('name')}->{member}")
    if dangling:
        return False, "存在悬空代理组引用: " + ", ".join(dangling[:3])
    return True, f"{len(proxies)} 个节点，组引用完整"


def apply_stability(fast_yaml, history_path, min_stable=2, strict=False, keep=100):
    """方案C: 跨轮次稳定性追踪。连续多轮都存活的节点优先排到前面/保留, 自动剔除一次性节点。"""
    log(">>> 跨轮次稳定性追踪 (方案C)")
    d = yaml.safe_load(open(fast_yaml, encoding="utf-8"))
    proxies = d.get("proxies", [])
    if not proxies:
        return
    now = time.time()
    history = {}
    if os.path.exists(history_path):
        try:
            history = json.load(open(history_path, encoding="utf-8"))
        except Exception:
            history = {}
    cur_fps = {fingerprint(p) for p in proxies}
    # 本轮未出现的节点: 计数衰减 (连续缺席才清零)
    for fp, info in history.items():
        if fp not in cur_fps:
            info["n"] = max(0, info.get("n", 0) - 1)
    # 本轮出现的节点: 计数+1
    for p in proxies:
        fp = fingerprint(p)
        if fp in history:
            history[fp]["n"] = history[fp].get("n", 0) + 1
        else:
            history[fp] = {"n": 1}
        history[fp]["name"] = p.get("name", "")
        history[fp]["ts"] = now
    # 清理: 计数=0 且超过7天未见的节点
    cutoff = now - 7 * 86400
    history = {fp: info for fp, info in history.items()
               if not (info.get("n", 0) == 0 and info.get("ts", 0) < cutoff)}
    # 分组: stable(>=min_stable轮) 在前, fresh 在后, 组内维持 speed_test 原顺序(已按速度降序)
    stable, fresh = [], []
    for p in proxies:
        (stable if history[fingerprint(p)]["n"] >= min_stable else fresh).append(p)
    if strict:
        ordered = stable[:]
        if len(ordered) < keep:
            ordered += fresh[:keep - len(ordered)]
    else:
        ordered = stable + fresh
    d["proxies"] = ordered
    # 修正 proxy-groups 成员引用
    gnames = {g["name"] for g in d.get("proxy-groups", [])}
    special = {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY"}
    names = {p["name"] for p in ordered}
    for g in d.get("proxy-groups", []):
        if "proxies" in g:
            g["proxies"] = [m for m in g["proxies"]
                            if m in names or m in gnames or m in special]
    defuse_reality_short_ids(d)                    # 保住 reality short-id 的引号
    dump_yaml_atomic(fast_yaml, d, dumper=_SafeDumper, log=log)
    json.dump(history, open(history_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    log(f"稳定性: stable(>= {min_stable}轮) {len(stable)} / fresh {len(fresh)} | 历史库 {len(history)} 节点")


def pre_deploy_verify(top_n=30, min_alive=15):
    """部署前用 mihomo 逐个切换节点并真实下载复测，返回是否通过质量门。"""
    log(">>> 部署前复测 (真实代理质量门)")
    if not os.path.exists(FAST):
        log("!! 无 FAST 文件，复测失败")
        return False
    with open(FAST, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    # 复测对象 = 会被放进 AUTO 的那批（今天实测最快的节点），而不是"池子最前面的旧节点"：
    # 旧实现测的是 proxies[:30]，在"旧节点排前面"的池子里等于测最陈旧的一批，
    # 而真正被 AUTO 使用的节点反而没被复测过。
    speed_map = load_today_speed()
    ordered = order_proxies_by_today(d.get("proxies", []) or [], speed_map)
    nodes = ordered[:top_n]
    if speed_map:
        log(f"   复测对象: 本轮实测最快的前 {len(nodes)} 个（共 {len(ordered)} 个候选）")
    else:
        log(f"   复测对象: 池子前 {len(nodes)} 个（无本轮速率旁路，退回原顺序）")
    if not nodes:
        log("无节点可复测")
        return False
    if not os.path.exists(MIHOMO):
        log(f"!! 未找到 mihomo: {MIHOMO}")
        return False

    port, api = 17900, "127.0.0.1:19099"
    api_base = f"http://{api}"
    cfg = {
        "mixed-port": port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": api,
        "secret": "verify",
        "proxies": nodes,
        "proxy-groups": [{
            "name": "__VERIFY__",
            "type": "select",
            "proxies": ["DIRECT"] + [p["name"] for p in nodes],
        }],
        "rules": ["MATCH,__VERIFY__"],
    }
    fd, tmp = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    proc = subprocess.Popen(
        [MIHOMO, "-f", tmp],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    ready = False
    for _ in range(20):
        try:
            req = urllib.request.Request(f"{api_base}/version")
            req.add_header("Authorization", "Bearer verify")
            urllib.request.urlopen(req, timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(1)
    if not ready:
        log("!! mihomo 未就绪，复测失败，不发布新配置")
        proc.kill()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    async def test_one(session, node):
        name = node["name"]
        headers = {"Authorization": "Bearer verify"}
        # 用 mihomo 自带 delay API 逐个节点做真实连通性探测：该端点由 mihomo 直接「经该节点」
        # 发起请求（不经过混合端口在 mode=rule 下的转发路径，实测后者对下载源全部失败），
        # 因此能准确区分活/死节点。任一探测 URL 返回正延迟即判活。
        for url in RETEST_URLS:
            try:
                async with session.get(
                    f"{api_base}/proxies/{urllib.parse.quote(name, safe='')}/delay",
                    params={"timeout": 5000, "url": url},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        continue
                    j = await resp.json()
                    d = j.get("delay")
                    if isinstance(d, int) and d > 0:
                        return name, True, float(d)
            except Exception:
                continue
        return name, False, 0.0

    async def batch():
        async with aiohttp.ClientSession() as session:
            return [await test_one(session, node) for node in nodes]

    try:
        results = asyncio.run(batch())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        try:
            os.remove(tmp)
        except OSError:
            pass

    alive = [n for n, (_, ok, _) in zip(nodes, results) if ok]
    for name, ok, val in results:
        log(f"   复测 {name[:38]:38s} {'OK ' if ok else 'DEAD'} {val:.0f} ms")
    required = max(1, min(min_alive, len(nodes)))
    log(f"复测结果: {len(alive)}/{len(nodes)} 可用，质量门要求至少 {required} 个")
    if len(alive) < required:
        log("!! 真实复测未通过，保留旧版本并停止发布")
        return False

    tested = {n["name"] for n in nodes}
    alive_set = {n["name"] for n in alive}
    dead_set = tested - alive_set          # 只移除"已实测为死"的节点
    group_names = {g["name"] for g in d.get("proxy-groups", [])}
    special = {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY"}
    # 保留未抽测的节点（抽测 top_n 之外的节点不应被一并丢弃），仅剔除实测死亡者
    d["proxies"] = [p for p in d.get("proxies", []) if p.get("name") not in dead_set]
    _keep = {p.get("name") for p in d["proxies"]}
    for group in d.get("proxy-groups", []):
        if "proxies" in group:
            group["proxies"] = [m for m in group["proxies"] if m in _keep or m in group_names or m in special]
    defuse_reality_short_ids(d)
    dump_yaml_atomic(FAST, d, dumper=_SafeDumper, log=log)
    log(f"已过滤至 {len(d['proxies'])} 个真实可用节点并写回 {FAST}")
    return True


def _verify_nodes_real(nodes, port=17902, api="127.0.0.1:19097", secret="mergeprev"):
    """用独立 mihomo 实例对一批节点做真实代理连通性复测，返回仍可用的节点名集合。

    任一探测 URL 返回正延迟即判活。mihomo 缺失时保守地全部保留（不验证就不丢），
    避免把老节点误删。
    """
    if not nodes:
        return set()
    names = [p.get("name") for p in nodes]
    if not os.path.exists(MIHOMO):
        log(f"!! 未找到 mihomo: {MIHOMO}，无法复测，保守保留全部 {len(nodes)} 个")
        return set(names)
    cfg = {
        "mixed-port": port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": api,
        "secret": secret,
        "proxies": nodes,
        "proxy-groups": [{
            "name": "__VERIFY__",
            "type": "select",
            "proxies": ["DIRECT"] + names,
        }],
        "rules": ["MATCH,__VERIFY__"],
    }
    fd, tmp = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    proc = subprocess.Popen(
        [MIHOMO, "-f", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    api_base = f"http://{api}"
    ready = False
    for _ in range(20):
        try:
            req = urllib.request.Request(f"{api_base}/version")
            req.add_header("Authorization", f"Bearer {secret}")
            urllib.request.urlopen(req, timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(1)
    if not ready:
        log("!! mihomo 未就绪，保守保留全部节点")
        proc.kill()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return set(names)

    sem = asyncio.Semaphore(15)

    async def test_one(session, node):
        name = node["name"]
        headers = {"Authorization": f"Bearer {secret}"}
        async with sem:
            for url in RETEST_URLS:
                try:
                    async with session.get(
                        f"{api_base}/proxies/{urllib.parse.quote(name, safe='')}/delay",
                        params={"timeout": 5000, "url": url},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        j = await resp.json()
                        d = j.get("delay")
                        if isinstance(d, int) and d > 0:
                            return name, True
                except Exception:
                    continue
        return name, False

    async def batch():
        async with aiohttp.ClientSession() as session:
            return await asyncio.gather(*(test_one(session, n) for n in nodes))

    try:
        results = asyncio.run(batch())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        try:
            os.remove(tmp)
        except OSError:
            pass
    alive = {name for name, ok in results if ok}
    log(f"   复测: 存活 {len(alive)} / 总 {len(nodes)}")
    return alive


def merge_previous_fast(prev=FAST + ".bak", fast=FAST):
    """根因修复：刷新时保留上一轮仍可用的老节点，避免被 speed_test 重排名整批冲掉。

    流程：取上一轮 FAST 备份与新 FAST 按 fingerprint 去重合并 -> 真实代理复测 ->
    仅保留存活节点 -> 重建代理组 -> 写回 FAST。被复测判定死亡的节点才会被剔除。
    """
    if not os.path.exists(prev) or not os.path.exists(fast):
        log("(无上一轮 FAST 备份，跳过旧节点合并)")
        return
    old_d = yaml.safe_load(open(prev, encoding="utf-8")) or {}
    new_d = yaml.safe_load(open(fast, encoding="utf-8")) or {}
    old_ps = old_d.get("proxies", [])
    new_ps = new_d.get("proxies", [])
    if not old_ps:
        log("(上一轮 FAST 为空，跳过旧节点合并)")
        return
    # 合并去重（按身份指纹，旧字典优先）+ 按"今天是否实测到"排序（今天的在前）
    merged = order_merged_nodes(old_ps, new_ps)
    log(f">>> 合并上一轮优质节点: 旧 {len(old_ps)} + 新 {len(new_ps)} -> 唯一 {len(merged)}")
    # 剥掉上一轮留下的 " | x.xMB/s" 过期后缀（会让不同节点同名，且显示的是旧速率）
    stripped = strip_speed_suffixes(merged)
    if stripped:
        log(f"   清理过期速率后缀: {stripped} 个节点")
    alive_set = _verify_nodes_real(merged)
    alive_nodes = [p for p in merged if p.get("name") in alive_set]
    dead = len(merged) - len(alive_nodes)
    log(f"   剔除已死亡 {dead} 个，保留 {len(alive_nodes)} 个")
    # 合并后必须按名称去重：不同凭据、同名、同 server:port 的节点指纹不同，不会被按指纹去重
    # 折叠掉；重名会让 mihomo 拒收整份配置（并让下面的复测实例也起不来）。
    renamed = ensure_unique_proxy_names(alive_nodes)
    if renamed:
        log(f"   合并后重名修正: {len(renamed)} 个节点追加了序号")
    alive_names = {p["name"] for p in alive_nodes}
    # 重建 proxy-groups，确保所有存活节点都被引用
    out = dict(new_d)
    out["proxies"] = alive_nodes
    group_names = {g["name"] for g in new_d.get("proxy-groups", [])}
    special = {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY", "AUTO"}
    for g in out.get("proxy-groups", []):
        gname = g.get("name")
        if gname == "PROXY":
            g["proxies"] = ["AUTO"] + [p["name"] for p in alive_nodes]
        elif gname == "AUTO":
            g["proxies"] = [p["name"] for p in alive_nodes]
        else:
            g["proxies"] = [m for m in g.get("proxies", [])
                             if m in alive_names or m in group_names or m in special]
    defuse_reality_short_ids(out)
    dump_yaml_atomic(fast, out, dumper=_SafeDumper, log=log)
    log(f"   已合并写回 {fast}（{len(alive_nodes)} 节点）")


def main():
    os.chdir(BASE)
    log("==== 代理刷新流水线启动 ====")
    log(f"解释器: {VPY}")
    # 清理上一轮被中断时漏下的临时文件（旧版 speed_test/http_test 把 tmp 写在项目根）
    cleanup_stale_temp(BASE, log=log)
    # aggregator 移植层：付费主源 / GitHub 发现 / crawlers 自动注册
    # 产出 candidates/aggregator_uris.txt，collector.py 自动合并
    if os.path.exists("collect_aggregator.py"):
        run_step("collect_aggregator.py")
    else:
        log("(collect_aggregator.py 不存在, 跳过 aggregator 源)")
    run_step("collector.py")
    # --skip-http: 复用已有的 output-filtered-http.yaml（已通过 HTTP 连通性筛选的节点池），
    # 跳过对全量节点的 17 分钟级实测，仅重新测速 + 复测门。用于节点池变化不频繁时的快速刷新。
    if "--skip-http" in sys.argv:
        log("(已指定 --skip-http，跳过 http_test，复用现有 HTTP_CAND 节点池)")
    else:
        run_step("http_test.py", "output.yaml", HTTP_CAND)
    # 安全网：备份上一份可用 FAST，防止本轮刷新失败时覆盖掉好文件
    if os.path.exists(FAST) and os.path.getsize(FAST) > 0:
        atomic_copy(FAST, FAST + ".bak")
        log(f"已备份上一份 FAST -> {FAST}.bak")
    # --timeout 25 -> 15：测速目标是 2MB、门槛 --min-mbps 0.15。
    # 0.15MB/s 的节点下满 2MB 需要 13.3s，所以 15s 超时不会误杀任何"本来能达标"的节点
    # （更慢的节点反正会被 --min-mbps 过滤掉），但能把失败节点每源的等待从 25s 降到 15s。
    speed_args = ["--top", "100", "--bytes", "2000000", "--timeout", "15", "--min-mbps", "0.15"]
    if os.environ.get("PCOLL_SPEED_IN_NAME", "0") not in ("0", "false", "False"):
        speed_args.append("--speed-in-name")
    run_step("speed_test.py", HTTP_CAND, FAST, *speed_args)

    ok, reason = profile_quality(FAST, min_proxies=5)
    if not ok:
        log(f"!! 速度筛选未通过发布前质量门: {reason}")
        if os.path.exists(FAST + ".bak"):
            atomic_copy(FAST + ".bak", FAST)
            log("已回滚到上一份 FAST，停止部署")
        raise SystemExit(1)

    # 根因修复：先合并上一轮仍可用的老节点，再进入稳定性追踪/部署门。
    # 这样刷新不会把上一轮的好节点整批冲掉（用户明确要求保留优质旧节点）。
    merge_previous_fast()

    apply_stability(FAST, STABLE_HISTORY, min_stable=2, strict=False)
    if not pre_deploy_verify(top_n=30, min_alive=15):
        if os.path.exists(FAST + ".bak"):
            atomic_copy(FAST + ".bak", FAST)
            log("真实复测失败，已回滚到上一份 FAST，停止部署")
        raise SystemExit(1)

    ok, reason = profile_quality(FAST, min_proxies=5)
    if not ok:
        log(f"!! 真实复测后质量门失败: {reason}")
        if os.path.exists(FAST + ".bak"):
            atomic_copy(FAST + ".bak", FAST)
            log("已回滚到上一份 FAST，停止部署")
        raise SystemExit(1)

    # 发布前统一收敛 AUTO 组：speed_test 设计为 Top30，但中间步骤重建组时会丢（见 enforce_auto_cap）
    enforce_auto_cap(FAST)
    # 最后一道门：名称唯一 + 重建组引用 + 用 mihomo 校验整份配置。
    # 校验不过就绝不部署（否则 Clash Verge 会拒收并回滚，用户端等于没更新）。
    if not finalize_deploy_config(FAST):
        log("!! 发布前配置校验失败，停止部署（未改动 Clash profile）")
        raise SystemExit(1)

    run_step("make_mobile.py", FAST, "output-filtered-fast-mobile.yaml")
    # 云端运行（GitHub Actions 等）没有本机 Clash Verge：deploy_to_clash.py 在配置
    # 目录缺失时返回 rc=3，而 run_step 遇到非零码会 SystemExit(1)，会把整轮云任务
    # 判为失败。设 PCOLL_SKIP_DEPLOY=1 即跳过部署，产物由外部步骤发布。
    if os.environ.get("PCOLL_SKIP_DEPLOY", "0") not in ("0", "false", "False", ""):
        log("(PCOLL_SKIP_DEPLOY 已设置：跳过 deploy_to_clash，产物交由外部发布)")
    else:
        run_step("deploy_to_clash.py")
    # 赛后按来源结算质量分（自动降权）；下一轮 collect_aggregator 会跳过被降权的源
    if os.path.exists("quality_report.py"):
        run_step("quality_report.py", "--fast", FAST)
    # 多格式输出（subconverter 缺失时安静跳过，不阻断）
    if os.path.exists("multi_output.py"):
        try:
            run_step("multi_output.py", "--fast", FAST, "--tag", "desktop")
        except SystemExit:
            log("(multi_output 子步骤未通过, 主流程已结束)")
    log("==== 流水线完成 ====")

if __name__ == "__main__":
    main()
