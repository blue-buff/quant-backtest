# quant-backtest — A股/港股量化回测学习项目

用 Python 完成从「技术指标回测」到「ML 因子选股 + 压力测试」的完整学习链路，覆盖 A股真实交易规则建模。

## 项目组成

| 模块 | 工具 | 能力 |
|---|---|---|
| 快速回测 | `backtesting.py` | 双均线策略，A股/港股通用（无 A股规则） |
| 真实规则回测 | `rqalpha` | A股 T+1 / 涨跌停 / 印花税 / 100股整数倍 / 佣金，引擎内置 |
| ML 因子选股 | `qlib` | Alpha158 因子 + LightGBM / Linear，月度 top50 等权调仓 |
| 压力测试 | 组合 | 分段回测、换模型、换股票池，检验超额收益的可靠性 |

## 数据源（中国网络环境实测）

| 用途 | 方案 | 说明 |
|---|---|---|
| A股日线 | baostock | 免费无需注册，`adjustflag="2"` 前复权 |
| 港股日线 | 腾讯行情接口 | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` |
| rqalpha 行情库 | 米筐 bundle | 官方下载约 1GB，解压 3.3G，含分红/复权/ST/停牌 |
| ⚠️ 东财接口（akshare） | 云服务器/容器 IP 会被断连 | 仅本地可用 |

数据文件均不入库：CSV 由 `qlib_scripts/export_csv*.py` 重新导出，qlib bin 数据由 `dump_bin.py` 生成，rqalpha bundle 由官方 `rqalpha download-bundle` 下载。

## 目录结构

```
quant/
├── demo_backtest.py          # backtesting.py 双均线（茅台 600519 / 腾讯 00700）
├── hk_batch.py               # 港股批量回测
├── rqalpha/                  # rqalpha 双均线 + 批量回测（真实 A股规则）
│   ├── strategy.py           #   双均线策略（含 subscribe 示例）
│   ├── rq_run.py             #   Python API 跑回测并输出绩效
│   ├── batch_rq.py           #   多股票批量回测
│   └── config.yml            #   rqalpha 配置模板
├── qlib_examples/            # qlib ML 因子选股 + rqalpha 桥接
│   ├── lightgbm_alpha158_full.yaml      # 沪深300 全段配置
│   ├── lightgbm_alpha158_zz500.yaml     # 中证500 配置
│   ├── linear_alpha158.yaml             # Linear 模型配置
│   ├── make_plan*.py        #   pred.pkl → 月度 top50 调仓计划 CSV
│   ├── rq_strategy_qlib*.py #   rqalpha 真实规则执行调仓计划
│   ├── rq_run_qlib*.py      #   rqalpha 回测入口（100 万本金）
│   └── rebalance_plan*.csv  #   调仓计划产物（示例）
└── qlib_scripts/            # 数据管道
    ├── export_csv.py        #   baostock → CSV（沪深300 成分）
    ├── export_csv_zz500.py  #   baostock → CSV（中证500 成分）
    ├── dump_bin.py          #   CSV → qlib bin 格式
    ├── dump_utils.py
    └── get_data.py          #   qlib 官方数据下载脚本（备用，官方数据较旧）
```

## 环境

- Python 3.12
- `pip install -r requirements.txt`
- rqalpha 需下载行情库：`rqalpha download-bundle`（或将已有 bundle 指向 `data_bundle_path`）
- 路径统一由 `config.py` 管理（自动基于项目根推导，rqalpha 行情库路径可用环境变量 `RQALPHA_BUNDLE` 覆盖），无需修改代码

## 完整复现步骤（ML 因子选股）

```bash
# 1. 导出股票池日线（baostock，约 2-5 分钟）
python3 qlib_scripts/export_csv.py          # 沪深300 → qlib_data_src/
python3 qlib_scripts/export_csv_zz500.py    # 中证500 → qlib_data_src_zz500/

# 2. 转 qlib 格式（dump_bin.py 为 qlib 0.9.x 适配版，参数名 --data_path）
python3 qlib_scripts/dump_bin.py dump_all \
  --data_path qlib_data_src --qlib_dir ~/.qlib/qlib_data/cn_data \
  --include_fields open,high,low,close,volume,vwap,factor
# 中证500 同理，qlib_dir 换 cn_data_zz500；universe 列表：
# grep -v '^SH000905' ~/.qlib/qlib_data/cn_data_zz500/instruments/all.txt > .../csi500.txt

# 3. 训练 + 简化规则回测（qlib 自带撮合，含成本、无 T+1 等 A股细则）
cd qlib_examples
MLFLOW_ALLOW_FILE_STORE=true qrun lightgbm_alpha158_full.yaml

# 4. 生成月度调仓计划（取最新 pred.pkl）
python3 make_plan.py          # → rebalance_plan.csv

# 5. 真实规则回测（rqalpha：T+1/涨跌停/印花税/100股整数倍）
python3 rq_run_qlib.py
```

## 压力测试结论（2025-01 ~ 2026-08，约 20 个月）

**核心结论：策略超额只在「沪深300 + LightGBM」组合上成立，不是普适 alpha。**

| 关卡 | 检验 | 超额年化（含成本） | 结论 |
|---|---|---|---|
| 基线 | 沪深300 + LightGBM | **+12.8%**（IR 1.09） | 真实规则下总收益 +44.8%，跑赢同期指数 +22.1% 约 22.7pct |
| 1. 时间段 | 2025H1 vs 2026 | +22.4% → +11.1%（IR 2.72 → 0.75） | 超额随时间衰减一半 |
| 2. 模型 | LightGBM → Linear | +12.8% → +5.6% | 超额减半但为正：一半来自模型能力，一半是真实规律 |
| 3. 股票池 | 沪深300 → 中证500 | **-2.4%** | 中证500 真实规则 +13.6%，大幅跑输同期指数 +44.1%；IC 相近（0.013）但换手成本+因子拥挤吃掉全部 alpha |

风险提示：回测 ≠ 实盘（未计滑点/冲击成本/停牌）；成分股为当前快照（幸存者偏差）；超额随时间段/模型/股票池全面衰减。

## 关键坑（详见 skill: cn-stock-backtesting）

1. **backtesting.py 0.6.x**：全仓买入后裸 `sell()` 被 margin 逻辑静默取消 → 平仓必须 `self.position.close()`；比例单 `size=0.9999` 按 margin 算股数，官方示例同样失效
2. **rqalpha 6.x**：必须显式 `subscribe()` 否则 `bar_dict` 无该标的、下单不成交；`data_bundle_path` 指向 bundle 内容目录；CLI 日志级别会吞绩效摘要（用 Python API `run()`）
3. **qlib 0.9.x**：
   - 实际训练用 **`task.model`**，不是顶层 `model`（只改顶层会静默跑旧模型）
   - 模型类名 `LinearModel` 而非 `Linear`
   - `dump_bin` 参数名是 `--data_path`（旧文档 `--csv_path` 报错）
   - 官方/镜像数据为 2020 年旧数据，建议自建（baostock → dump_bin）
4. **回测可信度自查**：交易数异常（0 或过少）= 策略有 bug；确认收益/胜率不是 nan

## 免责声明

本项目为学习用途。所有回测结果不代表未来收益，不构成投资建议。实盘需自行评估滑点、流动性、执行偏差与策略失效风险。
