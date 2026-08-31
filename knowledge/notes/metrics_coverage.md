# 指标可测性矩阵（清单全覆盖核对）

> 生成：2026-08-27。目标：文中每一层每个指标，标清「现在怎么测 / 本次新增 / 缺口」。
> 图例：✅ 已有 ｜ 🆕 本次新增（pipeline/metrics.py，已带测试）｜
> 🔬 board 级（跨 run 才有意义）｜ ⚠️ 需数据扩展或执行器层 ｜ 📋 计划中（见 P1-P3 计划）。
> 全部新增指标落在 metrics.json（并随 run 入 MLflow 台账 artifacts）；
> 数值叶子经 _flatten_metrics 进 MLflow metrics（列表类如 execution_lag 在 metrics.json 内）。

## 1. 数据层（多为数据属性，非单-run 指标）
| 指标 | 状态 | 位置 / 说明 |
|---|---|---|
| Survivorship bias | ⚠️ 已知 caveat | 数据 v3 为当前成分（已退市补到退市日），AGENTS §4 标注；board 无列 |
| Point-in-time universe | ⚠️ 未实现 | 当前成分 ≠ PIT；属数据 v4 欠账，结论强制带 caveat |
| Coverage | 🆕 | 信号层 `coverage`（有限值占比）；组合另有契约 `date_frac` |
| Missing data | 🆕 隐含 | 1 - coverage；精确逐段缺失分布需 data 层补 |
| Corporate action | ✅ 近似 | 价格缓存为后复权 close；动态复权未做（⚠️） |
| Trading constraint | ⚠️ | 停牌/涨跌停/T+1 未建模；fill rate/liquidity 需 volume+停牌数据 |
| Data revision | ✅ 不适用 | 只用价格量价（Alpha158/360），无财务 revision |
| Sample window | ✅ | `sample_window` + `n_days`（信号/回测两段都有） |

## 2. 信号层（pipeline.metrics compute_full）
| 指标 | 状态 | key |
|---|---|---|
| IC | ✅ | `mean_ic` |
| RankIC | ✅ | `mean_rank_ic` / `nonoverlap_mean_rank_ic` |
| ICIR | ✅ | `icir` |
| RankICIR | ✅ | `rank_icir` / `nonoverlap_rank_icir` |
| Non-overlap RankIC | ✅ | `nonoverlap_mean_rank_ic`（h 步长） |
| 不同 offset 的 non-overlap | 🆕 | `nonoverlap_offsets`（全相位 0..h-1）+ `nonoverlap_min_rank_ic` |
| p 值 | ✅ | `bootstrap_rankic.p_le0` |
| Bootstrap CI | ✅ | `bootstrap_rankic.ci95_lo/hi` |
| Decile monotonicity | ✅ | `deciles.monotonicity_spearman` |
| Top-bottom spread | ✅ | `deciles.top_minus_bottom` |
| Top group excess | 🆕 | `deciles.top_minus_universe`（top 组 vs 全体等权均值） |
| Hit rate | ✅ | `hit_rate` |
| AUC | ✅ | 分类任务 `mean_auc` / `bootstrap_auc.p_le05` |
| Monthly / quarterly IC | ✅ | `monthly_ic` / `quarters` |
| IC decay | ⚠️ | 单 run 只有单 horizon；需跑多 horizon 多 spec（计划，见 P2） |

## 3. 组合层（compute_portfolio）
| 指标 | 状态 | key |
|---|---|---|
| Weight IC | ✅ | `weight_ic_mean` |
| Turnover（双边） | ✅ | `turnover_mean` |
| One-way turnover | 🆕 | `oneway_buy_mean` / `oneway_sell_mean` |
| n_held | ✅ | `n_held_mean` |
| HHI | ✅ | `hhi_mean` |
| Cash fraction | ✅ | `cash_frac_mean` |
| Active share | 🆕 | `active_share_mean`（vs 等权全体，标注口径） |
| Tracking error | 🆕 | 回测段 `backtest.tracking_error` |
| Position concentration | 🆕 | `top1_frac_mean` / `top5_frac_mean` / `top10_frac_mean` |
| Rebalance frequency | 🆕 | `rebalance_day_frac`（建仓后调仓天数占比） |
| Rank buffer | ⚠️ | 执行器策略属性；P1 在 topk 执行器实现 buffer |

## 4. 执行层（compute_backtest）
| 指标 | 状态 | key |
|---|---|---|
| Gross return | ✅/🆕 | `gross_ann_ret` + 🆕 `gross_total_return` |
| Net return | ✅/🆕 | `ann_ret` + 🆕 `total_return` |
| Cost drag | ✅ | `cost_drag_ann`（年化毛-净） |
| Cost / alpha | 🆕 | `cost_alpha_ratio`（cost_drag / 毛超额） |
| Commission | ✅ | `cost_components.commission` |
| Stamp tax | ✅ | `cost_components.stamp` |
| Slippage | ✅ | `cost_components.slippage` |
| Market impact | ⚠️ | 需 volume/成交额 → 数据扩展 |
| Implementation shortfall | ⚠️ | 需真实成交价 → paper trading（P2） |
| Liquidity participation | ⚠️ | 需 volume → 数据扩展 |
| Capacity | ⚠️ | 需 volume/流通市值 → 数据扩展 |
| Execution lag | 🆕 | `execution_lag`（lag 0/1/2 三档 close 成交对照） |
| Fill rate | ⚠️ | 需停牌/涨跌停 → 数据扩展 |

> 执行层 5 个 ⚠️（market impact / liquidity / capacity / fill rate / T+1 开盘）
> 的共因：价格缓存只有 close 一列。要补测需在 price cache 加 open/volume/amount
> + 停牌标志，属数据 v4 范畴，写入 P1-P3 计划的「数据前置」。

## 5. 绩效层（compute_backtest）
| 指标 | 状态 | key |
|---|---|---|
| Total return | 🆕 | `total_return` / `gross_total_return` / `bmk_total_return` |
| Annualized return | ✅ | `ann_ret` |
| Annualized volatility | ✅ | `ann_vol` |
| Sharpe | ✅ | `sharpe` |
| Sortino | 🆕 | `sortino` |
| Calmar | 🆕 | `calmar` |
| Max drawdown | ✅ | `mdd` |
| Drawdown duration | 🆕 | `mdd_duration_days` |
| Excess return | ✅ | `excess_ann` |
| Beta | ✅ | `beta` |
| Alpha（CAPM） | 🆕 | `alpha_ann`（ann_ret - beta×bmk_ann_ret） |
| Information ratio | 🆕 | `information_ratio` |
| Win rate | 🆕 | `win_rate`（日度净收益>0 占比） |
| Profit factor | 🆕 | `profit_factor` |
| Avg win / loss | 🆕 | `avg_win` / `avg_loss` |

## 6. 风险层（compute_backtest）
| 指标 | 状态 | key |
|---|---|---|
| Worst day | ✅ | `worst_day` |
| Worst week / month | 🆕 | `worst_week` / `worst_month` |
| VaR | 🆕 | `var_95` |
| CVaR | 🆕 | `cvar_95` |
| Skewness | 🆕 | `skewness` |
| Kurtosis | 🆕 | `kurtosis`（超额峰度） |
| Tail ratio | 🆕 | `tail_ratio`（右尾5%均值/左尾5%均值） |
| Rolling drawdown | 🆕 | `rolling_mdd_60d` |
| Regime breakdown | 🆕 | `quarterly_returns`（分季 net/bmk/excess/vol）+ 信号层 `quarters` |
| Factor exposure | ⚠️ | 需风格因子（市值/动量/波动率）→ 数据扩展 |
| Liquidity risk | ⚠️ | 需 volume/流通市值 → 数据扩展 |
| Leverage | 🆕 | `max_exposure` / `min_exposure`（契约 ≤1，此处为监测） |

## 7. 统计稳健层
| 指标 | 状态 | 位置 |
|---|---|---|
| n_days | ✅ | `n_days`（信号/回测） |
| n_nonoverlap | ✅ | `n_nonoverlap` |
| p value | ✅ | bootstrap p |
| p_bonf | 🔬 | board 列（p × n_variants，AGENTS §8） |
| Variants tried | 🔬 | board `n_variants` / `multiplicity_risk` |
| PSR | 🆕 | `backtest.psr`（vs 基准 sharpe） |
| DSR | 📋 | P3（won't-do 冲突，见计划） |
| PBO | 📋 | P3（需多 run/路径） |
| CPCV | 📋 | P3（won't-do 冲突，见计划） |
| Purge / Embargo | 📋 | P0（数据层边界处理） |
| Seed variance | 📋 | P2（多 run） |
| Parameter plateau | 📋 | P1/P2（多 run 参数平台） |
| Walk-forward | 📋 | P2（won't-do 冲突，见计划） |
| Holdout | 📋 | P2（won't-do 冲突，见计划） |
| Live / paper trading | 📋 | P2（未来） |

## 8. 本次改动清单
- `pipeline/metrics.py`：+214 行（净增），新增信号 3 键、组合 7 键、回测约 30 键、
  回撤持续/分季/执行延迟对照 3 个辅助函数；PRED_SECTIONS["rankic"] 纳入新键。
- `tests/test_metrics_checklist.py`：新增 7 个测试（手算对照 + 恒等关系 + 结构存在）。
- 兼容性：既有测试 25/25 通过（test_backtest / test_portfolio / test_metrics_checklist）；
  既有 key 不变，board / attribution / review 消费路径不破。
