#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Clash Verge profiles.yaml 中移除 FreeSub 17170010.yaml 远程配置及其 5 个增强组件。
用户已确认 option 1：放弃该配置，只用自动部署的 output-filtered-fast.yaml。
- 备份 profiles.yaml
- 删除 6 个 uid 条目
- 把对应物理文件移到 _freesub_removed/ 以便可逆
"""
import os, shutil, time
import yaml

BASE = r"C:\Users\zxf72\AppData\Roaming\io.github.clash-verge-rev.clash-verge-rev"
PROFILE_DIR = os.path.join(BASE, "profiles")
PYAML = os.path.join(BASE, "profiles.yaml")

TARGET_UIDS = {
    "RD2k0o1Rm3QZ",  # 远程主配置 17170010.yaml
    "mLnrEIdki5lW",  # merge
    "sCLsXqzf4nW8",  # script
    "rQUxtsfEeXWi",  # rules
    "p3N2NnIfAlmQ",  # proxies
    "gCBHRN7qUaUr",  # groups
}

# 1. 备份
bak = PYAML + f".bak.freesub_{int(time.time())}"
shutil.copy(PYAML, bak)
print("BACKUP=" + bak)

# 2. 读
with open(PYAML, encoding="utf-8") as f:
    doc = yaml.safe_load(f)

items = doc.get("items", []) or []
before = len(items)
removed = [it.get("uid") for it in items if it.get("uid") in TARGET_UIDS]
kept = [it for it in items if it.get("uid") not in TARGET_UIDS]
doc["items"] = kept

# 3. current 校验：若指向被删条目，回退到自动部署条目
cur = doc.get("current")
if cur in TARGET_UIDS:
    auto = [it.get("uid") for it in kept if it.get("name") == "output-filtered-fast.yaml"]
    doc["current"] = auto[0] if auto else cur
    print("CURRENT_REPOINTED=" + doc["current"])

# 4. 写回
with open(PYAML, "w", encoding="utf-8") as f:
    yaml.safe_dump(doc, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print("REMOVED_COUNT=" + str(len(removed)))
print("REMOVED_UIDS=" + str(removed))
print("ITEMS_BEFORE=" + str(before) + " AFTER=" + str(len(kept)))

# 5. 物理文件隔离（不硬删，移走以便可逆）
quar = os.path.join(BASE, "_freesub_removed")
os.makedirs(quar, exist_ok=True)
moved = []
uid_to_file = {it.get("uid"): it.get("file") for it in items}
for uid in removed:
    fn = uid_to_file.get(uid)
    if not fn:
        continue
    sp = os.path.join(PROFILE_DIR, fn)
    if os.path.exists(sp):
        dp = os.path.join(quar, fn)
        shutil.move(sp, dp)
        moved.append(fn)
print("MOVED_FILES=" + str(moved))
print("QUARANTINE_DIR=" + quar)
