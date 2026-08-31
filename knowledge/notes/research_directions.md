# 后续研究方向（专家经验 + 链接审读）

> 生成：2026-08-27。输入：《量化研究指标清单 × 专家经验手册》（用户整理）。
> 链接读取方式：x.com 原文需登录、无法匿名直读；已用 web_search 交叉核验——
> López de Prado《Causal Factor Investing》剑桥专著与 ADIA Lab "Factor Mirage" 协议、
> Ernest Chan 方法论综述、pysystemtrade / qstrader 两个仓库确认存在且用途一致。
> 逐条观点以你给的摘要为权威解读，下面只做"对我平台"的转译与排序。

## 0. 一句话结论
预测层显著 ≠ 交易层赚钱。把主判据从 RankIC 切到「净超额」，
把 80% 精力从"造更复杂模型"移到「信号 → 组合 → 执行 → 稳健」这条链的
翻译质量与证伪纪律上。

## 1. 逐条来源 → 落地（简表）

| 来源 | 核心观点 | 对我平台的落地 |
|---|---|---|
| López de Prado（研究≠回测） | 回测只是证据之一；预注册 expectation、记录失败实验 | 已有 expectation 对照 + 失败入账；缺口：hypothesis/expectation 强制化 |
| pepper（实盘清单） | Sharpe>5≈过拟合；跨熊市 + walk-forward；容量>收益；alpha/beta 分离；参数平台 | 主判据换 net excess；每交易实验必报 turnover + cost_drag；参数平台检验 |
| Goshawk（13 条） | edge 讲不清就没有；过程>结果；组合是一盘生意；平庸部署 > 10 个漂亮回测 | 选一个简单 baseline 长期跑（第 11/13 条优先） |
| 豪仔（机器挖因子） | 归纳知道 what 不知 why；regime 切换失效难归因 | 每个因子回答 4 问（机制 / 对手方 / 未定价原因 / 失效条件） |
| López de Prado（Factor Mirage） | 相关稳定仍可能是假因子（collider/confounder/specification） | advisory review 加因果 5 问 |
| Aporia（AI 加速犯错） | AI 别替代判断力摩擦；统计交给确定性引擎 | 平台已符合：agent 提 spec、固定测试器算数；AI 解释标 advisory ≠ evidence |
| Ernest Chan（稳健验证） | 验证稳健区间，不做数学优化 | top_k / buffer / rebalance / lag 做平台检验，找稳定区间非孤峰 |
| Rob Carver（pysystemtrade） | 波动率目标 / 风险预算 / 执行解耦的完整参考 | 组合风险层系统化（下一步缺的不是模型） |
| qstrader | 事件驱动回测引擎参考 | 执行层设计参考（T+1 / 滑点 / 成本模块） |

## 2. 汇总：后续研究方向（按优先级）

- **R1 交易层主判据（P0，证据口径）**：净超额（excess_ann）为主判据；
  执行延迟对照（T 日/T+1/T+2 close）；label 边界 purge/embargo。
  本次已落地：backtest 新增 execution_lag 三档对照、cost_alpha_ratio、
  alpha_ann（CAPM）、总收益/季报口径。
- **R2 组合/风险层系统化（P1 降换手 + Carver 参考）**：rank buffer、
  weekly/monthly rebalance、top_k 参数平台；组合层 one-way turnover /
  active share / 集中度 / 调仓频率本次已可测。长期引入波动率目标与风险预算
  （参考 pysystemtrade 的 forecast 组合、position sizing、risk overlay）。
- **R3 因果归因（每因子 4 问 + advisory 5 问）**：把"为什么有效"写进
  hypothesis；advisory review 增加 confounder / collider / 因子代理检查。
  与 Factor Mirage 协议对齐：相关性稳定不构成因果。
- **R4 稳健性验证（P2）**：不同 seed、不同 non-overlap offset（本次已实现
  nonoverlap_offsets / nonoverlap_min_rank_ic）、分季度（已实现 quarterly_returns）、
  参数平台、未来 paper trading。
- **R5 统计升级（P3）**：PSR（本次已实现）、DSR / PBO / CPCV、purge/embargo。
  见 dev_plan_trade_p1p3.md 与 won't-do 冲突标注。
- **R6 部署一个平庸策略长期跑（Goshawk 第 11/13 条）**：选一个最简单可解释
  baseline（如等权 topK + buffer），固定规则长期模拟，记录真实漂移；
  这比继续堆复杂模型更接近"把研究做成生意"。

## 3. 与项目现行规则的冲突（需你裁决）

- AGENTS.md won't-do 明确列了 **deflated Sharpe、walk-forward 定期任务、
  dev/holdout 拆考场、分块 bootstrap**；但你的新文档 P3 要求 PSR/DSR/PBO/CPCV、
  P2 要求 walk-forward / paper trading。
- 本次按「最新指令优先」处理：PSR 已实现进 tester；DSR / PBO / CPCV /
  walk-forward 写入 P3 计划（不直接实现），并在此标注与 won't-do 冲突，待你确认是否解冻。
