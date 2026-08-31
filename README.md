# QLab 量化实验平台 🧪

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-180%20passed-2e7d32)
![MLflow](https://img.shields.io/badge/ledger-MLflow%20sqlite-0194E2)

> A 股量化实验平台：写一个 spec 提交实验，管线负责取数、固定评分、记账；
> 执行器内部完全自由，只靠契约与测试器保证**可比性**。

## ✨ 核心特性

- **契约式执行器**：只需实现 `main.py --config --train --test --out`，输出 `pred.pkl`
  （选股）与 `portfolio.pkl`（交易）即可接入；管线从不 inspect 执行器内部。
- **固定测试器**：`pipeline.metrics` 是唯一指标来源——预测五族（rankic/bootstrap/deciles/
  quarters/hit）+ 交易四族（portfolio/backtest/attribution），spec 勾选制，杜绝口径漂移。
- **单写者台账**：MLflow sqlite，入库自动带 `qlab.*` tags（spec_hash / commit /
  data_rev / runner…），board 一键导出，含同 base_ref 的 Bonferroni 多重检验修正。
- **纯排队调度**：FIFO 队列（submit / retry / cancel / unblock / heal），无资源分配；
  `--once` 一轮即退、`--watch` 排空。
- **知识闭环**：每个实验自动落一条 auto-claim，expectation 预注册自动对照
  （met→confirmed / not_met→falsified），claims.jsonl 全自动回写。
- **诚实纪律内建**：脏代码门禁、spec_hash 防漂移、取数时数据修订号、快照双备份、
  180 条 pytest 回归测试（含并发 / 竞态 / 故障注入）。

## 🏗️ 架构总览

```
spec.json ──► 队列(jobs.db) ──► dispatcher ──► run 进程
                                                    │
    取数(pipeline.data 固定菜单) → 执行器(自由) → 契约检查 → 固定测试器 → 记账
                                                    │
                    board.csv / claims.jsonl / WebUI ◄──┘
```

管线只拥有「可比性三件套」：**同一份数据、同一个评分口径、同一本账**。
模型、预处理、训练协议、交易策略全部属于执行器自由。

## 📦 环境

- Python 3.12+，`pip install -r requirements.txt`
- MLflow sqlite 台账（`python -m pipeline.board` 主入口，无需启动 server）
- 通知桥与 WebUI 为可选组件（node，零依赖）

## 🚀 快速开始

```bash
# 0. 初始化知识库（首次使用）
python -m pipeline.kb init

# 1. 写 spec（base 用内联 dict 即可，无需预置 refs/）
mkdir -p experiments/specs experiments/batches
cat > experiments/specs/my_exp.json <<'EOF'
{
  "exp_id": "my_first_exp",
  "base": {
    "universe": "hs300",
    "dataset": {
      "class": "DatasetH",
      "handler": {
        "class": "Alpha158",
        "fit_start_time": "2021-06-01",
        "fit_end_time": "2024-06-30",
        "instruments": "hs300"
      }
    },
    "label": {
      "ref": "$close", "shift": -11, "horizon": 10,
      "formula": "Ref($close,-11)/Ref($close,-1)-1"
    },
    "processors": { "learn": [{"class": "DropnaLabel"}] },
    "model": { "class": "LGBModel", "loss": "mse" }
  },
  "changes": "首个实验",
  "expectation": {"rankic_mean_min": 0.04},
  "action": {"kind": "train", "executor": "executors/_example_lgb"}
}
EOF

# 2. 写 batch 并提交（幂等，已 done 自动跳过）
cat > experiments/batches/b-my.json <<'EOF'
{"batch_id": "b-my", "specs": ["experiments/specs/my_exp.json"]}
EOF
python -m pipeline.queue submit experiments/batches/b-my.json

# 3. 跑一轮（--once 只领 concurrency 个；排空用 --watch）
python -m pipeline.queue run --once --concurrency 2

# 4. 看结果
python -m pipeline.board --json --formal    # 正式行（FINISHED 非 smoke）
python -m pipeline.queue status --json      # 队列状态
```

跑完自动入账：metrics 全部来自固定测试器；台账行带 spec_hash / git commit / data_rev，
run 目录内**每一个文件**（模型、spec 原件、portfolio）全部归档为 artifacts。

人类看板（可选）：`node scripts/webui.js` → **http://127.0.0.1:8099**
（只读旁观：总览 / 队列 / 台账 / claims / 事件，无操作入口）。

## 📁 目录结构

```
quant/
├── pipeline/                 # 平台核心
│   ├── spec.py               #   spec 校验 / hash / base 解析
│   ├── queue.py              #   队列：submit / run / retry / cancel / unblock / heal
│   ├── harness.py            #   执行链：取数→执行器→契约→测试器→记账
│   ├── metrics.py            #   固定测试器（唯一指标来源，bootstrap 种子 42）
│   ├── data.py               #   数据固定菜单 + 特征/价格缓存 + 修订号
│   ├── executor.py           #   执行器契约检查 + venv 管理
│   ├── registry.py / board.py#   台账读写 + board 导出（多重检验修正）
│   ├── kb.py / review.py     #   claims 知识库 + advisory 评审
│   ├── backup.py             #   快照（在线一致性 db 副本）
│   └── remote.py             #   远端执行（可选，runner=spark）
├── executors/                # 执行器（自由区）：_example_lgb / _example_topk_portfolio /
│   └── qlib_bench/           #   qlib 官方 benchmark 复现（vendor + shim）
├── qbt/                      # 独立 CLI 工具（data / plan / backtest / report）
├── qlib_scripts/             # 数据导出与 dump_bin 工具
├── rqalpha/                  # rqalpha 规则回测（独立链路，仅参考）
├── data/extra/               # 额外数据 manifest（退市股补齐等）
├── tests/                    # pytest（180 passed, 2 skipped）
├── scripts/                  # qexec / qremote / update_tail / webui.js / mlflow_server.sh
├── demo_backtest.py          # 双均线最小回测 demo（baostock 数据）
├── config.py / requirements.txt / pyproject.toml
└── .github/workflows/ci.yml  # CI
```

## ⚙️ 一个实验的一生

1. **提交**：spec 解析并算 `spec_hash`（metrics / expectation 计入口径，改口径必重跑）；
2. **取数**：固定菜单构建 float32 特征缓存（按配置 hash 复用，修订号随数据更新）；
3. **执行**：子进程运行执行器，executor.log 流式落盘；
4. **契约检查**：pred.pkl 的 MultiIndex / 覆盖度 / 常数检测，不过即 QLAB_CONTRACT_FAIL；
5. **固定评分**：测试器按 spec 勾选的族产出 metrics.json（与谁写的执行器无关）；
6. **记账 + 知识闭环**：MLflow 入账（幂等复用）、auto-claim 追加、
   expectation 对照回写 claim 状态；
7. **复核**：board 看 p_bonf / multiplicity_risk，`review run <id>` 跑 advisory 检查。

## ❓ 常见问题

- **Q：`run --once` 为什么只跑了一轮？** A：--once 语义 = 领 concurrency 个就退
  （防误跑长任务），排空用 `run --watch`。
- **Q：任务 blocked 怎么办？** A：`unblock <job_id>` 强制本地，或
  `retry --blocked`（保留 runner，网络恢复可回远端重试）。
- **Q：spec 被拒（QLAB_SPEC_DRIFT）？** A：排队期间改了 spec 文件；改回原文件后 retry，
  或重新 submit 记录新配置。
- **Q：远端任务报 FileNotFoundError？** A：git archive 只打包**已提交**文件，
  新 spec / 执行器先 `git commit` 再 submit。
- **Q：系数/指标为什么不能拿来自比？** A：模型系数尺度随预处理口径（标签归一化、
  截距、窗口）变化极大，不是质量指标；只认固定测试器的指标。

## 📚 更多文档

- 执行器契约与示例：`executors/README.md`、`executors/_example_lgb/`
- qlib benchmark 复现说明：`executors/qlib_bench/README.md`、`VENDOR.md`
- qbt CLI：`qbt/README.md`

## 📄 免责声明

本项目为学习与研究用途。所有回测结果不代表未来收益，不构成投资建议。
实盘需自行评估滑点、流动性、执行偏差与策略失效风险。
