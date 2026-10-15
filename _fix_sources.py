import re, shutil, sys
p = r"C:/Users/zxf72/proxy_collector/config.yaml"
shutil.copy(p, p + ".runbak")  # 备份
s = open(p, encoding="utf-8").read()
lines = s.splitlines()
out = []
for ln in lines:
    m = re.search(r"Danialsamadi/v2go/main/Sub(\d+)\.txt", ln)
    if m and 10 <= int(m.group(1)) <= 30:
        continue  # 丢弃失效源 Sub10-Sub30
    out.append(ln)
s2 = "\n".join(out)
# 设置 max_nodes 上限（给 http_test 限流），不改其它段
s2 = re.sub(r"max_nodes:\s*\d+", "max_nodes: 20000", s2)
# 末尾保留一个换行
s2 = s2.rstrip("\n") + "\n"
open(p, "w", encoding="utf-8").write(s2)
print("修改后 subscriptions 数:", len(re.findall(r"^- https://", s2, re.M)))
print("含 Pasimand 前:", "Pasimand" in s2)
# 追加 Pasimand（若没有）
if "Pasimand" not in s2:
    s2 = s2.replace(
        "filter:",
        "- https://raw.githubusercontent.com/Pasimand/v2ray-config-agg/main/config.txt\nfilter:",
        1,
    )
    open(p, "w", encoding="utf-8").write(s2)
    print("已追加 Pasimand")
print("max_nodes=20000:", "max_nodes: 20000" in s2)
print("含 Sub10-Sub30 残留:", bool(re.search(r"v2go/main/Sub(1[0-9]|2[0-9]|30)\.txt", s2)))
