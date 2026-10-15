# -*- coding: utf-8 -*-
"""
source_discovery.py — 自动发现免费代理源（移植自 aggregator 的 Telegram/GitHub/Twitter 爬虫）
================================================================================================

把 aggregator 的「源发现」能力单独拆出来：不是直接产出节点，而是
**发现订阅 URL / 原始代理链接**，再交给已有的 subscription_parser.parse_subscription()
解析成通用 URI，并入 proxy_collector 主链路。

  发现源(URL/link) --> parse_subscription() --> 通用 URI --> 去重/TCP/mihomo/输出

三大源可行性（务必读）：
  * GitHub  : 高。Search API + 扫已知代理仓库 raw 文件，提取订阅链接。需 GITHUB_TOKEN(建议) 与网络。
  * Telegram: 中。telethon(api_id/api_hash/session) 最稳；t.me/s 网页抓取是脆弱降级方案。
  * Twitter : 低。官方已锁死，nitter 镜像多挂。本模块提供"尽力而为"版，可能空手而归，勿依赖。

依赖：仅标准库（urllib）。telethon 为 Telegram API 模式的可选依赖，未装时自动降级到网页抓取。
运行位置：必须在能访问 github.com / t.me 的机器上跑（你本机走 Clash 7897）。

模块只负责"发现 URL"，不负责验证/测速——那部分交给 proxy_collector 现有链路。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
import urllib.error
import urllib.parse
from typing import Iterable, Optional

# 提取订阅/代理链接的正则（共享）
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
_SUB_EXT_RE = re.compile(r"https?://[^\s\"'<>)\]]+\.(?:yaml|yml|txt|json)(?:[?&][^\s\"'<>)\]]*)?", re.I)
_TME_RE = re.compile(r"https?://t\.me/[^\s\"'<>)\]]+", re.I)
_PROXY_LINK_RE = re.compile(
    r"(?:ss|ssr|vmess|vless|trojan|snell|hysteria2|hysteria|socks5?|http)://[^\s\"'<>)\]]+", re.I
)

# 已知代理列表仓库（直接扫 raw 文件，命中率高）
KNOWN_REPOS = [
    "proxifly/free-proxy-list",
    "v2rayN/v2rayN",
    "aidan-github/proxy-list",
    "clash-verge-rev/clash-verge-rev",  # 社区订阅常出现
]

GITHUB_SEARCH_TOPICS = ["proxypool", "free-proxy", "v2ray", "clash", "subscription"]


def _http_get(url: str, timeout: int = 20, headers: Optional[dict] = None,
              retries: int = 2) -> str:
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36",
        "Accept": "*/*",
    }
    if headers:
        hdrs.update(headers)
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        hdrs["Authorization"] = f"Bearer {token}"
    last: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last = e
    raise RuntimeError(f"http_get failed: {url} -> {last}")


# ===========================================================================
# GitHub 源发现
# ===========================================================================
class GitHubDiscoverer:
    """搜索 GitHub 上的代理列表仓库，并扫描其 raw 文件提取订阅 URL。"""

    def __init__(self, repos: Optional[list[str]] = None,
                 topics: Optional[list[str]] = None,
                 queries: Optional[list[str]] = None,
                 max_repos: int = 20):
        self.repos = list(repos or KNOWN_REPOS)
        self.topics = list(topics or GITHUB_SEARCH_TOPICS)
        self.queries = list(queries or [])
        self.max_repos = max_repos

    def discover(self) -> list[str]:
        urls: list[str] = []
        # 1) 已知仓库直接扫
        for repo in self.repos:
            urls += self._scan_repo(repo)
        # 2) 按 topic / query 搜索仓库
        for q in self.queries + [f"topic:{t}" for t in self.topics]:
            try:
                found = self._search_repos(q)
            except Exception as e:
                print(f"[github] search '{q}' failed: {e}")
                continue
            for repo in found[: self.max_repos]:
                urls += self._scan_repo(repo)
        return _dedupe(urls)

    def _search_repos(self, query: str) -> list[str]:
        q = urllib.parse.quote(query + " proxy OR v2ray OR clash")
        api = f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=20"
        doc = json.loads(_http_get(api))
        return [r["full_name"] for r in doc.get("items", [])]

    def _scan_repo(self, repo: str) -> list[str]:
        out: list[str] = []
        # 候选 raw 路径
        candidates = [
            "proxies/all/data.json", "proxies/data.json", "README.md",
            "clash.yaml", "subscribe.txt", "proxies.txt", "list.txt",
        ]
        for path in candidates:
            raw = f"https://raw.githubusercontent.com/{repo}/main/{path}"
            try:
                text = _http_get(raw, retries=1)
            except Exception:
                # 试 master 分支
                try:
                    text = _http_get(raw.replace("/main/", "/master/"), retries=1)
                except Exception:
                    continue
            out += self._extract_from_text(text)
        return out

    @staticmethod
    def _extract_from_text(text: str) -> list[str]:
        urls: list[str] = []
        # 订阅文件链接优先
        urls += _SUB_EXT_RE.findall(text)
        # t.me 频道/订阅链接
        urls += _TME_RE.findall(text)
        # 直接裸露的代理 URI（如 data.json 里）
        urls += _PROXY_LINK_RE.findall(text)
        # README 里常见的 [订阅](url) / url 形式
        urls += _URL_RE.findall(text)
        return urls


# ===========================================================================
# Telegram 源发现
# ===========================================================================
class TelegramDiscoverer:
    """从 Telegram 频道发现代理源。

    两种模式：
      * api 模式（稳）：需 TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION 环境变量，
        用 telethon 读频道最近 N 条消息，提取链接/代理 URI。
      * web 模式（脆弱降级）：抓 https://t.me/s/{channel}，正则提取。常被拦。
    """

    def __init__(self, channels: Iterable[str], limit: int = 50, use_api: bool = True):
        self.channels = list(channels)
        self.limit = limit
        self.use_api = use_api and bool(os.environ.get("TELEGRAM_API_ID"))

    def discover(self) -> list[str]:
        if self.use_api:
            try:
                return self._discover_api()
            except Exception as e:
                print(f"[telegram] api 模式失败，降级网页: {e}")
        return self._discover_web()

    def _discover_api(self) -> list[str]:
        try:
            from telethon import TelegramClient  # type: ignore
        except ImportError:
            print("[telegram] 未安装 telethon，改用网页模式")
            return self._discover_web()
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
        session = os.environ.get("TELEGRAM_SESSION", "proxy_discovery")
        urls: list[str] = []
        async def _run():
            async with TelegramClient(session, api_id, api_hash) as client:
                for ch in self.channels:
                    try:
                        async for msg in client.iter_messages(ch, limit=self.limit):
                            if msg.text:
                                urls.extend(self._extract(msg.text))
                    except Exception as e:
                        print(f"[telegram] channel {ch} failed: {e}")
        import asyncio
        asyncio.run(_run())
        return _dedupe(urls)

    def _discover_web(self) -> list[str]:
        urls: list[str] = []
        for ch in self.channels:
            name = ch.lstrip("@").split("/")[-1]
            url = f"https://t.me/s/{name}"
            try:
                text = _http_get(url, retries=1)
                # t.me/s 里链接在 href，提取 http(s) 部分
                urls += self._extract(text)
            except Exception as e:
                print(f"[telegram] web {ch} failed: {e}")
        return _dedupe(urls)

    @staticmethod
    def _extract(text: str) -> list[str]:
        out = _PROXY_LINK_RE.findall(text)
        out += _SUB_EXT_RE.findall(text)
        out += _TME_RE.findall(text)
        out += _URL_RE.findall(text)
        return out


# ===========================================================================
# Twitter / X 源发现（尽力而为，大概率空手）
# ===========================================================================
class TwitterDiscoverer:
    """从 Twitter/X 发现代理源。

    ⚠️ 现实：X 官方 API 收费且锁死；nitter 公共实例大量失效。
    本类尝试若干 nitter 镜像抓用户时间线，提取 t.co 展开后的链接。
    大概率返回空列表——把它当"锦上添花"，不要依赖。
    """

    NITTER_INSTANCES = [
        "https://nitter.net", "https://nitter.poast.org", "https://nitter.privacydev.net",
        "https://nitter.1d4.us", "https://nitter.kavin.rocks",
    ]

    def __init__(self, users: Iterable[str], limit: int = 30):
        self.users = list(users)
        self.limit = limit

    def discover(self) -> list[str]:
        urls: list[str] = []
        for user in self.users:
            name = user.lstrip("@")
            for inst in self.NITTER_INSTANCES:
                try:
                    text = _http_get(f"{inst}/{name}", retries=1, timeout=15)
                    urls += self._extract(text)
                    break  # 一个可用实例即可
                except Exception:
                    continue
        if not urls:
            print("[twitter] 所有 nitter 实例失败（大概率，X 已锁死），返回空")
        return _dedupe(urls)

    @staticmethod
    def _extract(text: str) -> list[str]:
        out = _PROXY_LINK_RE.findall(text)
        out += _SUB_EXT_RE.findall(text)
        out += _URL_RE.findall(text)
        # 去掉 nitter 站内链接
        return [u for u in out if "nitter" not in u.lower()]


# ===========================================================================
# GitHub commit-watch（新鲜度优先：抢在节点失效前抓到）
# ===========================================================================
class GitHubCommitWatcher:
    """监听已知仓库最近若干次 commit，抓取「提交时刻」的订阅文件。

    和 GitHubDiscoverer（扫仓库当前 raw 文件）的区别：
      * 当前 raw 文件里的节点可能已经公开几天、被用烂；
      * commit-watch 抓的是「最近 N 次提交」里改动的订阅文件，
        节点往往刚被推送几分钟，验证通过率远高于陈年老库。
    依赖：需 GITHUB_TOKEN（API 速率上限）；需能访问 api.github.com（走 Clash）。
    """

    # 候选订阅文件路径（与 GitHubDiscoverer 保持一致）
    CANDIDATE_PATHS = [
        "proxies/all/data.json", "proxies/data.json", "README.md",
        "clash.yaml", "subscribe.txt", "proxies.txt", "list.txt",
    ]

    def __init__(self, repos: Optional[list[str]] = None,
                 commits_per_repo: int = 5,
                 candidate_paths: Optional[list[str]] = None):
        self.repos = list(repos or KNOWN_REPOS)
        self.commits_per_repo = max(1, int(commits_per_repo))
        self.candidate_paths = list(candidate_paths or self.CANDIDATE_PATHS)

    def watch(self) -> list[str]:
        urls: list[str] = []
        for repo in self.repos:
            urls += self._watch_repo(repo)
        return _dedupe(urls)

    def _watch_repo(self, repo: str) -> list[str]:
        out: list[str] = []
        api = (f"https://api.github.com/repos/{repo}/commits"
               f"?per_page={self.commits_per_repo}")
        try:
            doc = json.loads(_http_get(api))
        except Exception as e:
            print(f"[commit-watch] list commits failed for {repo}: {e}")
            return out
        if not isinstance(doc, list):
            return out
        for c in doc:
            sha = (c or {}).get("sha")
            if not sha:
                continue
            # 只读本次提交里「真的改动过」的候选路径，省请求
            changed = [(f.get("filename") or "") for f in (c.get("files") or [])]
            for path in self.candidate_paths:
                if not any(path == cf or cf.endswith("/" + path) or path in cf
                           for cf in changed):
                    continue
                raw = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
                try:
                    text = _http_get(raw, retries=1)
                except Exception:
                    continue
                out += GitHubDiscoverer._extract_from_text(text)
        return out


# ===========================================================================
# 统一入口
# ===========================================================================
def discover_all(config: dict) -> list[str]:
    """按 config 跑启用的发现源，返回去重后的「URL/链接」列表。

    config 形如 discovery_config.example.json 的 discovery 段。
    返回的是订阅 URL / 代理链接，需再经 parse_subscription() 解析。
    """
    urls: list[str] = []
    d = config.get("discovery", config)

    gh = d.get("github")
    if gh and gh.get("enabled", True):
        urls += GitHubDiscoverer(
            repos=gh.get("repos"), topics=gh.get("topics"),
            queries=gh.get("queries"), max_repos=gh.get("max_repos", 20),
        ).discover()
        # 新鲜度优先：监听最近提交，抢在节点失效前抓到
        if gh.get("commit_watch", False):
            urls += GitHubCommitWatcher(
                repos=gh.get("repos") or KNOWN_REPOS,
                commits_per_repo=gh.get("commit_watch_commits", 5),
            ).watch()

    tg = d.get("telegram")
    if tg and tg.get("enabled", False):
        urls += TelegramDiscoverer(
            channels=tg.get("channels", []), limit=tg.get("limit", 50),
            use_api=tg.get("use_api", True),
        ).discover()

    tw = d.get("twitter")
    if tw and tw.get("enabled", False):
        urls += TwitterDiscoverer(users=tw.get("users", []), limit=tw.get("limit", 30)).discover()

    return _dedupe(urls)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen = set()
    out = []
    for u in items:
        u = u.strip().rstrip(")").rstrip("]").rstrip('"').rstrip("'")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


if __name__ == "__main__":
    import sys, yaml  # yaml 可选

    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "discovery_config.example.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) if yaml else json.load(f)
    for u in discover_all(cfg):
        print(u)
