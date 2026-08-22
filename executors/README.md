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
   ensemble / rounds / early_stopping / num_threads / save_models。

## 流程（管线侧，harness train action）

取数（缓存 parquet）→ 运行执行器 → 契约检查（schema/索引/覆盖度，不过则
QLAB_CONTRACT_FAIL）→ 固定测试器（唯一指标来源）→ 台账自动入库（metrics
来自测试器，artifacts 原样归档，tags 记 qlab.executor/qlab.handler/qlab.task/
qlab.data_key）。

## 参考实现

- executors/_example_lgb —— LightGBM 回归（原 pipeline.trainer 路径的移植），
  支持多种子 rank_mean 集成。写新执行器时复制改 train() 即可。
- 分类任务：task="classification"，pred.pkl 产出连续打分，测试器按日算 AUC。

## 反模式（会污染对比，禁止）

- 在特征里偷看未来（用 test 窗口数据训练）；
- 只预测子集股票刷 IC（契约会记录覆盖率并打 tag，review 会看）；
- 自己写 metrics.json —— 管线只认 pipeline.metrics 重算的结果。
