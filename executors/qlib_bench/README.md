# executors/qlib_bench — qlib 官方 benchmark 复现执行器

用途：把 qlib 官方 benchmark 模型（官方 workflow_config_*.yaml 的参数，只改时间窗）
在管线固定数据菜单上原样跑一遍，产出 pred.pkl + portfolio.pkl，指标全部由
pipeline.metrics 固定测试器计算。

## 结构

- `main.py`：通用执行器。模型由 spec 的 `model.{class,module_path,kwargs}` 指定，
  直接从 `qlib.contrib.model` 导入官方类并调用官方 `fit/predict`（代码原样，不改）。
- `ParquetHandler`：把管线特征 parquet 按 DataHandlerLP 接口喂给官方
  DatasetH / TSDatasetH / MTSDatasetH —— 特征构建仍在 pipeline.data（固定菜单），
  执行器只做数据转接，不自造特征。
- `portfolio.pkl`：复刻官方 TopkDropoutStrategy（topk=50, n_drop=5, 等权 1/k）。
- `requirements.txt`：torch(CUDA aarch64 阿里镜像)/xgboost/catboost/pytorch-tabnet。
  qlib 本身从**环境解释器**的 site-packages 注入（pyqlib 无本平台 wheel），
  main.py 顶部自动处理。
- `benchmarks/LSTM/`：官方预训练 LSTM 检查点（GATs/HIST/IGMTF 的 base_model），
  从 microsoft/qlib examples/benchmarks/LSTM 原样 vendor，官方 model_path 相对
  路径保持不变。

## 已知不可部署（上报）

- TFT：官方实现依赖 tensorflow-gpu==1.15 + pandas==1.1（Python ≤3.7 时代），
  与当前 Python 3.12 不兼容，本轮跳过。
- HIST：需要概念矩阵 `qlib_csi300_stock2concept.npy`（GitHub 自动下载，
  本环境网络不可达时不可用）；且其 stock_index 与官方 2020 年成分绑定，
  与当前 hs300 成分存在错位风险。

## 与官方口径的差异（统一评分纪律下有意为之）

1. 评分：管线固定测试器（相同样本窗 2025-01~2026-08、相同 label、相同成本模型）
   对所有模型统一口径；官方榜单的绝对数字不可比（数据期不同）。
2. 成本模型：管线 佣金双边万2.5 + 印花税卖出千1 + 滑点1bp；官方
   open_cost 0.0005 / close_cost 0.0015 / min_cost 5 / limit 0.095。回测数字
   只做模型间横向比较。
3. GATs/HIST/IGMTF 的 LSTM base 用官方预训练权重（在官方 2010-2020 数据上训出），
   本任务在其上做图/概念微调 —— 与官方流程一致。
4. Linear 官方回测为 long-short（ana_long_short: True），本任务统一用
   long-only TopkDropout 口径（跨模型可比优先）。
