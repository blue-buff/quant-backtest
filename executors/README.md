# Executor contract（P5）

管线只管两头：取数（pipeline.data 固定菜单）+ 固定测试器（pipeline.metrics）。
执行器内部是什么，管线丝毫不管 —— 写执行器是 agent 的事。

## 契约

1. 位置：executors/<name>/main.py（<name> 不含 ..，git 提交后可用）。
2. CLI：main.py --config <json> --train <pq> --test <pq> --out <dir>
3. 输出：<out>/pred.pkl —— pickle 的 DataFrame（列 "score"，MultiIndex
   (datetime, instrument)）或等价 Series。其余文件（模型、日志、图）原样归档。
4. 输入数据：管线提供的 float32 特征 parquet（feature 列 + y 标签列，
   (datetime, instrument) 索引）。执行器只许读，不许自造特征——这是
   board 上所有实验可比的根基。
5. 指标：执行器不报指标。指标由固定测试器从 pred.pkl + label 重算。
6. 依赖：executors/<name>/requirements.txt 存在时，管线自动建独立 venv
   （results/venvs/<name>）并安装；远程跑各自 docker 镜像。
7. config json 内容：pool / instruments / handler_class / fit / train / valid /
   test_start / test_end / label_formula / horizon / task / model / seeds /
   ensemble / rounds / early_stopping / num_threads / save_models /
   params —— spec.params 原样透传（供执行器自定义超参；管线不解释、不校验）/
   price_pq（交易型实验才有；管线提供的价格 parquet 路径）/
   metric_families（本实验勾选的指标族，供执行器自省，不影响契约）。

## 流程（管线侧，harness train action）

取数（缓存 parquet）→ 运行执行器 → 契约检查（schema/索引/覆盖度，不过则
QLAB_CONTRACT_FAIL）→ 固定测试器（唯一指标来源）→ 台账自动入库（metrics
来自测试器，artifacts 原样归档，tags 记 qlab.executor/qlab.handler/qlab.task/
qlab.data_key）。

8. 自由特征旁路：data/extra/<feature_name>.parquet（约定见 data/extra/README.md）。
   用了必须在 <out>/run_info.json 声明 "extra_features": [...]；管线只记录不校验
   （contract_report.json + tag qlab.extra_features，空不写）。
   常数预测（分数 std=0 / 唯一值<2）会被契约拒绝（QLAB_CONTRACT_FAIL）。
9. 交易输出（P8）：<out>/portfolio.pkl —— DataFrame，MultiIndex (datetime, instrument)，
   列 "weight"，float；每行权重 ≥0 且 sum ≤1（现金 = 1 - sum）；不持有的行可省略。
   它是执行器交给测试器的"交易指令"，测试器直接拿它跑回测，没有中间格式。
   负数/行和>1/常数权重会被契约拒绝（QLAB_CONTRACT_FAIL）；交易型 spec
   （metrics 勾了 portfolio/backtest/attribution）未产出 portfolio.pkl 不 fail，
   只打 tag qlab.portfolio_missing 并只跑 prediction 族。
10. 价格：config 里的 price_pq 是管线提供的价格 parquet（(datetime, instrument)、
    列 close，来自 qlib_data_src 的 hfq close）。执行器禁止自算价格、禁止自带行情。
    执行器只读 cfg["price_pq"] 路径；价格缓存不存在时管线会自动构建。

## 参考实现

- executors/_example_lgb —— LightGBM 回归（原 pipeline.trainer 路径的移植），
  支持多种子 rank_mean 集成。写新执行器时复制改 train() 即可。
- 分类任务：task="classification"，pred.pkl 产出连续打分，测试器按日算 AUC。

## 反模式（会污染对比，禁止）

- 在特征里偷看未来（用 test 窗口数据训练）；
- 只预测子集股票刷 IC（契约会记录覆盖率并打 tag，review 会看）；
- 自己写 metrics.json —— 管线只认 pipeline.metrics 重算的结果。
