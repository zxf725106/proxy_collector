"""从 speed_test 产出的桌面高速精选版生成手机兼容版。
修复项：
1) 去掉 mixed-port（手机 Clash 用 VPN 模式，不需要本地 mixed 端口）
2) AUTO 组确保有 url/interval/tolerance/timeout（缺失则补）
3) PROXY 组首成员从 AUTO 改为 Top1 具体快节点（手机打开即用，不等测速）
4) 用 output._SafeDumper + _defuse_shortid 写出：safe_load 之后 reality short-id 的
   引号标记已丢失，原先直接 safe_dump 会把 3e58 写成科学计数法（mihomo 报 invalid short ID）
5) 原子写 + 占用重试：原先目标文件被占用会抛 PermissionError 并中止整条流水线
用法:
    python make_mobile.py <fast.yaml> <out_mobile.yaml>
"""
import os
import sys

import yaml

from atomic import dump_yaml_atomic
from output import _SafeDumper, _defuse_shortid, ensure_unique_proxy_names


def _defuse_all(d):
    """给所有 reality-opts.short-id 重新加引号（safe_load 后标记丢失）。"""
    for p in d.get('proxies', []) or []:
        if p.get('reality-opts'):
            p['reality-opts'] = _defuse_shortid(p['reality-opts'])
    return d


def main(src, dst):
    with open(src, encoding='utf-8') as f:
        d = yaml.safe_load(f)

    # 1) 去掉 mobile 不需要的顶层端口
    for k in ('mixed-port', 'port', 'socks-port', 'redir-port', 'tproxy-port', 'external-controller'):
        d.pop(k, None)

    groups = {g['name']: g for g in d.get('proxy-groups', [])}
    proxies = d.get('proxies', [])

    # 2) AUTO 组兜底补 tolerance/timeout
    auto = groups.get('AUTO')
    if auto is not None:
        auto.setdefault('url', 'https://www.gstatic.com/generate_204')
        auto.setdefault('interval', 300)
        auto.setdefault('tolerance', 50)
        auto.setdefault('timeout', 8000)

    # 3) PROXY 首成员改为 Top1 具体节点（proxies 第一个即最快）
    proxy = groups.get('PROXY')
    if proxy is not None and proxies:
        top1 = proxies[0]['name']
        members = proxy.get('proxies', [])
        if members and members[0] == 'AUTO':
            members[0] = top1
            # 保证 AUTO 还在组内（放到第 2 位）
            if 'AUTO' not in members:
                members.insert(1, 'AUTO')
        else:
            members = [top1] + ['AUTO'] + [m for m in members if m not in (top1, 'AUTO')]
        proxy['proxies'] = members

    # 原子写回（带占用重试），避免手机配置在替换过程中出现半截 YAML
    _defuse_all(d)
    # 手机端同样会因重名整份拒收，这里再兜一道（FAST 侧已由 finalize_deploy_config 保证）
    renamed = ensure_unique_proxy_names(d.get('proxies', []) or [])
    if renamed:
        print(f"  重名修正: {len(renamed)} 个节点追加了序号")
    dump_yaml_atomic(dst, d, dumper=_SafeDumper)

    # 校验
    proxy_names = {p['name'] for p in proxies}
    group_names = {g['name'] for g in d.get('proxy-groups', [])}
    dangling = []
    for g in d.get('proxy-groups', []):
        for ref in g.get('proxies', []):
            if ref not in proxy_names and ref not in group_names:
                dangling.append((g['name'], ref))
    print(f"Mobile 版已生成: {dst}")
    print(f"  proxies={len(proxies)} groups={len(d.get('proxy-groups', []))}")
    print(f"  AUTO: { {k:auto.get(k) for k in ('url','interval','tolerance','timeout')} if auto else None}")
    if proxy:
        print(f"  PROXY 首成员: {proxy['proxies'][0]}")
    print(f"  悬空引用: {len(dangling)}")
    if dangling:
        for g, r in dangling[:10]:
            print(f"    DANGLING {g} -> {r}")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: python make_mobile.py <fast.yaml> <out_mobile.yaml>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
