# pysystemtrade 可借鉴点报告（面向 QLab 重构）

> 分析对象：`/Users/lucas/projects/quant_refs/pysystemtrade/`（GPLv3，Rob Carver《Systematic Trading》
> 开源实现；sparse checkout：sysinit/ + systems/ + private/，不含 data/ 大数据文件）
> 阅读范围：systems/{forecast_scale_cap, forecast_combine, positionsizing, portfolio, risk,
> risk_overlay, buffering, trading_rules, forecast_mapping}.py、systems/accounts/account_costs.py、
> systems/provided/ 示例系统。
> **本报告是 cleanroom spec**（第一阶段产出：读码者提取公式/接口/测试向量）。
> 实现阶段只读本 spec、不翻源码，独立重写（见 §6）。
> 全部结论标注 `文件:行号` 与类/函数名。
> **姊妹篇**：`knowledge/notes/pysystemtrade_borrow_report.md`（更细的 12 组件版，
> 含 sysquant 层：robust_vol_calc、相关矩阵清洗、SR-adjustment、capital multiplier 等；
> 本文件聚焦 8 个核心组件 + 公式核实）。

---

## 1. 架构地图（"系统"分层）

| 层 | 模块 | 职责（一句话） |
|---|---|---|
| 数据 | `systems/rawdata.py` + sysinit 数据机制 | 原始行情 → 标准化（波动率、动量等 raw 数据） |
| 交易规则 | `systems/trading_rules.py` | 把 raw 数据变成 raw forecast（一个规则 = 一个信号） |
| 缩放封顶 | `systems/forecast_scale_cap.py` `ForecastScaleCap` | 把每个规则的 forecast 缩放到可比量纲（默认均值绝对=10），封顶 ±20 |
| 信号组合 | `systems/forecast_combine.py` `ForecastCombine` | 多规则加权和 + 分散化乘数（FDM），生成 combined forecast |
| 仓位 | `systems/positionsizing.py` `PositionSizing` | combined forecast → 子系统仓位（波动率目标定总风险） |
| 组合 | `systems/portfolio.py` `Portfolio` | 多资产组合：instrument weights（优化器/等权）× 组合波动率目标 |
| 风险 | `systems/risk.py` `Risk` + `systems/risk_overlay.py` | 组合风险核算；风险预算缩放（只降不加） |
| 记账 | `systems/accounts/*.py` | 逐规则 PnL / 换手 / 成本（SR 口径）归因，一切可审计 |

Carver 的"系统"是**数据流流水线**：每层吃上一层的输出、产出下一层的输入，
层与层之间通过 `@output()/@diagnostic()` 缓存解耦，配置驱动（`Config` 对象）。

### 与 QLab 契约的映射

pysystemtrade 把"信号→仓位→组合→风控"分成 4 层；QLab 契约里这些**全部压缩在
executor 内部**（pred.pkl 之前是模型层，portfolio.pkl 是最终持仓）。所以：
- QLab 的"信号层"（executor 内模型出分）对应 pysystemtrade 的 trading rules；
- QLab 缺失的正是 forecast scale/cap → combine → position sizing → risk 这 4 层；
- 这 4 层全部可以放进 executor 自由区，不碰冻结管线。

---

## 2. 最有价值的可借鉴组件（按价值排序）

### #1 forecast scale/cap：把任意量纲的分数变成"可比 forecast"

- **出处**：`systems/forecast_scale_cap.py:76-106` `get_scaled_forecast`、
  `:167-235` `_get_forecast_scalar_estimated`、`:366-454` `_get_forecast_scalar_fixed`
- **它做了什么**：每个规则的 raw forecast 乘一个 scalar，使历史平均绝对 forecast =
  `target_abs_forecast`（默认 10）；再 `clip(-forecast_cap, +forecast_cap)`（默认 ±20）。
  scalar 可固定（config）或估计（`target_abs_forecast / 历史平均绝对 forecast`）。
- **为什么比 topK 等权强**：ML 分数量纲任意（LGB 的 score 可能是 -1~1 或任意实数），
  topK 只消费"排名"，把信号强度扔掉了。scale/cap 之后：
  (a) 分数跨模型/跨 horizon 可比；(b) position sizing 可以用分数绝对值配仓；
  (c) ±20 封顶防止极端值主导。
- **QLab 落地**：训练/验证期估计 `scalar = 10 / mean(|score|)`，测试期
  `scaled = (score × scalar).clip(-20, 20)`。一行代码，立即让"信号强度"进入仓位决策。

### #2 vol-target position sizing：波动率目标定总风险

- **出处**：`systems/positionsizing.py:86-126` `get_subsystem_position`、
  `:427-480` `get_vol_target_dict` / `get_daily_cash_vol_target`、
  `:206-250` `get_instrument_value_vol`
- **核心公式**（已逐行核实）：
  ```
  subsystem_position = vol_scalar × forecast / avg_abs_forecast
  vol_scalar         = daily_cash_vol_target / instrument_value_vol
  daily_cash_vol_target = notional_capital × percentage_vol_target / √252
  ```
  即：目标仓位 = 波动率目标(年化，默认 20%) 折算成每日现金波动，除以该资产的
  波动率（单位价值波动），再按 forecast 强度（相对 10 的倍数）缩放。
- **为什么比 topK 等权强**：等权把"高风险股票"和"低风险股票"一视同仁；
  vol targeting 让每只股票的**风险贡献**相等（低波动股可以配更多仓）。
  组合层面，总风险被钉在目标上——牛市不虚高、熊市不爆仓。
- **QLab 落地**（A 股个股池）：
  ```python
  vol_i = prices_close.pct_change().rolling(60).std() * sqrt(252)   # 个股年化波动
  w_raw_i = scaled_score_i / vol_i                                  # 波动率倒数配仓
  w = w_raw / w_raw.sum() × invest_frac                             # 归一到预算
  ```
  组合年化波动 ≈ vol_target 由"波动率倒数 × 截面归一"近似实现（个股级 vol targeting，
  组合级可再加 risk overlay 校准）。σ 缺失（新股/停牌）时置截面中位数。

### #3 forecast combine + 分散化乘数（FDM）

- **出处**：`systems/forecast_combine.py:1008-1024` `get_forecast_diversification_multiplier`、
  `:1100-1140` `get_forecast_diversification_multiplier_estimated`、
  `:119-130`（combined forecast = 加权和 × FDM）
- **核心公式**：`FDM = Σw_i / √(Σw_i²)`（零相关时）；有相关时用相关矩阵估计
  （sysquant.estimators.diversification_multipliers），带 `dm_max` 上限。
- **为什么强**：多个低相关信号组合后，组合波动 < 各信号波动之和——FDM 把这个
  "分散化红利"换成可加的总仓位。我们现在的 multi-seed ensemble 只是 rank_mean
  等权，既没按信号质量加权，也没拿到分散化乘数。
- **QLab 落地**：多模型/多 horizon 集成时，`comb = Σ w_i × scaled_i`，然后
  `total_position ×= FDM`（FDM 从训练期信号相关矩阵估计，`dm_max=1.5` 保守上限）。
  A 股单市场选股：不同模型（LGB/XGB/MLP）或不同 horizon（5/10/20 日）之间做 combine。

### #4 仓位/组合 buffer（降换手的系统做法）

- **出处**：`systems/buffering.py:35-90` `calculate_buffers`（buffer_method:
  "forecast" | "position" | "none"）、`:90-139` `get_forecast_method_buffer` /
  `get_position_method_buffer`（position 法 = 目标仓位 ±buffer_size，默认 10%）
- **它做了什么**：目标仓位算出来后，套一个"死区"——只要当前仓位在
  `[target - buffer, target + buffer]` 内就不交易。position 法是按仓位比例，
  forecast 法是按预测强度。
- **为什么比现执行器强**：`_example_topk_portfolio` 的 buffer 是
  `|w_new - w_cur| > 0.005`——一个**绝对值**阈值，与仓位量纲无关，语义模糊；
  Carver 的 buffer 是**相对目标仓位的百分比死区**，与波动率目标联动，且可以
  按"预测变化是否超过成本门槛"（SR 口径）动态化。
- **QLab 落地**：`band = buffer_pct × target_w`（目标权重 × 10%），
  `|w_new - w_cur| > band` 才交易；配合 rank buffer（跌出 topK 集合 < r 个不调仓）。

### #5 成本→SR 拖累换算（"值不值得交易"的判据）

- **出处**：`systems/accounts/account_costs.py:14-41` `get_SR_cost_for_instrument_forecast`、
  `:163-190` `get_SR_cost_given_turnover` / `get_SR_trading_cost_only_given_turnover`
- **它做了什么**：把交易成本换算成**每单位换手吃掉的 Sharpe**（成本/波动率口径），
  与信号带来的预期 SR 直接比较——`成本 SR > 信号 SR` 时这个信号不可交易。
- **为什么强**：我们 metrics 已有 `cost_drag_ann` / `cost_alpha_ratio`（事后统计），
  但**执行器做决策时**不知道"这一笔交易值不值"。SR 换算让"是否交易"变成
  `Δw × 单边成本(SR) vs 预期 alpha(SR)` 的即时比较——这正是 playbook 红线 #2
  （cost drag 接近 gross alpha 必须降级）的前置判据。
- **QLab 落地**：执行器内 `c_sr = (commission + slippage + stamp×sell_side) / vol`，
  只对 `|Δw| × c_sr < 预期信号 SR × 系数` 的交易下单；成本常数从 DEFAULT_COSTS 读
  （纪律 B：与固定测试器同源）。

### #6 risk overlay：风险预算只降不加

- **出处**：`systems/risk_overlay.py:4-76` `get_risk_multiplier` /
  `multiplier_given_series_and_limit`；`systems/risk.py:63-79` `_get_portfolio_risk_given_positions`
- **它做了什么**：实时核算组合当前风险（波动率），超预算时**整体缩放仓位**，
  乘数 ∈ (0,1]（只降不加）。市场高波动时自动降杠杆。
- **QLab 落地**：`multiplier = min(1, vol_target / current_portfolio_vol)`，
  `w ×= multiplier`。A 股个人资金组合的"总风险预算"显式化。

### #7 forecast_mapping：非线性压缩 forecast

- **出处**：`systems/forecast_mapping.py`（tanh 等 S 形映射，把 forecast 压缩到 [-1,1] 附近）
- **QLab 落地**：可选；对极端分数做 tanh 压缩，减少 outliers 影响（与 #1 的 ±20 cap 二选一或叠加）。

### #8 组合权重层（多资产加权思想）

- **出处**：`systems/portfolio.py:422+` `get_instrument_weights`（config 手配或优化器）、
  `:1290+` `get_percentage_vol_target`
- **QLab 落地**：A 股选股场景的"instrument weights"≈ 个股截面权重——本报告 #2 的
  波动率倒数配仓就是它的个股版。多资产/多策略层面暂不需要。

---

## 3. 不值得抄的（抄了会亏）

| pysystemtrade 组件 | 为什么不抄 |
|---|---|
| **多期货市场基础设施**：roll calendar、carry/期限结构、跨期价差、IB 实盘对接、外汇折算 | 我们是 A 股个股单市场，全部是死代码 |
| **@output()/@diagnostic() 缓存系统**（`systems/stage.py`、`system_cache.py`） | QLab 有 parquet 缓存 + MLflow 台账，不需要类内缓存框架 |
| **Config 对象三层继承机制**（sysinit/configtools） | QLab spec.params 已覆盖配置透传；抄这个 = 引入新框架 |
| **逐规则记账体系**（account_trading_rules / account_forecast 全系列） | QLab 的 attribution 四层已够用；逐规则 PnL 是期货多规则场景 |
| **估计权重/收缩估计的复杂路径**（`_get_forecast_scalar_estimated` 的滚动估计、相关矩阵逐日重估） | 个人研究先用手动/训练期固定估计，滚动估计是优化项不是必需品 |
| **Long-only 的期货实现细节**（`_apply_long_only_constraint_to_position` 的期货合约取整） | 我们契约已强制 w≥0，直接 clip 即可 |

---

## 4. 如果只抄 3 件事

**① forecast scale/cap（#1）**：一行代码让信号强度进入仓位决策，是所有仓位改革的基石。
**② vol-target position sizing（#2）**：替换"等权 topK"的仓位分配——这是"组合层系统性设计"
（playbook Rob Carver 节）的核心，直接解决"我们最缺的不是更复杂模型，而是组合与风险层的
系统化设计"。
**③ 成本→SR 判据（#5）**：把"值不值得交易"变成可计算的门槛，与固定测试器成本口径同源，
是降换手（P1）和净超额主判据（P0#3）的执行器侧闭环。

**为什么是这三件**：它们恰好覆盖"信号→仓位→交易决策"整条链上我们最弱的三个环节，
全部落在 executor 自由区、不需要新数据、实现量小（每题 <100 行）。FDM（#3）和
buffer 系统化（#4）是这三件之上的增强，按需再上。

---

## 5. 落地优先级（与 refactor_dev_plan.md §2.2 对应）

1. **P0（一个执行器迭代）**：#1 scale/cap + #2 vol-target sizing → 新执行器
   `executors/systems_topk/` 的第一版（替换等权分配）。
2. **P1**：#5 成本-SR 判据（成本感知交易）+ #4 position buffer（换手控制）。
3. **P2**：#3 FDM 多模型 combine（与多 seed/horizon 集成一起做）。
4. **P3**：#6 risk overlay（组合波动回溯校准）、#7 forecast_mapping（可选）。

---

## 6. 许可证与合规（cleanroom）

- pysystemtrade = **GPLv3**，本项目 = **MIT**。本报告是 **cleanroom 规格说明（spec）**：
  只含不受版权保护的"公式 / 接口签名 / 参数默认值 / 测试向量"，不含源码的"表达"
  （代码结构、命名、注释、逐行逻辑）。
- **实现纪律**：执行器实现（`executors/systems_topk/impl/`）只读本 spec，**不 open
  `quant_refs/pysystemtrade/` 源码**，独立重写——产出的代码是独立创作，非 GPL 派生，
  项目保持 MIT。
- 合规留痕见 `knowledge/notes/cleanroom_log.md`。
