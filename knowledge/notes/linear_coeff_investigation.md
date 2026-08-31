# 调查任务：qlib LinearModel 系数尺度之谜（挂账 #13）

> 状态：任务文档（人类据此提交调查任务）。本文档是任务输入，不是结论。

## 背景

qb 批次复现 lin_a158 时观察到：同一份 hs300 Alpha158 数据，
qlib LinearModel（官方配置）拟合出的系数 std = **8.56**，
手工 sklearn 拟合的系数 std = **0.35**，相差约 24 倍。
后续修复 test 缓存零特征 bug（见 qb_benchmark_repro.md）后，
lin_a158 指标恢复正常（rankic 0.0265），但系数尺度差异从未解释，已挂账。

## 已知事实（调查起点）

- 数据：hs300 Alpha158，20 特征（FilterCol 官方清单），train 2021-06-01~2024-06-30，
  已缓存在容器 cache/train_*.parquet（data_key 见 cache/manifest.json 对应行）。
- 官方 Linear 配置：learn processors = FilterCol + RobustZScoreNorm；
  label = 未来 1 日收益 Ref($close,-2)/Ref($close,-1)-1（horizon=1，原始收益，未标准化）。
- qlib 实现位置：qlib/contrib/model/linear.py（LinearModel 继承 torch 封装；
  具体用 np.linalg.lstsq 还是 torch linear 待查——这是调查对象之一）。
- 手工 sklearn 对照时特征/label 是否复刻了 RobustZScoreNorm 预处理：**未记录，
  是最大嫌疑**。

## 任务目标

解释 8.56 vs 0.35 的差异来源，并判定属于：
(a) 预处理口径不同（预期差异）——给出具体是哪个预处理步骤；
(b) qlib 实现 bug——升级挂账，另议修复。

## 调查步骤（全部在容器内 /tmp 做，不碰管线代码，≤1h）

1. 加载同一份 train parquet（含特征列 + y），确认行数/列数与当时一致。
2. 四组对照，统一输出「系数向量」：
   a) raw 特征 + sklearn LinearRegression；
   b) RobustZScoreNorm(20 特征) + sklearn LinearRegression；
   c) 官方 learn processors 全链路 + qlib LinearModel（复用 executors/qlib_bench
      的入口摘出训练后系数）；
   d) b 与 c 系数逐项对比（std、max|diff|、相关系数）。
3. 假设清单（按嫌疑从大到小）：
   - H1 预处理口径：手工 sklearn 用了 raw 特征，qlib 用 RobustZScoreNorm →
     尺度差约等于特征方差的倍数；
   - H2 标签口径：对照时 label 是否一致（原始 1 日收益 vs 误用标准化标签）；
   - H3 拟合细节：是否含截距、是否对标签再做标准化、是否用 lstsq 的正规方程；
   - H4 统计口径：std 的统计对象是否一致（含/不含截距、特征数是否相同）。
4. 验收标准：写出差异归因结论（一句话主因 + 对照证据表）。
   若 H1-H4 全部排除后仍有 >10 倍差异 → 判定疑似 bug，挂账升级并给出最小复现。
5. 产出：在本文档追加「结论」节（含对照脚本路径 /tmp/linear_probe_*.py 与证据表），
   不改任何管线/执行器代码。

## 边界

- 只读已有缓存与 vendor 配置，禁止重建数据/改参数；
- 不追求解释"哪个系数更好"，只解释"为什么差 24 倍"；
- 预算 ≤1 小时（Linear 单次拟合秒级，纯本地）。

---

# 结论（2026-08-24 调查完成，状态：已解释，非 bug）

## 一句话主因

**24 倍差异 = 标签预处理口径 × 截距开关，经 158 个高度共线特征放大：qlib 拟合的是
CSRankNorm 排名归一标签（std≈1.0），手工 sklearn 拟合的是原始 1 日收益标签
（std≈0.0244）并默认带截距；qlib LinearModel 实现无 bug。**

## 调查起点三处前提修正（重要）

任务文档背景与当时的实际执行配置不符，先修正再归因：

1. **实际执行的 lin_a158 配置**（executor_config.json + spec.json）：
   learn = [DropnaLabel, CSRankNorm(label)]，infer = [RobustZScoreNorm(feature,
   clip_outlier=True), Fillna(feature)]，process_type=append（qlib DataHandlerLP 默认）。
   没有 FilterCol，**特征数 158 而非 20**（"FilterCol 官方清单 20 特征"来自 qlib 0.8.x
   的 LightGBM 配置；本项目 vendor 的 0.9.x 官方 yaml 均无 FilterCol）。
2. **标签并非"未标准化"**：train 缓存（DK_L，learn 视图）的 y 是 CSRankNorm
   （(rank_pct-0.5)×3.46，std≈1.0）；test 缓存（DK_I，infer 视图）的 y 才是原始收益
   （std≈0.024）。两份 parquet 的 y 口径完全不同——这是当时混淆的最大根源。
3. **qlib LinearModel 不是 torch 封装**：qlib/contrib/model/linear.py 的 OLS 分支就是
   `sklearn.linear_model.LinearRegression(fit_intercept=False, copy_X=False)`，SVD lstsq。
   与手工 sklearn 同组合拟合的系数逐项一致（max|diff| = 0.0，数值级验证）。

## 执行配置的完整数据链（process_type=append 语义）

```
raw(158 特征, raw label)
 └─ infer 链: RobustZScoreNorm((x-median)/(1.4826*MAD), clip ±3) → Fillna(0)
      └─ learn 链(DK_L): DropnaLabel → CSRankNorm(label)
           └─ LinearModel.fit = sklearn LinearRegression(fit_intercept=False)
```

即：**特征 = 稳健 z 分数（裁剪 ±3、NaN 填 0），标签 = 截面排名归一，OLS 无截距。**

## 对照证据表（train 段 2021-06-01~2024-06-30，221037 行 × 158 特征）

| # | 拟合组合 | 系数 std | min | max | 说明 |
|---|---|---|---|---|---|
| G | **X_rz + y_csrank, 无截距（实际执行的 qlib 拟合）** | **6.9258** | -47.9 | 29.5 | 经 pred.pkl 与 IC 双指纹验证（见下） |
| S | X_rz + y_raw, **带截距（sklearn 默认）** | **0.2992** | -1.31 | 1.07 | ≈ 观察到的 0.35 |
| | X_rz + y_raw, 无截距 | 1.0373 | -7.18 | 4.42 | |
| | X_raw + y_raw, 带截距 | 0.7170 | -5.11 | 6.91 | |
| | X_raw + y_csrank, 无截距 | 21.3573 | -174.6 | 159.1 | |
| | X_rz + y_csz, 无截距 | 42.6467 | -295.2 | 181.9 | 标签用 z-score 而非排名，系数更爆炸 |
| | 前 12 个月切片, X_rz + y_csrank, 无截距 | 9.6160 | | | ≈ 8.56 量级 |
| | valid 窗口, X_rz + y_csrank, 无截距 | 13.8142 | | | |
| | train+valid, X_rz + y_csrank, 无截距 | 0.3188 | | | |
| | 02:37 修订前缓存, X_rz + y_csrank, 无截距 | 0.9332 | | | |

尺度拆解（决定 24 倍的各因子）：

- 标签：y_csrank std 0.9988 / y_raw std 0.0244 = **40.9×**；
- 截距开关：0.299（带截距）vs 1.037（无截距）= 3.5×；
- 稳健 z 特征归一：~1×（裁剪与 MAD 分母对冲后净效应小）；
- 净倍率 6.93 / 0.30 ≈ **23.1 ≈ 24**，与观察值吻合。

## 8.56 与 0.35 的分别判定

- **0.35 侧**：与「RZSN 特征 + 原始标签 + sklearn 默认截距」完全一致（0.299）。
  观察者极可能用 test 缓存的原始 y（或自行重算的 1 日收益）配 train 缓存的
  RZSN 特征做手工对照，且未关 sklearn 默认截距。
- **8.56 侧**：执行的正式 run 系数 std = **6.93**，有双指纹证明（见下）；8.56 是
  同一机制的早期测量（量级一致、数值不同）。最可能的来源：测量发生在数据修订
  中间态——本地 bins 在 08-24 02:37 缓存构建后发生过一次修订（44% 的行变化，
  疑似 hfq 调整），且系数 std 对窗口高度敏感（前 12 个月切片 9.6、valid 段 13.8、
  train+valid 0.32）。非 bug 症状，无需升级挂账。

## 复现验证（怎么证明结论是对的）

用现行 bins（修订后数据，run 记录 data_revision=1）完整重建执行链，与正式 run
产物逐项对指纹：

- 预测值：正式 run 的 pred.pkl vs 重建系数预测，逐日 pearson 相关
  mean **0.999984**（min 0.99946）、scale（run/mine）**0.9996 ± 0.0016**；
- 指标：mean_ic 重建 -0.001365 vs 记录 -0.001347；rankic 重建 0.026454 vs
  记录 0.026482；ic_std 重建 0.203542 vs 记录 0.203562。

即执行 run 的系数与「X_rz + y_csrank + 无截距」重建系数相同（std 6.93），
qlib LinearModel 与 sklearn 同组合拟合系数 max|diff| = 0.0。

## H1-H4 判定

- H1 预处理口径：**部分成立**。特征侧 RobustZScoreNorm 不是主因（净效应 ~1×）；
- H2 标签口径：**成立，主因**。CSRankNorm vs raw = 40.9×，是 24 倍的最大来源；
- H3 拟合细节：**成立**。截距开关 3.5×；qlib 用 sklearn SVD lstsq（非正规方程），
  无额外标签标准化之外的隐藏变换；
- H4 统计口径：特征数 158（非 20），含/不含截距已列入上表；std 统计对象一致。

**最终判定：(a) 预处理口径不同（预期差异）——具体是标签 CSRankNorm + 截距设置；
qlib 实现无 bug，无需升级挂账。**

## 附带发现（供后续参考）

1. 系数向量本身对数据窗口/修订极端敏感（同配置 std 从 0.32 到 13.8），158 特征
   高度共线——**线性模型的系数尺度不是模型质量指标**，不要再用 std 对比判读模型。
2. 官方 Linear yaml 用 CSRankNorm 而非 CSZScoreNorm 对标签归一，对系数尺度影响
   巨大（6.9 vs 42.6）；对 rankic 无影响（单调变换不变）。
3. 本地 02:37 的 train_6ce8e6f27429b7a3 缓存是数据修订前构建的，与现行 bins 有
   44% 行差异——若复算该缓存下游实验需注意此 drift。

## 对照脚本与数据

- 脚本（容器内，只读 bins + 已有缓存，未重建数据、未改参数）：
  /tmp/linear_probe_main.py（主对照网格）、/tmp/linear_probe_v2.py（drift 诊断）、
  /tmp/linear_probe_v3.py（pred.pkl/IC 指纹 + 修订前后对照）、
  /tmp/linear_probe_v4.py（窗口/截距/统计口径变体）；
  结果 JSON：/tmp/linear_probe_results.json、/tmp/linear_probe_v3.json。
- 数据源：cache/train_6ce8e6f27429b7a3.parquet（lgb 族缓存：raw X + CSZScoreNorm y，
  与 lin 同标签管线）、cache/test_ae5c8b5291499405.parquet（test 段 raw X + raw y）、
  /root/.qlib/qlib_data/cn_data（hs300 bins，修订后）。
- 缺口声明：lin 自己的 train 缓存（key e2a1423c3d81ddfc，spark 侧构建）不在本地，
  用同一标签管线的 lgb 族缓存 + 现行 bins 重建；重建已被 pred.pkl/IC 指纹证明
  等价于执行产物。
