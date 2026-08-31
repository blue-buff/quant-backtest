# QLab Development Rules（AGENTS.md）

> 本文件只约束**开发/工程**行为。量化实验、数据、队列、台账、备份、远程计算等
> **实验室操作规范**已拆分到 [`docs/QLAB_LAB_SPEC.md`](docs/QLAB_LAB_SPEC.md)。
> 实验室规范是权威版本；本文件与其冲突时，实验场景以实验室规范为准。

## 1. Agent 入口分流

- 如果你（agent）被指定进行量化实验，**必须先完整阅读
  [`docs/QLAB_LAB_SPEC.md`](docs/QLAB_LAB_SPEC.md)**，再执行实验。
- 实验包括但不限于：submit/run 队列任务、训练/回测、数据 ensure/invalidate、
  MLflow 记账、实验评审、结果分析、paper trading、远端/Spark 计算、备份 push。
- 仅做代码开发、测试、文档、构建修复时，按本文件执行；一旦需要触碰实验链路，
  就按上一条读取实验室规范。

## 2. 工程环境

- 本仓库在 macOS 本机开发；Python 统一用 `.venv/bin/python`。
- 运行测试使用：

  ```bash
  PYTHONPATH=. .venv/bin/pytest -q tests
  ```

- `results/`、`cache/`、`mlflow-server/`、数据源目录是运行产物/数据，不属于代码；
  不要提交，也不要在代码评审时当作源码变更。
- 临时脚本放 `/tmp`，不要把临时 `.py` 放进仓库根目录；harness 有脏代码门禁。

## 3. 代码与提交

- 修改代码前先看现有实现和测试，不重复造轮子。
- 新功能或修复必须带对应测试；测试不绿不交付。
- 许可证合规：
  - MIT 源码可以 vendor，但必须保留 LICENSE 和来源说明。
  - GPLv3 源码不得阅读、复制或派生；只能依据已产出的 cleanroom spec 独立实现，
    并更新合规留痕。
- 提交信息写清主题；涉及实验的提交带上 `batch_id` / `exp_id` 或本轮主题。
- 需要跑 harness 入账前，代码必须先 commit；`qlab.git` 必须指向真实产生结果的 commit。

## 4. 安全边界

- 不执行破坏性 git 操作；不删除用户数据、缓存、台账、队列或结果目录。
- 不把 token、密码、私钥写入日志、事件、spec、代码或提交信息。
- 远端机器、长任务、全市场任务、数据重建等高影响动作必须先获得用户明确批准。
