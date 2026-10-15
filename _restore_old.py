#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并旧 FAST(.bak) 优质节点 + 当前 FAST，用 mihomo 真实代理复测，
仅保留仍可用的，写回 output-filtered-fast.yaml 并重新部署到 Clash Verge。

目的：更新时保留上一轮仍可用的老节点，避免被 speed_test 重排名整批冲掉。
（用户反馈：更新后原来的节点全没了，要求先看看原来的 yaml、把优质节点保留下来。）
"""
import os, time, json, yaml, tempfile, asyncio, aiohttp, urllib.request, urllib.parse, shutil, subprocess, sys

VPY = r"C:\Users\zxf72\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
BASE = r"C:\Users\zxf72\proxy_collector"
MIHOMO = r"C:\Program Files\Clash Verge\verge-mihomo.exe"
FAST = "output-filtered-fast.yaml"
OLD = "output-filtered-fast.yaml.bak"
MOBILE = "output-filtered-fast-mobile.yaml"
STABLE_HISTORY = "node_stability.json"

RETEST_URLS = [
    "http://www.gstatic.com/generate_204",
    "http://www.google.com/generate_204",
    "http://cp.cloudflare.com/generate_204",
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def fingerprint(p):
    from parser import proxy_identity
    return proxy_identity(p)


def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def atomic_copy(src, dst):
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def verify_nodes(nodes, port=17901, api="127.0.0.1:19098", secret="restore"):
    if not os.path.exists(MIHOMO):
        log(f"!! 未找到 mihomo: {MIHOMO}")
        return set()
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
            "proxies": ["DIRECT"] + [p["name"] for p in nodes],
        }],
        "rules": ["MATCH,__VERIFY__"],
    }
    fd, tmp = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    proc = subprocess.Popen([MIHOMO, "-f", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
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
        log("!! mihomo 未就绪，无法复测")
        proc.kill()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return set()

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
                            return name, True, float(d)
                except Exception:
                    continue
        return name, False, 0.0

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

    alive = {name for name, ok, val in results if ok}
    for name, ok, val in results:
        log(f"   复测 {name[:42]:42s} {'OK ' if ok else 'DEAD'} {val:.0f} ms")
    return alive


def update_stability(alive_nodes, history_path):
    """给合并进来的老节点补稳定性信用，避免下轮 strict 模式把它们当一次性节点。"""
    now = time.time()
    history = {}
    if os.path.exists(history_path):
        try:
            history = json.load(open(history_path, encoding="utf-8"))
        except Exception:
            history = {}
    for p in alive_nodes:
        fp = fingerprint(p)
        info = history.get(fp, {"n": 0})
        info["n"] = max(info.get("n", 0), 2)   # 至少算稳定 2 轮
        info["name"] = p.get("name", "")
        info["ts"] = now
        history[fp] = info
    json.dump(history, open(history_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    log(f"稳定性库已更新（{len(history)} 节点）")


def main():
    os.chdir(BASE)
    log("==== 合并旧节点 + 重新复测 + 重部署 ====")
    if not os.path.exists(OLD):
        log("!! 无 .bak 旧 FAST，无法恢复旧节点")
        sys.exit(1)
    old_d = load(OLD)
    new_d = load(FAST)
    old_ps = old_d.get("proxies", [])
    new_ps = new_d.get("proxies", [])
    log(f"旧 FAST(.bak): {len(old_ps)} 节点 | 新 FAST: {len(new_ps)} 节点")

    # 合并去重：按 fingerprint，旧节点优先保留（已验证可用的老节点优先）
    by_fp = {}
    old_names, new_names = set(), set()
    for p in old_ps:
        fp = fingerprint(p)
        if fp not in by_fp:
            by_fp[fp] = p
            old_names.add(p.get("name"))
    for p in new_ps:
        fp = fingerprint(p)
        if fp not in by_fp:
            by_fp[fp] = p
            new_names.add(p.get("name"))
    merged = list(by_fp.values())
    log(f"合并去重后唯一节点: {len(merged)}（旧唯一 {len(old_names)} + 新增唯一 {len(new_names)}）")

    # 真实代理复测（并发）
    alive_set = verify_nodes(merged)
    alive_nodes = [p for p in merged if p.get("name") in alive_set]
    dead = len(merged) - len(alive_nodes)
    log(f"复测：存活 {len(alive_nodes)} / 总 {len(merged)}（剔除死亡 {dead}）")

    alive_names = {p["name"] for p in alive_nodes}

    # 重建 proxy-groups：保证所有存活节点都被引用
    out = dict(new_d)
    out["proxies"] = alive_nodes
    groups = new_d.get("proxy-groups", [])
    for g in groups:
        gname = g.get("name")
        if gname == "PROXY":
            g["proxies"] = ["AUTO"] + [p["name"] for p in alive_nodes]
        elif gname == "AUTO":
            g["proxies"] = [p["name"] for p in alive_nodes]   # 全部参与 url-test
        else:
            g["proxies"] = [m for m in g.get("proxies", []) if m in alive_names or m in {"DIRECT", "REJECT", "PROXY-DIRECT", "PROXY", "AUTO"}]
    out["proxy-groups"] = groups

    # 写回 FAST（先备份当前版）
    atomic_copy(FAST, FAST + ".prerestore.bak")
    fd, t = tempfile.mkstemp(dir=BASE, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(t, FAST)
    log(f"已写回 {FAST}（{len(alive_nodes)} 节点）；覆盖前版本存 {FAST}.prerestore.bak")

    update_stability(alive_nodes, STABLE_HISTORY)

    # 重新生成 mobile
    log(">>> make_mobile.py")
    subprocess.run([VPY, "make_mobile.py", FAST, MOBILE], cwd=BASE, check=True)
    # 重新部署到 Clash Verge
    log(">>> deploy_to_clash.py")
    subprocess.run([VPY, "deploy_to_clash.py"], cwd=BASE, check=True)
    log("==== 合并+复测+重部署 完成 ====")


if __name__ == "__main__":
    main()
