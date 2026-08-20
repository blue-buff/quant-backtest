# 训练与回测过程审计报告（逐文件对比）

> 审计日期：2026-08-20 | 方法：本项目代码与对照项目文件**同时打开逐行对比**（非凭记忆）
> 对照对象：qlib 0.9.7 官方源码 + qlib 官方 benchmarks（github.com/microsoft/qlib）+ rqalpha 6.3.0 官方源码 + hands-on-qlib

## 一、逐文件对比清单

| 本项目文件 | 对照文件 | 结论 |
|---|---|---|
| qlib_examples/lightgbm_alpha158_full.yaml | qlib/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml | ✅ 结构一致；模型超参逐项相同；差异均为已记录改进（去极值/only_tradable/limit_threshold） |
| qlib_examples/lightgbm_alpha158_zz500.yaml | 同目录 *_csi500.yaml | ✅ 同上 |
| qlib_scripts/dump_bin.py | qlib/scripts/dump_bin.py | ✅ **diff 零差异**（官方原版） |
| Alpha158 使用（handler） | qlib/contrib/data/handler.py | ✅ 标签 Ref($close,-2)/Ref($close,-1)-1 与官方一致 |
| qbt/commands/train.py | qlib 官方 qrun workflow | ✅ 调用 qlib.cli.run + mlflow 记录，标准流程 |
| qbt/planlib.py（计划生成） | qlib/contrib/strategy/signal_strategy.py（TopkDropoutStrategy） | ✅ 无规则错误（差异为频率/保留机制，已文档化） |
| qlib_examples/rq_strategy_qlib.py | rqalpha 官方 API（order/order_target_percent/LimitOrder/is_st_stock/get_position/subscribe） | ✅ API 签名逐一匹配；负数量卖出为官方语义 |
| qbt/commands/backtest.py | rqalpha 官方 mod（sys_simulation/sys_transaction_cost/sys_analyser） | ✅ 费用/滑点/参与率/涨跌停/停牌均官方处理 |
| qbt/commands/report.py | rqalpha 官方 analyser summary | ✅ 指标全部官方口径（total_returns/annualized/sharpe/mdd/win_rate/turnover） |

## 二、规则/逻辑检查结论（无 bug 项）

1. **样本外隔离**：train 2023-01~2024-06 / valid 2024-07~12 / test 2025-01~2026-08，无重叠、时间顺序正确 ✅
2. **无前视**：T 日收盘信号 → T+1 交易日执行（P1-4）✅
3. **费用**：rqalpha 默认启用佣金（万八）+ 印花税（千一，卖出）+ 最低 5 元（源码确认 deep_update 合并，非替换）✅
4. **滑点与限价单交互**：matcher 先以 bar 价做限价穿越校验，成交价再经 PriceRatioSlippage 调整（夹涨跌停）——限价单下官方滑点正常生效，无"滑点>0 时限价单全部不成交"问题 ✅
5. **涨跌停**：按方向拒单（涨停拒买/可卖、跌停拒卖/可买）✅
6. **停牌**：bar 缺失拒单；策略层跳过 + 次日重试（P2-4）✅
7. **100 股整数倍**：策略 target_quantity 取整 + rqalpha order_shares 官方再取整（双保险）✅
8. **卖出语义**：order 负数量 = 卖出（官方文档明确）✅
9. **ST 过滤**：is_st_stock（P0-1）✅
10. **dump 数据**：dump_bin 与官方零差异；turn/factor 字段已入 bin ✅
11. **权重**：0.99/持仓数（留 1% 现金）✅
12. **卖出先于买入**（同日回笼资金）✅

## 三、本轮审计发现并已修复

| # | 问题 | 级别 | 修复 |
|---|---|---|---|
| A | 未配置 rqalpha 官方 benchmark——超额指标自己用本地 CSV 算，与官方 bundle 口径可能不一致；官方 excess_sharpe 等指标未利用 | 中 | backtest.py 配置 sys_analyser.benchmark（SH000300→000300.XSHG），state 记录官方 benchmark_returns/excess_returns；report 优先用官方值，本地 CSV 兜底 |
| B | planlib sort_values 默认不稳定排序（quicksort），同分股票边界抖动可能造成无谓换手 | 低 | 两处改为 kind="mergesort" 稳定排序 |
| C | qbt plan 的 zz500 日历误读 hs300 的 qlib_dir（A股日历当前相同，无实际影响，但逻辑不正确） | 低 | _calendar_from_cfg 接收 pool_cfg，按池取 qlib_dir |
| D | lgb 训练时 experiment_name 未设置（run 混入 mlflow Default 实验） | 低 | 每次训练固定设置独立实验名 workflow_{pool}_{时间戳} |

## 四、完善建议（未实施，需决策）

- **E. 特征 warmup**：数据从 2023-01 开始，Alpha158 前 60 日窗口使训练集前 ~2 个月样本被 Dropna 丢弃。建议数据向前多拉 1 年（fetch_sina --start 2022-01-01），或接受现状（少 2 个月训练数据）。
- **F. 分层检验**：报告只有 IC/ICIR，建议加"按分数分 10 层、每层月度收益"单调性检验（alphalens 或自实现）——判断预测力的结构。
- **G. bootstrap 显著性**：scripts/analysis_bootstrap.py 已备好，建议对 IC 序列跑 bootstrap 置信区间。

## 五、结论

训练与回测链路**未发现规则性/逻辑性 bug**；本轮修复 4 个完善项（A 中，B/C/D 低）。系统结构（yaml 配置、qlib workflow、rqalpha 撮合、费用与规则引擎）与官方基准一致，可以作为后续工作的可靠基础。
