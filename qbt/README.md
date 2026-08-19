# qbt — A股/港股量化回测流水线 CLI

一条命令从数据到报告：baostock 导出 → qlib 训练（Alpha158 + LightGBM/Linear）→ 月度调仓计划 → rqalpha 真实规则回测（T+1/涨跌停/印花税/100股整数倍）→ HTML 报告。

## 安装

```bash
# 项目根目录
pip install -e .
# 运行依赖：qlib、rqalpha、baostock（见 ../requirements.txt）
# CLI 自身依赖：typer、rich、pandas、pyyaml
```

安装后 `qbt --help` 可用。

## 快速开始

```bash
qbt init                          # 生成 qbt.yaml + results/
qbt all --pool hs300 --model lgb  # 一键全链路（约 10 分钟）
qbt status                        # 查看各阶段结果
# 报告在 results/report.html
```

## 命令参考

| 命令 | 说明 | 常用参数 |
|---|---|---|
| `qbt init` | 生成 `qbt.yaml` 配置 + `results/` 目录 | `-f/--force` 覆盖已有配置 |
| `qbt data fetch` | baostock 导出成分股日线（后复权 + 真实 factor + turn，含基准指数） | `--pool hs300/zz500`、`--limit N`（测试）、`--start/--end` |
| `qbt data validate` | 交叉校验：本地 CSV vs 腾讯行情接口 | `--n 抽查数`、`--days`、`--threshold`（默认 0.5%） |
| `qbt data dump` | CSV → qlib bin 格式，自动生成 universe 文件 | `--pool`、`--qlib-dir` |
| `qbt train` | qlib 训练 + 简化规则回测，解析 IC/超额指标 | `--model lgb/linear`、`--pool`、`--tag`、`--yaml-path` |
| `qbt plan` | 训练 lineage 对应 pred.pkl → 每月末交易日调仓计划（rank-buffer + T+1 执行） | `--topk`（默认 50）、`--buffer`（默认 10）、`--freq`（默认 ME）、`--pool` |
| `qbt backtest` | rqalpha 真实规则回测（月度等权调仓；滑点/参与率/停牌重试经 qbt.yaml 注入） | `--capital`（默认 100 万）、`--start/--end`、`--pool`、`--bundle`；执行参数 `backtest.slippage/participation/retry_days` |
| `qbt report` | 汇总各阶段结果 → `results/report.html` | `--out` |
| `qbt all` | 一键全链路：fetch → dump → train → plan → backtest → report | `--pool`、`--model`、`--capital` |
| `qbt status` | 各阶段状态 + 关键指标（读 `results/status.json`） | — |
| `qbt version` | 版本号 | — |

## 配置（qbt.yaml）

`qbt init` 生成，所有 CLI 参数都有配置默认值，参数优先于配置：

```yaml
project:
  results_dir: results          # 状态与日志目录
data:
  start: 2023-01-01             # 数据导出区间
  end: 2026-08-15
  adjust: "1"                   # 后复权 + 真实 factor（P2-3）
train:
  yaml: qlib_examples/lightgbm_alpha158_full.yaml   # 训练模板
  qlib_dir: ~/.qlib/qlib_data/cn_data               # qlib 数据目录
plan:
  topk: 50                      # 每月持仓数
  freq: ME                      # 月末调仓
  rank_buffer: 10               # P1-3: 跌出 top-(K+N) 才卖
backtest:
  capital: 1000000              # 回测本金
  start: 2025-01-01             # 回测区间（样本外）
  end: 2026-08-14
  slippage: 0.0                 # A3: 滑点比例（0=关闭）
  participation: 0.05           # A3: 单日成交量参与率上限
  retry_days: 2                 # P2-4/A4: 未成交买单重试天数上限
```

## 工作流示例

```bash
# 中证500 全链路
qbt all --pool zz500

# 模型对比：Linear vs LightGBM（对比实验，独立 mlflow 实验名）
qbt train --model linear --pool hs300 --tag linear_vs_lgb

# 只重跑回测（数据/训练已有，改本金）
qbt backtest --capital 2000000

# 小规模测试数据管道（10 只）
qbt data fetch --pool hs300 --limit 10

# 数据质量检查
qbt data validate --n 10
```

## 产物与状态

```
results/
├── status.json            # 各阶段状态与关键指标（qbt status 读取）
├── report.html            # 汇总报告
└── logs/                  # 训练日志（失败时排查）
```

`qbt train` 的 mlflow 实验记录在 `qlib_examples/mlruns/`（已被 .gitignore 排除）。

## 常见问题

| 问题 | 处理 |
|---|---|
| `qbt train` 失败，日志报 qlib 错误 | 先 `qbt data fetch` + `qbt data dump` 确认数据就绪；训练日志在 `results/logs/` |
| 缺 rqalpha 行情库 | `rqalpha download-bundle`，或环境变量 `RQALPHA_BUNDLE=/path/to/bundle` |
| `qbt backtest` 报调仓计划不存在 | 先 `qbt train` 再 `qbt plan --pool <池>` |
| 数据导出失败股票多 | baostock 对停牌/数据缺失股票返回空，属正常，日志显示 `fail=N` |
| `qbt data validate` 报差异 | 前复权口径差异（不同数据源基准日不同）属正常，>0.5% 才需关注 |

## 已知限制（诚实声明）

- **幸存者偏差**：默认 fetch 仍为当前成分快照；已提供 `qlib_scripts/export_csv_hist.py` 按调仓日拉历史成分（baostock 历史参数已验证：2023-06 与当前成分差 74 只），配合 dump 后可消除
- 2026-08 执行层修复完成：月末交易日对齐（P0-1）、plan lineage（P0-2）、rank-buffer 换手口径（P1-3）、T+1 执行（P1-4）、指标读 mlruns 文件（P1-5）、topping-up（P2-1）、停牌重试（P2-4/A4）、滑点与参与率（A3）、后复权真实 factor（P2-3）、turn 字段（P2-5）；旧压力测试结论为缺陷版 v1.0，基线以最新 `qbt all` 结果为准
- **滑点为固定比例 + 参与率上限**（qbt.yaml `backtest.slippage/participation`），非逐笔冲击成本模型，敏感性分析需自行分档
- 训练/回测区间硬编码于 `qbt.yaml` 与 qlib yaml 模板，改区间需同步两处
- 简化规则（qlib 段）无 T+1/涨跌停，真实规则（rqalpha 段）才有

## 开发

```bash
# 运行环境
pip install -e . && qbt --help

# 语法检查
python -m py_compile qbt/*.py qbt/commands/*.py

# 单元测试（OPTIMIZATION.md C2；纯函数，零网络/零数据依赖）
python -m pytest tests/ -q
```

冒烟建议：`qbt data fetch --limit 5` → `qbt data dump` → 小样本训练快速验证链路。
