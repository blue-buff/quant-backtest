# 代码对比审查报告（开源社区 vs 本项目）

> 审查时间：2026-08-20 | 对比对象：qlib 0.9.7 官方源码、rqalpha 6.3.0 官方源码、
> hands-on-qlib（Daryl9441，qlib 实战教程）、Multi-Factor-Strategy-Development-Framework（RQAlpha 多因子框架）
> 审查方式：容器内逐文件对照官方/社区实现与本项目对应功能代码

## 一、对比清单（相同功能代码对照）

| 功能 | 本项目 | 对照对象 | 结论 |
|---|---|---|---|
| 因子集 | Alpha158（qlib 内置） | qlib 官方 Alpha158 | ✅ 一致（158 特征/标签/CSZScoreNorm 已验证） |
| 简化层策略 | TopkDropoutStrategy topk50/n_drop5（官方类） | qlib 官方 | ✅ 一致 |
| 月度计划生成 | planlib.build_plan_with_buffer（自研） | 官方 TopkDropout 日频机制 | ⚠️ 语义近似非等同（见问题 1） |
| 真实规则执行 | rq_strategy_qlib（自研） | rqalpha 官方 API/撮合 | ⚠️ 见问题 2/3/4 |
| 费用 | 依赖 rqalpha 默认 mod | 官方 mod_config.yml | ✅ 佣金万八+印花税千一+最低5元，**默认启用已确认** |
| 滑点 | 自研 slippage_price | 官方 PriceRatioSlippage | ⚠️ 公式一致，官方多涨跌停夹取（问题 3） |
| 参与率 | 自研 participation_capped | 官方 matcher volume_limit | ⚠️ 官方按当日累计，自研按单笔（问题 4） |
| 涨跌停 | 依赖官方撮合/PriceValidator | 官方 | ✅ 一致 |
| 停牌 | bar_dict 缺失跳过+次日重试 | 官方 OrderRejected | ✅ 本项目更完善 |
| T+1/100股/印花税 | 依赖官方引擎 | 官方 | ✅ 一致 |
| 数据处理 | dump_bin（官方脚本）+ 新浪 fetch | qlib 官方 dump_bin | ✅ 一致（含 turn/factor 扩展字段） |
| 因子预处理 | CSZScoreNorm（qlib 默认） | 社区 winsorize+zscore+neutralize | ⚠️ 缺去极值（问题 5） |
| 选股池 | 指数成分（未排 ST） | 社区显式过滤 ST/停牌/涨跌停 | ❌ 缺 ST 过滤（问题 2） |

## 二、发现的问题（按严重度）

### 🔴 P0-1：真实规则层未排除 ST 股票（不符合股市规则）
- 社区（Multi-Factor 框架）策略显式 `is_st_stock()` 过滤 ST
- 本项目 universe=指数成分（沪深300 极少含 ST，但中证500 有），qlib 简化层与 rqalpha 层均无 ST 排除
- ST 股有退市风险、涨跌幅 5%、机构禁投——纳入组合违反常规投资规则
- 影响：样本外选股可能买入 ST（中证500 场景概率更高）；历史回测数字被 ST 股污染

### 🔴 P0-2：简化层未开启 only_tradable（静默持有不可交易标的）
- 官方 TopkDropoutStrategy 参数 `only_tradable=False`（默认）：买卖决策不检查停牌/涨跌停可交易性
- 本项目 yaml 未配置该参数 → 简化层回测可能"持有"停牌股/涨停买不到的单
- 真实规则层有撮合兜底，但两层口径不一致（P1-3 换手对比被进一步污染）
- 修复：yaml 加 `only_tradable: true`

### 🟡 P1-1：报告无基准（benchmark）对比（影响结果解读）
- 本项目 report.html 只有绝对收益/Sharpe，无"同期沪深300 涨幅/超额"
- 社区与官方惯例：回测必须报超额（benchmark 对照）
- 修复：report 增加 benchmark 收益与超额列（数据已有 sh000300 CSV）

### 🟡 P1-2：滑点自研实现（语义差异）
- 官方 PriceRatioSlippage：price±price×rate，且**夹在涨跌停价内**
- 本项目 slippage_price 无涨跌停夹取（默认 0 时无影响；启用滑点后限价单可能超涨停被官方 validator 拒单——不算错误但行为依赖官方拒单兜底）
- 修复建议：改用官方 sys_simulation.slippage 配置（删除自研滑点分支），或自研版补涨跌停夹取

### 🟡 P1-3：参与率自研实现（语义差异）
- 官方 matcher：`volume_limit = bar_volume×percent - 当日该股已成交累计`（跨订单累计）
- 本项目：下单前按单笔 cap（不跨订单累计）
- 修复建议：启用官方 `sys_simulation.volume_limit/volume_percent`，删除自研 cap

### 🟡 P1-4：简化层与真实规则层换手口径仍未对齐
- 官方 TopkDropout：日频 n_drop 替换（换手 2×5/50=20%/日）
- 本项目月度 rank-buffer（top50+buffer10）：月频，换手远低于日频
- OPTIMIZATION.md P1-3 的"对齐"实为近似：rank-buffer 只是降换手手段，不是官方机制
- 影响：简化层 IC/超额与真实规则层数字的映射关系仍然模糊（zz500 归因问题未彻底解决）
- 修复建议：文档明示两层调仓频率差异；若需严格对齐需日频调仓（工作量中）

### 🟡 P1-5：无去极值（winsorize）
- 社区惯例：因子处理 = 去极值(MAD/分位) + 标准化 + 中性化
- qlib Alpha158 默认 CSZScoreNorm 只做 zscore（对极端值敏感），无去极值
- 修复建议：qlib handler 加 winsorize 处理器（官方有 RobustZScoreNorm/可自定义）

### 🟢 P2：待验证/低优先
- baostock turn 单位与新浪 turnover×100 的一致性（baostock 服务恢复后对照验证，若不一致需统一）
- 社区用 rqdatac（米筐商业数据），本项目免费源——无问题，仅记录
- 社区策略用 scheduler 周频调仓——本项目月频为设计选择，无问题

## 三、结论

1. **费用/涨跌停/T+1/停牌/100股**：与官方引擎一致，无静默缺失（重点排查项已逐一确认源码）
2. **3 个影响结果的真实问题**：ST 未过滤（P0-1）、简化层 only_tradable 未开（P0-2）、报告无 benchmark 对比（P1-1）
3. **4 个完善项**：滑点/参与率改官方配置（P1-2/3）、两层换手口径文档化（P1-4）、去极值（P1-5）
4. 建议：先修 P0-1/P0-2/P1-1（低风险、直接影响规则合规与结果解读），再评估 P1-2~5
