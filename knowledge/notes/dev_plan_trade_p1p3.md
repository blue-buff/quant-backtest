# 交易闭环 P1-P3 开发计划

> 生成：2026-08-27。承接《量化研究指标清单 × 专家经验手册》第四部分优先行动，
> 与 trade_backtest_rubric.md 的四层门控对齐。
> 每项标注：目标 / 任务与验收 / 涉及文件 / 本次已落地或剩余。
> 总原则：指标口径冻结在固定测试器（pipeline.metrics），执行器只交 pred/portfolio。

## 前置 P0（先修证据口径，P1-P3 的闸门）
| 任务 | 验收 | 状态 |
|---|---|---|
| 执行延迟对照：T 日收盘 / T+1 收盘 / T+1 开盘 | backtest 输出三档净年化+超额 | 🆕 已落地 T/T+1/T+2 close（`execution_lag`）；T+1 开盘需 open 价，待数据扩展 |
| 标签边界 purge/embargo | train/valid/test 边界剔除 horizon 重叠标签 | 📋 剩余：pipeline/data.py 加边界裁切（10 日标签裁 horizon 天） |
| 净超额作为主判据 | 交易结论先看 excess_ann/cost_drag，再谈 RankIC | 🆕 指标已全（excess_ann/alpha_ann/cost_drag_ann/cost_alpha_ratio）；SOP 已写进 rubric |

## P1 降换手（目标：turnover 从 ~0.3/天 降到成本可控，net excess 不为换手吃光）
| 任务 | 验收 | 状态 |
|---|---|---|
| 真正 rank buffer（跌出 topK 不立即卖，buffer 内保留） | executors/_example_topk_portfolio 加 buffer 参数；组合层 turnover 可测 | 📋 剩余（执行器层） |
| weekly / monthly rebalance | backtest 支持按周期采样权重日；换手随频率下降 | 📋 剩余（执行器产出稀疏权重 + tester 已验证稀疏权重可用） |
| top_k 参数平台（30/50/70/100） | 4 个 spec 同 base_ref，board 比 n_variants + 换手/超额曲线 | 📋 剩余（实验设计） |
| 报告 turnover 与 cost drag | 每个交易 run 台账含 `turnover_mean/ann` + `cost_drag_ann` | 🆕 指标已落地；需 board 汇总列确认 |
| 参数平台检验（buffer 0/5/10/20，rebalance 日/周/月） | 找稳定区间非孤峰，写入 conclusion | 📋 剩余（与上两条合并成一批） |

## P2 稳健性（目标：单一窗口/单一种子通过不算数）
| 任务 | 验收 | 状态 |
|---|---|---|
| 不同 seed | 同 spec 3 seed，board 比 rankic/超额方差 | 📋 剩余（多 run） |
| 不同非重叠取样 offset | 报告全部相位 + 最差相位 | 🆕 已落地 `nonoverlap_offsets` / `nonoverlap_min_rank_ic` |
| 分季度表现 | 信号层 `quarters` + 回测层 `quarterly_returns` | 🆕 已落地 |
| 参数平台 | 与 P1 top_k/buffer/rebalance 平台共用 | 📋 剩余 |
| 未来 paper trading | 固定规则脚本 + 漂移记录 | 📋 剩余（远期，需部署形态） |

## P3 统计升级（目标：给关键策略上"防过拟合"统计）
| 任务 | 验收 | 状态 |
|---|---|---|
| PSR | `backtest.psr` 对基准 sharpe 的概率 | 🆕 已落地（Bailey & López de Prado 公式） |
| DSR（deflated Sharpe） | 多尝试次数 + 非正态修正 | 📋 计划（**won't-do 冲突**：AGENTS §9 明确列了 deflated Sharpe，需你解冻） |
| PBO | 过拟合概率（多路径） | 📋 计划（需多 run/路径，与 CPCV 一起设计） |
| CPCV | 组合式净化交叉验证 | 📋 计划（**won't-do 冲突**：分块 bootstrap/拆考场类） |
| 因果性检查清单 | review.py 加 5 问（代理/confounder/collider/方向/剥离后剩多少） | 📋 剩余（advisory，非阻断） |

## 执行顺序建议
1. 先 P0 剩余（purge/embargo + T+1 开盘数据），因为它是 P1/P2 结论可信的前提。
2. P1 一批（buffer + rebalance + top_k 平台）→ 目标是 Stage C 及格线（excess_ann>0、alpha>0）。
3. P2 一批（seed + 参数平台 + 分季度）→ 追 Stage C 良好线（+5%/sharpe≥1）。
4. P3 只在"关键策略升 refs 基线"时上（DSR/PBO/CPCV），并需先解冻 won't-do。

## 与本次代码的关系
- 本次只改固定测试器（metrics.py + 新测试），执行器/数据层一律未动；
  所有 P1/P2 的"剩余"项都在执行器或数据层，不碰测试器口径（遵守 P7 冻结线）。
- 数据前置（open/volume/amount/停牌）是执行层 5 个 ⚠️ 指标与 T+1 开盘延迟的共因，
  建议与 P0 的 purge/embargo 一起并入一次数据升级立项，不散装改动。
