# Kronos 部署记录（executors/kronos）

> 目标：按照实验流程（spec→batch→submit→dispatcher→契约→固定测试器→台账）
> 部署零样本金融时序基础模型 Kronos 并跑测试。本地容器 CPU 执行。
> 状态：smoke 已闭环 / hs300 全池测试已闭环 / 交易层接入 + 回测已闭环（详见文末数字）。

## 1. 模型与上游

- 模型：Kronos（shiyu-coder/Kronos，MIT）。论文同名基础模型（"A Foundation
  Model for the Language of Financial Markets"）的官方演进仓库。
- 开源模型族：Kronos-mini(4.1M)/small(24.7M)/base(102.3M)，上下文 512；
  large(499M) 未开源。本项目选 **Kronos-base**（102.3M，d_model 832 × 12 层，
  s1/s2_bits=10）+ tokenizer Kronos-Tokenizer-base（24.7M，d_in=6：
  open/high/low/close/volume/amount）。
- 获取：GitHub 直连不通 → gitee.com/mirrors/Kronos（vendor commit `67b630e`）；
  权重 huggingface.co 不可达 → `HF_ENDPOINT=https://hf-mirror.com`（未 gated）。
- vendor：`executors/kronos/model/{__init__,module,kronos}.py` 原样拷贝，
  唯一补丁 = 包内相对导入 2 行（`kronos.py`：删 `sys.path.append("../")`、
  `from model.module import *` → `from .module import *`）。模型数学零改动。
  LICENSE 同目录留存。上游 README 的 predict/predict_batch API 与
  `KronosPredictor(model, tokenizer, device, max_context)` 直用。

## 2. 管线接入

- 执行器契约：`main.py --config --train --test --out` → `pred.pkl`
  (MultiIndex (datetime,instrument), 列 "score")。score = 预测次日 hfq 收盘/
  昨收 − 1（1 日收益预测），与管线默认 1d 标签 Ref($close,-2)/Ref($close,-1)-1
  对齐。零样本：不读 train 特征（Kronos 无训练期）。
- 输入数据：`data/extra/kronos_ohlcv.parquet`（契约第 8 条 extra 特征旁路；
  run_info.json 声明 `extra_features:["kronos_ohlcv"]`）。由 qlib_data_src 的
  hfq 价格 CSV 一次性构建（open/high/low/close/volume/amount，float32，
  301 只，2021-06-01..2026-08-20；构建脚本 scratch_probe/kronos_build_extra.py，
  manifest 入 git，parquet 本体 gitignore）。与管线 price cache 同源——
  raw OHLCV 是基础模型的输入而非自造特征，但口径仍超出固定菜单，board 对比
  需与 Alpha158 实验分开。
- 执行逻辑：逐测试日截面批预测（predict_batch，B=当日股票数 × lookback × 6，
  一次前向）；历史不足 lookback 或含 NaN 的股票当日跳过（批长度一致性要求）。
- 依赖 venv：requirements.txt（torch==2.5.1+cpu 走 download.pytorch.org/whl/cpu，
  einops/huggingface_hub==0.33.1/safetensors/pyarrow 等）；管线自动建
  results/venvs/kronos（stamp 机制），权重预缓存于 ~/.cache/huggingface。

## 3. 踩坑（已修复）

1. **pip ENOSPC**：pip 解包 torch 轮子默认落 /tmp（512MB tmpfs）爆盘 →
   构建脚本设 TMPDIR=/root/quant/results/venvs/.piptmp（大分区）。
2. **pids.max=256 触顶**：torch 8 线程 + webui 轮询 + dispatcher 叠加 →
   libgomp/OpenBLAS 线程创建失败。执行器 torch_threads 默认 4。
3. **REPO_ROOT off-by-one**（parents[1] 指到 executors/）→ extra parquet
   FileNotFoundError；已改 parents[2]（job81 失败即此因）。
4. **searchsorted dtype 坑**：`.index.values` 与 pd.Timestamp 比较报
   "int vs Timestamp" → 改 `(dfi.index < d).sum()`。

## 4. 性能实测（CPU，torch_threads=4，Kronos-base）

| 规模 | 单日耗时 | 推算 |
|---|---|---|
| 30 股 × lookback 120 | ~30-40s | smoke 30 天 ≈ 16-20 min |
| 265 股 × lookback 120 | ~400s（6.7min） | hs300×30d ≈ 3.5h；×250d ≈ 28h 不可行 |
| 模型加载 | ~55s | 每次 run 一次 |

结论：CPU 本机只适合小窗口测试；全池长窗需 GPU（spark 远程，未获授权不动）。

## 5. 测试配置

- smoke：`kronos_smoke`（30 只 hs300，lookback 120，test 2025-01-01..2025-02-14，
  timeout 30，runner=local）。
- 正式：`kronos_hs300_30d`（hs300 全池 300 只，lookback 120，同窗口 ~30 截面，
  timeout 240，runner=local）。
- 采样：T=1.0 / top_p=0.9 / sample_count=1（上游默认），seed=42——
  非确定性采样，run_info.json 记录；确定性版（T=0）挂账待做。
- 预注册：无 expectation（部署验证，exploratory）；30 截面 IC 样本薄，
  数字只作链路验证不作信号结论。

## 6. 挂账（未做）

- [ ] 远端 SIGTERM 杀手的最终实体定位（疑为 10.0.0.4:22 侧的他人进程；
      与用户确认远端容器是否共享）。
- [ ] 确定性采样（T=0/greedy）对照 + sample_count>1 集成。
- [ ] Kronos-small/mini 速度-质量取舍对照。
- [ ] 用 Kronos 官方 A 股 qlib 示例的 vwap 字段口径（本项目用 amount）复核。
- [ ] 更长测试窗口的正式信号评估（GPU 已通，~3.5s/天/300 只，250 天 ≈ 15 分钟，
      成本不再是大问题；但需预注册 expectation + n_variants 纪律）。
- [ ] 远端取数预置缓存的制度化（当前是手动 scp；若常跑 spark 可考虑管线化）。

## 9. K线重建质量实测（2026-08-24，40 只 hs300 × 26 天，零样本，T=1.0/top_p=0.9/sample=1）

评估脚本 scratch_probe/kronos_recon_eval.py + kronos_recon_analyze.py；
原始预测存 results/kronos_probe/kronos_recon_preds.pkl（1040 行）。

| 指标 | open | high | low | close |
|---|---|---|---|---|
| Kronos MAPE | **0.69%** | 1.27% | 1.23% | 1.61% |
| 随机游走 MAPE | 1.22% | 1.16% | 1.00% | 1.14% |
| Kronos/RW 比值 | **0.56** | 1.09 | 1.17 | **1.35** |

- 收盘方向命中率 51.7%（n=991）≈ 抛硬币；预测次日收益 std 0.0134 vs 实际
  0.0174（欠分散，向零收益收缩）。
- K线形态自洽率 93.4%（高≥max(开,收) 且 低≤min(开,收)；实际恒 100%）。
- 结论：价格水平预测的"高精度"（corr≈0.9999）是价格持续性的假象——随机游走
  同级别；Kronos 唯一显著优于 RW 的是**开盘价**（0.56×），收盘价反而差 35%。
  零样本日线重建不构成对 RW 的优势，方向无信息。与独立文献一致
  （arXiv 2606.xxxx "Foundation models do not beat simple volatility
  benchmarks: 5000 Chinese stocks"）；论文自报 IC 也仅 0.004~0.044。
  若追求重建质量，正确路线是上游 finetune（repo 自带 A 股脚本）而非零样本。

## 7. 数字

### smoke（job83, mlflow 4d3576298472468e, runner=local）
- 30 只 hs300 × 26 截面（2025-01-02..2025-02-14），契约 ok：覆盖 100%/100%，
  nan 0，extra_features 声明入账。
- rankic_mean **-0.0477**（bootstrap p_le0=0.873，ci95 [-0.128, 0.034]）；
  mean_ic -0.0002（p_le0=0.4985）；hit 0.440；decile 单调性 -0.26。
- 解读：零样本 Kronos 在此窗口无显著截面信号；样本仅 26 截面，仅作链路验证。

### hs300 全池（job84, mlflow ca977b94438e42, runner=spark, GPU GB10）
- 298/300 只（2 只 NaN 窗口跳过）× 26 截面（2025-01-02..2025-02-14）；
  契约 ok：date_frac 1.0 / inst_frac 0.993 / nan 0；extra_features 入账；
  device=cuda:0；executor 仅 **95.4s**（GPU ~3.5s/天 vs 本地 CPU ~400s/天）。
- rankic_mean **-0.0130**（bootstrap p_le0=0.7545，ci95 [-0.048, 0.022]）；
  mean_ic -0.0127；hit 0.481。
- 解读：与 smoke 方向一致（弱负、不显著）。零样本 Kronos-base 在 2025-01~02
  hs300 上无截面 alpha 信号；26 截面样本薄，仅作部署验证与口径示范，
  不作信号结论。若后续要判断模型有效性，需更长窗口 + 预注册 expectation
  + n_variants 纪律。

## 8. spark 远程部署要点（2026-08-24）

- 远端 hf-mirror 不可达（SSL EOF）→ 权重 tar（394MB）本地打包 scp 到
  /home/dev/.cache/huggingface/hub（持久共享）。
- extra 数据 → scp 到 /home/dev/quant/cache/kronos_ohlcv.parquet（共享 cache
  目录，执行器按 data/extra/ → cache/ 顺序回退查找）。
- requirements.txt 双索引（download.pytorch.org/whl/cpu + aliyun cu128）：
  远端 aarch64 装 cu128 torch，本地 x86_64 用 cpu 轮子；本地 venv 已按新
  stamp 重签（torch>=2.0 满足，免重建）。
- main.py 设备自动检测（cuda 可用则 GPU）；spec runner=spark。
- 远端 ssh 用 -p / scp 用 -P（remote.py 的坑，别混）。
- **远端 loky 取数必死（已定案，2026-08-25）**：qlib 默认 14 进程
  multiprocessing 取数在远端 ~3-40s 内被 SIGTERM 整组杀。内核级 bpftrace
  取证证明**没有外部发送者**：真凶是 harness 自己的 SIGTERM handler
  （killpg(0)）被 fork 出的 joblib worker 继承；joblib 池正常关闭给 worker
  发例行 TERM 时，worker 的 handler killpg(0) 把整个进程组（含主进程）带走，
  自伤级联。修复 = harness handler 增加 fork 子进程守卫（commit b9d123d），
  修复后 14 进程取数连续两次完整存活（46s/48s）。详细取证见
  knowledge/notes/spark_sigterm_investigation.md §8。
  sitecustomize.py 的 kernels=1 兜底已无必要（且实测在该启动路径下不可靠），
  已移除（2026-08-26 收尾提交），恢复远端 14 进程取数。
- **HF 远端不可达的第二个坑**：hf_hub online 模式会对 resolve URL 做 etag
  HEAD → SSL EOF 失败；必须 `HF_HUB_OFFLINE=1`（main.py 已强制，权重部署时
  预缓存到 /home/dev/.cache/huggingface/hub）。

## 10. 交易层接入与回测（2026-08-26，job85）

信号→交易持仓这一步已接入：executors/kronos/main.py 新增 `build_portfolio()`
（复刻 qlib TopkDropoutStrategy 语义 = qlib_bench._portfolio），在写出 pred.pkl
之后把 score 转成每日目标权重，产出 `portfolio.pkl`（MultiIndex (datetime,
instrument)，列 "weight"，等权 1/topk，权重和 =1）。spec `kronos_hs300_30d`
的 metrics 追加 portfolio/backtest/attribution 三族，params 加 topk=50 /
n_drop=5；runner=spark、timeout_min=300 不变。commit cb43701。

### 摩擦（本次新踩，已解决/挂账）

1. **pandas 3.x（执行器 venv）→ 2.3.3（harness 解释器）pickle 不兼容**（关键坑）：
   执行器 venv 的 pandas 3.0.5 写 `stack()` 派生的 MultiIndex（含 datetime64
   层）时，序列化格式无法被 harness 的 pandas 2.3.3 unpickle（
   `NDArrayBacked.__setstate__ NotImplementedError`）。pred.pkl 恰好兼容（
   `set_index` 构造），但 portfolio.pkl 的 `stack()` 构造不兼容 → check_portfolio
   会以 "unreadable" 拒绝。本地与远端 venv/harness pandas 版本完全相同
   （venv 3.0.5 / harness 2.3.3），远端同样会踩。修复 = build_portfolio 末尾
   `reset_index().set_index(["datetime","instrument"])` 重建索引（与 pred.pkl
   同构造，已验证跨版本可读）。挂账：qlib_bench 的 `_portfolio` 用同样 `stack()`
   构造，若其 venv 也是 pandas 3.x 且跑 trade metrics 会同样失败，待复验。
2. **容器 pids.max=256 很紧**（实测 238/256）：任何未显式
   `OMP/OPENBLAS/MKL_NUM_THREADS=1` 的 numpy/scipy 导入都会 OpenBLAS 线程创建
   失败（`pthread_create failed ... Resource temporarily unavailable`）。验证
   脚本必须单线程。
3. **docker cp 到容器 /tmp 静默失败**（/tmp 是 512MB tmpfs 已 97% 满）：临时
   脚本放 results/scratch/（gitignored）而非 /tmp。
4. 远端 price 缓存由 dispatch 自动 staging（remote.py P8 T1：本地
   qlib_data_src 建 price parquet → scp 到远端 cache），无需手动预置；本次远端
   正确生成了 `prices_hs300_9dcfbc0d0b8da9d0.parquet`。

### 数字（job85, mlflow 797d0b0c3dce47bab5abe0d0e81d544d, runner=spark）

契约：pred 契约 ok（7748 行 / 26 截面 / 298 只）；**portfolio_contract.ok=true**
（1300 权重行，date_frac 1.0，max_daily_sum 1.0，nan 0，无 issues）。

| 层 | 指标 | 值 |
|---|---|---|
| 截面信号（不变） | rankic_mean / p_le0 | **-0.0130** / 0.7545（ci95 [-0.048, 0.022]，n=26）|
| | mean_ic / hit | -0.0127 / 0.481 |
| portfolio | turnover_mean | 0.2308（含首日建仓 1.0；稳态 ~0.16/日 ≈ n_drop/topk 量级）|
| | weight_ic_mean | 0.1570 |
| | n_held / hhi / cash | 50 / 0.02(=1/50) / 0 |
| backtest | **excess_ann** | **+0.3719**（vs SH000300）|
| | sharpe / ann_ret / ann_vol | 2.952 / 0.733 / 0.192 |
| | mdd / beta | -0.022 / 1.289 |
| | cost_drag_ann / total_cost | 0.079 / 0.0044 |
| attribution | pred / align / cost / market | weak / **poor** / moderate / **helped** |

attribution 四层读数：
- **pred weak**：rankic -0.013、p=0.7545，信号不显著（负）。
- **align poor**：weight_ic +0.157（权重与分数排名正相关，topK 构造使然），
  但分数与收益负相关 → ratio_vs_signal = -12.05。
- **cost moderate**：佣金万2.5双边 + 印花税卖出千1 + 滑点1bp，年化拖累 7.9%。
- **market helped**：excess_ann +37%、beta 1.29，超额主要来自市场 beta 而非选股。

### 结论（诚实口径）

- 表面数字（excess_ann +37%、sharpe 2.95）很漂亮，但 attribution 已把责任
  定位清楚：**pred 层 weak + align 层 poor + market 层 helped**——正超额来自
  beta 1.29 放大上涨 + 等权 topK 与市值加权基准的偏离 + 25 个交易日短窗口噪声，
  **不是 Kronos 的选股 alpha**。
- 与零样本截面结果（rankic_mean≈-0.013、p=0.7545，弱负不显著）完全自洽：
  零样本 Kronos 在此窗口无截面信号，topK 选股本质是"噪声选股"，正超额是 beta
  不是信号；与 §9 K线重建结论（零样本无方向信息）一致。
- 样本仅 25 个交易日（月频 ~2 个收益点），Sharpe/MDD 估计误差极大（§8.5 统计
  纪律），**不得据此升级 refs 或宣称显著**。要判断模型有效性仍需更长窗口 +
  预注册 expectation + n_variants 纪律（挂账清单不变）。
