# 提高模型预测能力的可操作方法 —— 深度调研报告

> 调研对象：本项目的技术栈（qlib 0.9.7 + LightGBM + Alpha158，A 股日频，沪深300 池）
> 调研方式：网络深度调研（微软 qlib 官方源码与 benchmark、LightGBM 官方文档、国金/华泰/西南证券研报、量化工程长文、IC 方法论资料），所有结论均附来源链接
> 日期：2026-08-20 ｜ 基线提交：e4df9c8

---

## 0. 结论先行（TL;DR）

当前模型 **RankIC ≈ 0.014、IC ≈ -0.007**，处于"噪声"区间（IC < 0.01 即噪声，见 [paperswithbacktest IC 判定表](#参考来源)）。对照 qlib 官方 benchmark，**LightGBM + Alpha158 + 沪深300 的官方成绩是 IC 0.0448 / RankIC 0.0469 / ICIR 0.366**（20 次运行均值，[qlib benchmarks README](#参考来源)）。差距主要不是"算法不行"，而是五个可修复的结构性原因：

| # | 差距 | 我们现状 | 官方 benchmark | 修复成本 |
|---|---|---|---|---|
| 1 | 训练窗口 | 18 个月（2023-01~2024-06） | **7 年**（2008-2014 训练、2015-16 验证、2017-20 测试） | 低（数据前移重拉） |
| 2 | 标签-调仓错配 | 标签=未来 1 日收益，调仓=月度 | 标签 1 日、日频滚动调仓（topk50 每日微调） | 低（改标签实验） |
| 3 | 预处理 | RobustZScoreNorm（MAD 去极值+标准化，特征与标签全部处理） | 官方默认：**CSZScoreNorm 只作用于标签** + DropnaLabel（[handler.py 源码](#参考来源)） | 低（改 yaml A/B） |
| 4 | 训练方式 | 一次性训练 | 多 seed 20 次取均值（std≈0 说明稳定） | 低（集成） |
| 5 | 样本量 | 300 股 × 18 个月 ≈ 13.5 万行 | 300 股 × 7 年（官方）；全 A 训练是券商标配（5000+ 股） | 中（全市场数据） |

**行动优先级**（详见第 2 节）：数据前移 → 预处理 A/B → 标签实验 → 滚动重训 → 多种子集成 → 中性化 → 排序学习。按此顺序，把 **RankIC 从 0.014 提到 0.03~0.04、ICIR 提到 0.3 以上**是现实目标；这是官方数据在"旧时段+长窗口"下的水平，2023-2026 这段近期行情下不应期望更高。

**诚实声明**：本报告所有建议是"提高预测能力"的方法，不等于"提高收益"。策略收益中 beta（市场涨跌）占比很大（我们总收益 +18.82% 而超额仅 +2.22%），alpha 提升是渐进过程；任何声称 IC > 0.1 的结果在宽流动性股票池上都应怀疑数据泄漏或过拟合（[microalphas](#参考来源)）。

---

## 1. 现状基线（与官方对照）

### 1.1 我们的最新基线（提交 e4df9c8 后）

| 层 | 指标 | 数值 |
|---|---|---|
| 模型信号 | IC / ICIR | **-0.0065 / -0.041** |
| 模型信号 | RankIC / RankICIR | **+0.0136 / +0.085** |
| qlib 简化层（top50 日频，无成本） | 年化超额 / IR | +2.58% / 0.25 |
| qlib 简化层（含成本） | 年化超额 / IR | -2.21% / -0.21 |
| rqalpha 真实规则层（月度调仓） | 总收益 / Sharpe | +18.82% / 0.87 |
| rqalpha 真实规则层 | 官方基准 / 超额 | +16.60% / **+2.22%** |

关键事实：
- 训练段 2023-01-01 ~ 2024-06-30（**18 个月**），验证 2024H2，测试 2025-01 ~ 2026-08（20 个月）
- 超参与官方 benchmark 完全一致（num_leaves=210、lr=0.2、max_depth=8、colsample=0.8879、subsample=0.8789、L1=205.7、L2=581.0）
- 预处理为 DropnaLabel + RobustZScoreNorm + DropnaProcessor（**这是我们与官方唯一的预处理差异之一**，改动后 IC 从 +0.0013 变为 -0.0065，实测为负）

### 1.2 qlib 官方 benchmark（同一算法、同一池、官方数据）

来源：[microsoft/qlib examples/benchmarks/README.md](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)（20 次运行均值±std）

**CSI300 / Alpha158**：

| 模型 | IC | ICIR | Rank IC | Rank ICIR | 年化收益 | IR | 最大回撤 |
|---|---|---|---|---|---|---|---|
| LightGBM | **0.0448** | **0.3660** | **0.0469** | **0.3877** | **9.01%** | **1.0164** | -10.38% |
| Linear | 0.0397 | 0.3000 | 0.0472 | 0.3531 | 6.92% | 0.9209 | -15.09% |
| XGBoost | 0.0498 | 0.3779 | 0.0505 | 0.4131 | 7.80% | 0.9070 | -11.68% |
| CatBoost | 0.0481 | 0.3366 | 0.0454 | 0.3311 | 7.65% | 0.8032 | -10.92% |
| DoubleEnsemble(基模型LGBM) | 0.0521 | 0.4223 | 0.0502 | 0.4117 | 11.58% | 1.3432 | -9.20% |
| LSTM(选20特征) | 0.0318 | 0.2367 | 0.0435 | 0.3389 | 3.81% | 0.5561 | -12.07% |
| GRU(选20特征) | 0.0315 | 0.2450 | 0.0428 | 0.3440 | 3.44% | 0.5160 | -10.17% |

**CSI500 / Alpha158**：LightGBM IC 0.0399 / ICIR 0.4065 / RankIC 0.0482 / RankICIR 0.5101 / 年化 12.84% / IR 1.565。

**结论**：① LightGBM 在官方数据上强于 Linear 但差距不大（IC 0.0448 vs 0.0397）——非线性收益有限，说明"特征工程+标签+训练协议"比模型选择更值钱；② 深度模型（LSTM/GRU）在官方数据上**不如** LightGBM，不需要急着上深度学习；③ 官方训练窗口 7 年是我们 18 个月的 4.7 倍——**这是最值得先补的短板**。

### 1.3 IC 多少算"能用"（判定标准）

| IC 区间 | 判定 | 策略含义 |
|---|---|---|
| > 0.10 | 罕见，先怀疑数据错误/泄漏 | 强独立信号 |
| 0.05 ~ 0.10 | 很好 | 核心 alpha 信号 |
| 0.02 ~ 0.05 | 好 | 多信号合成有用 |
| 0.01 ~ 0.02 | 边缘 | 只有高广度才值得交易 |
| < 0.01 | **噪声** | 不值得交易 |

来源：[paperswithbacktest/wiki IC 页](https://github.com/paperswithbacktest/wiki/blob/main/information-coefficient-signal-quality/page.md)、[microalphas.com IC 指南](https://microalphas.com/information-coefficient/)。
补充（microalphas）：0.02~0.07 是"有意义的"区间；**RankIC 是行业默认口径**（对异常值稳健）；ICIR = mean(IC)/std(IC)，**ICIR > 0.5 才算稳定**，年化 ×√252（日频）或 ×√12（月频）；IC 随预测周期自然衰减，要画 1/5/10/20 日的衰减曲线决定调仓频率；Grinold-Kahn 基本定律 IR ≈ IC×√BR——广度（独立下注数）与 IC 互补，加 100 只不相关股票的效果远大于把单票预测磨到小数点后四位。

---

## 2. 可操作方法（按 影响 × 成本 排序）

### P0 快赢（每个 1~3 天，先做这些）

#### P0-1 ★★★ 数据窗口前移：训练数据从 18 个月拉到 3 年+

- **做什么**：把新浪数据拉取起点从 2023-01-01 改到 **2022-01-01**（或 2021-06-01），重跑 fetch → dump → train。Alpha158 有大量 60 日滚动窗口特征，当前训练集前 ~2 个月被 Dropna 丢弃（正是审计报告建议 E）；且 18 个月训练样本几乎一定会过拟合到当期市场风格。
- **怎么做**：`qlib_scripts/fetch_sina.py` 顶部 `START, END = "2023-01-01", ...` 改为 `"2022-01-01"`（或加 `--start` 参数）；重跑 `qbt all --pool hs300 --model lgb`。
- **证据**：
  - 官方 benchmark 训练 2008-2014（**7 年**），[官方 yaml](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml)
  - [quant67《机器学习选股》](https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html) 坑十一："用 1~2 年数据训练几乎一定会过拟合到当期市场风格，A 股选股训练集底线是 5 年，10 年更稳"
- **预期**：训练样本 ×2.5；warmup 问题消失；RankIC 有望从 0.014 回到 0.02+ 量级（前提：2022 年熊市样本不会把模型带偏——这正是要做的"时段稳健性"检验）。
- **风险**：2022 年行情特征与 2025-26 差异大，滚动验证（P0-4）是配套手段。

#### P0-2 ★★★ 预处理 A/B：恢复官方默认的"标签 CSZScoreNorm"，与 RobustZScoreNorm 对照

- **做什么**：官方 Alpha158 默认 learn_processors 是 `[DropnaLabel, CSZScoreNorm(fields_group="label")]`（[handler.py 源码](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/handler.py)），即**只对标签做截面 z-score，特征不动**（官方 infer 用 ZScoreNorm+Fillna）。我们改成 RobustZScoreNorm 后，**IC 实测从 +0.0013 掉到 -0.0065**。两种方案各跑一次测试期，按 RankIC/ICIR 定夺。
- **怎么做**：复制 yaml 为 `lightgbm_alpha158_csz.yaml`，learn_processors 改回 `[DropnaLabel, CSZScoreNorm(fields_group: label), DropnaProcessor]`；infer_processors 用 `[ZScoreNorm, DropnaProcessor]`（与官方 infer 对齐）。用 mlflow 记录两个实验，对比测试期 RankIC/ICIR/超额。
- **证据**：
  - 官方默认配置（源码级证据，非记忆）
  - [国金证券《机器学习全流程重构》（2024-03-29）](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/765007906021/index.phtml)：特征与标签的预处理方式对截面/时序模型影响不同，**截面模型适合用整个训练集 ZScore 保留日期间相对大小**
  - quant67 坑二/坑三：全局 z-score 或全局 fillna 会把未来信息融进训练数据（我们的 RobustZScoreNorm 拟合窗口是 fit 段，无泄漏问题，但 MAD 去极值可能把标签尾部信息压平）
- **预期**：这是"回到官方默认"的低风险修正；如果 CSZScoreNorm 版 RankIC 反超，就永久切换并写进 README。
- **风险**：无（A/B 留档即可）。

#### P0-3 ★★★ 标签与调仓周期对齐：1/5/10/20 日标签实验 + IC 衰减曲线

- **做什么**：我们月度调仓，但标签是未来 1 日收益——**预测周期与持有周期错配**（信号在第 1 天就衰减完，剩下 19 天在裸奔）。做 {1,5,10,20} 四组标签（均用后复权价，如 20 日标签 = `Ref($close, -21)/Ref($close, -1) - 1`），画 IC/IR 随 horizon 的衰减曲线，按月频调仓的成本-收益取最优。
- **怎么做**：yaml 的 handler kwargs 加 `label: ["Ref($close, -6)/Ref($close, -1) - 1"]`（5 日）等四组；训练后从 mlruns 读 IC 对比；同时输出各标签方案的换手率（rqalpha 报告已有）。
- **证据**：
  - [quant67](https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html)："H=1 日标签噪声极大（单日收益信噪比 <0.1）；A 股最常用 H=5；月度持仓用 H=20；经验法则先在 {5,10,20} 上各跑一次看 IC 衰减，挑衰减最慢的"
  - [microalphas](https://microalphas.com/information-coefficient/)：horizon effect——IC 自然随周期衰减，衰减曲线决定最优调仓频率
  - 西南证券排序学习报告（见 P1-7）：月度/更长持仓下排序目标优于 1 日 MSE
- **预期**：月频调仓下 10~20 日标签的 RankICIR 通常显著高于 1 日标签，且换手率大降（成本吃掉我们 4.8pct/年：无成本超额 +2.58% vs 含成本 -2.21%）。
- **风险**：长标签的"等价独立样本"少一个数量级（quant67），需配合 P0-1 拉长数据。

#### P0-4 ★★★ 滚动（walk-forward）重训：从"一次性训练"到"每季度重训"

- **做什么**：当前是一次性训练（2023-01~2024-06 训完就用 20 个月）。改为滚动：每次用"截至 T 的全部数据"训练，预测 T 之后一个月，T 步进一个月（或一季度），全程 walk-forward 回放。
- **怎么做**：qlib 官方有现成参考——[examples/model_rolling/task_manager_rolling.py](https://github.com/microsoft/qlib/blob/main/examples/model_rolling/task_manager_rolling.py)（RollingGen 生成滚动任务段）+ [examples/online_srv/rolling_online_management.py](https://github.com/microsoft/qlib/blob/main/examples/online_srv/rolling_online_management.py)。qbt 层面：给 `train.py` 加 `--fit-end` 步进循环，或在 plan/backtest 前按窗口循环调用。
- **证据**：
  - qlib 官方 rolling 示例（源码）
  - [国金证券](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/765007906021/index.phtml)："一次性 vs 滚动 vs 扩展训练对比：选取合适的样本区间能使模型更能适应不同的市场环境"
  - [quant67](https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html)：重训练频率（周/月/季按策略周期），重训前做预测分布对齐校验
- **预期**：消除"模型训练截止 2024-06 却预测到 2026-08"的时滞；2025-26 行情变化快，滚动训练通常带来实质 RankIC 提升。
- **风险**：训练次数 ×12，LightGBM 分钟级训练可接受；注意每个滚动窗口都写独立 mlflow run（train.py 已支持 experiment_name 隔离）。

#### P0-5 ★★ 多种子集成：5~10 个 seed 平均预测

- **做什么**：同一 yaml 用不同随机种子（LightGBM 的 `seed`/`bagging_seed`/`feature_fraction_seed`）训练 5~10 个模型，预测取均值（或 rank 平均）再进 plan。
- **怎么做**：train.py 加 `--seeds 5,13,42,77,123` 循环；plan 层读多 run 预测做 rank 平均（planlib 已有 lineage 机制，扩展成"多 run 平均"即可）。
- **证据**：[quant67](https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html)"多种子集成：同一份代码同一份数据 5~10 个不同 seed 取预测均值，**IC 提升 5%~15%**，性价比最高"；qlib 官方表"20 次运行均值±std"正是这个做法（std≈0 说明稳定）。
- **预期**：RankIC +0.005~0.01 量级的稳定提升；几乎零成本。
- **风险**：无。

### P1 主力提升（每个 1~2 周）

#### P1-6 ★★★ 行业 + 市值中性化（补齐"风格剥离"）

- **做什么**：当前特征和标签都没有做行业/市值中性化，模型学到的大量"预测力"可能是风格暴露（大盘 vs 小盘、行业 beta）。做法：每个截面（每天）把特征（或预测/标签）对"申万一级行业哑变量 + log 市值"做 OLS 回归取残差。
- **怎么做**：数据层需要补两张表——① 行业分类（akshare 有 `ak.stock_industry_category_cninfo` 或新浪行业，免费；注意用 **PIT/时点分类**，quant67 坑十四：行业分类会调整，训练不能用未来分类）；② 总市值 = close × 总股本（需股本快照数据，akshare 有，或 tushare pro 更规范）。然后 qlib 侧可用自定义 processor 或在特征集里加行业/市值特征让树自己学——**但"中性化"要求显式回归取残差**，建议在 dump 前加工成独立字段 + 一个 CSNeutralize 自定义 processor。
- **证据**：
  - [华泰人工智能系列之二十八（2020-02-19）](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/635436933247/index.phtml)：合成因子做"行业、市值、20 日收益率、20 日波动率、20 日换手率"五因子中性化后 **RankIC 8.87%、IC_IR 1.16**，TOP 组合年化超额 9.65%、IR 3.08——中性化是 A 股因子工程基本动作
  - [quant67](https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html) 特征工程章节：行业市值中性公式与理由（风格暴露伪装因子有效性；GBDT 对条件期望偏差敏感）
  - 国金证券：全 A vs 成分股训练与基准相关（中性化后不同池的可比性更好）
- **预期**：剥离风格后 RankIC 可能先降（暴露被去掉），但 ICIR/稳定性与样本外可复现性上升；叠加到组合层后超额更干净。
- **风险**：行业/市值数据源新增（akshare 免费可拉）；PIT 处理要认真。

#### P1-7 ★★★ 排序学习（LambdaRank）：把"回归收益率"改成"优化截面排序"

- **做什么**：LightGBM 原生支持 `objective: lambdarank`，以"每个交易日一个 group"训练，直接优化 NDCG@K（K=持仓数）。西南证券实证：**相同因子库下，LambdaRank 比 MSE 回归夏普 +66%（1.86 vs 1.12）、换手率 -46%**。
- **怎么做**：qlib 的 `LGBModel`（qlib/contrib/model/gbdt.py）不原生支持 rank 目标（loss 参数映射的是回归/分类），需要写一个小的自定义模型包装（继承 LGBModel 重写 fit，group=每日截面；或直接用 `lgb.rank_train`/`lgb.train(objective='lambdarank')` 自行训练并把预测写成 qlib 信号）。标签用 10/20 日收益（与月频调仓对齐）；评估指标加 NDCG@10/50。
- **证据**：[西南证券《基于排序学习的选股增强策略》（中证网 2025-12-01）](https://cs.com.cn/qs/202512/t20251201_6526009.html)：LambdaRank vs MSE，夏普 1.86 vs 1.12（+66%），换手率 -46%；quant67"多分类 vs 二分类 vs 回归 vs 排序"表：工业界更常用"二分类+Top/Bottom 截取"与"Rank 回归"，LightGBM lambdarank 对截面排序优化很自然。
- **预期**：对"月频选 Top50 多头"这类任务，这是**目标函数层面的对齐**，比调超参更本质。
- **风险**：需少量自定义代码；qlib 生态外训练/推理一致性要自己保证（quant67 坑：特征顺序、预处理一致）。

#### P1-8 ★★ 特征精选 + SHAP 归因：从 158 特征到 top 20~50

- **做什么**：先跑一版 LightGBM 特征重要性（gain），保留 top 20~50 再训练；同时用 SHAP 看每个特征对预测的贡献方向是否稳定（跨滚动窗口 top-10 一致性）。
- **怎么做**：train.py 加 `--topk-features 50`（从已训练模型读 `model.feature_importance("gain")` 生成新 yaml 的 `FilterCol` processor 或直接建 Alpha158 子集）；SHAP 用 `shap.TreeExplainer`（容器已装 shap？未装则 pip install）。
- **证据**：qlib 官方 benchmark 注记"LSTM/GRU 用的是 **LightGBM 特征重要性选出的 20 个特征**"（README 表格下方注）；quant67：因子相关性过高会让树在多个相关因子间随机切换分裂，解释性下降；坑四：IC 由中间分位贡献时 top/bottom 分位可能完全没超额——**要同时看分层收益**。
- **预期**：噪声特征减少 → 过拟合下降 → 样本外 ICIR 提升；SHAP 让你知道模型到底在"看什么"（为后续因子研究提供方向）。
- **风险**：特征选择本身是多重检验的一部分，必须只基于训练段做选择（禁止用测试段选特征）。

#### P1-9 ★★ 超参数与模型变体：DART、叶子数、早停、样本权重

- **做什么**（每个都做一次 A/B，只动一个变量）：
  1. `boosting_type: dart`（DART = Dropout 的 GBDT）：国金实测 DART 超过 GBDT，缓解过拟合；
  2. `num_leaves` 210 → 64~128，加 `min_child_samples: 200~500`：我们 13.5 万行训练样本、等价独立样本更少（quant67 估计 A 股 10 年面板等价独立样本仅 20~50 万），官方 210 叶子的配置是为 7 年数据准备的；LightGBM 官方调参指南明确 num_leaves 是复杂度主控、leaf-wise 容易过拟合；
  3. 早停：确认 LGBModel 已用 valid 集早停（官方 LGBModel 默认支持 early stopping，检查 train.py 传入的 `early_stopping_rounds`）；
  4. 样本权重：按 `|ret|` 或波动率倒数给样本加权（高波动期信号更强；de Prado uniqueness weight 降低重叠样本权重）——收益不确定，先小实验。
- **证据**：[国金证券](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/765007906021/index.phtml)（DART>GBDT）；[LightGBM Parameters Tuning 官方文档](https://lightgbm.readthedocs.io/en/latest/Parameters-Tuning.html)（leaf-wise 过拟合、num_leaves/min_data_in_leaf）；[quant67](https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html)（参数建议 num_leaves 63/min_child_samples 200；样本权重；模型复杂度与等价样本量匹配表：10^5~10^6 → num_leaves 64~256）。
- **预期**：DART 与更浅树在短窗口下通常更稳；样本权重可能小幅提升 IC 或无效，留档即可。
- **风险**：超参搜索有"研究过拟合"风险（de Prado：尝试 N 个策略最优期望夏普被高估 σ√(2lnN)），**每次实验前先写好假设，用滚动验证集评估，禁止反复在同一测试段上挑选**。

#### P1-10 ★★ 评估与检验体系落地（与"提升"配套的"证明"）

- **做什么**：把审计建议 F、G 落地到报告流水线：
  1. **分层单调性检验**（F）：预测分 10 层，输出每层月度收益表 + 单调性（Spearman 分层收益 vs 层号）；alphalens 的思路，自实现即可（不用装 alphalens，30 行以内）；
  2. **IC bootstrap 置信区间**（G）：`scripts/analysis_bootstrap.py` 已就绪，对测试期 RankIC 序列跑 10k 次 bootstrap，输出 95% CI 与 p 值；
  3. **IC 衰减曲线**：1/5/10/20 日 horizon 的 IC 表（与 P0-3 共用）；
  4. **换手与成本敏感性**：qbt.yaml 扫描 slippage {0, 0.1%, 0.3%} × participation {0.05, 0.2}，输出超额对成本的敏感度表。
- **证据**：[alphalens](https://github.com/quantrocket-llc/alphalens)（分位收益是因子检验标配）；quant67 坑四（IC 好但分位收益差是常见陷阱）；microalphas（ICIR、衰减、TC：多头约束下 TC 可能只有 0.4~0.6，实盘 IR = TC×IC×√BR）。
- **预期**：让"提升"有统计依据，而不是看单个数字讲故事。

### P2 进阶（1 个月+，按需投入）

#### P2-11 全市场训练（5000+ 股）再映射回组合
- 样本量 ×10~20（300 → 5000 股），GBDT 在 10^5~10^7 独立样本区间最稳（quant67 表）；国金：全 A vs 成分股与基准和模型特性相关，需要分情况测试；实现：新浪全市场日线批量拉取（约 5000 只 × 双接口，分片+断点续传，估计 1~2 天可拉完），dump 后训练池改全 A、回测池仍用 300/500。
- 注意：全市场要处理 ST、停牌、上市 < 60 日、退市（生存者偏差），我们的 ST 过滤/only_tradable 已具备（P0 修复），标签侧也要对齐（quant67：停牌/退市口径要与回测引擎一致）。

#### P2-12 分钟/高频因子
- 光大证券"成交量占比"高频因子、华泰"全频段量价"系列：分钟数据能提供独立于日频的 alpha，但数据获取（akshare 分钟接口历史长度有限，tushare pro 需积分）与存储成本高。先做"选 20~50 只股票试点验证增量"再铺开。

#### P2-13 深度模型（GRU/LSTM/Transformer on top20 特征）
- 官方数据上 LGBM（IC 0.0448）明显强于 LSTM（0.0318）/GRU（0.0315），**不建议优先**；若做，用官方 top20 特征方案，且必须有 purged/embargo 验证协议（quant67：神经网络对训练协议和正则化要求更高）。

#### P2-14 另类数据（分析师预期、北向、融资融券、龙虎榜、舆情）
- 与量价因子相关性低，是组合层面最值钱的增量（华泰：GP 挖掘的因子能提供独立于传统因子的增量超额）。数据源：tushare pro（需申请 token 与积分）、akshare 部分免费接口。**API Key 需求**：若走 tushare，需要 `tushare pro token`（日线/股本/行业/分析师/北向接口，基础积分即可覆盖大部分）。

---

## 3. 30 天行动路线图

| 周 | 动作 | 产出/验收 |
|---|---|---|
| W1 | P0-1 数据前移重拉（2022-01 起）+ 全链路重跑；P0-5 多种子集成 | 新基线报告：RankIC/ICIR/超额 vs 旧基线对比表 |
| W2 | P0-2 预处理 A/B；P0-3 标签 {1,5,10,20} 实验 | 预处理决策 + IC 衰减曲线表，写入 docs |
| W3 | P0-4 滚动重训（季度步进）；P1-10 分层单调性 + bootstrap 落地 | 滚动验证 RankIC 序列图；报告新增 F/G 小节 |
| W4 | P1-6 行业/市值数据补全 + 中性化实验；或 P1-7 LambdaRank 原型 | 中性化 vs 非中性化 A/B；或排序学习原型对照 |

**纪律**：每周只改一个变量；所有实验进 mlflow（train.py 已按 experiment_name 隔离）；报告页（docs/report.html）加"实验记录"表；连续 3 个实验无改进就回到上一版，避免在测试段上反复挑选。

---

## 4. 预期与诚实声明

1. **现实目标**：RankIC 0.014 → 0.03~0.04、RankICIR 0.09 → 0.3+、月频调仓含成本超额 +2.22% → +5~8%/年。这需要 P0 五项全部落地；单项平均贡献 RankIC +0.003~0.01。
2. **官方 0.0448 的语境**：2008-2020 数据、7 年训练、日频调仓、无真实费用约束的简化层；我们的 2023-2026 近期数据 + 月度调仓 + 真实规则层，不能直接对标。若 2025-26 是风格剧烈切换期（微盘流动性危机、急涨急跌），同期 IC 普遍低于历史均值。
3. **IC > 0.1 要警惕**：宽流动性股票池上稳定 IC > 0.1 大概率是数据泄漏/未中性化风格暴露/过拟合（microalphas）。
4. **收益 ≠ 预测力**：当前总收益 +18.82% 主要是 beta（基准 +16.60%）。提升预测力后，超额的正贡献才会显现。
5. **本报告所有建议均可在当前代码库落地**（qbt 的 fetch/dump/train/plan/backtest/report 六步管线已具备 A/B 与 mlflow 留档基础），不需要换技术栈。

---

## 参考来源

**官方/源码**
1. qlib benchmarks 结果表：https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md
2. qlib LightGBM Alpha158 官方 yaml（超参与分段）：https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml
3. qlib 滚动训练示例（RollingGen/TaskManager）：https://github.com/microsoft/qlib/blob/main/examples/model_rolling/task_manager_rolling.py
4. qlib 在线滚动预测示例：https://github.com/microsoft/qlib/blob/main/examples/online_srv/rolling_online_management.py
5. qlib Alpha158 源码（label=Ref($close,-2)/Ref($close,-1)-1，默认处理器 CSZScoreNorm(label)）：https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/handler.py
6. qlib processor 源码（CSZScoreNorm/CSRankNorm/RobustZScoreNorm）：https://github.com/microsoft/qlib/blob/main/qlib/data/dataset/processor.py
7. LightGBM 调参官方文档：https://lightgbm.readthedocs.io/en/latest/Parameters-Tuning.html

**方法论**
8. paperswithbacktest IC 判定与基本定律：https://github.com/paperswithbacktest/wiki/blob/main/information-coefficient-signal-quality/page.md
9. microalphas IC 指南（RankIC/ICIR/衰减/TC）：https://microalphas.com/information-coefficient/
10. quant67《机器学习选股：标签构造、防过拟合、SHAP 归因》：https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html
11. López de Prado, Advances in Financial Machine Learning (Wiley, 2018) —— 标签/样本权重/金融交叉验证/回测过拟合（Purged K-Fold、Embargo、CPCV、Deflated Sharpe 出处）
12. Bailey, Borwein, López de Prado, Zhu, "Pseudo-Mathematics and Financial Charlatanism" (Notices of the AMS, 2014) —— 回测过拟合概率上界

**券商研报**
13. 国金证券《ALPHA掘金系列之十：机器学习全流程重构》（2024-03-29，高智威/王小康）：https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/765007906021/index.phtml
14. 华泰证券《人工智能系列之二十八：基于量价的人工智能选股体系概览》（2020-02-19，林晓明等）：https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/635436933247/index.phtml
15. 西南证券《基于排序学习的选股增强策略》（中证网 2025-12-01）：https://cs.com.cn/qs/202512/t20251201_6526009.html
16. 光大证券 多因子系列《见微知著：成交量占比高频因子》：https://mf.bigquant.com/wiki/doc/61etdmb4NI

**工具**
17. alphalens（分层收益/分位单调性检验）：https://github.com/quantrocket-llc/alphalens
18. akshare（行业/股本/北向等免费数据）：https://akshare.akfamily.xyz/
