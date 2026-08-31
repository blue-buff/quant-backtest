# 本机跑通任务交接单（macOS /Users/lucas/projects/quant）

> ✅ **已完成（2026-08-30）**：全部步骤执行完毕——venv 重建、commit 6ddcaa3、b-local-smoke
> 两 job 全绿（local_train_hs300 FINISHED，expectation met）、pytest 168 passed / 2 skipped
> （预置 torch skip）、CLI 冒烟（board/queue/kb/qexec/notify_bridge/webui）全过、
> docs/LOCAL_RUN.md 已写、备份已 push（remote main 643afb6）。详见 docs/LOCAL_RUN.md「验证记录」。
> 新增坑位：lightgbm 需 libomp（venv 内 sklearn 自带 dylib + rpath 就地修复，重装需重做，见 docs）。

> 2026-08-29 | 任务：把 QLab 项目在本机（macOS，无容器）跑通，Windows/容器那套保持原样。
> 用户已批准完整计划（含一次短窗口真实训练）。本会话因 DSH 运行时进程执行层故障
> （`spawn bash ENOENT`，仅影响本会话；新会话正常）中断，以下为精确交接。

## 已完成（全部落盘，未 commit）

1. `pipeline/__init__.py`：QLAB_ROOT 默认 = env → `/root/quant`（存在时）→ 仓库根自动探测。
2. `pipeline/data.py`：QLAB_QLIB_DATA 默认 = env → `/root/.qlib`（存在时）→ 本机
   `~/projects/.qlib` 或 `~/.qlib` 探测。
3. `scripts/qexec.sh`：容器不可达或 `QLAB_QEXEC_LOCAL=1` 时本地直接执行
   （cwd=QLAB_ROOT，解释器按扩展名），容器在时原逻辑不变。
4. `notify_bridge.js`：容器不可达或 `QLAB_BRIDGE_LOCAL=1` 时本机跑
   `python -m pipeline.queue ...`（venv python + cwd=QLAB_ROOT）；默认仍走容器。
5. `scripts/webui.js`：同上（`QLAB_WEBUI_LOCAL=1`），含 heartbeat 本地读、runDetail
   本地 stdin 模式。
6. `scripts/update_tail.py`：`/root/quant` 硬编码改为 QLAB_ROOT/脚本位置推导。
7. `.gitignore`：新增 `.dsh/`。
8. 新实验文件：
   - `experiments/specs/local_smoke_hs300.json`（action kind=smoke，无数据）
   - `experiments/specs/local_train_hs300.json`（hs300 Alpha158 10d，fit 2021-06-01~2024-06-30，
     test 2025-01-01~2025-03-31，_example_lgb，seeds [42]，rounds 200/early 30）
   - `experiments/batches/b-local-smoke.json`（含上面两个 spec）

## 关键事实（本机）

- 仓库根：`/Users/lucas/projects/quant`（git HEAD 81a6dd8；`quant-backtest` 是过期镜像，别碰）。
- qlib bins 已迁移：`/Users/lucas/projects/.qlib/qlib_data/{cn_data,cn_data_zz500,cn_data_all}`
  （352M）。data.py 新默认会自动探测到，也可显式 `QLAB_QLIB_DATA=/Users/lucas/projects/.qlib`。
- 当前 `.venv` 是 **Windows 版**（pyvenv.cfg home 指向 C:\Users\ningj...），必须先重建。
- 台账/队列在本仓库：`mlflow-server/mlflow.db`、`results/queue/jobs.db`（103 个历史 job）。
- 缺失（迁移时排除）：`results/cache`、`results/venvs`、`results/backup` —— 前两者首次运行自动建，
  backup 由 snap 建。

## 下一步（严格按顺序）

```bash
cd /Users/lucas/projects/quant

# 1. 重建 venv 并装依赖（pyqlib 0.9.7 有 cp312 macosx universal2 wheel，直接装）
rm -rf .venv
/Users/lucas/.local/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install pyqlib==0.9.7 mlflow pandas numpy pyyaml \
    scikit-learn pyarrow lightgbm typer rich pytest fire loguru
.venv/bin/python -m pip install -q -e .   # qbt 包（可跳过）
# 验证：
QLAB_QLIB_DATA=/Users/lucas/projects/.qlib .venv/bin/python -c \
  "import qlib; qlib.init(provider_uri='/Users/lucas/projects/.qlib/qlib_data/cn_data', region='cn'); print('qlib OK', qlib.__version__)"

# 2. 先 commit 代码改动（harness 的 dirty-code 门禁要求干净树才能跑实验）
git add pipeline/__init__.py pipeline/data.py scripts/qexec.sh notify_bridge.js \
    scripts/webui.js scripts/update_tail.py .gitignore \
    experiments/specs/local_smoke_hs300.json experiments/specs/local_train_hs300.json \
    experiments/batches/b-local-smoke.json
git commit -m "feat(local): macOS 本机跑通适配（QLAB_ROOT/QLAB_QLIB_DATA 自动探测 + 工具链本地回退）+ local smoke/train spec"

# 3. 队列冒烟（先 smoke 后 train，concurrency 1 稳妥）
.venv/bin/python -m pipeline.queue submit experiments/batches/b-local-smoke.json
QLAB_ROOT=$PWD .venv/bin/python -m pipeline.queue run --once --concurrency 1
# 验证：status 里 local_smoke_hs300 done；再跑一轮出 train：
QLAB_ROOT=$PWD .venv/bin/python -m pipeline.queue run --once --concurrency 1
# train 首次会构建 hs300 Alpha158 特征缓存（约 10-25 分钟，34GB/10 核），是规范缓存，以后复用
# 验证：python -m pipeline.board --json --formal 出现 local_train_hs300 行；run 目录 results/runs/local_train_hs300/

# 4. pytest（QLAB_ROOT 指向仓库跑全量；test_import_cli.py 的 /root/quant insert 无害）
QLAB_ROOT=$PWD .venv/bin/python -m pytest tests/ -x -q   # 如失败，最小改动修复/标记 skip

# 5. 冒烟 CLI：board / queue status / kb search <词> / qexec 本地回退
#    QLAB_QEXEC_LOCAL=1 scripts/qexec.sh <一个 .py 脚本> 应本地直接跑
#    node notify_bridge.js --once --no-dsh（本地模式应能读 events，无需容器）
#    node scripts/webui.js --port 8099 后 curl http://127.0.0.1:8099/api/state（本地模式）

# 6. 文档 + 收尾
#    docs/LOCAL_RUN.md（本机用法：env、数据位置、工具链回退、与 Windows 差异）
#    .venv/bin/python -m pipeline.backup snap
#    .venv/bin/python -m pipeline.backup push --message "feat(local): macOS 本机跑通 ..."
#    （git 里 results/backup/snaps/*.zip 的删除是迁移预期，随 push 一起提交）
```

## 汇报要求（对用户）

- 跑通后汇报：跑了什么、结果数字（rankic/p 值带窗口）、台账位置、备份状态。
- 注意措辞纪律：smoke 行不入正式结论；local_train 是验证性实验（expectation 是宽松门，
  标记 exploratory）。
- 若 commit 前有意外改动，先 `git status` 核对再提交。
