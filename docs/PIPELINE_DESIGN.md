> 【归档横幅】v1.1 历史设计稿，与实现有出入；以根目录 AGENTS.md 为准（2026-08-23 P7 冻结）。

# QLab：面向 Agent 的个人量化模型试验管线设计

> 版本 v1.1（按用户反馈调整：限制最少化，记录最大化）
> 哲学：**飞行记录仪，不是闸门**。agent 拥有最大自由度；管线不拦截决策，只把决策全部记录下来，
> 让错误可以被事后发现、复盘、修正，而不是被事前禁止。

---

## 0. 硬约束与默认值

### 0.1 硬约束（仅三条，其余都不是规则）

1. **诚实**：结果必须真实，禁止编造/美化（用户红线）。
2. **双备份**：每次实验本地 + GitHub 双备份（用户明确要求），由管线自动执行。
3. **机器可读**：实验产出统一为 metrics.json 等结构化文件，agent 和人都能读。

### 0.2 默认值哲学

其余一切设计（评审、预注册、单变量变更、指标清单）都是**默认值**：
管线默认这样做，agent 随时可以跳过或偏离，只需在 registry 里留一行偏差记录（§10.4）。
偏离不被惩罚，被记录——这是本设计与 v1.0 的根本区别。

---

## 1. 总体架构

~~~
quant-backtest/
├── pipeline/                  # 新管线核心（Python 包，agent 的全部操作面）
│   ├── registry.py            #   sqlite 账本：实验/运行/评审意见/偏差记录/知识条目
│   ├── spec.py                #   spec 解析、继承(base+overrides)、默认值填充
│   ├── metrics.py             #   指标计算：核心必算 + 一键全算
│   ├── harness.py             #   单实验封装：resolve → train → predict → eval → collect
│   ├── queue.py               #   批量队列执行器（并发/断点续跑/自动重试）
│   ├── review.py              #   对抗性评审 CLI（advisory，无否决权）
│   ├── kb.py                  #   知识库工具（claims/ideas/notes 增删查）
│   ├── backup.py              #   双备份（自动执行 + sha256 校验）
│   └── board.py               #   “读数据”入口：汇总表 + 单实验全链条视图
├── experiments/
│   ├── refs/                  # 参考配置（当前最佳已知基线）
│   ├── specs/                 # 实验规格 *.json
│   ├── batches/               # 批次清单 *.json
│   └── archive/               # 归档规格
├── results/
│   ├── registry.db            # ★ 唯一事实来源（飞行记录仪）
│   ├── runs/<exp_id>/         # effective_config.json / metrics.json /
│   │                          #   review.json / pred.parquet / train.log / meta.json
│   ├── board.csv              # 自动汇总视图
│   ├── eval/                  # 历史深度评估 JSON（保留）
│   └── backup/                # 本地快照 + manifest.sha256
├── knowledge/
│   ├── papers/                # 来源文献摘录（kXXX-标题.md）
│   ├── notes/                 # 阅读笔记（frontmatter + 正文）
│   ├── claims.jsonl           # 论点抽取库
│   └── ideas/                 # 假说队列（可选起点，不强制）
├── docs/                      # 报告 / SOP / 结论 / 本设计文档
├── scripts/                   # 旧脚本（保留，逐步退役）
└── qlib_examples/             # qrun yaml（保留为底层执行器输入）
~~~

一次实验的生命周期：

~~~
任意起点（ideas 队列 / claims / agent 突发灵感 / 直接抄历史 spec 改几处）
   │
   ▼
specs/xxx.json（最小必填只有 exp_id + base；其余字段可选、有默认值）
   │
   ▼
queue.py 批量执行（默认并发 2，失败自动重试 1 次，断点续跑）
   │  harness：生效配置 → 训练 → 预测 → metrics.json
   ▼
registry.db 记录运行结果；默认跑一次 advisory 评审（可 --no-review 跳过）
   │
   ├── 默认回写 claims/ideas 状态（可跳过）
   └── backup.py 自动本地快照 + git push + sha256 校验
~~~

---

## 2. 实验规格（spec）—— 最小必填，其余默认

### 2.1 最小可运行 spec

~~~json
{
  "exp_id": "all10d_dart_001",
  "base": "ref:all10d_ens3",
  "overrides": { "model": { "class": "LGBModel", "kwargs": { "boosting": "dart" } } }
}
~~~

就这些。没有 hypothesis、没有预注册、没有单变量约束，照样能跑。
其余字段（hypothesis / pre_registration / idea / runner / timeout / changes）全部可选，
缺失时按默认值填充，并在 registry 标记 exploratory。

### 2.2 推荐填写（不强制）

- changes：一句话说明改了什么（多变量变更也允许，写清即可）；
- hypothesis / expectation：预期方向与判据（写了就能自动做预注册对照，不写就是探索性实验）；
- idea：关联假说队列条目（方便回溯，可从任意起点出发）；
- runner：local / remote / auto（默认 auto）；
- timeout_min：默认 120，agent 可按预估改。

### 2.3 校验器只做三件事

1. exp_id 全局唯一；
2. base 能解析（refs/ 里存在或给出完整配置）；
3. overrides 合并后配置结构合法（字段名拼错会报错——这是防手滑，不是限制）。

refs/ 可以新增、可以修订（修订时记一条 ref 变更记录，旧 ref 归档不删）。

---

## 3. 账本 registry.db —— 飞行记录仪

### 3.1 表结构

~~~sql
experiments(exp_id PK, spec_hash, base_ref, changes, hypothesis, idea_id, status, created_at)
runs(run_id PK, exp_id FK, started_at, finished_at, runner, data_version,
     code_commit, exit_code, status, metrics_json, review_json, backup_sha256)
reviews(review_id PK, run_id FK, stage, verdict, reviewer, reasons_json, created_at)
deviations(id PK, run_id, kind, note, created_at)
claims(claim_id PK, text, source, ctype, status, linked_exp_ids, tags, created_at, updated_at)
ideas(idea_id PK, title, body, source_claim_ids, status, spec_ids, created_at, updated_at)
ref_changes(id PK, ref_id, old_spec_hash, new_spec_hash, note, created_at)
~~~

### 3.2 状态机（简化）

~~~
QUEUED → RUNNING → DONE / FAILED
FAILED 自动重试 1 次（可关）→ 仍失败则留档（失败也是记录）
~~~

评审不再改变实验状态。review 是附在 run 上的意见标签（PASS / FLAG / NOTE），
agent 自行决定怎么处理意见，处理结果记入 runs.metrics_json 的 conclusion 字段。

### 3.3 历史 backfill

现有 74 个实验（docs/evidence/exps/*.json）+ 关键评估（results/eval/*.json）
批量导入 registry，标记 data_version=legacy、code_commit=4d883bf。只归档，不重跑。

---

## 4. 指标（metrics.py）—— 核心必算，全量按需

### 4.1 核心（每次实验自动算，5 项）

~~~json
{
  "meta": { "exp_id": "...", "run_id": "...", "data_version": "v3",
            "sample_window": ["2025-01-02", "2026-08-20"], "n_days": 0, "n_inst": 0 },
  "rankic": { "mean": 0.0, "std": 0.0, "ir": 0.0, "p_value": 0.0 },
  "hit": { "rate": 0.0, "top_bottom_mean": 0.0 },
  "conclusion": { "text": "...", "expectation_check": "met | not_met | n/a" }
}
~~~

### 4.2 一键全算（agent 觉得需要时再跑，一行命令）

ic / non_overlap rankic / bootstrap CI / deciles 分层 / hs300+zz500 子域 / 分季度 /
多重检验修正（记录同一假说下试过的变体数，按 Bonferroni 出保守结论）。
全算结果合并进同一份 metrics.json，board 自动感知。

---

## 5. 对抗性评审（review.py）—— advisory，无否决权

### 5.1 默认行为

- 每个 DONE 的实验默认跑一次结果评审（red-team subagent 找茬：泄露、选择性报告、
  结论强于证据、p 值解读等），产出 PASS / FLAG / NOTE + 理由，存入 review.json；
- agent 可以用 --no-review 跳过（比如深夜批量小实验）；
- **评审不拦截任何东西**。它只是把“一个挑刺者会怎么看这个结果”提前写成文字留给后人。

### 5.2 深审策略（只在真正要紧时）

以下三种场景建议跑完整深审（设计+结果双评审，checklist 各 10 条）：
1. 结论要汇报给用户 / 写进最终报告；
2. 某配置要被升为 refs/ 基线；
3. 结果好得可疑（远超历史同类实验）。

深审同样是 advisory——意见归档，决定权在 agent，但深审意见必须随结论一起呈现给用户。

### 5.3 使用方式

~~~bash
python -m pipeline.review results --run <run_id>            # 默认轻审
python -m pipeline.review deep --spec <spec> --run <run_id> # 完整深审（双闸）
~~~

---

## 6. 知识库（kb.py）—— 工具，不是关卡

### 6.1 三层结构

| 层 | 载体 | 内容 |
|---|---|---|
| 来源 | papers/kXXX-*.md | 论文/文章摘录，头部记 URL、作者、年份 |
| 笔记 | notes/*.md（frontmatter） | 阅读笔记：方法、数据、结论、可借鉴点 |
| 论点 | claims.jsonl | 一条可验证的 claim 一行，带状态机 |

### 6.2 claims 状态闭环（保留，因为它是工具不是限制）

~~~json
{"claim_id": "c-0001", "text": "横截面中性化后 Alpha158+LightGBM 的 RankIC 提升约 10-20%",
 "source": "papers/k003-xxx.md", "ctype": "empirical",
 "status": "untested", "linked_exp_ids": [], "tags": ["neutralization"],
 "created_at": "2026-08-22"}
~~~

状态：untested → testing → confirmed / falsified / partial。
实验跑完由 harness 自动把 spec 里关联的 claim 挂上 exp_id 并更新状态——agent 无需手动操作。

### 6.3 检索

~~~bash
python -m pipeline.kb search "neutralization"      # 跨 claims/notes/papers 全文检索
python -m pipeline.kb claims --status untested     # 待验证论点列表
python -m pipeline.kb add-note --source <url> --file <md>
~~~

---

## 7. 批量队列（queue.py）—— 快，且容错

~~~json
{
  "batch_id": "b-2026-08-22-label-horizons",
  "specs": ["experiments/specs/all10d_label5d_001.json",
            "experiments/specs/all10d_label15d_001.json",
            "experiments/specs/all10d_label20d_001.json"],
  "concurrency": 2,
  "runner_policy": "auto",
  "timeout_min": 120,
  "retry": 1,
  "review": "auto"
}
~~~

~~~bash
python -m pipeline.queue run experiments/batches/b-xxx.json      # 跑整批
python -m pipeline.queue status b-xxx                            # 进度
python -m pipeline.queue retry b-xxx --only-failed               # 只重跑失败项
~~~

行为：
- 断点续跑：已 DONE 且 spec 未变的自动跳过（--force 重跑）；
- 失败自动重试 1 次，仍失败记录原因继续下一个，不拖垮整批；
- 并发默认 2（agent 可按机器负载改），日志逐 run 落盘；
- 性能默认开：parquet 特征缓存（首算存盘、重跑直读）+ te 段只加载预测所需截面 +
  kernels 默认 min(核数,16)——这些都是提速默认值，不是限制。

---

## 8. 双备份（backup.py）—— 用户要求的硬约束，自动执行

- 每批次结束自动：本地快照（容器 + 主机 D 盘双落点）+ git commit/push +
  pred 转 parquet(zstd) 进 git + manifest.sha256 校验；
- 失败自动重试 3 次；仍失败标 BACKUP_PENDING 上板，不阻塞后续实验，但一定会被看到；
- 主机 D 盘 172GB 与容器磁盘都够用：特征缓存全量保留不删，这是批量重跑的提速投资。

---

## 9. 计算资源策略

| 算力 | 规格 | 角色 |
|---|---|---|
| 容器 hermes-1679f5b2 | 20 线程 / 12GB（可上调） | 默认 runner（数据与环境在此） |
| 主机（本机） | 7840H 16 线程 / 30GB / D 盘 172GB | 备份落点 + 快照 + git 推送 |
| 4090D（song@10.110.12.99） | 32 线程 / 32GB / RTX 4090D | 重型 runner（整批全市场等） |

默认 auto 路由：按内存预估，整批全市场 → 4090D，其余 → 容器；spec 里 runner 字段可随时覆盖。
remote 流程复用已验证经验（Scheduled Task + icacls 等坑位已固化），只碰 C:/Users/song/qbt_work。
GPU 边界：LightGBM 用不上；深度模型阶段另行启用。

---

## 10. Agent 标准作业程序（SOP）—— 三动词 + 偏差记录

### SEARCH（搜资料）
产物 = papers 摘录 + claims 入库（带来源 URL）。搜到什么写什么，无配额限制。

### DESIGN（设计实验）
任意起点：claims/ideas/历史 spec 改几处/突发灵感。写最小 spec 即可（§2.1），
推荐补 changes 与 expectation 两行，其余随意。

### RUN（跑实验）
丢进 batch → queue 执行 → 断点续跑/重试自动处理。深夜批量小实验可 --no-review。

### READ（读数据）
board（汇总表）+ pipeline.show <exp_id>（全链条视图）是主要入口，也允许直接翻
metrics.json / pred.parquet——入口是工具，不是规则。读后写一行 conclusion。

### 10.4 偏差记录（唯一的新“要求”，且极其轻量）

当 agent 偏离任何默认值（跳过评审、多变量变更、改 runner、改超时、动 refs 等），
在 registry.deviations 记一行 {kind, note}。半句话即可，例如：
“跳过评审：与 e08 同构仅换种子”“三变量同改：dart+15d+zz500，想快速探组合”
“timeout 提到 300：远程跑 20d 标签慢”。

目的：让三个月后的任何人（包括复查 agent）能还原当时的决策语境。
不写也不拦截——但写了的实验在 board 上多一列可追溯，结论也更有分量。

---

## 11. 落地阶段（每阶段结束 commit + push）

| 阶段 | 内容 | 产物 | 估时 |
|---|---|---|---|
| P0 账本与指标 | registry.py / spec.py / metrics.py；历史 74 实验 backfill | registry.db + board.csv v1 | 半天 |
| P1 执行与队列 | harness.py / queue.py / board.py；特征缓存；一个真实 3-spec 批次端到端跑通 | 批量能力 | 半天 |
| P2 评审与知识 | review.py（advisory 轻审+深审）+ kb.py + 现有文档导入 claims/notes | 评审意见 + 知识闭环 | 半天 |
| P3 备份与远程 | backup.py 自动化 + remote runner | 双备份 + 远程路由 | 半天 |
| P4 固化 | SOP 文档 + subagent prompt 模板（searcher/reviewer/runner）+ 全流程演练 | 可移交任何 agent | 半天 |

迁移原则：旧脚本继续可用；已 pin 的结论原样入账，不在迁移中重新解释。

---

## 12. 明确的非目标

- 不做交易系统、实盘对接、真实下单规则回测（用户明确只评预测）；
- 不重写 qlib / 不换回测框架；
- 不搞分布式调度平台（两台机器，规则路由即可）；
- 深度模型（GPU）阶段另行立项。

---

## 附：v1.0 → v1.1 调整对照

| v1.0 的限制 | v1.1 的调整 |
|---|---|
| OVAT 强制：diff 超过 1 个变量直接拒绝 | 允许多变量，推荐写 changes 说明即可 |
| 预注册强制，否则不允许进结果闸 | 可选；写了自动对照，不写标记 exploratory |
| 双闸门评审有驳回权 | advisory 无否决权；默认轻审可跳过；深审只在要紧场景 |
| 每实验 10 条 checklist 强制 | checklist 保留为深审工具，不设关卡 |
| spec 必填 8 项 + 模板限定键 | 最小必填 2 项（exp_id + base），其余全默认 |
| refs 只增不改 | 可修订，修订留档、旧版归档 |
| 备份失败 = 实验没做完 | 自动执行+重试，失败上板不阻塞 |
| 必须从 ideas 队列取实验 | 任意起点，ideas 只是其中一个来源 |
| 评审驳回改变实验状态 | 评审只是附在 run 上的意见标签 |
| 多重检验修正在每次结果中强制 | 保留为一键全算选项，必要时再用 |
| 新增 | 偏差记录（半句话，可选但推荐）——飞行记录仪的核心 |

---

## 13. As-built 状态（构建进度与偏差登记，2026-08-22 P0-4 后更新）

以代码为准。已完成与设计承诺的对应关系：

### 13.1 已实现（含本轮评审 P0 修复）

- registry.py：MLflow sqlite 账本。agent 默认直连（无 2s 探测税）；get-or-create 加跨进程锁；
  NaN/Inf 指标、非法 key、超长参数/标签全部带 tag 记录而不炸任务；mark_failed（RUNNING→FAILED）；
  heal-zombies（僵尸 RUNNING 清理）；export_json 原子写。
- queue.py：真并发（线程池 + 原子认领 + WAL + 部分唯一索引）；心跳 = 后台线程每 5 秒写
  '<epoch> <pid>'；heal 验活 PID（含 /proc 僵尸判定）后才动手，心跳缺失拒判（unknown）；
  超时 SIGTERM→SIGKILL 杀进程组并台账闭环；spec 漂移拒跑（QLAB_SPEC_DRIFT）；
  retry/unblock 打通 blocked 出路；notify 全量排空不丢事件。
- harness.py：smoke / log_legacy / eval_existing / train / sleep_ok / hang / crash 七种 action。
- trainer.py（P0-4 新增）：train action 真实训练。Alpha158 特征（parquet 缓存，按配置 hash 命名）→
  每种子 LightGBM（valid 早停）→ 样本外分块预测 → rank_mean 集成 → 产物落 results/runs/<exp_id>/。
  已实测：p2r_zz500_10d 全流程 done（RankIC 0.0385, p=0.034, 385 天）；p2r_all10d_ens3 基线复跑中。
- metrics.py（P0-4 新增）：核心 5 项指标（§4.1 形状）+ 全量（bootstrap p/deciles/季度/月度），
  逻辑与 legacy eval_pred.py 同源；core_metrics.json 含 expectation 对照结论。
- harness.py 另含（另一 agent 加）：未提交代码门禁 git_dirty_code（QLAB_UNCOMMITTED_CODE，exit 3）。
- backup.py：snap 含 jobs.db/mlflow.db 在线一致性副本 + run 制品 + manifest.json；
  push 只追踪最新 zip。
- board.py：--json --formal 视图（FINISHED 非 smoke 的正式研究行）。
- notify_bridge.js：容器时钟复核 + heal 状态机（healed→通知；unknown→人工核查通知；alive_but_stale→静默）。

### 13.2 已知差距（设计有、代码没有）

1. spec 校验器（exp_id 唯一 / base 可解析 / 结构合法）未实现，只有 load/merge/hash。
2. 偏差记录 deviations 落点未实现（设计 §10.4）；review 结果不进 run artifact；claims 联动未实现。
3. 失败自动重试（1 次）未实现，只有手动 retry；资源声明/背压调度（§9）未实现。
4. 产物布局部分实现：results/runs/<exp_id>/ 有 pred/label/meta/metrics/core/work，缺 review.json
   与 pred.parquet（现为 pkl，legacy 同源口径）。
5. 远程 runner（§9 远程机）未启用：runner!=local 一律 blocked，unblock 才能强制回本地。
6. 桥在主机无自动启动（重启后需手动拉起），长驻通知依赖人工值守。
7. 已知口径偏差（记录在 run meta.note）：train action 走 learn-processor 特征路径（与
   scripts/train_allmarket.py 完全一致，保证与 legacy 数字可比）；ref 里的 infer_processors
   暂未应用，未来要做 infer 口径的实验需先对齐此差异。

---

## 14. P5 执行器契约（2026-08-22 上线，取代 §13 的 trainer.py 路线）

用户定稿的设计：管线丝毫不管执行器内部，写执行器是 agent 的事。管线只管五样：
spec、队列、取数、固定测试器、入库。

- 数据固定菜单：pool（all/hs300/zz500）× handler（Alpha158/Alpha360）× 标签 × 窗口，
  由 pipeline/data.py 建成 float32 parquet 缓存（切片式内存友好构建）；执行器只许读，
  不许自造特征——board 上所有实验可比的根基。
- 执行器契约（executors/README.md）：executors/<name>/main.py --config <json> --train <pq>
  --test <pq> --out <dir>，输出 <out>/pred.pkl（(datetime, instrument) MultiIndex，列 score）；
  requirements.txt 存在时自动建独立 venv。参考实现 executors/_example_lgb（原 trainer 移植）。
- 链：取数 → 执行器 → 契约检查（QLAB_CONTRACT_FAIL）→ 固定测试器 pipeline.metrics
  （唯一指标来源；regression=IC 族，classification=日度 AUC 族）→ 台账自动入库
  （metrics 全部重算自 pred/label，执行器自报数字一律不采信）。
- expectation 通用化：旧式 {rankic_mean_min, p_le0_max} 与通用 {"metric": 路径, "min"/"max"}
  及列表形式并存。
- 远程 runner="spark"（DGX Spark docker）：P6 v1 代码完成、QLAB_SPARK_SSH 留空待配。
  pipeline/remote.py 流程 = git archive 打包 → scp → 远端 docker exec harness run
  --compute-only（只算不记账）→ rsync 回传 results/runs/<exp_id> → 本地 harness import
  记账（sqlite 单写者不变）；未配置时 blocked。本地彩排已跑通（p3c_rehearse_hs300_10d，
  compute-only + import 两段与本地直通数字一致）。
- 已知差距更新：§13.2 第 1/5 条部分解决（数据配置校验 QLAB_SPEC_INVALID；runner=spark
  有占位语义）；trainer.py 已删除，逻辑迁至 executors/_example_lgb；遗留旧脚本已按用户
  指示清理（train_allmarket/run_exps/eval_pred/gen_exps* 等 22 个）。

## 15. P8 交易闭环（2026-08-23，已上线）

- 交易产物契约：<out>/portfolio.pkl = 每日目标权重（MultiIndex (datetime, instrument)，
  列 weight，≥0、行和≤1）；测试器直接拿它跑回测，没有中间格式。
- 价格缓存：cache/prices_<pool>_<key>.parquet（qlib_data_src 的 hfq close；本地构建 +
  spark 预放，与特征缓存共用 manifest/修订号/原子写机制）。
- 四族测试器（pipeline.metrics）：prediction（原样）+ portfolio（turnover/HHI/n_held/
  weight_ic/top_decile_weight_frac/cash_frac）+ backtest（日频收益、gross vs net、
  sharpe/MDD/年化/超额/beta、月度表、费用明细）+ attribution（pred/align/cost/market
  四层 verdict，成绩差定位到具体层）。spec.metrics 勾选制（缺省 = prediction 全族）。
- 成本模型默认：佣金双边万2.5、印花税卖出千1、滑点1bp、收盘价成交次日生效
  （spec action.costs 可覆盖）；benchmark hs300→sh000300、zz500→sh000905、all→等权全体。
- 路由（D6）：runner 默认 auto = spark 优先、失败自动回退本地（事件 spark_fallback）；
  runner=spark 显式远程、不可达 blocked；runner=local 强制本地。
- 远端加固：harness SIGTERM→killpg 对齐本地超时杀组；QLAB_VENV_DIR 把执行器 venv 放
  出 repo（跨 dispatch 持久）；价格缓存本地构建 + scp 预放；特征缓存远端自建。
- 参考执行器：executors/_example_topk_portfolio（topK 等权 + 调仓缓冲）、
  executors/_torch_mlp（requirements 冒烟 + GPU 任务，torch 2.10 cu128 aarch64）。
- 验收：p8_trade_zz500_10d（spark 真实链，四族入账 + attribution）；
  p8_torch_hs300_10d（GPU 冒烟）；p8_smoke_spark（smoke kind 走远程）。