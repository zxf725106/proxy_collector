#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实带宽实测筛选：对每个节点通过 mihomo 实际下载测速，按 MB/s 排序，
只保留真正高带宽的节点，生成"高速精选"配置。

用法:
  python speed_test.py <input.yaml> <output.yaml> [--top N] [--bytes 3000000] [--timeout 8]

逻辑:
  1. 加载 input.yaml 节点
  2. 质量过滤: 仅保留 A 级(trojan / vless+flow) + B 级(ss/vmess), 剔除 vless 无 flow
  3. 按 /24 段去重(每段保留1个)
  4. 启动 mihomo(独立端口), 用 select 测试组逐个切节点 -> 经 mixed-port 下载测速文件 -> 测速率
  5. 按速率降序, 写 output.yaml (PROXY select + AUTO url-test 引用最快子集)
"""
import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time

import aiohttp
import yaml

from atomic import dump_yaml_atomic, remove_quiet, tempdir_for_run, write_text_atomic
from output import _SafeDumper, _defuse_shortid

# 复用 http_test 的多路径查找（含 PATH 兜底），避免核心路径写死导致 FileNotFoundError
try:
    from http_test import _kill_proc as kill_proc
    from http_test import find_mihomo
except Exception:                                   # pragma: no cover
    def find_mihomo():
        return MIHOMO if os.path.exists(MIHOMO) else None

    def kill_proc(proc):
        try:
            proc.terminate()
        except Exception:
            pass

API_PORT = 19097
MIXED_PORT = 17902
API_SECRET = "speedtest123"
MIHOMO = r"C:/Program Files/Clash Verge/verge-mihomo.exe"

# 测速 URL —— 全部经 diag_source.py/diag_source2.py 直连验证：
#   要求 200 + 有数字 Content-Length + 能真正下满 3MB。
# 注意: 旧的 www.ovh.net/files/10Mio.dat 已失效(返回 200 但只有 2KB 的页面),
#       曾导致所有节点被算成 40-135 MB/s 的假速率, 排序完全变成噪声。切勿再用。
# 顺序很重要: 经 diag_bottleneck.py 交叉验证, ms-cdn 对绝大多数节点都能测出最高速率
# (US节点在 sgp.ovh 上为 0.00 却能在 ms-cdn 跑出 4.66 MB/s), 故置于首位。
SPEED_URLS = [
    # 首位: cloudflare 精准 bytes 下载接口, 经绝大多数公网节点可达(实测比 ovh/microsoft
    # 更易连通), 且 Content-Length 精确 = 请求 bytes, 不会被"假 200 小页面"污染。
    "https://speed.cloudflare.com/__down?bytes=2000000",          # 经代理通常可达, 精准 bytes
    "https://sgp.proof.ovh.net/files/10Mb.dat",                   # 直连 0.94~1.56 MB/s (新加坡原生IDC)
    "https://download.microsoft.com/download/2/0/E/20E90413-712F-438C-988E-FDAA79A8AC3D/dotnetfx35.exe",  # 直连 5.60 MB/s
    "http://speedtest.tokyo2.linode.com/100MB-tokyo2.bin",         # 直连 0.73~0.84 MB/s
    "http://speedtest.singapore.linode.com/100MB-singapore.bin",   # 直连 0.16~0.98 MB/s
    "http://212.183.159.230/10MB.zip",                             # 直连 0.26 MB/s (纯IP, 抗DNS污染)
]


def quality(p):
    t = p.get("type")
    if t == "trojan":
        return "A"
    if t == "vless" and p.get("flow"):
        return "A"
    if t == "vless":
        return "C"  # 无 flow, 低速重灾区
    if t in ("vmess", "ss"):
        return "B"
    return "?"


def build_candidates(nodes):
    # 质量过滤
    kept = [p for p in nodes if quality(p) in ("A", "B")]
    # /24 去重
    seen = set()
    dedup = []
    for p in kept:
        srv = str(p.get("server", ""))
        m = re.match(r"(\d+\.\d+\.\d+)\.\d+", srv)
        seg = m.group(1) if m else srv
        if seg not in seen:
            seen.add(seg)
            dedup.append(p)
    return dedup


def port_up(port=API_PORT):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


# 有效测速的硬门槛 —— 防止"秒回小响应"被算成超高速
MIN_BYTES = 128 * 1024   # 至少真实下载 128KB, 否则视为劫持页/错误页/被掐断
MIN_DT = 0.30            # 至少持续 0.3s, 否则 dt≈0 会让除法炸出天文数字
MAX_SOURCE_TRY = 2       # 每个节点最多试 2 个源, 避免坏节点拖满 5×timeout
EARLY_STOP = 2.0         # 首个源已跑出 >=2.0MB/s 则认定够快, 不再试其他源(省时)

# AUTO 组(url-test)的候选上限: 只取按带宽排序的前 N 个。
# 原因: Clash 的 url-test 是**按延迟**自动选, 会绕过我们按带宽排好的顺序; 若把全部节点都
# 塞进 AUTO, 它就很可能挑中"低延迟但拥塞/低速"的节点(体感: 明明测出 5MB/s 却很慢)。
# 收窄为 Top30 后, url-test 只在"已实测快的节点"里按延迟选。其余节点仍可在 PROXY 组手动挑。
AUTO_TOP = 30


async def measure(name, session_http, proxy_url, nbytes, timeout):
    """通过代理下载测速，返回真实 MB/s；不可信或失败返回 0.0

    三重校验(缺一不可)：
      1. HTTP 200 且 Content-Length 是数字并 >= nbytes  -> 排除坏源/劫持页
      2. 实际下载量 >= MIN_BYTES                        -> 排除秒回小响应
      3. 持续时间 >= MIN_DT                             -> 排除 dt≈0 的除零级噪声

    取多源"最大值"而非"首个成功值"：不同节点到不同测速源的路由差异极大
    (实测某US节点 sgp.ovh=0.00 但 ms-cdn=4.66)，只取首个成功源会严重低估节点能力。
    """
    best = 0.0
    for url in SPEED_URLS[:MAX_SOURCE_TRY]:
        try:
            async with session_http.get(
                url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    continue
                # 校验1: 源必须声明足够大的文件, 否则这个源对该节点不可信
                clen = resp.headers.get("Content-Length", "")
                if not str(clen).isdigit() or int(clen) < nbytes:
                    continue
                t0 = time.time()
                downloaded = 0
                async for chunk in resp.content.iter_chunked(1 << 16):
                    downloaded += len(chunk)
                    if downloaded >= nbytes:
                        break
                dt = time.time() - t0
                # 校验2+3: 下载量与持续时间都要够, 否则数据不可信 -> 换下一个源
                if downloaded < MIN_BYTES or dt < MIN_DT:
                    continue
                mbps = (downloaded / (1024 * 1024)) / dt
                if mbps > best:
                    best = mbps
                if best >= EARLY_STOP:   # 已足够快, 无需再试其他源
                    return best
        except Exception:
            continue
    return best


async def main(input_yaml, output_yaml, top_n, nbytes, timeout, min_mbps=0.15, speed_in_name=False):
    with open(input_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    nodes = data.get("proxies", [])
    candidates = build_candidates(nodes)
    print(f"[*] 候选节点(质量过滤+去重): {len(candidates)}", flush=True)

    # 清洗候选: 修复残缺字段 + 剔除 mihomo 无法解析的节点(避免整体 fatal 退出)。
    # 注：原实现在这里还构造了一份主流程从未使用的 cdata（run_slice 会各自建配置），已删除。
    import base64
    clean = []
    dropped = 0
    for p in candidates:
        p.pop("_http_delay", None)
        if p.get("reality-opts"):
            p["reality-opts"] = _defuse_shortid(p["reality-opts"])
        t = p.get("type")
        if t == "vmess":
            # 这批源 vmess 字段普遍残缺(security 缺失 / cipher 写成字符串 'null' 等),
            # 会导致 mihomo 1.19 解析时 fatal 退出; 逐项补合法默认值让其能加载
            if not p.get("security"):
                p["security"] = "auto"
            cv = str(p.get("cipher")).lower() if p.get("cipher") is not None else ""
            if cv in ("null", "none", ""):
                p["cipher"] = "auto"
            if not p.get("uuid"):
                p["uuid"] = "00000000-0000-0000-0000-000000000000"
            if "alterId" not in p:
                p["alterId"] = 0
            clean.append(p)
        elif t == "ss":
            # 2022 系列 cipher 要求 password 为 base64 编码的 32/64 字节密钥,
            # 源数据多为明文 -> mihomo 解析 fatal; 非法则剔除(本就连不上)
            cipher = str(p.get("cipher") or "")
            if cipher.startswith("2022-"):
                pw = str(p.get("password") or "")
                try:
                    kb = base64.b64decode(pw, validate=True)
                    if len(kb) not in (32, 64):
                        raise ValueError("bad key length")
                except Exception:
                    dropped += 1
                    continue
            clean.append(p)
        else:
            clean.append(p)
    candidates = clean
    print(f"[*] 清洗后参与测速: {len(candidates)} (剔除 {dropped} 个 mihomo 无法解析的节点)", flush=True)

    # 多实例并发: 把候选轮流分给 K 个 mihomo 实例, 每实例独立端口串行测自己那批,
    # 实例之间真并发 -> 整体提速约 K 倍 (单实例内仍用 select 切节点, 但实例间无竞争)
    # 注意: K 不宜过大 —— 过多 mihomo 实例会吃满 CPU 且抢本地端口/句柄; 实测 K=12 在
    # 本机(8~16 核)已足够快且稳定, 且能给每个节点留出完整下载带宽, 避免被并发饿死导致误判 0。
    K = max(1, min((os.cpu_count() or 4), 12))
    slices = [candidates[i::K] for i in range(K)]
    print(f"[*] 并发实例数: {K}, 每实例约 {len(candidates)//K} 节点", flush=True)
    try:

        # 多个实例并发测速, 合并结果
        results_list = await asyncio.gather(
            *(run_slice(sl, i, nbytes, timeout) for i, sl in enumerate(slices))
        )
        results = {}
        for _d in results_list:
            results.update(_d)
        print(f"[*] 各实例测速完成, 共得到 {len(results)} 个结果", flush=True)
        # 排序
        ranked = sorted(results.items(), key=lambda x: -x[1])
        fast = [(n, v) for n, v in ranked if v > min_mbps]
        if top_n:
            fast = fast[:top_n]
        print(f"\n[*] 带宽实测完成: 有效(>{min_mbps}MB/s) {len(fast)} 个 / 候选 {len(candidates)}", flush=True)
        print(f"[*] 最快 Top10:", flush=True)
        for n, v in ranked[:10]:
            print(f"      {n[:40]:42s} {v:.2f} MB/s", flush=True)

        # 写高速精选配置
        fast_names = {n for n, _ in fast}
        fast_nodes = [p for p in candidates if p["name"] in fast_names]
        # 按速率排序 proxies
        rank_map = {n: v for n, v in ranked}
        fast_nodes.sort(key=lambda p: -rank_map.get(p["name"], 0))
        # 实测速率旁路保存（供来源质量打分），默认不再写进节点名。
        # 原因：名字里带速率会让同一节点每轮名字都变，Clash 客户端的"当前选中节点"与
        # 历史每轮失效；需要保留"客户端里直观可见"这个效果时加 --speed-in-name。
        from parser import proxy_identity

        speeds = {}
        for p in fast_nodes:
            try:
                speeds[proxy_identity(p)] = rank_map.get(p["name"], 0)
            except Exception:
                pass
        if speed_in_name:
            for p in fast_nodes:
                v = rank_map.get(p["name"], 0)
                p["name"] = f"{p['name']} | {v:.1f}MB/s"
        # 重建配置(沿用原模板风格)
        out = {
            "mixed-port": 7890,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "info",
            "proxies": fast_nodes,
            "proxy-groups": [
                {"name": "PROXY", "type": "select", "proxies": ["AUTO"] + [p["name"] for p in fast_nodes]},
                {"name": "AUTO", "type": "url-test",
                 # 只放按带宽排序的 TopN, 避免 url-test 按延迟选中"低延迟但慢"的节点
                 "proxies": [p["name"] for p in fast_nodes[:AUTO_TOP]],
                 "url": "https://www.gstatic.com/generate_204", "interval": 120, "tolerance": 50, "timeout": 8000, "lazy": False},
            ],
            "rules": ["MATCH,PROXY"],
        }
        dump_yaml_atomic(output_yaml, out, dumper=_SafeDumper)
        print(f"[*] 已写入高速精选: {output_yaml} ({len(fast_nodes)} 节点)", flush=True)

        # 实测速率旁路文件：供来源质量打分使用（原先这份信息只以"节点名后缀"的形式存在）
        try:
            speeds_path = os.path.join(os.path.dirname(os.path.abspath(output_yaml)),
                                       "outputs", "fast_speed.json")
            write_text_atomic(speeds_path, json.dumps(
                {"ts": time.time(), "unit": "MB/s", "speeds": speeds}, ensure_ascii=False))
            print(f"[*] 实测速率旁路已写: {speeds_path} ({len(speeds)} 条)", flush=True)
        except Exception as e:
            print(f"[!] 速率旁路写入失败(非致命): {e}", flush=True)
    finally:
        pass


async def run_slice(nodes, wid, nbytes, timeout):
    """单个 mihomo 实例负责一批节点的串行测速; 多个 run_slice 并发 -> 整体提速约 K 倍。
    每个实例用独立的 API/mixed 端口, 互不干扰(单实例内仍用 select 切节点, 实例间真并发)。"""
    api_port = 29097 + wid
    mixed_port = 28900 + wid
    cdata = {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{api_port}",
        "secret": API_SECRET,
        "allow-lan": False,
        "mode": "global",
        "proxies": nodes,
        "proxy-groups": [
            {"name": "__SPEEDTEST__", "type": "select",
             "proxies": ["DIRECT"] + [p["name"] for p in nodes]},
        ],
        "rules": ["MATCH,__SPEEDTEST__"],
    }
    tmp = os.path.join(tempdir_for_run("speed"), f"slice-{wid}.yaml")
    dump_yaml_atomic(tmp, cdata, dumper=_SafeDumper)
    mihomo = find_mihomo()
    if not mihomo:
        print(f"[!] worker {wid}: 找不到 mihomo 核心，跳过该分片", flush=True)
        return {}
    try:
        proc = subprocess.Popen([mihomo, "-f", tmp], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                # CREATE_NO_WINDOW 只在 Windows 上存在；Linux 访问该属性会
                                # AttributeError 直接崩掉测速。补齐项目其它 3 处已有的平台守卫。
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            ready = False
            for _ in range(45):
                if port_up(api_port):
                    ready = True
                    break
                await asyncio.sleep(1)
            if not ready:
                print(f"[!] worker {wid}: mihomo API 未就绪", flush=True)
                return {}
            base = f"http://127.0.0.1:{api_port}"
            hdr = {"Authorization": f"Bearer {API_SECRET}"}
            proxy_url = f"http://127.0.0.1:{mixed_port}"
            results = {}
            async with aiohttp.ClientSession() as s:
                for idx, p in enumerate(nodes):
                    name = p["name"]
                    try:
                        async with s.put(f"{base}/proxies/__SPEEDTEST__",
                                         json={"name": name}, headers=hdr,
                                         timeout=aiohttp.ClientTimeout(total=10)) as r:
                            if r.status not in (204, 200):
                                results[name] = 0.0
                                continue
                    except Exception:
                        results[name] = 0.0
                        continue
                    await asyncio.sleep(0.4)
                    mbps = await measure(name, s, proxy_url, nbytes, timeout)
                    results[name] = mbps
                    if (idx + 1) % 50 == 0 or mbps >= 2.0:
                        print(f"    [w{wid} {idx+1}/{len(nodes)}] {name[:34]:36s} {mbps:.2f} MB/s", flush=True)
            return results
        finally:
            kill_proc(proc)
    finally:
        # 临时配置放系统临时区：被中断时不再把 tmp*.tmp 漏在项目根
        remove_quiet(tmp)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--bytes", type=int, default=3000000)
    ap.add_argument("--timeout", type=int, default=8)
    ap.add_argument("--min-mbps", type=float, default=0.15,
                    help="低于此速率(MB/s)的节点视为不达标, 不进入高速精选")
    ap.add_argument("--speed-in-name", action="store_true",
                    help="把实测速率写进节点名（客户端直观可见，但名字每轮会变、"
                         "会重置客户端的选中节点状态；默认关闭）")
    args = ap.parse_args()
    asyncio.run(main(args.input, args.output, args.top, args.bytes, args.timeout,
                     args.min_mbps, args.speed_in_name))
