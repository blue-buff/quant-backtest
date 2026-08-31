# Cleanroom 合规留痕（pysystemtrade → QLab）

> 目的：记录 cleanroom 两阶段的隔离，作为"独立实现非 GPL 派生"的合规证据。
> 原则：阶段 1 读码者产出 spec 后，阶段 2 实现者不得再接触 pysystemtrade 源码。

## 阶段 1：读码者产出 spec（已完成）

- **日期**：2026-08-31
- **读码者**：DeepSeek Harness agent（本会话）
- **读了什么**：`/Users/lucas/projects/quant_refs/pysystemtrade/`（commit b4a25e6，sparse
  checkout）的 `systems/`（forecast_scale_cap / forecast_combine / positionsizing /
  buffering / risk_overlay / risk / portfolio / trading_rules）、`systems/accounts/account_costs.py`、
  `sysquant/estimators/`（vol / correlations / diversification_multipliers / forecast_scalar）、
  `systems/provided/`（futures_chapter15 / rob_system）。
- **产出 spec（两份，即实现阶段唯一依据）**：
  - `docs/pysystemtrade_borrow_report.md`（8 组件精炼版）
  - `knowledge/notes/pysystemtrade_borrow_report.md`（12 组件详版）
  - spec 只含：公式、接口签名、参数默认值、测试向量（如 EDOLLAR doctest 数值、
    rob_system/config.yaml 的 percentage_vol_target=25 / notional=500000）。

## 阶段 2：实现者独立实现

- **实现者承诺**：
  - 只读上述两份 spec，**不 open `/Users/lucas/projects/quant_refs/pysystemtrade/` 任何源文件**；
  - 代码表达（命名 / 结构 / 注释 / 逐行逻辑）独立创作，不逐字复刻；
  - 用 spec 里的数值契约对拍结果验证正确性，不看源码。

### 已执行（2026-08-31 00:46 CST / Codex /root）

- 实现者声明：本会话（Codex /root）在编写 `executors/systems_topk/impl/*` 及
  `executors/systems_topk/main.py` 的 GPL 对应算法期间，未打开、grep、列目录或以任何
  方式读取 `/Users/lucas/projects/quant_refs/pysystemtrade/` 源文件；仅读取了用户指定的
  两份 cleanroom spec、主重构计划与 QLab 本地代码/测试。
- 代码落点：`executors/systems_topk/impl/`（分数缩放/封顶、滚动波动率与 vol-inverse
  sizing、全量目标向量、band、成交可行性）；MIT 部分另在 `vendor/qstrader/` 保留原码。
- 验证：cleanroom 基元单测 + 执行器契约端到端 smoke（`tests/test_systems_topk.py`）；
  数值检查包括 forecast scalar、±cap、预算/单票上限、加权输出、清零、band 和成交约束。

## 涉及代码落点

- `executors/systems_topk/impl/`：pysystemtrade 算法的 cleanroom 独立实现。
- `executors/systems_topk/vendor/qstrader/`：MIT 直接 copy（无传染，保留 LICENSE）。

## 时间线

| 时间 | 事件 |
|---|---|
| 2026-08-31 | 阶段 1 完成：读源码 → 产出两份 spec |
| （未开始） | 阶段 2：cleanroom 独立实现（记录启动时间与执行 agent） |
