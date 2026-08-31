# pysystemtrade 可借鉴点报告（QLab「信号→持仓→风控」层重构参考）

> 分析对象：`/Users/lucas/projects/quant_refs/pysystemtrade/`（commit b4a25e6，GPLv3）。稀疏检出已按需补齐 `sysquant/`、`systems/provided/`、`sysdata/`、`syscore/`、`sysobjects/`，全部结论基于实际读到的源码。
> 目标：A 股个人量化平台 QLab。契约：执行器输出 `pred.pkl`（score）+ `portfolio.pkl`（每日权重，≥0、行和≤1）。
> 基线对照：`executors/_example_topk_portfolio/main.py`——LightGBM 出分 → 每日 topK 等权 + 固定阈值 buffer（|目标−当前|>0.005 才动）。
> A 股语境：T+1、卖出印花税千1、佣金双边万2.5、滑点1bp、涨跌停、long-only、个人资金量小。
> **姊妹篇**：`docs/pysystemtrade_borrow_report.md`（精炼 8 组件版，核心公式逐一核实；
> 本文件为 12 组件详版，含 sysquant 层实现细节）。
> **本报告是 cleanroom spec**：GPLv3 只提取公式/接口/测试向量（不受版权保护），
> 实现阶段只读本 spec、不翻源码、独立重写（纪律见 `refactor_dev_plan.md` §2.4/§8）。

---

## 1. 架构地图：系统分层

`systems/basesystem.py::System` = **stage 列表 + Config + 数据**。每个 stage 继承 `systems/stage.py::SystemStage`，stage 间只通过 `system.<stage_name>.<method>()` 引用上游输出；`systems/system_cache.py` 的装饰器做记忆化——`@input`（上游引用，不缓存）、`@diagnostic`（中间量）、`@output`（关键输出），`protected=True` 标记跨品种慢估计（相关矩阵、优化权重）防误删；`delete_items_for_instrument` 支持实时行情到来时只失效单品种缓存。**缓存层就是数据流图**，谁算过就记住——这是「管线冻结 + 执行器自由」的另一种工程形态，QLab 执行器内可照抄这个装饰器思路（免重复计算、免手工传参）。

| 层 | 一句话职责 | 关键类/文件 | QLab 对应 |
|---|---|---|---|
| 数据/raw | 价格→收益→波动率（EWMA），供规则与仓位共用 | `systems/rawdata.py::RawData`、`sysquant/estimators/vol.py::robust_vol_calc` | pipeline.data 固定菜单（已冻结，勿动） |
| 规则 forecasting | 函数式规则（function+数据引用+参数）→ raw forecast | `systems/forecasting.py::Rules`、`systems/trading_rules.py::TradingRule` | 模型打分（执行器内） |
| 缩放封顶 | 各规则 forecast 统一尺度（平均绝对=10）再 clip ±20 | `systems/forecast_scale_cap.py::ForecastScaleCap`、`sysquant/estimators/forecast_scalar.py::forecast_scalar` | 分数标准化（新） |
| 组合 combine | 多规则加权求和 × forecast 分散乘数 FDM，再 clip | `systems/forecast_combine.py::ForecastCombine` | 多模型集成（新） |
| 仓位 sizing | 波动率目标法：forecast → 头寸 | `systems/positionsizing.py::PositionSizing` | **目标权重（核心）** |
| 组合 portfolio | instrument 权重 × 分散乘数 IDM → 组合名义仓位 | `systems/portfolio.py::Portfolios` | 权重矩阵构建 |
| 风险覆盖 | 组合风险超限 → 全局缩仓乘数（0~1） | `systems/risk.py::Risk`、`systems/risk_overlay.py::get_risk_multiplier` | **风控层（核心）** |
| 执行 accounts | buffer 不交易区、成交/成本、P&L 五层归因、资本乘数 | `systems/accounts/`（`accounts_stage.py::Account`） | 执行器尾部（测试器已有指标） |

组合起来的完整信号链（`systems/provided/futures_chapter15/basesystem.py::futures_system` 是标准拼法，`systems/provided/rob_system/run_system.py` 是加风险层/动态优化的进阶拼法）：

```
raw data → rules(raw forecast) → forecastScaleCap(scaled, capped ±20)
→ combForecast(加权和 × FDM, capped ±20)
→ positionSize(vol_scalar × forecast/10)         # 波动率目标
→ portfolio(× instrument_weight × IDM, × risk_scalar)
→ accounts(buffered position, fills, P&L, costs)
```

配置即代码：`systems/provided/rob_system/config.yaml` 用纯 YAML 声明 40+ 规则、逐品种 forecast 权重、vol 计算、risk_overlay 参数——QLab 执行器的 params 透传已经是同构的。

---

## 2. 12 个最有价值的可借鉴组件（按 A 股选股平台价值排序）

### C1. 波动率目标仓位（vol targeting）——topK 等权的直接升级

- **位置**：`systems/positionsizing.py` — `PositionSizing.get_subsystem_position`、`get_average_position_at_subsystem_level`、`get_daily_cash_vol_target`、`annual_cash_vol_target`；vol 估计 `sysquant/estimators/vol.py::robust_vol_calc`。
- **做了什么**：把 forecast 换成「每单位波动率预算对应的头寸」。比 topK 等权强在：**高波动股票自动低配、低波动股票自动高配**——组合暴露的是"风险"而不是"名义金额"；全组合风险被钉在 vol_target 常数上，风险/收益的量纲正确。
- **数学（原文链）**：
  - `annual_cash_vol_target = notional_capital × percentage_vol_target/100`；`daily_cash_vol_target = annual / ROOT_BDAYS_INYEAR`（`ROOT_BDAYS_INYEAR=√256`）
  - `instr_value_vol = block_value × daily_perc_vol`（期货 block 概念；A 股对应"持仓市值 × 日波动率%"）
  - `vol_scalar = daily_cash_vol_target / instr_value_vol`
  - **`subsystem_position = vol_scalar × combined_forecast / avg_abs_forecast`**（`avg_abs_forecast` 默认 10）
  - long-only：`_apply_long_only_constraint_to_position` 负头寸清零（A 股天然适用）
- **QLab 落地（直接可移植）**：契约输出权重矩阵，A 股版即**波动率倒数加权**：
  ```
  # 每交易日 t：
  σ_i(t) = EWMA(span=35, min_periods=10)(日收益).std() × √252      # robust_vol_calc 的 ewvol
  σ_i(t) = max(σ_i(t), rolling(500, min_periods=100).quantile(0.05))  # vol floor，防停牌后脉冲
  w_raw_i(t) = max(score_i(t),0) / σ_i(t)
  w_i(t) = w_raw_i(t) / Σ_j w_raw_j(t) × invest_frac               # 截面归一，行和≤1
  ```
  直接抄：`simple_ewvol_calc` 三层防御（EWMA + 绝对下限 + 500 日 5% 分位地板）。改造点：去掉 block_value/FX；score 需先非负化（C6）；涨跌停日照常出权重（是否成交由外部执行决定，不阻塞矩阵生成）。

### C2. Risk overlay：组合风险预算的总闸门（保命件）

- **位置**：`systems/risk_overlay.py::get_risk_multiplier`、`multiplier_given_series_and_limit`；组合风险 `sysquant/portfolio_risk.py::calc_portfolio_risk_series`、`calc_sum_annualised_risk_given_portfolio_weights`；冲击波动率 `sysquant/estimators/stdev_estimator.py::seriesOfStdevEstimates.shocked`（10 年滚动 99% 分位）；实例参数见 `systems/provided/rob_system/config.yaml`（`risk_overlay:` 段）。
- **做了什么**：组合层面算四个风险读数，各自除以限额得乘数，**取最小值**做全局仓位乘数（0~1）乘到所有仓位。topK 等权在组合层面是盲的：个股各自 1/k 权重，**相关性聚集（如 A 股银行+地产同涨同跌）时组合风险爆表**，这是个股级方法永远兜不住的。
- **数学**：
  - `σ_p = √(w'Σw)`，Σ = diag(σ)·C·diag(σ)，C 为 EWMA 相关（span≈250~500，缺失 offdiag 填 0.99）
  - `Σ_abs = Σᵢ|wᵢ|σᵢ`（无分散假设的和风险）；`leverage = Σ|wᵢ|`
  - 乘数 `mult_k = risk_limit_k / max(risk_measure_k, risk_limit_k)`；`joint_mult = min(mult_normal, mult_shocked, mult_sumabs, mult_leverage)`
  - 原文参数（rob_system）：normal 1.75×vol_target、shocked 4.0×、sum_abs 4.0×、leverage 20.0
- **QLab 落地（整体移植，~40 行）**：
  ```
  # 每交易日 t：
  σ_p(t) = sqrt(w_t' Σ_t w_t)          # Σ_t = D_t C_t D_t
  Σ_abs(t) = Σ_i w_it σ_it
  mult(t) = min( vol_target/σ_p, 1.0×vol_target/σ_shock, 1.2/Σ_abs, 1.5/leverage, 1.0 )
  w'_t = w_t × mult(t)                  # 行和≤1 契约下杠杆项≈1，缩仓即 min 生效
  ```
  A 股无杠杆：leverage 限额≈1.0~1.5、sum_abs 限额≈1.0~1.2、normal 限额 = 目标年化波动（如 15%）。注意 500 票相关矩阵病态，需先做 C8 清洗或降到行业层算（C3）。

### C3. 相关聚类权重（handcraft / 行业层风险平价）——从「等权」到「去相关」

- **位置**：`sysquant/optimisation/optimisers/handcraft.py`（`handcraft_optimisation`、`handcraftPortfolio.risk_weights`、`aggregate_risk_weights_over_sub_portfolios`、`multiplied_out_risk_weight_for_sub_portfolios`）；聚类 `sysquant/estimators/clustering_correlations.py::cluster_correlation_matrix`（scipy complete-linkage，cutoff 取恰分成 N/2 簇）。
- **做了什么**：只用相关矩阵（不用收益均值）的权重方案：递归两两聚类 → 每组 0.5 权重 × 组内分散乘数 → 合并。比 topK 等权强：**高相关股票对（行业抱团）自动降配**，组合不会在 3 个同涨同跌的行业上重复押注。
- **数学**：递归 `w_asset = 0.5 × DM_group × w_inside_asset`；叶子组内等权 1/n；组 DM 见 C4。最后按 SR 差异微调（C9）。
- **QLab 落地**：500 票全相关矩阵不可靠 → **降到申万一级行业层**（~30 个）或「行业×规模」格子：行业内 vol 倒数加权（C1），行业间等权×IDM（C4），即行业中性风险平价。这是 C1 在 A 股实践中的必改项，两者合成一条流水线。

### C4. 分散乘数 DM/IDM：组合"合法杠杆"的唯一定价

- **位置**：`sysquant/estimators/diversification_multipliers.py`（`diversification_mult_single_period`、`diversification_multiplier_from_list`）；消费点 `systems/forecast_combine.py::get_forecast_diversification_multiplier_estimated`、`systems/portfolio.py::get_estimated_instrument_diversification_multiplier`。
- **做了什么**：组合分散后真实风险低于"名义权重和"暗示的风险，用 DM 把仓位整体放大回目标风险。**等权组合隐含 DM=1 = 风险永远低于目标**，等于把分散红利白白扔掉。
- **数学**：
  - 单期：`DM = min(1/√(w'Cw), 2.5)`（w 归一化权重、C 相关矩阵）；等权闭式 **`DM = 1/√(ρ̄ + (1−ρ̄)/N)`**，ρ̄ 为平均相关
  - 与风险预算关系：**组合风险 = Σ_abs / DM**（Σ_abs = Σ|wᵢ|σᵢ）
  - 时序版：每估计段算一个 → ffill 到日频 → `ewm(span=125)` 平滑防跳变
- **QLab 落地**：契约行和≤1 本身就是杠杆约束，所以 DM 融进行和：`w = w_raw × DM`，若 Σw>1 则整体缩回 ≤1（等效 DM 被 1/Σw 截断，上限自动成立）。行业层 N≈30 时 DM≈1.3~1.8，能把组合风险利用率显著提高。

### C5. 持仓缓冲（buffer）：把换手率和成本砍半的交易纪律

- **位置**：`systems/buffering.py`（`calculate_buffers`、`get_forecast_method_buffer`、`get_position_method_buffer`、`apply_buffers_to_position`）+ `systems/accounts/account_buffering_subsystem.py::apply_buffer` / `apply_buffer_single_period`。
- **做了什么**：给目标仓位上下各一个带，**当前仓位落在 [bot, top] 内就不交易**；突破上沿才动（默认 `trade_to_edge=True` 只动到边界）。现有示例的 buffer 是固定 0.005；pysystemtrade 的 buffer 是**按平均仓位比例自适应**（默认 `buffer_size=0.10`），随 vol targeting 自动缩放。原理：把信号噪声带内的"噪声交易"（无预期收益、纯付成本）滤掉。
- **数学**：`buffer = buffer_size × |平均仓位|`（forecast 法：平均仓位 = vol_scalar×instr_weight×idm）；`top = pos+buffer, bot = pos−buffer`；逐期递推：
  ```
  if last > top:   new = top（或 optimal）
  elif last < bot: new = bot（或 optimal）
  else:            new = last        # 不动
  ```
  NaN 一律保持原仓位；roundpositions 时先 round 再套规则。
- **QLab 落地（直接移植，~20 行）**：把示例的固定 buffer 换成 `buffer = buffer_size × target_w`（topK 等权时 = buffer_size/k）。**这是 P8 成本模型（印花税+佣金+滑点）下最省钱的单点改动**，且完全在现有契约内（portfolio.pkl 的权重序列本身做递推，未持仓行省略前先算全矩阵）。

### C6. forecast 缩放：让「信号强度」进入仓位，而不是只看排序

- **位置**：`systems/forecast_scale_cap.py`（`ForecastScaleCap.get_capped_forecast`、`get_forecast_scalar`、`_get_forecast_scalar_estimated`）+ `sysquant/estimators/forecast_scalar.py::forecast_scalar`。
- **做了什么**：把不同模型的分数统一缩放到「平均绝对 forecast = 10」再 clip ±20。topK 等权只看排名不看强度；缩放让强度直接乘进仓位（C1 中 forecast 是权重分子），**同样排名、强度差 2 倍的信号，仓位差 2 倍**。
- **数学**：`scalar = target_abs_forecast(10) / avg_abs_forecast`，其中 avg 先做**截面中位数**（`cs_forecasts.ffill().abs().median(axis=1)`）再滚动均值（`rolling(window, min_periods=500).mean()`），0 值置 NaN 剔除；`scaled = raw × scalar`，`capped = clip(scaled, −20, 20)`。
- **QLab 落地**：`score_scaled = score / mean(|score|_train) × 10`，clip ±20（阈值可调）；标量只在训练段拟合、测试段应用（防泄漏）。执行器内 ~5 行。

### C7. SR 成本天花板：成本视角的「信号可交易性」过滤

- **位置**：`systems/forecast_combine.py`（`cheap_trading_rules`、`_cheap_trading_rules_generic`、`get_SR_cost_for_instrument_forecast`、`_remove_expensive_rules_from_weights`）+ `systems/accounts/account_costs.py`（`get_SR_cost_for_instrument_forecast`、`get_SR_cost_per_trade_for_instrument`、`forecast_turnover`）+ `sysobjects/instruments.py::instrumentCosts.calculate_sr_cost`。
- **做了什么**：把每个信号源/规则的成本折算成「年化 Sharpe 损耗」，超过上限（`ceiling_cost_SR`，如 0.25）就**剔除出权重**。A 股印花税+佣金下，高换手信号的成本常常吃掉全部 alpha——这个过滤器在信号进入组合前就把它掐掉。QLab 的 cost_drag 是事后指标，这是**事前**预算。
- **数学（SR cost 计算链原文）**：
  - 每笔成本（工具货币）= slippage + max(per_block, per_trade, percentage×成交额)（`instrumentCosts.calculate_cost_instrument_currency`）
  - **`SR_cost_per_trade = cost_ccy / (ann_stdev_price_units × block_multiplier)`**
  - **`annual_SR_cost = forecast_turnover × SR_cost_per_trade`**（+ 展期成本，A 股无）
  - turnover 口径：`annual_turnover = mean(|Δ(forecast/10)|) × 256`（见 C10）
- **QLab 落地**：A 股每股往返成本率 ≈ 佣金万2.5×2 + 印花税千1 + 滑点1bp ≈ 0.16%；`SR_cost_per_trade ≈ 0.16% / (年化波动率)`（个股 30% 波动 → 0.0053）。执行器内：估算每个候选信号的年换手 × 每笔 SR 成本 → 预计年化 SR 损耗 > 0.25 的**信号源/高换手个股**剔除或降权。可先做信号级（多模型集成时），个股级二期。

### C8. 相关矩阵清洗与收缩：C2/C3/C4 的地基

- **位置**：`sysquant/estimators/correlations.py`（`correlationEstimate.clean_corr_matrix_given_data`、`clean_correlations`、`shrink_to_average`、`modify_correlation`）+ `sysquant/estimators/exponential_correlation.py::exponentialCorrelation`（ewm span=250、min_periods=20、offdiag=0.99）。
- **做了什么**：EWMA 相关矩阵在缺失数据/短历史下非正定，直接喂 √(w'Σw) 会 NaN。清洗两步：**缺失对的 offdiag 填 0.99**（保守高相关，宁可高估风险）+ **shrink_to_average**（默认收缩 0.5 向平均相关）。A 股 500 票相关矩阵必然病态，这是 C2/C3/C4 能否跑起来的前置。
- **QLab 落地**：直接移植 `clean_correlations` + `shrink_to_average` 两个函数（~30 行），或降维到行业层（C3）后矩阵天然干净。

### C9. SR-adjustment：把权重往「统计上可信」的方向拉（防过拟合）

- **位置**：`sysquant/optimisation/SR_adjustment.py`（`adjust_weights_for_SR`、`mini_bootstrap_ratio_given_SR_diff`、`weights_given_SR_diff`）；消费点 handcraft（`adjust_weights_for_SR_on_handcrafted_portfolio`）。
- **做了什么**：等权/风险平价完全不利用历史表现，但满仓押历史冠军又会过拟合。SR-adjustment 用参数化 bootstrap 决定「SR 差异 → 权重偏离 1/n 多少」：**数据越短、相关性越高，越不敢偏离等权**。QLab 测试段只有 ~20 个月度收益点（P8 纪律明确估计误差极大），正是这个组件的适用场景。
- **数学**：对资产 i，`relative_SR_i = SR_i − mean(SR)`；均值差估计标准误 `ω_diff = √(2ω²(1−ρ̄))`，`ω = σ/√years`；对置信点 p∈(0.2,0.8) 用正态 ppf(p) 得到"有把握的均值差"，解 2 资产 max-SR，对 p 平均得 ratio；`w'_i = w_i × ratio_i` 再归一。
- **QLab 落地**：训练段按 IC 估计每票 SR（或行业 SR），`years_of_data` = 训练段真实年数，`avg_correlation` = 行业平均相关。**适用于行业层权重**（C3 之上），个股层 SR 噪声太大建议不做。

### C10. 换手率统一口径：一切持仓策略的公共标尺

- **位置**：`syscore/pandas/strategy_functions.py::turnover`；消费点 `systems/accounts/account_buffering_subsystem.py::subsystem_turnover`、`account_buffering_system.py::instrument_turnover`、`account_costs.py::_forecast_turnover_for_individual_instrument`。
- **做了什么**：turnover 定义为**头寸序列相对平均仓位的平均绝对变化率**（年化）：`turnover = mean(|Δ(x/y)|) × 256`，y 为平均仓位（先 `ewm(250).mean()` 平滑，注释明说"不加平滑则恒定风险时 turnover 为 0"）。与仓位规模无关、可与信号强度直接对比，是 C7 成本预算和 P&L 记账的基础。
- **QLab 落地**：测试器已有 turnover 指标（金额口径），建议**新增此口径**：`mean(|Δ(w/w_avg)|) × 252`，两者配合看「是调仓频繁（次数多）还是调仓幅度大（单次量大）」——这是定位 cost_drag 来源的第一刀。

### C11. 权重平滑 + 归一化：消除排名边缘的抖动脉冲

- **位置**：`systems/forecast_combine.py::get_forecast_weights`（`forecast_weight_ewma_span`）、`systems/portfolio.py::get_instrument_weights`（`instrument_weight_ewma_span`）；`syscore/pandas/strategy_functions.py::weights_sum_to_one`。
- **做了什么**：估计出的权重先 ewm 平滑再行归一（除零行特殊处理：全零行保持零，防 0/0）。topK 等权的权重天然阶跃（进出 topK = 0↔1/k），平滑把排名边缘抖动变成缓变，与 C5 配合可再砍一截换手。
- **QLab 落地**：`w_smooth_t = ewm(w_raw, span=span).mean()` 后 `w = w / Σw`（`weights_sum_to_one` 原样抄）。**A 股 T+1 注意方向**：当日权重次日生效，用 t−1 的平滑值。span 建议 20~60。

### C12. Capital multiplier：回撤后按实际资金缩仓（个人资金保命）

- **位置**：`systems/accounts/account_with_multiplier.py`（`capital_multiplier`、`get_actual_capital`）+ `syscore/capital.py`（`fixed_capital`、`full_compounding`、`half_compounding`）。
- **做了什么**：用已实现 P&L 缩放名义资本：`full_compounding: multiplier = cumprod(1 + r)`；`half_compounding: multiplier = multiplier×(1+r)` 且 `min(multiplier, 1)`（只缩不放）；`get_actual_capital` 里 `.shift(1)` **用昨日资本计今日 P&L 防前视**。个人资金量小，回撤后仍按原名义仓位交易 = 破产螺旋风险。
- **QLab 落地**：`w_t = w_raw_t × min(cumprod(1+r), 1)`，r 用已实现组合日收益（测试器 backtest 曲线可得）；`.shift(1)` 防前视必须保留。~5 行。

### 备选 C13（5 行小件）：波动率衰减 vol attenuation

- **位置**：`systems/provided/attenuate_vol/vol_attenuation_forecast_scale_cap.py`。
- `multiplier = 2 − 1.5 × quantile(vol / 10年vol均值)`，`ewm(span=10)` 平滑：**高波动期信号自动衰减 50%**。A 股 2015/2018 式极端波动期直接砍信号。价值与 C2 部分重叠（C2 已覆盖组合风险），实现极简，可作 bonus。

---

## 3. 不值得抄的（期货多市场特有 / 过度设计）

1. **Roll calendar / 合约滚动全套**（`sysinit/futures/build_roll_calendars.py`、`sysobjects/rolls.py`、`multiple_prices` 远期/近期/调整价三价合一、`sysinit/futures/*roll*` 一堆脚本）。A 股无到期合约，前复权价即可。抄了纯亏。
2. **Carry 规则**（`systems/provided/rules/carry.py`、`systems/rawdata.py::raw_carry`：期货隐含展期收益率）。A 股没有"展期收益"概念；股息率≠carry（红利税、除权除息处理完全不同），硬套必错。
3. **期货版截面动量**（`systems/provided/rules/rel_mom.py::relative_momentum`、rob_system 的 assettrend/normmom 系列——依赖"资产类别归一化价格"基础设施）。A 股截面动量 = rank/IC 层面已有，绕一大圈不值。
4. **IB（Interactive Brokers）对接**（`sysinit/futures/get_prices_and_contract_details_from_ib.py`、`systems/provided/scalper/broker.py`、`sysexecution/` 订单栈）。个人 A 股走券商 API，架构完全不同。
5. **动态优化（greedy 边际调仓）**（`systems/provided/dynamic_small_system_optimise/`：`optimisedPositions`、`objectiveFunctionForGreedy`、`greedy_algo_across_integer_values`、tracking-error-vs-成本目标）。为期货整数合约设计；A 股按权重即可，边际优化的复杂度远超收益。
6. **MongoDB/Arctic 数据栈**（`sysinit/` 大量 `*_db_*` 脚本、`sysdata/mongodb/`）。QLab 已有 parquet 缓存 + 修订号体系。
7. **小时级撮合/限价模拟**（`systems/provided/example/hourly_with_order_simulation.py`、`order_simulator/hourly_*`）。A 股 T+1 日频为主，小时撮合无意义；QLab 固定测试器"收盘价成交次日生效"已够。
8. **forecast 非线性映射**（`systems/forecast_mapping.py`：threshold→0、cap→a×20 的分段映射，为"小账户最少 1 手"设计）。A 股最小 100 股远小于个人资金粒度约束，映射复杂度不值。
9. **P&L 归因全塔 + 统计全家桶**（`systems/accounts/curves/` 的 account_curve_analysis、stats_dict 20+ 指标）。QLab 测试器指标已覆盖 Sharpe/Sortino/Calmar/MDD 等，只需对口径，不必抄实现。

---

## 4. 只抄 3 件事

**① 波动率目标 + 行业层风险平价 + 分散乘数（C1+C3+C4 合成一条流水线）**
score → 非负化缩放（C6）→ vol 倒数加权 → 行业等权×IDM → 行和≤1 → ewm 平滑（C11）。这是把「topK 等权」升级为「风险预算制」的一步到位方案，直接决定组合的风险-收益结构；A 股日收益数据完全够算，全部可在执行器契约内落地。

**② Risk overlay 组合风险闸门（C2，含 C8 清洗）**
`mult = min(限额/风险读数, 1)` 全局缩仓。它是唯一在**组合层面**兜底的组件：个股权重再合理，相关性聚集时组合风险照样爆，A 股牛熊切换剧烈，这是保命件。实现 ~40 行 + C8 清洗 ~30 行。

**③ 持仓缓冲（C5）**
按目标仓位比例的自适应不交易区（`apply_buffer_single_period` 原样移植）。与 P8 成本模型（印花税千1+佣金万2.5×2+滑点1bp）直接挂钩，是**换手率与成本拖累的最大单一杠杆**，实现 ~20 行，换手率和 cost_drag 立竿见影。

**优先级理由**：①决定"赚什么钱"（风险预算 vs 名义等权）；②决定"活不活得下来"（组合风险兜底，A 股波动极端）；③决定"赚到的是不是真的"（成本侵蚀，印花税下尤其狠）。三者互相独立、可各自单独验证（QLab 固定测试器 portfolio/backtest/attribution 四族指标直接对比），且都不动冻结管线——全部落在执行器内。

---

### 附：移植顺序建议（执行器内，按依赖排）

```
v0.1（~1 天）：C6 分数缩放 + C1 vol 倒数加权 + C11 平滑     → 替换 topK 等权
v0.2（~1 天）：C5 buffer（自适应）+ C10 换手口径自检        → 看 turnover/cost_drag 下降
v0.3（~2 天）：C8 清洗 + C2 risk overlay                    → 组合风险兜底
v0.4（~2 天）：C3 行业层 + C4 DM + C9 SR 调整               → 去相关、补回分散红利
v0.5（可选）：C7 SR 成本预算过滤 + C12 capital multiplier
```
每个版本单独 submit 一个 spec 对照测试器指标，不混批（P8 统计纪律：expectation 预注册 + n_variants 一起看）。
