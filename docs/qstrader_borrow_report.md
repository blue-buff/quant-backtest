# QSTrader 可借鉴点报告（面向 QLab 重构）

> 分析对象：`/Users/lucas/projects/quant_refs/qstrader/`（MIT，~4,100 行包代码 + examples + tests）
> 阅读范围：`qstrader/simulation/`、`trading/`、`system/`、`portcon/`、`broker/`、`exchange/`、`execution/`、`alpha_model/`、`risk_model/`、`signals/`、`statistics/`、`data/`、`asset/`，以及 `examples/long_short.py`、`sixty_forty_fees.py`、`momentum_taa.py`、`scripts/static_backtest.py`
> 结论全部基于上述源码，每处引用标注 `文件路径:行号` 与类/函数名。

---

## 1. 架构地图

### 1.1 分层

| 层 | 模块 | 职责（一句话） |
|---|---|---|
| 事件引擎 | `qstrader/simulation/daily_bday.py` `DailyBusinessDaySimulationEngine` | 按交易日产出 pre_market / market_open / market_close / post_market 四类 `SimulationEvent`（`simulation/event.py`），驱动整个回测时钟 |
| 回测装配 | `qstrader/trading/backtest.py` `BacktestTradingSession` | 装配 exchange / data_handler / broker / sim_engine / rebalance 日程 / QTS，并在主循环里驱动 broker 记账与调仓触发 |
| 策略系统 | `qstrader/system/qts.py` `QuantTradingSystem` | 把 alpha_model + PCM + ExecutionHandler 串成一条"调仓调用链"，是唯一业务入口 |
| 信号→权重 | `qstrader/alpha_model/*.py`（`AlphaModel` / `FixedSignalsAlphaModel` / example 里的 `TopNMomentumAlphaModel`） | 给定 dt，输出 `dict[asset -> 信号值/权重]`；信号可以是权重（可带负号=做空） |
| 约束钩子 | `qstrader/risk_model/risk_model.py` `RiskModel` | `__call__(dt, weights) -> weights`，在 alpha 输出之后、优化之前修改权重（仓库内只有接口，无实现） |
| 组合构造 | `qstrader/portcon/pcm.py` `PortfolioConstructionModel` | 核心组装器：alpha→risk→optimiser→sizer→与当前持仓 diff→生成订单列表 |
| 目标权重 | `qstrader/portcon/optimiser/*.py`（`FixedWeightPortfolioOptimiser` 透传 / `EqualWeightPortfolioOptimiser` 等权×scale） | 把 alpha 信号转换成目标权重向量 |
| 权重→股数 | `qstrader/portcon/order_sizer/*.py`（`DollarWeightedCashBufferedOrderSizer` / `LongShortLeveragedOrderSizer`） | 把目标权重换算成整数股数：归一化、现金缓冲、成本预扣、floor 取整 |
| 执行 | `qstrader/execution/execution_handler.py` `ExecutionHandler` + `execution_algo/market_order.py` | 对订单列表套 ExecutionAlgorithm（默认透传），再逐个 submit 给 broker |
| 模拟券商 | `qstrader/broker/simulated_broker.py` `SimulatedBroker` | 撮合：开市才执行、先卖后买排序、按 bid/ask 成交、算手续费（FeeModel）、更新现金与持仓 |
| 持仓记账 | `qstrader/broker/portfolio/*.py`（`Portfolio` / `PositionHandler` / `Position` / `PortfolioEvent`） | 买/卖两腿分离的仓位会计：加权平均成本、已实现/未实现 PnL、现金流历史 |
| 交易时段 | `qstrader/exchange/simulated_exchange.py` `SimulatedExchange` | 判断某时刻是否开市（硬编码 NYSE 时段，无节假日历） |
| 调仓日程 | `qstrader/system/rebalance/{daily,weekly,end_of_month,buy_and_hold}.py` | 预计算调仓时间戳列表，与策略逻辑完全解耦 |
| 统计 | `qstrader/statistics/*.py`（`TearsheetStatistics` 绘图 / `JSONStatistics` 机器可读 / `performance.py` 指标） | 权益曲线→收益→Sharpe/Sortino/回撤/CAGR，同一套 `get_results()` 双渲染 |
| 数据 | `qstrader/data/*.py`（`BacktestDataHandler` / `CSVDailyBarDataSource`） | 多数据源 fallback 取价；OHLCV 展开成 open/close 两时间点 bid/ask 序列（lru_cache 1M） |

### 1.2 主循环链路（`backtest.py:368-435` `run()`）

```
for event in sim_engine:                          # 每日 4 个事件
    broker.update(dt)                             # ① 持仓按中间价重估；开市则执行排队订单（先卖后买、bid/ask 成交、扣费）
    if event_type == "market_close": signals.update(dt)
    if dt ∈ rebalance_schedule:                   # ② 到调仓日才触发策略
        qts(dt):                                  # QuantTradingSystem.__call__ (qts.py:154)
            weights   = alpha_model(dt)           #   信号/权重
            weights   = risk_model(dt, weights)   #   约束钩子（可选）
            target_w  = optimiser(dt, weights)    #   目标权重
            target_q  = order_sizer(dt, full_w)   #   权重→整数股数（含成本预扣/缓冲）
            orders    = target_q − current_q      #   与当前持仓 diff，零单抑制
            execution_handler(dt, orders)         #   submit → broker 排队
    if event_type == "market_close": equity_curve.append(equity)   # ③ 收盘记权益
```

### 1.3 与 QLab 契约的映射（一句话结论）

qstrader 的 **alpha_model（信号）与 optimiser/sizer（持仓）分离**，恰好就是 QLab 契约里 `pred.pkl`（score）与 `portfolio.pkl`（weight）两个文件的分离——QLab 的契约本身就实现了 qstrader 架构的理想形态，而 qstrader 的 `PortfolioConstructionModel` 正是 QLab 当前最薄弱的"信号→持仓"翻译层（`executors/_example_topk_portfolio/main.py:59-91` `build_portfolio`）的完整参考实现。

三个隐含纪律（读 metrics.py 后确认，Qlab 契约下的硬约束）：

- **纪律 A：portfolio.pkl 必须每日输出完整持仓行。** `pipeline/metrics.py:387` 用 `w.reindex(index=close.index, columns=close.columns).fillna(0.0)` 对齐价格表，缺行的日子被当作 0 仓位 → 制造虚假卖出+再买入的换手与成本。所以"只在调仓日输出权重"会直接污染 backtest 族指标；必须像例子里那样每天输出（非调仓日输出与昨日相同的行）。
- **纪律 B：executor 内部成本假设必须与固定测试器同源。** tester 用 `DEFAULT_COSTS`（佣金双边万2.5、印花卖出千1、滑点 1bp，`metrics.py:373-408`）。executor 若做成本感知决策（buffer 阈值、换手预算），必须用同一套数字，否则自报口径与台账漂移。
- **纪律 C：T+1 在"收盘价成交、日末权重"契约下是弱约束。** 日末权重 w_t ≥ 0 自动满足"不卖空"；当日买入当日卖出（回转）在日末权重里不可见。真正需要 executor 模拟的是**成交约束**（涨停买不进、跌停/停牌卖不出），见组件 #7/#8。

---

## 2. 十个最有价值的可借鉴组件（按价值排序）

### #1 全量目标权重向量 + 自动清零（组合状态完备性）

- **出处**：`qstrader/portcon/pcm.py:58-120` `PortfolioConstructionModel._obtain_full_asset_list` / `_create_full_asset_weight_vector`；`pcm.py:274-278`（`__call__` 内组装）
- **它做了什么**：每个调仓日先取 **universe ∪ 当前持仓** 的并集，生成全零权重向量，再用优化器输出覆盖非零项。效果：**凡是掉出目标组合的名字（含掉出 universe 的）自动归零**，随后 `_generate_rebalance_orders`（pcm.py:154-213）diff 出卖出订单。持仓状态永远完备：不存在"幽灵持仓"。
- **为什么比 topK 等权强**：`build_portfolio` 里持仓是手工维护的 `current` dict（`_example_topk_portfolio/main.py:66-86`），靠 `del current[inst]` 清理。遇到 NaN 分数、股票停牌/退市/成分调整、universe 收缩时，这种"增量修补"式状态机容易留脏状态（该卖没卖、或误持已失效名字）。全量向量把"今天的目标是什么"变成每一天的完整快照，正确性是结构保证的，不是靠每个分支都写对。
- **QLab 契约下如何落地**：**可直接移植**。portfolio.pkl 是 (datetime, instrument) × weight，天然就是"每天一个全量目标向量"的稀疏表示（未持仓行省略=零行省略）。executor 内部改为：
```python
def target_vector(day_scores, prev_holdings, universe_today, top_k, w):
    full = set(universe_today) | set(prev_holdings)      # 并集
    target = pd.Series(0.0, index=sorted(full))
    picks = day_scores.nlargest(top_k).index
    target.loc[picks & target.index] = w                 # 覆盖
    return target                                        # 掉出的自动为 0
```
  再加一条可测不变量断言：`target ≥ 0`、`target.sum() ≤ 1`、`(target == 0) | (target >= 0)` 无 NaN——这比任何测试用例都便宜。

### #2 调仓日程对象化（换手的第一杠杆，一行配置）

- **出处**：`qstrader/system/rebalance/{daily,weekly,end_of_month,buy_and_hold}.py`（`DailyRebalance` / `WeeklyRebalance` / `EndOfMonthRebalance` / `BuyAndHoldRebalance`）；消费方 `backtest.py:122-137` `_is_rebalance_event` 与 `:233-259` `_create_rebalance_event_times`
- **它做了什么**：调仓频率是一个**预计算的 Timestamp 列表**，与策略逻辑完全解耦。同一个策略换一个 rebalance 参数就是另一种回测；主循环只问"今天在不在日程里"。
- **为什么比 topK 等权强**：例子把"每日调仓"硬编码进 `build_portfolio`。对 A 股，"每日满仓调仓"换手极高，成本拖累（`cost_drag`）可能吃掉全部 alpha；月度调仓是最自然的默认起点，但它必须是一个可配置项而不是改代码。qstrader 用数据（时间戳列表）而非控制流表达"何时交易"，这让"频率"进入可实验空间（spec.params 一行）。
- **QLab 契约下如何落地**：**直接移植为 config 参数**。注意纪律 A：**非调仓日仍要输出与昨日相同的权重行**（否则 tester 的 fillna(0) 制造虚假换手）。草图：
```python
rebal = params.get("rebalance", "daily")            # daily|weekly|monthly
days = sorted(scores.index.get_level_values(0).unique())
if rebal == "monthly":  rebal_days = set(days.to_period("M").map(lambda p: days[days.to_period("M") == p][-1]))
elif rebal == "weekly": rebal_days = {d for d in days if d.weekday() == int(params.get("weekday", 4))}
else:                   rebal_days = set(days)
# 循环内：d in rebal_days → 重算 target；否则 → 输出 prev 行（原样复制）
```
  可顺带把 `rebalance_weekday`、`pre_market` 之类的校验（`weekly.py:41-65` `_set_weekday` 的 ValueError 风格）抄过来，配置错误早炸早暴露。

### #3 Sizer 下沉"归一化 + 现金缓冲 + 成本预扣"（可行性不变量）

- **出处**：`qstrader/portcon/order_sizer/dollar_weighted.py` `DollarWeightedCashBufferedOrderSizer`：`_normalise_weights`（:82-113，和归一为 1、拒绝负权重）、现金缓冲（:132-135，`total_equity * (1 - cash_buffer_percentage)`）、**成本预扣**（:151-158，`after_cost_dollar_weight = pre_cost_dollar_weight - est_costs`，用 `broker.fee_model.calc_total_cost`）、floor 取整（:172-174）
- **它做了什么**：把"权重向量 → 可行持仓"的转换集中到一个可插拔对象里：归一化、留现金缓冲、每股预扣估计成本后再取整。策略代码永远不需要记得"要归一化/要留缓冲/成本会吃掉额度"。
- **为什么比 topK 等权强**：例子里 `invest_frac`（0.95）硬编码在 `build_portfolio` 里，`target_w = invest_frac / top_k` 直接当权重用，没有归一化/缓冲/成本层。sizer 化之后，这些成为**可测的不变量**：无论策略输出什么权重，出口处保证 `sum ≤ 1`、`each ∈ [0,1]`、预算被成本感知地花完。
- **QLab 契约下如何落地**：**直接移植（权重版）**。契约本身就是权重，所以 sizer 输出的是权重而非股数：
```python
def to_feasible(target, budget=0.95):
    target = target.clip(lower=0.0)
    s = target.sum()
    if s > budget: target = target * (budget / s)      # 归一化到预算内（保留 sum≤1）
    return target[target > 0]                           # 未持仓行省略
```
  成本感知的 QLab 版（连接纪律 B）：在"是否交易"决策里用 DEFAULT_COSTS 估算单边成本 `c = 2.5e-4 + 1e-4`（佣金+滑点，卖出另加千1 印花），**只对 `|Δw| × c 低于预期 alpha 收益` 的交易执行**——这就是把 qstrader 的"成本预扣"思想翻译到权重契约。

### #4 RiskModel 钩子：score→weight 之间的约束层接口

- **出处**：`qstrader/risk_model/risk_model.py` `RiskModel.__call__(dt, weights) -> weights`；调用序 `pcm.py:259-270`（alpha → **risk** → optimiser）
- **它做了什么**：定义了一个纯函数式回调，在 alpha 权重上叠加约束后再进优化器。qstrader 只留了接口（仓库无实现），但它把"信号"与"市场约束"的叠加点固定下来了。
- **为什么比 topK 等权强**：例子里"信号→持仓"只有一步：rank → topK。A 股真实约束（ST/*ST 排除、涨跌停过滤、停牌过滤、行业/市值暴露上限、流动性门槛）没有地方放；硬塞进 `build_portfolio` 会让这个函数无限膨胀且不可单测。RiskModel 钩子把每个约束变成**一个纯函数**，可分别测试、可组合。
- **QLab 契约下如何落地**：**接口设计直接移植，实现放 executor 自由区**。executor 内建约束链（作用于 scores 或权重）：
```python
def exclude_st(scores, st_set):      return scores.drop(index=st_set, errors="ignore")
def exclude_limit(scores, px, limit): # 涨停(买不进)/跌停(卖不出)日过滤：需要价格表，属"思想"级
    ...
def cap_exposure(w, max_per_name=0.05): return w.clip(upper=max_per_name)
scores = exclude_st(scores, ...)
weights = cap_exposure(to_feasible(target_vector(scores, ...)), ...)
```
  注意：QLab 固定菜单不含 ST/涨跌停标签，`exclude_st` 需要外部名单（思想级）；`cap_exposure`/`to_feasible` 可直接移植。

### #5 调仓单生成 = target − current 的 diff + 零单抑制（buffer 的插槽位置）

- **出处**：`qstrader/portcon/pcm.py:154-213` `PortfolioConstructionModel._generate_rebalance_orders`（先补齐两边缺失键 → `order_qty = target_qty - current_qty` → 只保留 `!= 0` 的订单）
- **它做了什么**：把"目标组合"与"当前组合"的差作为交易动作，**零 diff 不产生订单**。buffer/阈值语义天然可以插在 diff 之后（`|Δ| > 阈值才交易`），而不用侵入策略逻辑。
- **为什么比 topK 等权强**：例子的 buffer（`abs(w_new - w_cur) > buffer`）嵌在持仓更新循环里（`_example_topk_portfolio/main.py:77-86`），逻辑和状态搅在一起，难以扩展成更聪明的变体。diff 化之后：
  - **rank buffer**：仅当 topK 集合相对上次变化 ≥ r 个名字才调仓（比权重 buffer 更贴合"排名噪声"本质）；
  - **最小交易额过滤**：`|Δw| < 阈值 不交易`（避免碎单）；
  - 每个变体都是 diff 之后的一个独立函数，可单测。
- **QLab 契约下如何落地**：**直接移植**。改 `build_portfolio` 为"先算全量 target（#1），再 diff 上一次持仓（#2 的 prev 行），阈值过滤后写回"：
```python
def decide(prev, target, band=0.005, rank_buf=0):
    if rank_buf and set(target.index) != set(prev.index) and \
       len(set(target.index) ^ set(prev.index)) < rank_buf:
        return prev                                  # 集合变动太小，不调
    delta = target.sub(prev, fill_value=0.0)
    delta = delta.where(delta.abs() > band, 0.0)     # 零单抑制 + 权重 band
    return target.where(delta != 0.0, prev)          # 未过阈值的保持原权重
```
  这是把 qstrader 的"订单 diff"翻译成权重契约下的"持仓 diff"。

### #6 Top-N 截面选股 alpha 的参考实现（rank → 1/N）

- **出处**：`examples/momentum_taa.py:20-147` `TopNMomentumAlphaModel`（`_highest_momentum_asset` :54-92 排序取前 N；`_generate_signals` :94-118 赋 `1.0 / mom_top_n`；`__call__` :120-147 **warmup 不足时输出全零权重=空仓**）
- **它做了什么**：一个教科书级的截面选股 alpha：对全资产算动量 → 排序取前 N → 每只赋 1/N。注意两个细节：(a) 权重分母恒为 `mom_top_n`（不是"当天实际入选数"），总暴露稳定；(b) 数据不足期**默认空仓**而不是满仓。
- **为什么比 topK 等权强**：它不是更强的策略，而是**当前例子的规范形态**——把"排序、截断、赋权、数据不足保护"四件事拆成独立方法，且 warmup 门控（`if self.signals.warmup >= self.mom_lookback`）是例子里缺失的"开局保护"（QLab 测试段第 1 天就满仓建仓，turnover 被记为满额，`metrics.py:401`）。这是翻译层"应该长什么样"的参照物。
- **QLab 契约下如何落地**：**直接移植为结构**。executor 拆成 `score() → rank() → topk() → weight() → guard()` 五段；新增 `params.warmup_days`：测试段前 N 天输出空持仓（空 DataFrame 合法，`weight` 列无行），第 N+1 天起正常交易。权重分母固定用配置的 top_k（对应 qstrader 的 `mom_top_n` 语义，避免可用股不足时单票权重膨胀）。

### #7 持仓两腿记账（可卖数量 / 成交约束的记账基础）

- **出处**：`qstrader/broker/portfolio/position.py` `Position`：买/卖数量分离（`buy_quantity` / `sell_quantity`）、加权平均成本（`_transact_buy` :328-344 / `_transact_sell` :346-362）、已实现 PnL 按比例分摊佣金（`realised_pnl` :248-278）、零仓自动删除（`position_handler.py:31-33` `PositionHandler.transact_position`）
- **它做了什么**：单只股票的完整会计：买入腿、卖出腿分开记账，`net_quantity = buy − sell`，平均成本是加权平均（含佣金摊销），持仓归零自动移除。**"可卖数量"直接可读**。
- **为什么比 topK 等权强**：例子没有持仓会计——只有权重 dict，不知道"这只票我持有多久、可卖多少"。A 股语境下这是成交约束模拟的前提：
  - 涨跌停/停牌日：跌停想卖卖不出 → 权重无法降到目标 → 需要知道"今天必须卖出的量"；
  - 涨停一字板：想买买不进 → 权重无法升到目标。
  - qstrader 的 Position 给出了"按 leg 记账"的正确结构。
- **QLab 契约下如何落地**：**思想级（简化为权重版 ledger）**。executor 维护跨日状态：
```python
class Ledger:
    def __init__(self): self.hold_since = {}      # inst -> 首次持仓日
    def available(self, inst, dt):                # 可卖量（权重口径）
        return self.hold_since.get(inst) is not None and self.hold_since[inst] < dt
```
  配合 #8 使用：跌停/停牌日，若 `not ledger.available(inst)` 则不允许减仓（权重保持昨日），若涨停则不允许加仓。T+1 本身在日末契约下是弱约束（见 1.3 纪律 C），这个 ledger 的真正价值在成交约束。

### #8 执行序：先卖后买 + 开市门（成交约束模拟的参考序）

- **出处**：`qstrader/broker/simulated_broker.py:672-682` `update()`：`if self.exchange.is_open_at_datetime(...)` 才执行；`sorted_orders = sorted(orders, key=lambda x: x[1].direction)`（`direction = copysign(1, quantity)`，卖=-1 排前 → **先卖后买**，回笼资金再买入）
- **它做了什么**：成交模拟的两条铁律：非开市不成交；同批次订单先卖后买。
- **为什么比 topK 等权强**：例子里没有"成交"概念——权重一步到位。真实世界里单日调仓涉及资金约束与成交可能失败（A 股尤其），先卖后买是"用卖出的钱买"的现金流顺序。QLab 的 tester 不模拟这些（`metrics.py` 直接 `w * r`），所以**只有 executor 自己模拟**才可能真实。
- **QLab 契约下如何落地**：**思想级**。若 executor 模拟成交约束（涨跌停/停牌），内部处理顺序固定为：先处理卖出（跌停卖不出→保留昨日权重并标记），再处理买入（涨停买不进→差额留在现金）；卖出回笼的资金是买入的预算上限。权重契约下"预算"体现为 `sum(w) ≤ 1` 与当日现金 `1 − sum(prev)`。

### #9 burn-in / warmup 门控（开局保护）

- **出处**：`examples/momentum_taa.py:145-147`（warmup 不足输出全零权重）；`qstrader/trading/backtest.py:399-425` `burn_in_dt`（burn-in 前不调仓、不计权益曲线；`get_target_allocations` :364-366 还支持 burn-in 后截断）
- **它做了什么**：策略显式声明"数据不足期不交易、不评价"。burn-in 同时作用于调仓触发与权益曲线记录，保证统计窗口内策略行为稳定。
- **为什么比 topK 等权强**：例子里测试段第 1 天即满仓 topK。两种失真：(a) 首日换手被记为满额（`metrics.py:401` `turn.iloc[0] = w.iloc[0].abs().sum()`），(b) 若策略依赖滚动统计（如近 N 日均值），开局几天的持仓基于残缺历史。warmup/burn-in 是标准解法。
- **QLab 契约下如何落地**：**直接移植**。`params.warmup_days`：前 N 天输出空持仓；`params.burn_in_dt`（可选）配合 expectation 对照。注意 tester 的指标窗口是 portfolio.pkl 实际覆盖的天（`metrics.py:408` 起"只统计组合声明了权重的日子"），空持仓输出 `w=0` 的天也会进入统计——所以 warmup 期要么不输出行（窗口自动跳过），要么明确知道会以 0 仓位计入。

### #10 基准即策略的参数实例（对照实验复用同一代码路径）

- **出处**：`scripts/static_backtest.py:83-124`（strategy 与 benchmark 都是 `BacktestTradingSession`，仅 alpha/参数不同）；`examples/sixty_forty_fees.py:40-65`（同一策略 with/without `PercentFeeModel` 双跑）；`qstrader/alpha_model/fixed_signals.py` `FixedSignalsAlphaModel`（固定配置 alpha）
- **它做了什么**：对照实验不写第二套引擎——strategy 和 benchmark 只是同一构造器的不同参数。成本敏感性对照（有费用 vs 无费用）也是同一策略双跑。
- **为什么比 topK 等权强**：QLab 的 backtest 族已自动算 benchmark（hs300/zz500/等权，`metrics.py:389-396`），所以这条**不是抄 benchmark**，而是抄"**executor 内部分离策略增益与组合构造增益**"的方法：同一份代码，跑"全仓等权 topK（无 buffer、无 warmup）"作为组合构造基线，与主策略对照 → 差值就是"策略层（buffer/权重/约束）"的贡献，对应 QLab attribution 的 align 层。
- **QLab 契约下如何落地**：**思想级 + 极小代码**。executor 支持 `params.mode: "strategy" | "baseline"`，baseline 模式跳过 buffer/warmup/约束，输出纯等权 topK；两个 run 用同一 exp_id 不同 params 提交，board 里对比。这也是"预注册 expectation + n_variants"纪律的自然延伸（AGENTS §8）。

---

## 3. 不值得抄的（抄了会亏）

| qstrader 组件 | 出处 | 为什么不抄 |
|---|---|---|
| **多币种/多子账户券商层** | `simulated_broker.py` 的 `cash_balances` 多币种、`create_portfolio`/`subscribe_funds_to_portfolio`/`withdraw_*`、`PortfolioEvent` 现金流历史、master/子账户聚合 equity | QLab 单资产人民币、tester 已固定成本与收益口径；executor 不需要账户/现金流会计。抄 = 白维护几百行死代码 |
| **事件驱动引擎** | `simulation/daily_bday.py` 四事件循环 + `exchange/simulated_exchange.py` 开市判断 + `open_orders` 队列（收盘挂单次日处理） | QLab 是"收盘价成交、次日生效"的权重契约（`metrics.py:397`），撮合/排队/时段判断全部由 tester 承担；executor 复刻撮合 = 双口径风险 |
| **deque 价格缓冲信号层** | `signals/buffer.py` `AssetPriceBuffers`、`signals/signal.py`、`signals_collection.py` 每日统一喂价 | QLab 特征由固定菜单保证，滚动指标 pandas 一行；这套缓冲是给实时事件驱动省内存的，批处理回测里是纯负担 |
| **PercentFeeModel 的税模型** | `broker/fee_model/percent_fee_model.py:47-68`：`tax = tax_pct * abs(consideration)`，**买卖都收税** | 对 A 股是**错的**：印花税只卖出收（千1）。QLab 的 `DEFAULT_COSTS`（`metrics.py:373-408`：佣金双边万2.5+印花卖出千1+滑点1bp）已经比它更真实。抄它 = 把正确的成本口径改坏 |
| **多资产多空杠杆** | `portcon/order_sizer/long_short.py` `LongShortLeveragedOrderSizer`（gross_leverage=5x 的例子在 `long_short.py:34-46`）、`asset/equity.py` 的 `tax_exempt`（英国印花税元数据） | A 股个股融资融券有门槛、个人默认不做空；杠杆归一化数学（`_normalise_weights` :75-103）有价值但已被 #3 吸收，整套多空 sizer 不适用 |
| **CSV 数据源 + bid/ask 展开** | `data/daily_bar_csv.py`（OHLCV→open/close 双时间点、Adj Close 调整、`lru_cache(maxsize=1024*1024)`）、`data/backtest_data_handler.py` 多源 fallback | QLab 数据管道已冻结（float32 parquet 固定菜单），executor 只许读；这套是给 CSV 原始数据的适配层 |
| **统计双渲染层** | `statistics/tearsheet.py`（matplotlib 一页纸）、`statistics/json_statistics.py`、`performance.py`（`create_sharpe_ratio` :44-53 无风险利率=0 的朴素 Sharpe） | QLab 已有更全的固定测试器（backtest 族：Sharpe/Sortino/Calmar/MDD/VaR/CVaR/PSR…）+ MLflow 台账 + attribution 四层；tearsheet 是展示物，WebUI 不需要。`performance.py` 的指标口径比 QLab 弱 |
| **uuid Order / Transaction 对象礼仪** | `execution/order.py`（uuid4 order_id、方向推断）、`broker/transaction/transaction.py` | 权重契约下没有订单对象；这些是事件驱动 broker 的载体，纯搬运 |
| **DynamicUniverse 的资产进入时间表** | `asset/universe/dynamic.py` | 方向正确（universe 应是时间函数），但 QLab 数据固定菜单没有成分历史（AGENTS §4 已标注幸存者偏差 caveat），executor 无法自造——这是平台已知限制，不是组件可抄的 |

---

## 4. 如果只抄 3 件事

**① 全量目标权重向量 + 自动清零（组件 #1，`portcon/pcm.py`）**
这是正确性基石：把"每天的目标"变成完整快照而非增量修补，掉出组合的名字自动归零，持仓状态永不失真。零成本、立即生效，且是其他一切改造（buffer、rank buffer、约束层）的地基。

**② 调仓日程参数化（组件 #2，`system/rebalance/*.py`）**
换手/成本是第一杠杆，而"何时调仓"必须是一个 config 参数而不是代码。月度调仓是 A 股默认起点，这一件事就能把 `cost_drag` 从"吃掉 alpha"变成"可调旋钮"，并直接进入 spec.params 的实验空间。

**③ Sizer 下沉"归一化 + 现金缓冲 + 成本感知"（组件 #3，`portcon/order_sizer/dollar_weighted.py`）**
把"策略输出什么"与"组合是否可行"分开：出口处保证 sum≤1、each∈[0,1]、预算被成本感知地花完；buffer 阈值与 DEFAULT_COSTS 同源（纪律 B）。这让翻译层从"手写循环"变成"可测不变量"。

**为什么是这三件**：它们恰好覆盖 QLab 翻译层最弱的三个维度——**状态正确性（①）、时间结构（②）、可行性/成本（③）**——且全部落在 executor 自由区、不碰冻结管线、不需要新数据。其余七件（RiskModel 约束层、rank buffer、ledger、执行序、warmup、对照模式）都是在这三件之上的增量，按需再上。

---

## 5. 附：落地优先级建议（供排期）

1. **P0（一次改动，立即收益）**：#1 全量向量 + 断言；#2 调仓日程参数（daily 默认，兼容现有行为）。
2. **P1（一个执行器迭代）**：#3 sizer 下沉 + 成本感知 buffer；#6 结构拆分（warmup 门控）。
3. **P2（策略自由区探索）**：#4 约束链（cap_exposure 先行，ST/涨跌停思想级）；#7/#8 成交约束模拟；#10 对照模式。
4. **P3（仅思想）**：#9 burn-in 与 expectation 联动；#5 rank buffer（数据验证后再上）。
