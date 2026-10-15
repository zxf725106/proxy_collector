# proxy_collector

代理节点采集 → HTTP 存活检测 → 速度测试 → 稳定度加权 → 生成 Clash 配置 → 部署，全链路自动化流水线。

## 链路

```
collect_aggregator → collector → http_test → speed_test
    → merge_previous_fast → apply_stability → pre_deploy_verify
    → make_mobile → deploy_to_clash → quality_report / multi_output
```

## 依赖

- **Python 3.10+**（`http_test.py` / `fetcher.py` / `tester.py` 用了运行期的 `X | None` 注解）
- `pip install -r requirements.txt` —— 只有 `aiohttp` + `pyyaml`
- [mihomo](https://github.com/MetaCubeX/mihomo) 核心 —— `http_test` / `speed_test` / 发布前复测都靠它真实发起请求

## 路径可用环境变量覆盖

代码里的默认值是本机路径，换机器/换解释器**不需要改代码**：

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `PCOLL_PY` | 子步骤使用的 Python 解释器 | 本机 venv；失效时自动回退到当前解释器 |
| `PCOLL_BASE` | 项目目录 | 本机路径 |
| `PCOLL_MIHOMO` | mihomo 核心路径 | 本机 Clash Verge 自带 |
| `PCOLL_SKIP_DEPLOY` | 设为 `1` 则跳过部署步骤 | 不跳过 |
| `PCOLL_AUTO_TOP` | `AUTO`(url-test) 组成员上限 | `30` |
| `PCOLL_SPEED_IN_NAME` | 设为 `1` 把实测速率写进节点名 | 关闭 |
| `PCOLL_PREFILTER` | 设为 `0` 关闭 TCP 预筛 | 开启 |

mihomo 查找顺序：`PCOLL_MIHOMO` → 若干本机 Clash Verge 路径 → `PATH` 中的 `mihomo`。

## 云端自动刷新（GitHub Actions）

[`.github/workflows/refresh.yml`](.github/workflows/refresh.yml) 每 6 小时在 `ubuntu-latest`
上跑一轮完整流水线，并把产物推送到 `output` 分支。

也可以手动触发：**Actions → refresh → Run workflow**（可勾选 `skip_http` 跳过 HTTP 实测，快很多）。

### 订阅链接

| 用途 | 链接 |
|---|---|
| 桌面端 Clash | `https://raw.githubusercontent.com/zxf725106/proxy_collector/output/output-filtered-fast.yaml` |
| 手机端 Clash | `https://raw.githubusercontent.com/zxf725106/proxy_collector/output/output-filtered-fast-mobile.yaml` |

链接固定不变，每轮刷新后内容自动更新。

### ⚠️ 云端测速的已知偏差

`speed_test` 测的是「**运行机器 → 节点**」的带宽。云端 runner 在 Azure 机房（主要在美国），
衡量的是机房到节点的速度，**不等于国内的实际速度**。

缓解：配置里的 `AUTO` 组是 `url-test` 类型，**客户端会用自己的网络按延迟重新选择**，
云端主要负责保证节点池是活的；也可以在 `PROXY` 组里手动挑节点。

### 仓库里会留下什么

- `main` 分支：只有代码、配置模板和文档
- `output` 分支：**只有两个 YAML**，每轮新建的孤儿提交 + 强制推送，历史不会随轮次膨胀
- 跨轮状态（`node_stability.json` / `source_quality.json` / `candidates/legacy_sources.json` /
  上一轮产物）走 `actions/cache`，**不进 git**

## 安全约定（.gitignore）

以下内容**永不入库**：

| 排除项 | 原因 |
|---|---|
| `config*.yaml` | 含明文订阅 key 的真实配置（模板见 `config.example.yaml`） |
| `clash-import*.yaml` | 含真实节点的 `password` / `uuid` |
| `output*.yaml`、`outputs/*.json`、`*.log` | 逐轮产物与日志 |
| `.workbuddy/` | 个人笔记 |
| `_attic_*/`、`_backup_before_improve_*/` | 历史归档 |
| `node_stability.json`、`output-latency.json`、`candidates/legacy_sources.json` | 体积大、每轮变化 |

## 本地运行

```bash
python run_pipeline.py               # 完整一轮，约 30 分钟
python run_pipeline.py --skip-http   # 复用已有候选池，跳过 HTTP 实测
```
