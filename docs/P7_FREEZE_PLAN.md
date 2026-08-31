# P7 平台冻结任务单（一次执行到位，之后平台封板）

> 目标：把 QLab 从"P0-P6 已上线但半收尾"改到"研究可用、冻结封板"状态。
> 本清单就是全部范围：只做下面这些，一件都不多做。
> 喂给 harness 的话术：`读 D:\quant_backup\P7_FREEZE_PLAN.md，逐条执行、逐块提交，任何一条与你判断冲突时停下问用户，禁止扩大范围。`
> 已并入 2026-08-23 审计筛选结果（见 §1.5）：防蠢的、顺手修的进清单；超范围的进 won't-do；限制执行器自由的删掉。

---

## 0. 环境与前置（先读，再动手）

- 权威仓库：容器 `hermes-1679f5b2` 内 `/root/quant`；主机 harness 工作目录 `D:\quant_backup`（仅作镜像，不是仓库）。
- 先全文读一遍根目录 `AGENTS.md`（容器与主机各有一份），操作规范以它为准。
- 命令约定（AGENTS.md 铁律）：docker exec 外层单引号、内层不要单引号；复杂脚本用 heredoc；`git status` 必须干净才允许跑真实训练（dirty gate 会让任务 QLAB_UNCOMMITTED_CODE 失败）。
- 每一步改动后先 `python -m py_compile` 相关文件，再跑测试，再提交。
- 本清单已获用户批准的范围**只含** T9 那一个 zz500 单种子训练（≤30 分钟）。其余训练、全市场、远程一律禁止。

## 1. 硬红线（违反 = 任务失败）

1. 不新增任何 pipeline 模块（只能改现有 12 个 .py）。
2. 不碰 `pipeline/remote.py`、spark 链路、MLflow 服务器、rqalpha/qlib_scripts 旧链路（P8 才动）。
3. 不跑全市场实验；不重试 `p2r_all10d_ens3`（两次 failed 保持原样）；不动 blocked 的 `b-p6-spark`。
4. 不改 `pipeline/metrics.py` 的统计口径（P7 只加 board 修正列，算法不动；测试器扩展是 P8 的事）。
5. 不改任何已跑过的历史 spec 文件内容；`experiments/refs/all10d_ens3.json` 的 evidence 口径与 infer 声明是**唯一被授权的例外**（见 T8）。新文件随便建。
6. 主机上禁止 `taskkill //IM node.exe`；重启桥接只用 `scripts/probe_node.ps1 -KillBridge` 精确杀（AGENTS.md §7）。
7. 每块结束 `git commit`；T9 前后各做一次 snap；最终 push。
8. 代码量预算：`pipeline/*.py` 总行数（`wc -l pipeline/*.py | tail -1`）终态 ≤ 2750，当前约 2513。每加一段功能要么带测试、要么删等量旧代码。
9. 遇到和本清单冲突的任何"顺手还能再改"的想法：写进 won't-do（T8 的清单），禁止实现。

## 1.5 审计筛选结果（为什么只有这些进本清单）

**并入本清单（防蠢 / 顺手 / 低成本）：** 数据幂等（修订号进 done 判定）、缓存原子写+校验、refs 基线口径改池口径并列、--once 文档改 --watch、删"重跑需批准"空承诺、通知两阶段（送达后才推进 marker）、单 dispatcher 铁律、spec 未知键警告、台账写锁、LightGBM deterministic、board 按 source 分视图、桥接环境变量化、batch_completed 通知、pipeline 测试进 CI、备份自动挂钩、claims 联动回写、契约拒绝常数预测。

**舍弃（写进 won't-do，附理由）：** dev/holdout 拆考场（20 个月测试段再切只剩约 12 个非重叠样本，p 值失去意义，n_variants+p_bonf 是正确尺度）；分块 bootstrap（接受 iid + 结论措辞保守）；walk-forward 定期任务（排到 P8 之后）；数据 v4 重建（先标注 caveat）；资源感知并发（写死"重任务 --concurrency 1"）；执行器级独立超时（外层杀进程组已兜底）；KB 换 sqlite/向量检索、假说生成器、supervisor 常驻、灾难恢复演练（个人规模不需要）。

**转入 P8：** 交易测试器（用户新目标）、spark 启用前置（远端超时杀组、requirements 冒烟）、组合层验证与归因。

---

## T0 基线检查与开工快照

1. 连通性：`docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && git status --short && git log --oneline -1'`
2. 基线测试：`docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pytest -q'`（旧 qbt 测试应 47 全绿；如果本来就红，记录并继续，不要修旧的）。
3. 记录基线：`docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.board --json --formal | python3 -c "import sys,json;print(len(json.load(sys.stdin)))" && wc -l pipeline/*.py | tail -1 && python -m pipeline.queue status --json | python3 -c "import sys,json;from collections import Counter;print(Counter(r[\"status\"] for r in json.load(sys.stdin)))"'`
4. 开工快照：`docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.backup snap`（不 push）。

## T1 执行器参数透传 + spec 未知键警告

背景：执行器契约说"内部自由"，但 spec 的 `params` 字段（META_KEYS 已有）从来没传进 executor_config.json。**注意命名冲突：`action.task` 已被 data.py 占用表示 regression/classification，透传只能用 `params`。** 另外审计-19：spec 字段名拼错会静默走默认值，必须加警告。

1. 改 `pipeline/harness.py`（train 分支 `cfg.update({...})` 处）：加 `"params": eff.get("_params", {})`。
2. 改 `pipeline/spec.py`：新增 `KNOWN_TOP_KEYS = {"exp_id","base","overrides","changes","hypothesis","pre_registration","idea","expectation","runner","timeout_min","action","params","metrics"}` 和 `validate_warn(spec)`——顶级未知键打 stderr 警告 `QLAB_SPEC_WARN unknown key: xxx`（**只警告不拦截**，保证执行器/研究自由）；`exp_id` 缺失、`base` 缺失才报错。`resolve()` 与 CLI 都调用它。
3. 改 `executors/README.md` §7 config 内容清单，加一行：`params —— spec.params 原样透传（供执行器自定义超参；管线不解释、不校验）`。

验收：
- `printf "%s" "{\"exp_id\":\"t_p1\",\"base\":{},\"params\":{\"lr\":0.01},\"action\":{\"kind\":\"smoke\"}}" > /tmp/t_p1.json && python -m pipeline.spec resolve /tmp/t_p1.json` 输出出现 `"_params"`。
- 故意写错键（如 `dataste`）再 resolve，stderr 出现 QLAB_SPEC_WARN。
- `python -m py_compile pipeline/harness.py pipeline/spec.py` 通过。
- commit：`P7-T1: executor params passthrough + spec unknown-key warnings`

## T2 数据幂等 + 缓存原子写（最小形态，不做目录指纹扫描）

背景：审计-6/22：数据更新后 `DATA_VERSION` 仍是手写 "v3"，队列 done 判定只看 exp_id+spec_hash，重新提交会被"假跳过"；审计-7：缓存分块写一半被杀（真实发生过 6 次 oom_kill）后，残缺文件被静默复用。**实现选最便宜的形态：唯一数据变更入口负责失效并推进"数据修订号"，而不是每次实验扫几十万个 bin 文件。**

改 `pipeline/data.py`：
1. 缓存清单 `cache/manifest.json`（映射 文件名 → {pool, key, built_at}），原子写（tmp + os.replace）。函数化：`_manifest_path/_load_manifest/_save_manifest`（都带 cache_dir 参数便于测试）。
2. `record_cache(pq_name, pool, key, cache_dir=CACHE_DIR)`。
3. **数据修订号** `cache/data_revision.json`：`data_revision()` 读整数（无文件=0）；`invalidate(pool=None)` 删除该 pool（或全部）parquet、更新 manifest、并 **revision+1 写回**（这是"数据变了"的唯一信号源）。
4. **原子写**：`_fetch` 的 ParquetWriter 改写到 `<cache_path>.part`，writer.close() 后 `os.replace(part, cache_path)`。
5. **读侧轻校验**：`ensure()` 对已存在 parquet 用 `pyarrow.parquet.ParquetFile(...).metadata.num_rows` 校验 num_rows>0 且 schema 含 `y` 列；不通过则删掉重建。
6. 迁移规则（关键）：旧缓存存在但 manifest 无记录 → 只登记 built_at="unknown"，**不重建**、不推进 revision。
7. CLI：`ensure --rebuild`；新增 `invalidate` 子命令（`--pool`/`--all`）。

改 `pipeline/queue.py`：
1. jobs 表加列 `data_rev INTEGER DEFAULT 0`（ALTER TABLE IF NOT EXISTS 风格，用 PRAGMA table_info 判断）。
2. `submit()` 时读 `data.data_revision()` 写入新行；**skip_done 判定改为：已有 done 行且 `data_rev == 当前修订号` 才跳过**——数据更新后同 spec 重新提交会正常重跑。
3. `retry()` 不受影响（它只管 failed）。

改 `scripts/update_tail.py`：
1. 删模块常量 START/END，加 `--start/--end` 参数（默认：start=今天-30天，end=今天）。
2. 每个 pool 更新完且新增 ≥1 个日期时（未传 `--keep-cache`），调 `invalidate(pool)` 并打印。
3. 加 `--keep-cache` 开关。

改 `pipeline/harness.py` `_import_run`：tags 加 `qlab.data_rev = str(data_revision())`（账本里可见数据修订）。

新增 `tests/test_data_cache.py`：record/invalidate 分池删除正确；revision 只在 invalidate 时 +1；原子写路径（构造 part 残留场景）。

验收：
- `python -m pytest tests/test_data_cache.py -q` 全绿。
- `python -m pipeline.data invalidate --pool 不存在的池` 空操作；`cat cache/data_revision.json` 存在。
- **不要真的 invalidate hs300/zz500**（T9 还要用）。
- commit：`P7-T2: data revision + atomic cache write + queue dedupe honors revision`

## T3 extra/ 特征旁路 + 契约拒绝常数预测 + LGB deterministic

背景：固定菜单没扩展口；审计-21：LGB 同 seed 不同线程数结果有浮点噪声；契约目前放行常数预测（审计-想法5 的 sanity 部分）。

1. 新建 `data/extra/README.md`：约定 `data/extra/<feature_name>.parquet`（MultiIndex (datetime, instrument)，数值列）；执行器自由读取 join，必须在 `<out>/run_info.json` 声明 `"extra_features": [...]`；管线只记录不校验。
2. `.gitignore` 加 `data/extra/*.parquet`。
3. 改 `pipeline/executor.py`：
   - 抽 `declared_extra_features(run_dir)`；`check_pred(pred_path, test_pq, run_dir=None)` 把声明写进 `rep["extra_features"]`，文件缺失只 warn。
   - **新增**：分数全常数（std==0 或唯一值 <2）→ issue 并 fail（`QLAB_CONTRACT_FAIL`）。
4. 改 `pipeline/harness.py`：`check_pred(run_dir/"pred.pkl", d["test_pq"], run_dir)`；tags 加 `qlab.extra_features`（空不写）。
5. 改 `executors/_example_lgb/main.py` params：加 `"deterministic": True, "force_row_wise": True`（本地/远程数字可对齐，代价是稍慢）。
6. `executors/README.md` 契约加第 8 条（extra 目录）与"常数预测会被契约拒绝"。

新增 `tests/test_extra_features.py`：declared_extra_features 存在/缺失/类型错三态。
新增 `tests/test_contract.py`：check_pred 对常数分数返回 ok=False。

验收：
- `python -m pytest tests/test_extra_features.py tests/test_contract.py -q` 全绿。
- commit：`P7-T3: extra feature convention + constant-pred rejection + LGB deterministic`

## T4 通知闭环：完成通知 + 两阶段送达 + 批次汇总 + 备份自动挂钩 + 桥环境变量化

背景：审计-31/17/29 + 缺口3.1。桥只报失败；marker 先记账后送信，POST 失败就丢；桥硬编码路径/容器名/端口；备份靠 agent 自觉，GitHub 已落后 60+ 提交。

1. 改 `pipeline/harness.py`：`_import_run` 末尾（仅 train 且非 compute_only）向 `results/queue/done.log` 追加一行 JSON：`{ts, exp_id, run_id, batch_id, job_id, rankic_mean, p_le0, expectation_check}`。
2. 改 `pipeline/queue.py`：
   - `DONE_LOG/DONE_MARKER` + `notify_done()`（同 notify() 结构）。
   - **两阶段协议**（notify 与 notify-done 共用）：`notify [--peek]` 只打印 marker 之后的行、不推进；`notify --ack <last_id>` 才推进 marker。默认不带参数 = peek（兼容旧的轮询）；桥必须"先送信、成功后 ack"。
   - `run()` 排空一批后（本轮有任何 claimed 行时）：调 `pipeline.backup.snap()`；若 `QLAB_GITHUB_TOKEN` 在环境中则 `backup.push(...)`，否则写一条 `backup_pending` 事件（status="backup_pending"，error 带说明）。可用 `QLAB_AUTO_BACKUP=0` 关掉。snap/push 失败只写事件，不阻塞队列。
   - 事件表允许 status 为任意字符串（本来就是 TEXT 列）。
3. 改主机 `D:\quant_backup\notify_bridge.js`（同步改容器副本）：
   - 顶部常量改为环境变量带默认值：`QLAB_NOTIFY_DIR`（默认 D:/quant_backup/notify）、`QLAB_CONTAINER`（默认 hermes-1679f5b2）、`QLAB_DSH_URL`（默认 http://127.0.0.1:3080）。
   - `pollEvents()` 与新增 `pollDoneEvents()`：peek → 组装文本 → `postToDsh` **成功后**再 dockerExec ack；POST 失败不 ack（下轮重发，靠 state 记录 lastAckedId 去重）。
   - 完成文本：`【QLab 队列】完成 N 个任务：- exp#job: rankic=... p=... expectation=...`；`backup_pending` 事件单独组装"备份未推送"文本。
   - **批次汇总**：对事件里的 batch_id，post 前查 `python -m pipeline.queue status --json`，若该 batch 无 queued/running 则追加一行"批次 <id> 全部终态：done N / failed M"（state 里按 batch 去重）。
4. 改 `AGENTS.md` §3 通知段：补完成通知、批次汇总、备份自动挂钩说明。

验收（不跑训练）：
```
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && printf "%s\n" "{\"ts\":\"test\",\"exp_id\":\"t_notify_done\",\"run_id\":\"x\",\"rankic_mean\":0.5,\"p_le0\":0.01,\"expectation_check\":\"met\"}" >> results/queue/done.log && python -m pipeline.queue notify-done --peek'
```
- 主机 `node notify_bridge.js --once`：inbox.jsonl 出现该条、DSH 收到 post、随后 `python -m pipeline.queue notify-done --peek` 返回空（已 ack）。
- `QLAB_AUTO_BACKUP=0` 下再 `--once` 一次确认不误触发 snap。
- commit：`P7-T4: done notify + two-phase ack + batch summary + auto backup hook + env config`

## T5 多重检验 n_variants + board 分视图 + summary 命令

背景：审计-1/23 + 想法-1。同假说家族试了多少变体无人算；board 三套口径（legacy IC / harness rankic / eval）混一张表；agent 每回合缺一个"一眼现状"的起手命令。

1. 改 `pipeline/harness.py` `run()/\_import_run()`：tags 加 `qlab.base_ref`（`ref:xxx` 取 xxx，否则 "inline"）、`qlab.hypothesis`（有则写）。
2. 改 `pipeline/board.py`：
   - `FIELDS` 加 `base_ref/n_variants/p_bonf/multiplicity_risk`；`rows()` 读 base_ref tag。
   - 纯函数 `_multiplicity(data)`：按 base_ref 分组（无 base_ref 的 legacy 组不参与修正），`n_variants`=组内 FINISHED 行数，`p_bonf=min(1, p_le0*n_variants)`，`multiplicity_risk = p_bonf>0.05`；p_le0 缺失跳过。
   - CLI 加 `--source harness|legacy|eval` 过滤（默认全量；AGENTS 规则"对比只在同 source 内进行"）。
   - 新增子命令 `summary`：一次输出 {runs 数、queue 各状态计数、claims untested 数、最近 5 条 done（exp/rankic/p）、backup_pending 数}——作为 agent 每回合起手式（引 queue/kb 模块，注意无循环 import）。
3. `AGENTS.md` §8：`p_bonf>0.05` 时结论必须降档为"有信号需确认"。

新增 `tests/test_board_multiplicity.py`：同 base_ref 3 行 p=0.02 → n_variants=3, p_bonf=0.06, risk=true；legacy 不修正；p 缺失不崩。

验收：
- `python -m pytest tests/test_board_multiplicity.py -q` 全绿。
- `python -m pipeline.board --json --formal | head` 出新列；`python -m pipeline.board summary` 有输出。
- commit：`P7-T5: n_variants + board source filter + summary cmd`

## T6 知识闭环：auto-claim + 既有 claim 联动回写

背景：缺口3.4 + 审计-想法3。claims 的 linked_exp_ids 没有任何写入路径，"读数据→更新认知"靠 agent 自觉。

1. 改 `pipeline/kb.py`（函数化、带 kb_dir 参数便于测试）：
   - `append_run_claim(...)`：每个 train run 追加一行 auto-claim（status="untested"，tags=["auto"]，linked_exp_ids=[exp_id]，source=auto:metrics.json）。
   - `link_claims(claim_ids, exp_id, expectation_check)`：对 spec 声明的 claim：linked_exp_ids 追加 exp_id；expectation met → status "confirmed"，not_met → "falsified"，n/a 或 claim 不存在 → 不动。写入时加 updated_at。
2. 改 `pipeline/spec.py` META_KEYS：加 `"claims"`（列表）。
3. 改 `pipeline/harness.py` `_import_run`：core_metrics 后调 append_run_claim + link_claims（`spec.get("claims")`，没有则跳过）。
4. `AGENTS.md` §5：写清 spec 可带 `"claims": ["c-0005"]`，跑完自动回写状态。

新增 `tests/test_kb.py`：append 一行；link_claims 对 met/not_met/缺失三种状态的行为正确；二次调用不重复追加 exp_id。

验收：
- `python -m pytest tests/test_kb.py -q` 全绿。
- commit：`P7-T6: auto-claim + spec.claims linkback with status update`

## T7 确定性评审自动接线 + 台账写锁

背景：AGENTS §5 承诺"默认跑 advisory"但无挂钩（审计补充项）；registry 写 run 无锁，spark 回传 import 与本地并发写时会撞（审计-20）。

1. 改 `pipeline/review.py`：`from_file(path)` 若有 `sample_window`，传进 `extra["window"]`（window_reported 检查对新 run 也能过）。
2. 改 `pipeline/harness.py` `_import_run`：`registry.log_run` 之前算 `checks = reviewmod.from_file(run_dir/"metrics.json")`；写 `run_dir/review.json`（{checks, passed, total}）；加入 artifacts；tags `qlab.review = "n/total"`。只记录不拦截。
3. 改 `pipeline/registry.py`：`log_run` 整体包进现有 `_locked()`（建实验名与写 run 共用一把锁）。

新增 `tests/test_review.py`：临时 metrics.json（含 sample_window/p_le0/n_days）→ window 与 p_value 两项 pass。

验收：
- `python -m pytest tests/test_review.py -q` 全绿。
- commit：`P7-T7: auto advisory review + ledger write lock`

## T8 冻结条款写入 AGENTS.md + 文档收编 + ref 口径修正

1. 编辑 `/root/quant/AGENTS.md` 追加"## 9. P7 平台冻结线"一节：
   - 冻结原则：管线只拥有"可比性三件套"（同一份数据、同一个评分口径、同一本账）；模型/预处理/集成/训练协议/特征工程/交易策略全部属于执行器自由。
   - 功能准入门槛三条件（痛感≥2次 / ≤3个实验的硬前置 / 净增≤0），否则写 won't-do；未经批准新增 pipeline 模块=违规。
   - 维护预算：pipeline/*.py 上限 2750；每个新功能带测试或删等量旧代码；连续 3 个真实实验零平台改动=平台完成。
   - won't-do 清单：dev/holdout 拆考场、分块 bootstrap、walk-forward 定期任务、数据 v4 重建、资源感知并发、执行器级超时、KB sqlite/向量检索、ideas 队列、ref 版本化、deviations 表、队列 DAG/优先级/自动重试、模型注册、deflated Sharpe、假说生成器、supervisor 常驻、灾难恢复演练。
   - 各能力定格表（训练/跑分/数据/队列/知识/评审/通知/备份 三列：定格位置 / 明确不做）。
   - 数据变更规则：唯一入口 update_tail（参数化）；更新自动 invalidate+修订号；手工改数据必须 `pipeline.data invalidate --pool <pool>`；改窗口=改 spec=新 spec_hash。
   - 队列运行规则：排空用 `run --watch`（`--once` 只领 concurrency 个）；同一时刻只允许一个 dispatcher；重新 submit 即重跑（无 --force 概念）；重资源任务必须 `--concurrency 1`；**无资源分配、纯排队（FIFO）**，不做资源感知调度。
   - 术语表（消除歧义）：`harness` 一律指 DeepSeek Harness 运行时；管线的实验执行进程叫"run 进程（pipeline.harness run）"；文档禁止写"harness 子进程"。
   - 通知驱动的研究循环（核心 SOP）：任务完成通知（单任务 done 或 batch_completed 汇总）→ 唤醒 agent 读数据（board diff）→ 搜资料/读资料 → 提炼假说（claims）→ 修改笔记（knowledge/notes/*.md，自由 markdown）→ 设计下一批实验。**允许 agent 忽略单个任务的完成通知以等一整组实验；batch_completed 到达才必须开启分析。做实验环节不需要任何通知。**
   - 会话登记：负责研究的 agent 每个新会话的第一件事 = 把会话 id 写入 `D:\quant_backup\notify\session.txt`（通知找不到家 = 循环断掉）。
   - 知识库分工：claims.jsonl = 假说与结论（管线自动回写）；knowledge/notes/ = 自由笔记（agent 维护 markdown）；不做三层知识库。
   - 结论纪律补充：board 对比只在同 source 内；p_bonf>0.05 降档；hit_rate 口径说明（绝对收益方向 + 截面均值定方向；sign(0) 记错的可忽略偏差已注明）。
   - 数据 caveat：v3 成分是当前名单，存在幸存者偏差（已退市 2 只补齐至退市日）；结论必须带此标注；v4(point-in-time) 排期未定。
   - 新会话第一件事：更新 `D:\quant_backup\notify\session.txt`。
   - 时钟判定描述与实现对齐（宿主时钟快速判断 + 容器内 heal 验活）。
2. `/root/quant/docs/PIPELINE_DESIGN.md` 与 `docs/QLAB_RULES.md` 顶部加归档横幅："v1.1 历史设计稿，与实现有出入；以根目录 AGENTS.md 为准"；同时新建 `knowledge/notes/README.md`（笔记区说明：claims 存假说与结论、notes 存自由笔记，agent 自行维护，markdown 自由格式）。
3. **授权修改 `experiments/refs/all10d_ens3.json`（本清单唯一允许动的历史文件）**：
   - evidence 改为三口径并列：全市场截面 RankIC 0.0859（自评口径）；hs300 子集 0.0361 (p=0.050)；zz500 子集 0.0408 (p=0.032)。note 注明"基线对比以目标池口径为准"。
   - 删除 processors 里的 `infer` 段（管线实际用 infer_processors=[]，声明=执行；避免后人照 ref 复现出不同结果）。
   - 注意：ref 内容变化会让基于它的 spec_hash 变化，历史 done 行不受影响，未来重跑用新 hash——在 changes/note 里写明即可，不要慌。
4. 复制本任务单进仓库：`docker cp D:/quant_backup/P7_FREEZE_PLAN.md hermes-1679f5b2:/root/quant/docs/P7_FREEZE_PLAN.md`。
5. 镜像同步到主机：`docker cp hermes-1679f5b2:/root/quant/AGENTS.md D:/quant_backup/AGENTS.md`。

验收：
- 容器与主机两份 AGENTS.md diff 为空。
- `grep -c "P7 平台冻结线" AGENTS.md` ≥1；ref evidence 含两个池口径。
- commit：`P7-T8: freeze charter + doc banners + ref evidence pool-first`

## T9 端到端验收训练（本清单唯一允许的训练，已预授权）

前提：T1-T8 全部 commit，`git status --short` 为空（否则 dirty gate 直接失败）。

1. 新建 spec `experiments/specs/p7_freeze_zz500_10d.json`（复制 p2r_zz500_10d 结构，改如下）：

```json
{
 "exp_id": "p7_freeze_zz500_10d",
 "base": "ref:all10d_ens3",
 "overrides": {"universe": "zz500", "seeds": [42],
               "dataset": {"handler": {"instruments": "csi500"}}},
 "params": {"probe": "P7-freeze", "note": "params passthrough 验收"},
 "action": {"kind": "train", "test_start": "2025-01-01", "test_end": "2026-08-20"},
 "changes": "P7 冻结验收：zz500 单种子端到端回归；验证 params 透传/新 board 列/auto review/auto claim/完成通知",
 "expectation": {"rankic_mean_min": 0.01, "p_le0_max": 0.2},
 "timeout_min": 30
}
```

2. 新建 batch `experiments/batches/b-p7-freeze.json`（specs 指向上面，timeout_min 30）。
3. 重启桥接（加载 T4 新代码）——**严格按 AGENTS.md §7**：
   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File D:/quant_backup/scripts/probe_node.ps1 -KillBridge
   cd /d/quant_backup && nohup node notify_bridge.js --interval 15 >> notify/bridge.log 2>&1 &
   ```
   验证 `tail -5 notify/bridge.log` 无报错。
4. 提交并执行（预估 ≤30 分钟，zz500 缓存已存在，不会全量重建）：
   ```
   docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue submit experiments/batches/b-p7-freeze.json && python -m pipeline.queue run --batch b-p7-freeze --once --concurrency 1 && python -m pipeline.queue status --json'
   ```
5. 完成后逐项核验：
   - job done；executor_config.json 含 params；review.json/core_metrics.json/contract_report.json 存在；
   - done.log 尾行含 p7_freeze_zz500_10d 及 rankic/p；主机 inbox.jsonl 出现完成通知；
   - claims.jsonl 尾行是 auto-claim；board --json --formal 该行带 base_ref/n_variants/p_bonf/multiplicity_risk 与 data_rev tag；
   - 指标接近历史 p2r_zz500_10d（rankic ≈ 0.0385，p ≈ 0.034；±0.01 属正常，不一致停下报告，不许改数字）。
6. 收尾：`python -m pipeline.backup snap`；commit：`P7-T9: freeze acceptance run`。

若 job failed：按 AGENTS.md 三板斧定位，只允许修本清单改动引入的 bug；环境问题停下汇报。

## T10 终检、CI、push、宣布冻结

1. 全量测试：`docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pytest -q'` 全绿。
2. 行数预算 ≤ 2750；`git status --short` 干净；`queue status --json` 无新增异常（b-p6-spark blocked 属预期）。
3. 若 `.github/workflows/ci.yml` 存在：加一步 `python -m py_compile pipeline/*.py` + 新增的 pipeline 单测（test_board_multiplicity/test_data_cache/test_extra_features/test_contract/test_kb/test_review）。不存在则跳过并记录。
4. `python -m pipeline.backup snap`；拿 token 推 GitHub：`QLAB_GITHUB_TOKEN=<token> python -m pipeline.backup push --message "P7 freeze: platform locked"`。
5. 输出冻结报告：改动文件清单、测试数、终态行数、T9 指标、won't-do 清单、数据修订号当前值。

---

## P7 之后

- 平台封板；下一个已批准阶段是 **P8 交易闭环**（见 D:\quant_backup\P8_TRADE_PLAN.md；D1-D5 已按 2026-08-23 建议定稿，执行前只需补齐两个外部输入：spark SSH 三变量、GitHub token 持久化）。
- 全市场基线复现（p2r_all10d_ens3，目标 rankic≈0.0859）并入 P8 的 spark gate：用户提供 SSH 走远程，或明确批准本地重试。
