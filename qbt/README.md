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
| `qbt data fetch` | baostock 导出成分股日线（前复权，含基准指数） | `--pool hs300/zz500`、`--limit N`（测试）、`--start/--end` |
| `qbt data validate` | 交叉校验：本地 CSV vs 腾讯行情接口 | `--n 抽查数`、`--days`、`--threshold`（默认 0.5%） |
| `qbt data dump` | CSV → qlib bin 格式，自动生成 universe 文件 | `--pool`、`--qlib-dir` |
| `qbt train` | qlib 训练 + 简化规则回测，解析 IC/超额指标 | `--model lgb/linear`、`--pool`、`--tag`、`--yaml-path` |
| `qbt plan` | 最新 pred.pkl → 每月 top-K 调仓计划 | `--topk`（默认 50）、`--freq`（默认 ME）、`--pool` |
| `qbt backtest` | rqalpha 真实规则回测（月度等权调仓） | `--capital`（默认 100 万）、`--start/--end`、`--pool`、`--bundle` |
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
  adjust: "2"                   # 前复权
train:
  yaml: qlib_examples/lightgbm_alpha158_full.yaml   # 训练模板
  qlib_dir: ~/.qlib/qlib_data/cn_data               # qlib 数据目录
plan:
  topk: 50                      # 每月持仓数
  freq: ME                      # 月末调仓
backtest:
  capital: 1000000              # 回测本金
  start: 2025-01-01             # 回测区间（样本外）
  end: 2026-08-14
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

- **幸存者偏差**：成分股为当前快照，回看历史（改进方向：按调仓日拉历史成分）
- **无滑点模型**：真实规则已含印花税/佣金，但未计冲击成本
- 训练/回测区间硬编码于 `qbt.yaml` 与 qlib yaml 模板，改区间需同步两处
- 简化规则（qlib 段）无 T+1/涨跌停，真实规则（rqalpha 段）才有

## 开发

```bash
# 运行环境
pip install -e . && qbt --help

# 语法检查
python -m py_compile qbt/*.py qbt/commands/*.py
```

测试建议：`qbt data fetch --limit 5` → `qbt data dump` → 小样本训练快速验证链路。
