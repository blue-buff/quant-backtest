# macOS 本机跑通（无容器开发机）

> 2026-08-30 | 目标：QLab 项目在 macOS 本机（无 docker 容器）完整跑通，Windows/容器那套保持原样。
> 交接单：knowledge/notes/local_run_status.md。

## 环境事实

- 仓库根：`/Users/lucas/projects/quant`（git main；`quant-backtest` 是过期镜像，别碰）。
- Python：`/Users/lucas/.local/bin/python3.12`（uv 管理的 3.12.14，arm64）。
- venv：`.venv`（pyqlib 0.9.7 cp312 macosx universal2 wheel 直装）。
- qlib bins：`/Users/lucas/projects/.qlib/qlib_data/{cn_data,cn_data_zz500,cn_data_all}`（352M），
  由 `pipeline/data.py` 自动探测（env `QLAB_QLIB_DATA` → `/root/.qlib`（存在时）→
  `~/projects/.qlib` → `~/.qlib`）。也可显式
  `export QLAB_QLIB_DATA=/Users/lucas/projects/.qlib`。
- 台账/队列：本仓库 `mlflow-server/mlflow.db`、`results/queue/jobs.db`。
- 特征缓存：`cache/*.parquet`（本机已建 hs300 Alpha158 train/test，204M）。
- 备份：`pipeline.backup snap`（本地快照，只写 results/backup/，不进 git）+
  `push`（**代码推 main，数据快照 force-push 到 backup 分支**，main 保持纯代码）。
  本机实测自动备份 hook 已推成功（remote main 与 backup 分支均在）。

## 快速开始

```bash
cd /Users/lucas/projects/quant
# 队列冒烟 / 跑实验
.venv/bin/python -m pipeline.queue submit experiments/batches/<batch>.json
.venv/bin/python -m pipeline.queue run --once --concurrency 1   # 一轮（排空用 --watch）
.venv/bin/python -m pipeline.queue status --json
# 台账
.venv/bin/python -m pipeline.board --json --formal
# 知识库
.venv/bin/python -m pipeline.kb search <词>
# 测试
QLAB_ROOT=$PWD .venv/bin/python -m pytest tests/ -q
```

QLAB_ROOT 无需显式设置：`pipeline/__init__.py` 自动探测（env → `/root/quant`（存在时）→ 仓库根）。

## 工具链：默认本机直跑（不加 docker）

2026-08-30 起工具链默认全部本地直跑，docker 仅作显式 opt-in（Windows 主机等仍有容器的环境）：

| 工具 | 默认（本地） | 容器模式（显式 opt-in） |
|---|---|---|
| `scripts/qexec.sh` | 本机直接执行（cwd=QLAB_ROOT，解释器按扩展名） | `QLAB_QEXEC_CONTAINER=1` |
| `notify_bridge.js` | 本机跑 `python -m pipeline.queue ...`，通知数据落 `<repo>/notify/` | `QLAB_BRIDGE_CONTAINER=1` |
| `scripts/webui.js` | 本机读队列/台账，`/api/state` 等，桥状态读 `<repo>/notify/bridge_state.json` | `QLAB_WEBUI_CONTAINER=1` |

旧开关 `QLAB_QEXEC_LOCAL=1` / `QLAB_BRIDGE_LOCAL=1` / `QLAB_WEBUI_LOCAL=1` 保留（强制本机；
本机现在就是默认，兼容旧用法）。容器模式要求 docker 可达，否则自动回落本地。

冒烟验证（2026-08-30 全部通过）：
- `node scripts/webui.js --port 8099` → `curl :8099/api/state` 返回队列 JSON，根路径 200。
- `node notify_bridge.js --once --no-dsh`（默认本地）能读 events 并生成通知文本，无需任何 env。
- 注意：本机通知无 DSH 家（session.txt 不存在时 `--session <id>` 或建 `<repo>/notify/session.txt`），
  DSH POST 报 no-session 是预期，做实验不需要通知。

## 与 Windows/容器环境的差异

- **无 docker、无 spark**：spec 一律 `runner: local`（或 auto——spark 不可达自动回退 local，
  事件记 spark_fallback）。不要提交 runner=spark 的 spec 到本机队列。
- **无 GitHub token 文件**：push 用主机 credential helper；容器里的 `.qlab_github_token`
  不存在。`pipeline.backup push` 失败会退避重试，可手动 `git push` 兜底。
- **venv 是重建的 macOS 版**：旧 `.venv`（Windows 版 pyvenv.cfg）已删。若从 Windows 侧拷回
  仓库，注意 `.venv` 不进 git。
- **lightgbm/libomp**：macOS 上 lightgbm wheel 需要 OpenMP 运行时。本机 venv 内已就地修复：
  `sklearn/.dylibs/libomp.dylib` 复制到 `.venv/lib/` 并给
  `.venv/lib/python3.12/site-packages/lightgbm/lib/lib_lightgbm.dylib` 加了 rpath
  （`install_name_tool -add_rpath <venv>/lib`）。**重装 lightgbm 后需重做**：
  ```bash
  cp .venv/lib/python3.12/site-packages/sklearn/.dylibs/libomp.dylib .venv/lib/
  install_name_tool -add_rpath "$PWD/.venv/lib" \
    .venv/lib/python3.12/site-packages/lightgbm/lib/lib_lightgbm.dylib
  ```
- **pip 缓存被禁用**：`~/Library/Caches/pip` 权限问题（`Operation not permitted`），
  pip 每次全量下载。可 `export PIP_CACHE_DIR=$PWD/.pip-cache` 启用仓库内缓存。
- **联网策略**：国内可下的（pip 镜像等）直连不走 ClashVerge；必须国外下的（GitHub 等）
  才走代理。brew 装系统库需要沙箱外权限，优先用 venv 内方案。
- **通知会话登记**：本机通知目录是 `<repo>/notify/`（bridge 默认写入）；无 session 时 DSH POST
  报 no-session 属预期，不影响队列/台账功能。`notify_bridge.js` 常驻：
  `node notify_bridge.js --interval 15`。

## 验证记录（2026-08-30）

- `b-local-smoke` 两 job 全绿：`local_smoke_hs300`（smoke，run d88c0777）、
  `local_train_hs300`（run f6a2d32e，FINISHED）。
- local_train 结果（验证性/exploratory，宽松 expectation）：test 2025-01-02~2025-03-31
  （57 交易日，300 只），mean_rank_ic 0.1052（board 口径 nonoverlap 0.0749），
  bootstrap rankic p_le0=0.2499；expectation `{"rankic_mean_min":0.0,"p_le0_max":0.5}` met。
  措辞纪律：这是跑通验证，不是研究结论，smoke 行不入正式结论。
- pytest 168 passed / 2 skipped（skip 为 torch 未装的 qlib_bench shim 测试，预置）。
- 自动备份 hook 已 snap + push（remote main 781297c）。
