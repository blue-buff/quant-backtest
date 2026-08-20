# 预测力实验日志（EXPERIMENTS）

> 目标：把模型样本外预测力从"投骰子"（RankIC ≈ 0.01）提升到"勉强可用"（RankIC ≥ 0.02、RankICIR ≥ 0.2、跨池跨行情稳定为正）。
> 铁律：**只看样本外测试段**（2025-01-01 ~ 2026-08-14，训练/验证不参与任何指标）；所有指标从 mlruns 与 pred.pkl 原始产物读取，可被第三方复查。
> 每次尝试：yaml 变更 → git 提交 → run_exps 留痕（results/exps/<name>/）+ eval_pred 评估 JSON。

## 基线（要超越的对象）
- 数据 2023-01 起、RobustZScoreNorm、标签 1 日、单模型单种子、一次性训练（e4df9c8 时代）
- hs300 测试段：IC **-0.0065** / ICIR -0.041 / **RankIC +0.0136** / RankICIR +0.085
- 判定标准（调研报告）：IC<0.01 = 噪声；0.01~0.02 = 边缘；0.02~0.05 = 可用；ICIR>0.5 稳定

## 实验矩阵（第一轮）
| 编号 | 变量 | 内容 |
|---|---|---|
| e00 | 数据前移（唯一变量） | RobustZScoreNorm 不变，数据 2022-01-01 起 |
| e01 | 官方预处理 | learn=[DropnaLabel,CSZScoreNorm(label)]，infer=[ProcessInf,ZScoreNorm,Fillna] |
| e02/e03/e04 | 标签周期 | 官方预处理 + 未来 5/10/20 日收益标签 |
| e05 | 模型变体 | DART（drop_rate 0.1） |
| e06 | 超参 | lr 0.1 + num_leaves 128 + min_child_samples 100 + 轻正则 |
| e071/2/3 | 种子 | seed 100/200/300（多种子集成原料） |

## 结果登记（每次实验一行）

### 2026-08-20 第一轮

| 实验 | 池 | IC | ICIR | RankIC | RankICIR | train_l2 | valid_l2 | 备注 |
|---|---|---|---|---|---|---|---|---|
| （待跑） | | | | | | | | |
