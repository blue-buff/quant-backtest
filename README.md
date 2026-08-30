# QLab 量化实验平台 🧪

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-161%20passed-2e7d32)
![MLflow](https://img.shields.io/badge/ledger-MLflow%20sqlite-0194E2)
![Runner](https://img.shields.io/badge/runner-local%20%2B%20DGX%20Spark-8e24aa)

> 一个给 **研究 agent** 用的 A 股量化实验平台：写一个 spec 提交实验，管线负责取数、
> 固定评分、记账、通知；执行器内部完全自由，只靠契约与测试器保证**可比性**。

## ✨ 核心特性

- **契约式执行器**：agent 只写 `main.py --config --train --test --out`，输出 `pred.pkl`（选股）
  与 `portfolio.pkl`（交易）即可接入；管线从不 inspect 执行器内部。
- **固定测试器**：`pipeline.metrics` 是唯一指标来源——预测五族（rankic/bootstrap/deciles/
  quarters/hit）+ 交易四族（portfolio/backtest/attribution），spec 勾选制，杜绝口径漂移。
- **单写者台账**：MLflow sqlite，入库自动带 `qlab.*` tags（spec_hash/commit/data_rev/runner…），
  board 一键导出，含同 base_ref 的 Bonferroni 多重检验修正。
- **纯排队调度**：FIFO 队列（submit/retry/cancel/unblock/heal），无资源分配；
  `--once` 一轮即退、`--watch` 排空，支持本地与 DGX Spark 远端（每 job 独立目录、
  spark 优先自动回退）。
- **通知驱动的研究循环**：任务完成/轮结束/批次终态/blocked 自动推送 DeepSeek Harness
  （steer 模式），两阶段 ack 协议不丢消息。
- **知识闭环**：每个实验自动落一条 auto-claim，expectation 预注册自动对照
  （met→confirmed / not_met→falsified），claims.jsonl 全自动回写。
- **诚实纪律内建**：脏代码门禁、spec_hash 防漂移、取数时数据修订号、快照双备份、
  161+ 条 pytest 回归测试（含并发/竞态/故障注入）。

## 🏗️ 架构总览

```
spec.json ──► 队列(jobs.db) ──► dispatcher(本地 / DGX Spark) ──► harness
                                                              │
   取数(pipeline.data 固定菜单) → 执行器(agent 自由) → 契约检查 → 固定测试器 → 记账
                                                              │
                     board.csv / claims.jsonl / 通知桥 / WebUI ◄──┘
```

管线只拥有「可比性三件套」：**同一份数据、同一个评分口径、同一本账**。
模型、预处理、训练协议、交易策略全部属于执行器自由（详见 AGENTS.md §9 冻结线）。

## 📦 环境

- **本地容器**（权威仓库）：`hermes-1679f5b2:/root/quant`，Python 3.12 + pyqlib 0.9.7 +
  MLflow sqlite；内存 12GB、pids.max=256（重活走远端）。
- **DGX Spark 远端**（可选，runner=spark）：GB10 / CUDA 13.0 / torch 2.10.0+cu128，
  ssh 跳板接入；未配置时 spark 任务 blocked、auto 任务自动回退本地。
- **主机** `D:\quant_backup`：工作目录 + 备份镜像；通知桥（node）与 WebUI 在此运行。
- 跨机/跨容器命令一律用封装工具：`scripts/qexec.sh`（容器）、`scripts/qremote.sh`（远端），
  零引号税（详见 AGENTS.md §7）。

## 🚀 快速开始（提交一个实验）

```bash
# 1. 写 spec（最小必填 exp_id + base；推荐 expectation 预注册）
cat > experiments/specs/my_exp.json <<'EOF'
{
  "exp_id": "my_first_exp",
  "base": "ref:all10d_ens3",
  "changes": "把模型换成 xgb",
  "expectation": {"rankic_mean_min": 0.04},
  "action": {"kind": "train", "executor": "executors/_example_lgb"}
}
EOF

# 2. 放进 batch 并提交
python -m pipeline.queue submit experiments/batches/b-my.json   # 幂等，已 done 自动跳过

# 3. 跑一轮（--once 只领 concurrency 个；排空用 --watch）
python -m pipeline.queue run --once --concurrency 2

# 4. 看结果
python -m pipeline.board --json --formal    # 正式行（FINISHED 非 smoke）
python -m pipeline.queue status --json      # 队列状态
```

跑完自动入账：metrics 全部来自固定测试器；台账行带 spec_hash / git commit / data_rev，
run 目录内**每一个文件**（模型、spec 原件、portfolio）全部归档为 artifacts。

人类看板：主机 `node scripts/webui.js` → **http://127.0.0.1:8099**
（只读旁观：总览/队列/台账/claims/事件，分页 + 点击详情，无操作入口）。

## 📁 目录结构

```
quant/
├── AGENTS.md                 # 行为规范（铁律/坑位/冻结线/研究循环），研究 agent 必读
├── pipeline/                 # 平台（12 个模块，冻结线管辖）
│   ├── spec.py               #   spec 校验/hash（metrics+expectation 计入口径）
│   ├── queue.py              #   队列：submit/run/retry/cancel/unblock/heal/通知两阶段
│   ├── harness.py            #   执行链：取数→执行器→契约→测试器→记账（脏代码门禁）
│   ├── metrics.py            #   固定测试器（唯一指标来源，bootstrap 种子 42）
│   ├── data.py               #   数据固定菜单 + 特征/价格缓存 + 修订号
│   ├── executor.py           #   执行器契约检查 + venv 管理
│   ├── registry.py / board.py│   台账读写 + board 导出（多重检验）
│   ├── kb.py / review.py     #   claims 知识库 + advisory 评审
│   ├── backup.py             #   快照（在线一致性 db 副本）+ push + 退避
│   └── remote.py             #   DGX Spark 远端执行（每 job 独立目录 + 失败现场保回）
├── executors/                # 执行器（agent 自由区）：_example_lgb / _example_topk_portfolio /
│   └── qlib_bench/           #   qlib 官方 benchmark 复现执行器（vendor + shim，见 VENDOR.md）
├── experiments/              # specs / batches / refs（基线引用）
├── knowledge/                # claims.jsonl + notes/（自由笔记、挂账清单、调查文档）
├── tests/                    # pytest（161+ 全绿，含并发/竞态回归）
├── scripts/                  # qexec/qremote（工具链封装）、update_tail、gpu_wait_retry、
│                             #   webui.js（只读看板，主机运行）、push_watch.sh、mlflow_server.sh
├── docs/                     # 设计/计划/审计文档、REMOTE_ENV、remote_rebuild_runbook
├── results/                  # 台账 db、队列 db、runs/、backup/（全部 gitignored）
└── rqalpha/ qlib_examples/ qbt/   # 历史学习链路（真实规则回测），已冻结，仅参考
```

## ⚙️ 一个实验的一生

1. **提交**：spec 解析并算 `spec_hash`（metrics/expectation 计入口径，改口径必重跑）；
2. **取数**：固定菜单构建 float32 特征缓存（按配置 hash 复用，修订号随数据更新）；
3. **执行**：本地子进程或 DGX 远端（git archive 打包，**先 commit 再 submit**），
   executor.log 流式落盘；
4. **契约检查**：pred.pkl 的 MultiIndex/覆盖度/常数检测，不过即 QLAB_CONTRACT_FAIL；
5. **固定评分**：测试器按 spec 勾选的族产出 metrics.json（与谁写的执行器无关）；
6. **记账 + 通知 + 知识闭环**：MLflow 入账（幂等复用）、done.log → 通知桥 → DSH、
   auto-claim 追加、expectation 对照回写 claim 状态；
7. **复核**：board 看 p_bonf/multiplicity_risk，review run <id> 跑 advisory 检查。

## 🤖 给研究 agent 的规矩（摘要，全文见 AGENTS.md）

- 每个新会话第一件事：把会话 id 写入 `D:\quant_backup\notify\session.txt`。
- 跨机执行一律 `qexec.sh` / `qremote.sh`（脚本文件模式），禁止单行嵌套引号。
- 长任务（训练/全市场）必须显式经用户批准并给耗时估算；做实验环节不需要通知，
  等 batch_completed 再开启分析。
- 改 `pipeline/*.py` 后必须重启 watch dispatcher（旧进程跑旧代码）。
- 结论措辞不得强于证据：带窗口和 p 值；同 base_ref 看 n_variants/p_bonf；
  零结果同样入账。
- 诚实纪律：不许编造数据；用户可以随时派另一个 agent 复查。

## ❓ 常见问题

- **Q：`run --once` 为什么只跑了一轮？** A：--once 语义 = 领 concurrency 个就退（防误跑长任务），
  排空用 `run --watch`；每轮结束会推「本轮结束」通知。
- **Q：任务 blocked 怎么办？** A：spark 不可达。`unblock <job_id>` 强制本地，或
  `retry --blocked`（spark 任务保留 runner，网络恢复可回远端重试）；
  每个 blocked job 只提醒一次。
- **Q：远端 FileNotFoundError（spec 找不到）？** A：git archive 只打包**已提交**文件，
  新 spec/执行器先 `git commit` 再 submit。
- **Q：系数/指标为什么不能拿来自比？** A：模型系数尺度随预处理口径（标签归一化、截距、
  窗口）变化极大，不是质量指标；只认固定测试器的指标。
- **Q：本地容器 pthread_create 失败？** A：pids.max=256，多 python 并发会被 OpenBLAS
  线程打爆——并发工具一律带 `OMP/OPENBLAS/MKL_NUM_THREADS=1`；重活走远端。
- **Q：看板（WebUI）会干扰实验吗？** A：不会。只读旁观：复用只读 CLI、按需查询、
  零写入、容器内无新进程、无操作按钮。

## 📚 更多文档

- 行为规范：`AGENTS.md`（注入每个会话）
- 平台设计：`docs/PIPELINE_DESIGN.md`、`docs/P7_FREEZE_PLAN.md`、`docs/REMOTE_ENV.md`
- 实验记录：`knowledge/notes/`（qb 官方 benchmark 复现全表与挂账清单、调查文档）
- 远端重建教程：`docs/remote_rebuild_runbook.md`
- 历史学习链路：`qbt/README.md`（rqalpha 真实规则回测，已冻结）

## 📄 免责声明

本项目为学习与研究用途。所有回测结果不代表未来收益，不构成投资建议。
实盘需自行评估滑点、流动性、执行偏差与策略失效风险。
