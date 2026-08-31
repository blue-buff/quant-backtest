# 交易回测评判口径与目标跑分

> 缘起：job85（kronos trade-layer，25 天）回测 +37.2% 超额，但信号层
> rankic=-0.013（p=0.75 不显著）、持仓层 ratio_vs_signal=-12、beta=1.29。
> 复盘结论：该收益是"满仓 beta + 25 天小样本"的假象，不是 alpha。
> 据此制定可复用的交易回测评判口径，避免再次被小样本/高 beta 的假阳性误导。

## 1. 四层门控（任何一层不过即停，不向下解读）

### 门槛 0：样本量（硬性，先于一切）
- n_days >= 250（约一年交易日）。低于此值，所有年化指标
  （ann_ret / excess_ann / sharpe / mdd）无统计意义，只标注"链路验证"。
- 25 天窗口的 +37% 年化没有信息量（单日 1% 年化即 12 倍）。

### 门槛 1：信号层（alpha 的根）
- rankic_mean > 0 且 bootstrap_rankic.p_le0 < 0.05（强）或 < 0.20（提示）。
- 不过 -> 回测收益一律视为 beta/运气，直接判"无 alpha"，不进交易层解读。

### 门槛 2：持仓层（信号 -> 交易的忠实度）
- weight_ic_mean >= 0.40（权重与信号排序强正相关）。
- turnover_mean <= 0.30/天（对应成本可控）。
- 信号为正时 ratio_vs_signal >= 0.4（否则持仓与信号脱节）。

### 门槛 3：交易层（成绩，全部要过）
- excess_ann > 0（组合 vs 基准 sh000300 的年化超额 = 除大盘收益）。
- CAPM alpha_ann = ann_ret - beta x bmk_ann_ret > 0
  （beta 中性后的真选股 alpha；bmk_ann_ret = ann_ret - excess_ann）。
- sharpe >= 0.5；mdd >= -20%；cost_drag_ann <= 0.10；beta <= 1.3。

### 门槛 4：稳健性
- 至少两个不重叠测试窗（或滚动窗）都过门槛 1+3；单一牛/熊窗的通过不算数。

## 2. 历史基准锚定（qlib 官方 36 模型，395 天，topK=50 等权）

实测分布（mlflow 台账）：
- excess_ann：最好 +5.0%（qb_dens_a158）、+3.9%（qb_lgb_a158）；
  中位数约 -8%；大部分为负。
- sharpe：最好 1.26（qb_lgb_a158）；中位数约 0.3。
- weight_ic：0.38~0.65（多数 > 0.5）。
- cost_drag_ann：多数 0.03~0.05；高换手策略（p8_trade_zz500_10d）达 0.19。

## 3. 目标跑分（分阶段）

### 阶段 A：链路验证（已达成）
contract ok + metrics 出现 backtest/portfolio 段（job85 已过）。

### 阶段 B：信号层达标（现阶段主目标）
250+ 天窗口，rankic_mean > 0 且 p_le0 < 0.05。
Kronos 现状：26 天 rankic=-0.013、p=0.75 -> 未达标。零样本若在长窗仍不显著，
转微调（finetune_csv / qlib_test.py 路线）。

### 阶段 C：交易层达标（信号过 B 之后）
- 及格线：excess_ann > 0，CAPM alpha > 0，sharpe >= 0.5，cost_drag_ann <= 0.10。
- 良好线（追平历史最好）：excess_ann >= +5%，sharpe >= 1.0，
  mdd >= -20%，beta <= 1.3。
- 优秀线：excess_ann >= +10%，sharpe >= 1.5，且两窗稳健。

## 4. Kronos 对照
job85：门槛 0 不过（25 天）、门槛 1 不过（rankic 不显著）-> 停在阶段 B 之前。
交易层 +37.2% 超额不得引用为成绩。
