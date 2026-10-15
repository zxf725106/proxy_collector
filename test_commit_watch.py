# -*- coding: utf-8 -*-
"""GitHubCommitWatcher 离线自测（mock _http_get，不触网）。

运行： python test_commit_watch.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import source_discovery as sd

PASS = FAIL = 0


def check(c, m):
    global PASS, FAIL
    if c:
        PASS += 1
        print(f"  ok  - {m}")
    else:
        FAIL += 1
        print(f"  FAIL- {m}")


# mock：commit 列表 + 某次提交改动的 README 里的订阅链接
_COMMITS = '[{"sha":"abc123","files":[{"filename":"README.md"}]}]'
_README = "# 新鲜节点\n订阅 https://fresh.example.com/clash.yaml\nvmess://freshnode==#x\n"


def _fake_http_get(url, *a, **k):
    if "/commits" in url:
        return _COMMITS
    if "raw.githubusercontent.com" in url and "/abc123/README.md" in url:
        return _README
    raise RuntimeError(f"unexpected url: {url}")


# 替换模块级 _http_get
_orig = sd._http_get
sd._http_get = _fake_http_get
try:
    w = sd.GitHubCommitWatcher(repos=["some/repo"], commits_per_repo=1)
    urls = w.watch()
finally:
    sd._http_get = _orig

check(any("fresh.example.com/clash.yaml" in u for u in urls), "从 commit 抓到订阅链接")
check(any(u.startswith("vmess://") for u in urls), "从 commit 抓到裸代理 URI")
check("some/repo" in repr(w.repos), "repos 参数生效")

# discover_all 路由：commit_watch 开启时接入
sd._http_get = _fake_http_get
try:
    cfg = {"discovery": {"github": {"enabled": True, "commit_watch": True,
                                    "repos": ["some/repo"]},
                         "telegram": {"enabled": False}, "twitter": {"enabled": False}}}
    res = sd.discover_all(cfg)
finally:
    sd._http_get = _orig
check(any("fresh.example.com/clash.yaml" in u for u in res), "discover_all 接入 commit_watch 生效")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
