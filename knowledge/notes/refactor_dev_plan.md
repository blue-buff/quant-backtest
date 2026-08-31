# QLab 重构开发计划：cleanroom 隔离实现（2026-08-31 v3）

> 输入：`/Users/lucas/Documents/Codex/2026-08-27/ru-gu/outputs/quant-metrics-and-x-expert-playbook.md`
> （指标全景 + 8 位 X 专家经验）＋ 两个 GitHub 参考项目的源码深读：
> - `quant_refs/qstrader/`（MIT，**直接抄代码**，无许可证风险）→ `docs/qstrader_borrow_report.md`
> - `quant_refs/pysystemtrade/`（GPLv3，**cleanroom 隔离实现**，不逐字复制）→ 两份报告即 spec
> 目标：**激进重构——成熟项目验证过的做法直接替换掉我们做得烂的部分**。
> 落地方式按许可证分流：MIT 直接 vendor 源码；GPLv3 走 **cleanroom**（读码者已产出
> spec 文档，实现者只读 spec、不碰源码，独立重写，规避传染）。
> 总原则不变：指标口径冻结在固定测试器（pipeline.metrics），
> 执行器自由区（executors/）放策略/组合/风控；管线改动需用户批准（P7 冻结线）。

---

## 0. 差距总览（我们烂在哪，成熟项目好在哪）

| 维度 | 我们现状（烂） | 成熟项目做法（好） | 替换来源 |
|---|---|---|---|
| 信号→持仓翻译层 | 152 行 topK 等权硬编码，手工 current dict 增量修补 | 全量目标权重向量+自动清零；sizer 归一化/现金缓冲/成本预扣 | qstrader portcon/pcm.py + order_sizer |
| 换手/成本 | 每日满仓调仓，buffer 是权重差阈值，无 rank buffer | 调仓日程参数化（daily/weekly/monthly 一行配置）；diff+零单抑制+band | qstrader system/rebalance + pcm |
| 仓位管理 | 等权，无波动率目标、无按信号强度配仓 | forecast scale/cap → combine → vol-target position sizing → portfolio | pysystemtrade systems/ |
| 风控层 | 无（无单票上限/暴露约束/风险预算） | RiskModel 约束钩子；risk overlay 风险预算 | qstrader risk_model + pysystemtrade risk |
| 执行真实性 | 价格缓存只有 close；无涨跌停/停牌/最小佣金/T+1 开盘成交 | 成交约束（涨跌停买不进卖不出、停牌）、费用模型分层 | 数据升级（A 阶段）+ qstrader broker 思想 |
| 稳健性验证 | 无参数平台/walk-forward/随机基准；PSR 有，DSR/PBO/CPCV 无 | 参数平台检验（Ernest Chan）；随机历史生存检验；CPCV/PBO | playbook P2/P3 + 专家经验 |
| 审读闭环 | review.py 只有 6 项确定性检查 | Sharpe 红旗、因果性 5 问、因子机制 4 问 | playbook 红线规则 + Factor Mirage |
| 长期运行 | 无 paper trading | 一条部署的平庸算法 > 10 个漂亮回测 | Goshawk #11 |

---

## 1. 数据升级（A 阶段：一切执行层工作的前置，最小成本）

**背景事实（已核实）**：底层 CSV 数据源有完整 `date,open,high,low,close,volume,amount,vwap,turn,factor`
（`qlib_data_src*/sh600000.csv` 实测），但价格缓存 `_build_prices`（pipeline/data.py:395）只读
`usecols=["date","close"]`，丢了 8 列。**不需要重新拉数据**，只需扩展缓存构建 + invalidate。

**关键设计点**：
1. **原始价与复权价分开存**：CSV 的 OHLC 是未复权原始价、close 经 factor 后复权（hfq）。
   涨跌停判定、T+1 开盘成交用**原始价**；收益计算继续用 **hfq close**。缓存列：
   `open_raw, high_raw, low_raw, close_raw, close(hfq), volume, amount, vwap, turn, factor`。
2. **缓存 key 加列版本**：`price_key`（data.py:389）的 canon 目前只有 pool/window——
   加列后必须把列清单纳入 canon（或 bump 版本号），否则旧缓存不重建、静默缺列。
3. **涨跌停/停牌标记表**：由原始价推导（主板 ±10%、创业板/科创板 ±20%——按代码前缀
   SH600/601/603/605=10%，SH688=20%，SZ000/001/002/003=10%，SZ300=20%）；
   停牌 = 当日 volume=0（或无行情行）。输出 `limit_up/limit_down/suspended` 布尔表。
4. **改动范围**：pipeline/data.py `_build_prices`/`price_key`/`price_ensure` +
   `pipeline/metrics.py` 加 `execution_lag` 的 open 档（lag_open）。
   ⚠️ **碰冻结线，需用户批准**（净增行数少：约 +40 行 + 测试）。
5. **验收**：`tests/test_prices.py` 扩展（列齐全、原始价=CSV 原文、涨跌停推导抽查 1 只
   涨停股）；价格缓存 invalidate 后自动重建；`execution_lag` 出现 `lag_open` 三档对照
   （T 日收盘 / T+1 收盘 / T+1 开盘）。

**解锁的能力**（playbook P0#1 执行延迟对照完整版；执行层 ⚠️ 指标）：
T+1 开盘成交、涨跌停不可成交、停牌不可成交、流动性参与率（成交额×参与率）、容量估算。

---

## 2. 组合构建执行器重写（B 阶段：执行器自由区，vendor + cleanroom）

> **分流策略**：MIT 直接 copy 源码（零风险）；GPLv3 走 cleanroom——读码者（已完成）
> 产出 spec（两份 borrow report，含公式/接口/测试向量），实现者**只读 spec、禁止翻
> quant_refs/pysystemtrade 源码**，独立重写。核心算法逻辑照 spec 实现（公式不受版权
> 保护），代码表达独立创作，规避 GPL 传染。

### 2.1 qstrader 直接移植清单（MIT，逐字 copy 代码）

按 `docs/qstrader_borrow_report.md` 的组件优先级，新执行器 `executors/systems_topk/`
（在 `_example_topk_portfolio` 基础上重构）逐步落地：

| 组件 | 移植内容 | 出处 | 验收 |
|---|---|---|---|
| B1 全量目标权重向量+自动清零 | `target = Series(0.0, index=universe∪持仓)`，picks 覆盖，掉出自动归零；断言 sum≤1、无 NaN | qstrader portcon/pcm.py:58-120 | 停牌/成分调整日持仓状态不失真 |
| B2 调仓日程参数化 | `params.rebalance: daily\|weekly\|monthly`；非调仓日输出与昨日相同权重行（纪律 A） | qstrader system/rebalance/*.py | turnover 随频率单调下降 |
| B3 sizer 下沉 | `to_feasible(target, budget=0.95)`：clip→归一到预算→省略零行；成本感知决策与 DEFAULT_COSTS 同源（纪律 B） | qstrader order_sizer/dollar_weighted.py | 出口不变量 sum≤1、each∈[0,1] |
| B4 warmup 门控 | `params.warmup_days`：前 N 天不输出行（tester 自动跳过窗口） | qstrader momentum_taa.py:145-147 | 开局不产生首日满额换手 |
| B5 对照模式 | `params.mode: strategy\|baseline`，baseline 输出纯等权 topK → 分离策略增益 vs 组合构造增益 | qstrader static_backtest.py:83-124 | 同 exp_id 双 run 可对比 |

### 2.2 pysystemtrade cleanroom spec 清单（GPLv3，独立实现，不逐字复制）

> 两份报告即 **cleanroom spec**：`docs/pysystemtrade_borrow_report.md`（精炼 8 组件）＋
> `knowledge/notes/pysystemtrade_borrow_report.md`（12 组件详版，含 sysquant 层）。
> **实现纪律（cleanroom 铁律）**：写执行器代码时只引用 spec 里的公式/接口/测试向量，
> **不 open `quant_refs/pysystemtrade/` 任何源文件**；代码表达（命名/结构/注释）独立创作。

**核心思想链**：raw forecast（模型分数）→ scale/cap（归一化到可比量纲）→
combine（多规则加权 + 分散化乘数）→ position sizing（波动率目标定总风险）→
portfolio（组合权重）→ risk overlay（风险预算缩放）。

| 组件 | 算法要点（已核实） | 我们怎么落地（执行器自由区） |
|---|---|---|
| B6 forecast scale/cap | `scaled = raw × scalar`，`scalar = target_abs_forecast / mean_abs(raw)`（estimated，默认 target=10）；cap ±20（forecast_scale_cap.py:76-106, 366-454） | 训练/验证期算 scalar，测试期 `scaled_score = score × scalar`，clip ±20。让分数进入"可比量纲"，position sizing 才能按信号强度配仓（不再是等权） |
| B7 forecast combine | 多规则加权和 `comb = Σ w_i × scaled_i`；分散化乘数 `FDM = Σw / √(Σw²)`（零相关时，forecast_combine.py:1100-1140），有相关时用相关矩阵估计，带 dm_max 上限 | 多模型/多 horizon 集成时：rank_mean 换成"加权 scaled forecast"；FDM 作为总仓位缩放（多信号低相关 → 可加杠杆/加仓） |
| B8 vol-target position sizing | `position = vol_scalar × forecast / avg_abs_forecast`；`vol_scalar = daily_cash_vol_target / instrument_value_vol`；`daily_cash_vol_target = capital × pct_vol_target / √252`（positionsizing.py:86-126, 427-480） | 个股层面：`w_i ∝ scaled_score_i / σ_i`（σ_i = 滚动年化波动），整体缩放使组合年化波动 ≈ vol_target（默认 20%）。**替换等权 topK 的仓位分配**——高确定性高配、低波动高配 |
| B9 long-only 约束 | `_apply_long_only_constraint_to_position`（positionsizing.py:135-150） | w 恒 ≥0（契约已强制）；约束链里做 clip |
| B10 risk overlay / 风险预算 | 组合层按预测风险缩放总仓位（systems/risk.py、risk_overlay.py） | 单票上限 cap_exposure + 组合目标波动回溯缩放；A 股个人资金：总风险预算 = 波动率目标 |

**落地草图（新执行器 `executors/systems_topk/` 的 B6-B8 骨架）**：
```python
# 训练期估计
scalar = 10.0 / scores_train.abs().mean()          # forecast scale
vol = prices_close.pct_change().rolling(60).std() * sqrt(252)   # 个股年化波动
# 每日
scaled = (score * scalar).clip(-20, 20)
w_raw  = scaled / vol                             # 波动率倒数配仓
w      = w_raw / w_raw.sum() * invest_frac        # 归一到预算
# 可选 B7：多信号 combine + FDM
```

**注意**：σ_i 用训练期估计、测试期滚动更新（避免前视）；停牌/新股无足够历史时
σ 置为截面中位数（防除零）。

**P1 相关的两个补充（已核实）**：
- **成本→SR 拖累换算**（systems/accounts/account_costs.py:14-190 `get_SR_cost_given_turnover`）：
  把成本换算成"每单位换手吃掉的 Sharpe"，正是 playbook 的 cost/alpha 比概念——
  执行器做"值不值得交易"决策时用 `Δw × 单边成本 vs 预期 alpha（SR 口径）` 比较。
- **buffer 两种方法**（systems/buffering.py:35-90）：forecast 法（按预测强度浮动）与
  position 法（目标仓位 ±x%，默认 10%）——比现执行器的 `|w_new-w_cur|>0.005`
  权重差阈值更系统，可替换。

**详版补充组件**（来自 12 组件详版报告，按落地优先级）：
- **B11 行业层风险平价**（详版 C3）：500 票相关矩阵病态 → 降到申万一级行业层
  （~30 个），行业内波动率倒数、行业间等权 × IDM——行业中性风险平价；
- **B12 SR-adjustment 防过拟合调权**（详版 C9）：`ω_diff = √(2ω²(1−ρ̄))`，数据越短
  越不敢偏离等权——正好适配 QLab ~20 个月的测试段（防"调权过拟合"）；
- **B13 capital multiplier 回撤缩仓**（详版 C12）：`w_t = w_raw × min(cumprod(1+r),1)`
  + `.shift(1)` 防前视——回撤期自动降仓，~5 行；
- **B14 换手统一口径**（详版 C10）：`turnover = mean(|Δ(x/y)|) × 256`——在现有金额
  口径外新增，区分"调仓频繁 vs 幅度大"。

### 2.3 新执行器验收（B 阶段整体）

- 与旧 topK 执行器同 spec 对比：net excess / cost_drag / turnover 三项必须不劣于旧版；
- 测试段窗口、expectation 预注册；board 同 base_ref 比 n_variants。

### 2.4 落地流程（MIT vendor + GPLv3 cleanroom）

**目录结构**（执行器内，git 提交，随 spec 走 git archive 上 spark）：
```
executors/systems_topk/
  main.py                 # 契约 CLI + 适配层（cleanroom 独立实现的核心）
  vendor/
    qstrader/             # MIT，逐字 copy 自 quant_refs/qstrader/qstrader/
      portcon/pcm.py, portcon/optimiser/, portcon/order_sizer/
      system/rebalance/, risk_model/risk_model.py
      LICENSE              # 保留 MIT 版权声明
  impl/                    # cleanroom 独立实现（对应 pysystemtrade 的算法）
    forecast_scale_cap.py  # 只实现 spec 里的公式，不抄源码
    positionsizing.py      # vol-target sizing（spec 公式独立写）
    buffering.py, risk_overlay.py, diversify.py ...
```

**cleanroom 两阶段纪律**：
- **阶段 1（已完成）**：读 `quant_refs/pysystemtrade/` 源码 → 产出 spec（两份 borrow
  report，含公式/接口/测试向量/参数默认值）。
- **阶段 2（实现，尚未开始）**：只读 spec，**禁止 open quant_refs/pysystemtrade 任何
  文件**。实现者把 spec 里的公式（如 `position = vol_scalar × forecast / avg_abs_forecast`、
  `FDM = Σw/√(Σw²)`、`buffer = buffer_size × |avg_position|`）翻译成自己的代码表达。
- **交叉验证**：用 spec 里记录的测试向量（如 `positionsizing.py` 的 `EDOLLAR` doctest
  数值、rob_system/config.yaml 的默认参数）对拍，确认独立实现结果与原实现一致——
  对拍用数值结果，不看源码。

**I/O 适配层（唯一胶水，~100-150 行）**：
- 输入侧：parquet 特征/价格 → 适配层构造的 DataFrame（close 列当 price，hfq 收益当 returns）。
- 输出侧：目标权重 → 契约 `portfolio.pkl`（(datetime,instrument) × weight，≥0、行和≤1、每日全量行）。

**怎么验证做对了**：
- MIT vendor 模块：跑通 qstrader 自带 tests + 契约冒烟；
- cleanroom 模块：与 spec 测试向量对拍 + 契约冒烟（本地小 spec → 对比旧 topK metrics）。

**为什么 MIT 用 vendor、GPLv3 用 cleanroom**：MIT 无传染，直接抄最稳；
GPLv3 逐字复制会传染，cleanroom 独立实现规避（公式/接口不受版权保护，只重写"表达"）。

---

## 3. 执行真实性（C 阶段：成交约束，A 阶段数据升级之后）

| 任务 | 内容 | 位置 | 依赖 |
|---|---|---|---|
| C1 最小佣金+费用分层 | 佣金加最低 5 元/笔；大额滑点分档（成交额×参与率 5%/10%） | metrics.py DEFAULT_COSTS 扩展（冻结线，需批准）或执行器模拟 | A |
| C2 涨跌停不可成交 | 涨停日买不进（权重升不上去）、跌停/停牌日卖不出（保留昨日权重） | 执行器内 Ledger + 约束链 | A |
| C3 流动性参与率 | 目标量 > 当日成交额×参与率 → 降仓 | 执行器 | A |
| C4 capacity 估算 | 策略可容纳资金 = f(换手, 参与率, 流动性) | 执行器 run_info + metrics 汇总 | A |

⚠️ C2/C3 属于"模拟成交约束"——tester 不模拟，必须 executor 自己模拟
（qstrader 纪律 C：T+1 在日末权重契约下是弱约束，真正要模拟的是成交约束）。

---

## 4. 稳健性工具链（D 阶段：防过拟合，playbook P2/P3 + Ernest Chan）

| 任务 | 内容 | 位置 | 备注 |
|---|---|---|---|
| D1 参数平台批量 | top_k 30/50/70/100 × buffer 0/5/10/20 × rebalance d/w/m × seed 的网格 → 同 base_ref 一批，board 自动 n_variants/p_bonf；结论找稳定区间非孤峰 | 实验设计（多 spec + 批次） | 已有 base_ref 机制，零管线改动 |
| D2 随机组合基准（Ernest Chan 轻量落地） | 同约束（top_k、调仓频率、换手）下生成 N=200 个随机权重组合，跑同一回测 → 策略净超额的分布分位 | metrics.py 新增族或执行器自算 | "生成一万条没发生的历史，只保留全部幸存者"的工程化 |
| D3 walk-forward | 滚动再训练（如 6 段 × 12 月） | 实验设计 | ⚠️ won't-do 冲突（AGENTS §9 列了"walk-forward 定期任务"），需用户解冻 |
| D4 DSR/PBO/CPCV | 关键策略升 refs 前的最终统计 | metrics.py | ⚠️ won't-do 冲突（deflated Sharpe、分块 bootstrap/拆考场），需用户解冻 |
| D5 合成历史检验（远期可选） | 参数化随机价格路径上跑策略，验证"存活率" | 独立脚本 | Ernest Chan 完整版，成本高，排后 |

---

## 5. 审读与知识闭环升级（E 阶段）

| 任务 | 内容 | 位置 | 依据 |
|---|---|---|---|
| E1 Sharpe 红旗 | review.py 加：sharpe>3 预警、>5 视为泄漏/过拟合嫌疑（pepper 花椒） | pipeline/review.py（冻结线，小改动） | playbook 红线 #2 |
| E2 因果性 5 问 | 代理暴露/confounder/collider/因果方向/剔除行业市值波动率后剩多少 | review.py advisory | Factor Mirage |
| E3 因子机制 4 问 | spec 模板加字段：economic_mechanism / counterparty / unpriced_reason / failure_condition | spec.py 校验 + 模板 | 豪仔 |
| E4 净超额主判据落地 | expectation 用 `backtest.excess_ann_min`（_check_expectation 已支持 dotted path） | 实验规范（写进 README/SOP） | playbook P0#3 |

---

## 6. 长期运行基线（F 阶段：Goshawk #11「部署一条平庸算法」）

- F1：选最简单可解释 baseline（如 hs300 池 topK 周频等权 + 最小 buffer），固定参数常驻
  按周跑一次（沿用 b-local 本地直跑链），结果与漂移记录到 `knowledge/notes/paper_trading.md`。
- F2：连续 4 周与训练段表现对照（rankic / excess_ann 漂移），结论分级降档规则沿用 playbook 第三部分。

---

## 7. 执行顺序与依赖

```
A（数据升级：价格列 + 涨跌停/停牌表）──┬──> C（执行真实性）
                                       └──> B（组合执行器：qstrader vendor + pysystemtrade cleanroom）
D1（参数平台，零依赖，可与 A 并行）
E（审读升级，小改动，随时可上）
D3/D4（需用户解冻 won't-do）→ 关键策略升 refs 前
F（paper trading，B 稳定后）
```

建议第一批（本轮可开工）：
1. **A**：价格缓存列扩展 + 涨跌停/停牌表（数据层，先批冻结线）；
2. **B v0.1**：qstrader pcm/order_sizer/rebalance 直接 vendor + pysystemtrade
   forecast scale / vol-target sizing **cleanroom 独立实现**（只读 spec 公式），
   替换 `_example_topk_portfolio` 的等权分配；
3. **D1**：top_k × buffer × rebalance 参数平台第一批（实验设计，零管线改动）。

新执行器实现顺序（详版 spec v0.1-v0.5）：v0.1 scale+sizing+归一 → v0.2 buffer+
换手口径 → v0.3 相关清洗+risk overlay → v0.4 行业风险平价+DM+SR-adjustment →
v0.5 SR 成本天花板+回撤缩仓。每版单独 submit 一个 spec 对照固定测试器
（expectation 预注册）。

---

## 8. 冻结线合规与许可证合规

- **碰冻结线的改动**（需用户逐项批准）：A（data.py 价格缓存 + metrics.py lag_open）、
  C1（metrics.py 成本模型）、D2/D4（metrics.py 新族）、E1/E2（review.py）、E3（spec.py）。
  每项给出净增行数与测试计划，遵守 P7 三条件（痛感≥2 / ≤3 实验硬前置 / 净增≤0 或带测试）。
- **执行器自由区**（无需批准）：B 全部、C2/C3/C4 执行器侧、D1、F。
- **won't-do 解冻清单**（AGENTS §9 明列，需用户解冻才做）：D3 walk-forward、
  D4 deflated Sharpe / 分块 bootstrap / 拆考场（CPCV 类）。
- **许可证（cleanroom 纪律，规避 GPL 传染）**：
  - **qstrader（MIT）**：直接 copy 代码无传染风险，vendor 目录保留其 MIT 版权声明
    （`vendor/qstrader/LICENSE`）。
  - **pysystemtrade（GPLv3）**：不逐字复制。走 **cleanroom 独立实现**——阶段 1 读码者
    （已完成）产出 spec（两份 borrow report），阶段 2 实现者只读 spec、不碰源码，
    独立重写"表达"，只复用不受版权保护的公式/接口/测试向量。这样产出的代码不是
    GPL 派生作品，本项目**保持 MIT**，无传染。
    - 纪律落点：`executors/systems_topk/impl/` 内代码必须是 cleanroom 产物；
      实现过程记录在 `knowledge/notes/cleanroom_log.md`（谁读了什么、什么时候产出 spec、
      实现者承诺未接触源码）作为合规留痕。

---

## 9. 与既有计划的关系

- 承接 `knowledge/notes/dev_plan_trade_p1p3.md`（P0-P3 已落地项不重复）；
  本文新增：成熟项目借鉴（§2）、执行真实性（§3）、随机组合基准（D2）、审读升级（E）。
- 旧计划剩余项并入：purge/embargo（P0 剩余）与 A 阶段数据升级一并立项（原计划已建议）；
  paper trading（P2 剩余）并入 F 阶段。
