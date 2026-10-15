"""修复 fast-mobile 版：
1) AUTO 组补 tolerance: 50, timeout: 8000 + url 改 https
2) PROXY 组首成员 AUTO -> Top1 具体节点，AUTO 移到第 2 位
3) 验证 yaml 合法
"""
import re
import sys
import yaml

SRC = r'C:/Users/zxf72/proxy_collector/output-filtered-fast-mobile.yaml'
DST = r'C:/Users/zxf72/proxy_collector/output-filtered-fast-mobile-fixed.yaml'

text = open(SRC, encoding='utf-8').read()

# --- 1) AUTO 组补丁：url 改 https + 补 tolerance/timeout ---
# 原文 AUTO 块尾巴:
#   url: http://www.gstatic.com/generate_204
#   interval: 300
# rules:
old_auto_tail = (
    "  url: http://www.gstatic.com/generate_204\n"
    "  interval: 300\n"
    "rules:"
)
new_auto_tail = (
    "  url: https://www.gstatic.com/generate_204\n"
    "  interval: 300\n"
    "  tolerance: 50\n"
    "  timeout: 8000\n"
    "rules:"
)
assert old_auto_tail in text, "未找到 AUTO 组尾部，文件结构可能已变"
text = text.replace(old_auto_tail, new_auto_tail, 1)
print("[1] AUTO 组补 tolerance/timeout + url 改 https: OK")

# --- 2) PROXY 组首成员调整 ---
# 原文 PROXY 组:
# - name: PROXY
#   type: select
#   proxies:
#   - AUTO
#   - 🇸🇬 Singapore | TROJAN | www.ddwhm.com:443
#   ...
old_proxy_head = (
    "- name: PROXY\n"
    "  type: select\n"
    "  proxies:\n"
    "  - AUTO\n"
)
# 选 Top1 节点：🇭🇰 Hong Kong | SS | 103.192.179.132:443（speed_test 测过，最快之一）
top1 = "  - \U0001F1ED\U0001F1F0 Hong Kong | SS | 103.192.179.132:443\n"
auto_after = "  - AUTO\n"
new_proxy_head = (
    "- name: PROXY\n"
    "  type: select\n"
    "  proxies:\n"
    + top1
    + auto_after
)
assert old_proxy_head in text, "未找到 PROXY 组首段，文件结构可能已变"
text = text.replace(old_proxy_head, new_proxy_head, 1)
print("[2] PROXY 组首成员改为具体快节点，AUTO 移到第 2 位: OK")

# --- 3) 落盘 + yaml 验证 ---
with open(DST, 'w', encoding='utf-8') as f:
    f.write(text)
print(f"[3] 落盘: {DST}")

try:
    d = yaml.safe_load(text)
    print(f"[4] YAML 合法: proxies={len(d['proxies'])}, groups={len(d['proxy-groups'])}")
    for g in d['proxy-groups']:
        if g.get('type') in ('select', 'url-test'):
            extras = {k: g.get(k) for k in ('url', 'interval', 'tolerance', 'timeout') if g.get(k) is not None}
            print(f"     group[{g['name']}] type={g['type']} proxies={len(g.get('proxies',[]))} {extras}")
except Exception as e:
    print(f"[4] YAML 校验失败: {e}", file=sys.stderr)
    sys.exit(1)
