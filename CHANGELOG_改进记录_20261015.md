# 改进记录（2026-10-15）

一次代码审查后的批量修复。**每一条都附了证据与验证方式**；所有改动在落地前都在
项目外的副本里跑过 8 个回归测试 + 3 项有界端到端验证。

备份位置：`_backup_before_improve_20261015/`（原文件全量副本，可直接覆盖回去回滚）。

---

## 一句话结论（单轮耗时已用完整运行实测校正）

| 维度 | 改动前 | 改动后 |
|---|---|---|
| **单轮总耗时** | **33 分 32 秒**（2026-10-14 完整运行） | **26 分 0 秒**（2026-10-15 23:10:37–23:36:37 完整运行）⇒ **−22%** |
| http_test 阶段 | 15 分 15 秒（实测 19,840 个节点） | **9 分 55 秒**（只测 10,545 个 TCP 连通节点）⇒ −5 分 20 秒 |
| speed_test + 复测链 | 8 分 57 秒 | **7 分 35 秒** ⇒ −1 分 22 秒（超时 25→15s） |
| collector 阶段 | 7 分 37 秒 | **6 分 54 秒** ⇒ −43 秒（其中重复解析只占约 10 秒，其余是波动） |
| collect_aggregator | 1 分 40 秒 | 1 分 34 秒（未改动，仍有一次 90 秒 discovery 白等） |
| 部署配置的 AUTO 组 | **实测 254 个成员** | **实测 30 个成员**（日志：`AUTO 组收敛: AUTO组 351 -> 30 个成员`） |
| 部署文件 short-id 引号 | 实测 0 条带引号（4 条裸写，mihomo 会当浮点数） | **实测 4 条全部带引号、0 条裸写** |
| 来源质量统计 | 41 个源、32 个停用、`avg_latency` 非零的源 **0 个**、通过率≈7e-05（v2 身份对不上，全是噪声） | 39 个源、通过率 **0.000%–3.030%**（真实值）、`avg_latency` 非零的源 2 个 |
| 项目根的临时文件 | 中断一次留 8 个 `tmp*.tmp` | **实测 0 个**（改到 `%TEMP%\proxy_collector\`，且步骤结束即清理） |

### 完整运行的阶段明细（2026-10-15，rc=0）

| 阶段 | 时间 | 耗时 |
|---|---|---|
| collect_aggregator | 23:10:37 → 23:12:11 | 1 分 34 秒 |
| collector | 23:12:11 → 23:19:05 | 6 分 54 秒 |
| http_test（预筛，跳过 stage2） | 23:19:05 → 23:29:00 | 9 分 55 秒 |
| speed_test | 23:29:00 → 23:35:16 | 6 分 16 秒 |
| 合并旧节点 + 稳定性 + 部署前复测 | 23:35:16 → 23:36:35 | 1 分 19 秒 |
| make_mobile + 部署 + 质量结算 + 多格式 | 23:36:35 → 23:36:37 | 2 秒 |

### collector 的实测阶段明细（2026-10-15 22:52:24 → 22:59:21）

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 抓取 41 个订阅源 | 1 分 33 秒 | 网络决定 |
| 解析 252,787 个节点 | 约 10 秒 | **不是瓶颈** —— 所以合并重复解析只省了约 10 秒 |
| 去重 252,787 → 179,551 | 5 秒 | |
| **TCP 延迟测速 20,000 个节点** | **4 分 57 秒（71%）** | 现在有了正当用途：为 http_test 提供预筛依据 |
| 写 output.yaml (7.3MB) + output-mobile.yaml (6.0MB) | 20 秒 | 原子写 |

---

## 追加修复（2026-10-16 05:35）：重名导致 Clash 整份拒收，流水线却报成功

**现象（你截图的那个弹窗）**：
```
订阅配置校验失败，请检查订阅配置文件，变更已撤销
level=error msg="proxy 🇦🇺 Sydney, AU · G-Core Labs | darling-mutt.rooster465.autos
  :443·trojan·#4 | 0.6MB/s is the duplicate name" ... clash-verge-check.yaml test failed
```
也就是说：**流水线 rc=0 跑完了、也"部署"了，但 Clash Verge 校验不通过并把变更整体回滚
—— 用户端其实什么都没更新。**

**根因**：同一个 `server:port` 上有两个**不同密码**的节点、显示名却完全相同
（免费源常把一个显示名复用到多个账号）：
```
- name: 🇦🇺 ... darling-mutt.rooster465.autos :443·trojan·#4 | 0.6MB/s
  password: FC60532879        <- 行 734
- name: (同上完全相同)
  password: EQ30679154        <- 行 1807
```
它们的**身份指纹不同**（密码参与哈希），所以 `merge_previous_fast` 按指纹去重**折叠不掉**，
重名就一直跟着轮次传递下去。

**为什么流水线自己没发现**（三个盲区）：
1. `pre_deploy_verify` 只把 **top30** 放进临时配置，整份文件里的重名它根本看不到；
2. `profile_quality` 只检查"节点数 + 组引用是否悬空"，不检查重名；
3. `deploy_to_clash` 只负责写文件，不校验。

**顺带解释了昨晚那条 `!! mihomo 未就绪，保守保留全部节点`**：
`_verify_nodes_real` 的临时配置里也含这两个同名节点 → mihomo 加载即拒收 → 实例永远起不来
→ 才走到"不验证就不丢"的保底分支。**同一个重名，同时搞坏了复测和部署。**

**修法（4 处）**：
1. `output.py` 新增 `ensure_unique_proxy_names(proxies)`：重名追加 `" 2"/" 3"`，
   返回被改名清单；`generate_clash_config` 里原有的内联去重逻辑也改为调用它（去重逻辑只有一份）。
2. `merge_previous_fast`：合并后立刻按名称去重，**从源头保证池子干净、不再跨轮传递**。
3. `run_pipeline.py` 新增 `finalize_deploy_config()`：发布前做
   **名称唯一 → 重建 PROXY(`["AUTO"] + 全部节点`)/AUTO(前 30) 组引用 → `mihomo -t` 校验整份配置**；
   校验不过就**拒绝部署并 exit 1**（宁可保持上一版，也不推客户端会拒收的文件）。
   重建组引用是为了让被改名的节点仍然可被选中（不然它会成为"文件里有、但选不到"的孤儿）。
4. `make_mobile.py` 也兜一道去重（手机端同样会因重名整份拒收）。

**验证**：
- 复现：对修复前的文件 `mihomo -t -f` → **exit=1**，打印与截图完全相同的 duplicate name 错误；
- 修复后 → **exit=0 `test is successful`**；
- 当前三个文件（`output-filtered-fast.yaml` / `-mobile.yaml` / 部署出去的
  `profiles\rw451whNWnpB.yaml`）全部 `test is successful`；
- 新增 2 个回归测试（共 10 项，全过）：`test_ensure_unique_proxy_names_fixes_duplicates`、
  `test_finalize_deploy_config_repairs_and_validates`。

**更正**：我先前按行统计报的"5 个重名"是**错的** —— YAML 会把长名字折行，
按行统计只拿到名字的前半段。**真正的重名只有 1 个**（mihomo 修掉它之后就通过了）。

---

### 完整运行中发现的两个问题（都是既有缺陷，不是本次改动引入）1. **自动停用阈值与现实严重不匹配**：现在通过率是真实值了（0.000%–3.030%），
   而 `DISABLE_PASS_RATE = 0.05`（5%）比现实高一个数量级 ⇒ **本轮 39 个源里 21 个被判 OFF**。
   由于 `collector.py` 并不读取这个开关（只对 aggregator 侧生效，而 aggregator 侧样本量
   <200 所以没被停用），目前**没有功能影响**；但这个阈值意味着"任何大流量的免费源都会被停用"，
   一旦真的接上开关就会把订阅源全部关掉。建议改成相对判据（例如只有低于最优源通过率的
   1/5、且样本足够时才停用），而不是写死 5%。
2. **`!! mihomo 未就绪，保守保留全部节点`（23:35:56）**：合并上一轮节点时的复测实例没能在
   20 秒内就绪，于是按"不验证就不丢"的保守策略**保留了全部 351 个节点**，其中
   **219 个本轮没有被验证过**（本轮 HTTP 实测只通过 132 个）。这削弱了"部署的文件此刻一定是活的"
   这一核心承诺。`_verify_nodes_real` 把 mihomo 的 stdout/stderr 丢给了 DEVNULL，
   所以看不到失败原因；建议：① 把 mihomo 输出写到临时日志便于定位；
   ② API 端口被占用时自动换端口重试（20 秒就绪窗口也可放宽）。



---

## P0 级修复

### 1. 统一节点身份指纹（`parser.py`）

**问题**：`node_identity()`（解析阶段）与 `proxy_identity()`（配置阶段）各写一套默认值，
同一节点会算出两个指纹。实测：`candidates/legacy_sources.json` 里 **233,159 条身份只有 11 条**
能与部署产物 `output-filtered-fast.yaml` 对上；`source_quality.json` 里 41 个源有 **31 个**
被标记 `enabled: false`、`pass_rate_ema ≈ 7.2e-05`（全是噪声）。

**根因**：至少三处分叉 ——
- `network`：一侧按解析值（可能是 `tcp`），另一侧默认 `ws`。实测部署的 254 个节点里
  **只有 3 个**带 `network` 键，即 251 个节点必然算错。
- `cipher`：`output.py` 写的是 `node.cipher or "auto"/"none"`，而身份哈希的是空串。
- `reality` short-id 的清洗时机不同（一边在写出时兜底成 `3e58`）。

**改法**：新增 `canonical_proxy_fields()` / `canonical_fields_from_clash()`，
两侧都先把字段归一化成"与 `output.py` 实际写出的 Clash dict 等价"的形态，再走同一个
`_identity_from_fields()`。指纹版本从 `v2` 升到 **`v3`**（旧数据不再匹配，学习状态需重建）。

**⚠ 重要**：v2 旧身份与 v3 不兼容。`source_quality.json` 已在本次改动中**备份后重置**
（旧数据本来就是噪声），下一轮 `collector` 会重新写 `legacy_sources.json`（v3），
`quality_report` 随后就能正确结算。

### 2. 部署产物的 AUTO 组恢复到 Top30（`run_pipeline.py`）

**问题**：`speed_test.py:101-105` 特意用 `AUTO_TOP = 30` 限制 url-test 组（注释说明 url-test
按延迟选、塞全量会挑中"低延迟但拥塞"的节点），但 `merge_previous_fast` /
`apply_stability` / `pre_deploy_verify` 三处重建 proxy-groups 时都会把它重写成**全部存活节点**。
实测部署文件：`proxies=254`，**AUTO 组 254 个成员**。

**改法**：新增 `enforce_auto_cap()`，在 `make_mobile` 之前统一收敛一次，并顺带清理其它组里的
悬空引用（只在真的发生变化时才写盘）。

### 3. Clash 格式来源的 REALITY 节点不再失效（`parser.py`）

**问题**：`_parse_clash_yaml()` **从不读取** `flow` / `client-fingerprint` / `reality-opts`。
后果：从 Clash 格式订阅（mfuu/FreeProxies、awesome-vpn、Ruk1ng001 等）来的 REALITY 节点，
被重新写出时**丢掉 public-key**，整条节点直接不可用；`flow` 也被抹平。这同时是身份分叉
的第 4 个原因。

**发现方式**：身份指纹回归测试一次抓出 3 个 FAIL（vless reality / flow 相关），追下去才定位到。

### 4. short-id 引号保护被流水线自己破坏（`run_pipeline.py` / `output.py` / `make_mobile.py`）

**问题**：`output.py` 用 `_SafeDumper` + `_defuse_shortid` 给 `short-id` 加引号，
但 `apply_stability`（`yaml.dump`）、`pre_deploy_verify`（`yaml.safe_dump`）、
`merge_previous_fast`（`yaml.safe_dump`）、`make_mobile`（`yaml.safe_dump`）**都用普通 dumper 重写**，
引号全丢。实测对比：

```
output-filtered-http.yaml : short-id: '5d7a45ac0ad6adff'   ← http_test 写的，有引号
output-filtered-fast.yaml : short-id: 5d7a45ac0ad6adff     ← 部署文件，引号被吃掉
```

**为什么会坏**：PyYAML 的 float 规则要求小数点，所以 PyYAML 眼里 `3e58` 是字符串；
但 mihomo 用 go-yaml，其数字判定走 Go 的 `strconv.ParseFloat` —— `ParseFloat("3e58")` 成功，
于是变成 `3e+58`，mihomo 报 `invalid REALITY short ID`。而 `parser.py` 在源缺 `sid` 时的
**默认值恰好就是 `"3e58"`**，所以这条路径很容易踩到（当前部署文件里 4 个 short-id 恰好都不是
数字形态，属"侥幸没炸"）。

**改法**：`output.py` 新增公开的 `defuse_reality_short_ids(config)`；
`run_pipeline` 的三处重写、`make_mobile` 全部改为「先 defuse → 再用 `_SafeDumper` 原子写」。
验证：`make_mobile` 产出现在是 `short-id` **加引号 4 条 / 未加引号 0 条**。

---

## P0-4：http_test 分级预筛（省约 6 分钟）

**做法**：`collector.py` 把 TCP 实测结果写成旁路文件 `output-latency.json`
（key = `type|server|port`，值 = 延迟毫秒或 `null`）；`http_test.py` 先只测 TCP 连通的节点。

**为什么不做成"直接过滤"**：我实测过损失 —— 用项目自己的 `tester._tcp_ping` 对
**152 个 HTTP 可用节点**重测，其中 **4 个（2.6%）TCP 探测超时但实际能代理**。
所以做成**两阶段**：第一阶段存活数 `< PREFILTER_MIN_ALIVE`（默认 120）时，
自动补测剩下的 TCP 未连通节点；只有第一阶段产出足够时才跳过它们。

**开关**：`PCOLL_PREFILTER=0` 关闭预筛；`PCOLL_PREFILTER_MIN_ALIVE=N` 调阈值。
旁路文件不可信时自动退化为全量实测（对旧 collector 也安全）。

**可信度门槛（真实数据验证时改正过一个错误设计）**：最初我按"旁路文件必须比 `output.yaml` 新"
判断新鲜度 —— 实测发现这是**错的**：`collector` 是先写旁路（22:59:01）再写 `output.yaml`
（22:59:12），同一次运行的正常文件会被自己拒掉。现改为语义判据：
① 文件年龄超过 `LATENCY_TTL_SEC`（24 小时）不认；② 对当前节点池的**覆盖率**低于
`LATENCY_MIN_COVERAGE`（50%）不认（说明是别的节点集的遗留）。

**完整运行的实测效果**（2026-10-15 23:19）：
```
TCP 预筛: 先测 10545 个连通节点, 保留 9295 个待补测 (10545/19840 个节点 TCP 连通，覆盖 100%)
[stage1] HTTP 实测完成: 132/10545 节点可用
第一阶段已得 132 个可用（>= 阈值 120），跳过 9295 个 TCP 未连通节点
```
阶段耗时 **9 分 55 秒 vs 基线 15 分 15 秒（省 5 分 20 秒）**。
注意：**省下的时间明显少于节点数的减少比例（53%）** —— 因为 TCP 连通的节点恰好是
"会一直等到 8 秒超时"的那批，而被跳过的 TCP 不通节点多数是**秒失败**的。
实测单节点成本从 25.2 个/秒降到 18.6 个/秒（+36%），吃掉了大部分收益。
这是这个优化真实的天花板，记录下来避免以后高估。

---

## P1 级修复

| # | 文件 | 改动 |
|---|---|---|
| 5 | 新增 `atomic.py` | 统一「原子写 + 占用重试」（`os.replace` 遇 `PermissionError` 退避重试 8 次）。原先输出文件被占用会直接 `SystemExit(1)` 终止整轮（日志里真实发生过：`make_mobile` 因 `output-filtered-fast-mobile.yaml` 被占用而中止） |
| 6 | `speed_test.py` / `http_test.py` | 临时配置/日志改放**系统临时区**（原来 `mkstemp(dir=项目根)`，被中断就留 `tmp*.tmp`：2026-09-12、2026-10-15 各留下 8 个，mtime 与中断时刻完全吻合）。`run_pipeline` 启动时顺带清理 >10 分钟的历史遗留临时文件 |
| 7 | `http_test.py` | ① 不再把非标准字段 `_http_delay` 写进 YAML（实测 199 处），改为旁路到 `outputs/http_delays.json`；② 裁剪 proxy-groups 时保留 `AUTO`/`DIRECT`/`REJECT`/组名 —— 原先只保留节点名，已把 `PROXY` 组的首成员 `AUTO` 误删；③ `_kill_proc` 改用 terminate→kill（Windows 上 `CTRL_C_EVENT` 需要独立进程组，原实现经常无效）；④ 超时改为显式传参，不再改全局变量 |
| 8 | `output.py` | `client-fingerprint` 白名单归一化（实测有 38 个 `unsafe`，还有 `fi…</div>`、`chrom` 这类垃圾值，mihomo 每次都打印 `wrong clientFingerprint`）；校验器兜底拒绝非法值 |
| 9 | `collector.py` | 去掉重复全量解析：原实现为了写 `legacy_sources.json` 把**所有订阅内容重新 `parse_content()` 一遍**；顺带去掉没人用的 `uri_identity` 导入。**纠正**：实测解析 252,787 个节点只花约 10 秒，所以这条只省了约 10 秒（我最初估的 1.5 分钟是错的 —— collector 的时间被 TCP 测速占掉了 71%）。同时把解析日志改成带来源名，便于定位是哪个源贡献了多少 |
| 10 | `run_pipeline.py` | `run_step` 显式 UTF-8（父进程解码 + 子进程 `PYTHONIOENCODING=utf-8`）。默认情况下子进程 stdout 跟随控制台代码页（本机 GBK），一旦打印节点名里的国旗 emoji 就 `UnicodeEncodeError`；失败时打印的结尾从 1600 字符放宽到 3000（原来会把关键行截掉） |
| 11 | `run_pipeline.py` | `VPY`/`BASE`/`MIHOMO` 支持 `PCOLL_PY`/`PCOLL_BASE`/`PCOLL_MIHOMO` 覆盖；解释器优先级 环境变量 → 历史 venv（存在才用）→ 当前解释器。原来把 `.workbuddy` 私有 venv 写死且无回退，该环境一被重建就 `FileNotFoundError` 且提示不明；启动时打印实际使用的解释器 |
| 12 | `speed_test.py` | ① 节点名默认**不再**带 `\| 4.3MB/s` 后缀（名字每轮漂移会让 Clash 客户端的"当前选中节点"与历史每轮失效）；需要该效果时加 `--speed-in-name`，或 `PCOLL_SPEED_IN_NAME=1`。② 速率改为旁路写 `outputs/fast_speed.json`（按身份索引）。③ 删掉主流程从未使用的 `cdata` 死代码。④ mihomo 路径改用多候选 + PATH 兜底。⑤ 日志里的阈值与真实阈值一致（原写 `>0.3MB/s`，实际 0.15） |
| 13 | `source_quality.py` | 自动停用不再是**单向棘轮**：被停用满 `REENABLE_AFTER_RUNS`（8）轮后重新放行一次并进入"试用"状态（重置样本计数，避免用同一批旧数据立刻再停用）。原实现只会写 `enabled=False`，全项目没有任何路径能改回 `True` |
| 14 | `quality_report.py` | ① `load_source_map` 校验值必须是「字符串列表」，否则跳过并告警（把状态文件当映射传入会得到 `{源: [统计字段名]}`，随后 `TypeError` 崩掉结算步骤）；② 把 HTTP 实测延迟喂给质量评分（原 `add_verification` 的 `avg_latency_ms` **没有任何调用方**，`lat_factor` 恒为 1.0，每个源白拿 0.1 权重）；③ 删掉 `--no-save` 之后的不可达重复代码 |
| 15 | `deploy_to_clash.py` | **全部逻辑收进 `main()` + `__main__` 守卫** —— 旧版在 import 期就执行部署（任何工具只要 import 它就会真的改写并切换你的 Clash 配置）；路径支持 `PCOLL_FAST`/`PCOLL_CLASH_BASE` 覆盖；`profiles.yaml` 缺失/结构异常时给出明确错误码而不是崩栈；`profiles.yaml` 改原子写；备份失败不再致命 |

---

## 新增文件

| 文件 | 用途 |
|---|---|
| `atomic.py` | 原子写 + 占用重试 + 运行期临时目录 + 历史临时文件清理 |
| `test_core_regressions.py` | 8 个回归测试（`py -3 test_core_regressions.py`，也兼容 pytest） |
| `config.example.yaml` | 可安全入库的配置模板（明文 key 已替换为 `PUT_YOUR_KEY_HERE`） |
| `.gitignore` | 首次提交前就位（排除产物/日志/临时文件/16MB 学习状态/含 key 的真实配置） |
| `CHANGELOG_改进记录_20261015.md` | 本文件 |

---

## 验证方式（可复现）

```powershell
cd C:\Users\zxf72\proxy_collector
py -3 test_core_regressions.py          # 期望 8/8 passed
```

回归测试覆盖：身份三段一致（URI → Clash dict → 重新解析）、Clash 来源保住
reality/flow/fingerprint、数字型 short-id 必须加引号、AUTO 收敛到 30、
组引用裁剪保留 AUTO、指纹白名单、预筛旁路 key 格式互通、质量停用后可恢复。

另外做过 3 项有界端到端验证（只写系统临时区）：20 节点抽样跑真实 mihomo 测速、
用真实 FAST 生成手机版、在假的 Clash Verge 目录里部署两次（验证幂等与 uid 复用）。

---

## 回滚方法

```powershell
# 全部回滚
Copy-Item C:\Users\zxf72\proxy_collector\_backup_before_improve_20261015\* C:\Users\zxf72\proxy_collector\ -Force
# 只回滚某一个文件
Copy-Item ...\_backup_before_improve_20261015\run_pipeline.py ...\ -Force
```

---

## 仍需你决定 / 尚未做

0. **aggregator 那 90 秒白等没做**：近 14 次运行有 11 次打印"发现源: 超时 90s 未完成，
   跳过（线程将被丢弃）"，而它成功时也只贡献 20 个节点。改它需要动
   `collect_aggregator` 的线程/预算逻辑，风险与收益（90 秒 / 31 分钟 ≈ 5%）不匹配，
   所以本次**故意没碰**。如果要做，方向是把"遗弃线程"改成真正的取消（async + wait_for）。
1. **`remove_unreachable`**：`config.yaml` 现在是 `max_nodes: 20000` + `remove_unreachable: false`，
   而 `MEMORY.md` 记录你 2026-07-31 要求的是「全量测速 + 剔除不可达」。本改动**没有动它**
   （因为它是行为策略，不是效率参数）。现在 http_test 的预筛已经拿到了这份收益，
   你可以按需要再决定是否把它改回 `true`（`true` 会让死节点不进 `output.yaml`，
   整轮更快，但对瞬时 TCP 超时更敏感）。
2. **Git 未安装**：这台机器上找不到 `git.exe`（只有 `winget`）。`git init` + 首次提交还没做；
   `.gitignore` 与 `config.example.yaml` 已就位，装好 Git 后两条命令即可收尾。
3. **明文 key**：`config.yaml`（以及 `config.bak.yaml`/`config.sandbox.yaml`/`config.yaml.runbak`）
   里的 `sub.proxygo.org?...key=6c80fd…` 仍在本地明文存在 —— 这 4 个文件都已被 `.gitignore` 排除，
   但建议在服务端**轮换一次**这个 key。
4. **~40 MB 历史垃圾**（32 个 `collect_log*.txt`、36 个日志、历史 `output*.yaml` 快照、
   `backup_20260907/`、一次性脚本等）**尚未清理** —— 建议等 Git 就位后再清，才有回滚能力。
5. **`output-mobile.yaml`**（每轮 7 MB，无消费方，实际部署的是 `make_mobile` 的产物）
   没有删，因为要改 `config.yaml` 的 `mobile_output` 契约。
6. **`tester.py` 的 TCP 测速**：名字与 docstring 写着 HTTP 测试，实际只做 TCP connect，
   且对域名节点会打爆默认线程池（`MEMORY.md` 里记录过 6672 节点卡死）。本次**未改**，
   因为它现在多了一个正当用途 —— 为 http_test 提供预筛依据。
