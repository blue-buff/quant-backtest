# QLab 实验室使用规范（AGENTS.md）

> 本文件是 DeepSeek Harness 在本量化项目中的行为规范，会话开始时自动注入。
> 目的：防止 agent 遗忘项目规则后瞎折腾。铁律不可违背；其余为默认做法，偏离时记一行偏差记录。

## 1. 环境与位置（谁在哪）

- **权威仓库与实验环境 = 容器 hermes-1679f5b2 内 /root/quant**。
  实验代码、数据、台账、队列都在容器里。跨容器执行一律走封装工具（§7 工具链）：
  `scripts/qexec.sh [-p python3|-s] <脚本文件> [args...]`（宿主执行），
  或直接 `docker exec -i -w /root/quant hermes-1679f5b2 python -m ...`。
- **工具链默认本机直跑（2026-08-30 起，不加 docker）**：qexec.sh / notify_bridge.js /
  webui.js 默认本地执行（cwd=QLAB_ROOT，自动探测仓库根与 venv），docker 仅在显式
  `QLAB_*_CONTAINER=1` 时启用（Windows 主机等仍有容器的环境）；QLAB_ROOT/QLAB_QLIB_DATA
  自动探测（env → 容器路径（存在时）→ 仓库根/本机数据）。本机（macOS）无容器直跑已验证全绿，
  详见 docs/LOCAL_RUN.md。
- **主机 D:\quant_backup**：harness 工作目录，文件暂存 + 备份镜像（无 git 历史）。
  传文件用 docker cp：`docker cp "D:/quant_backup/xxx" hermes-1679f5b2:/root/quant/`。
- **4090D 远程机（song@10.110.12.99）**：默认不动。只有用户明确说"上远程"才可碰，
  且只碰 C:/Users/song/qbt_work，不碰系统、不用 GPU、不杀别的进程。
- **DGX Spark 远程机**：`ssh -J song@10.110.12.99 -p 2223 dev@10.0.0.5`，
  落点直接是计算容器（无 docker exec）。跨机执行用 `scripts/qremote.sh <脚本文件> [args...]`。
  - dispatcher 启动环境变量：
    QLAB_SPARK_SSH=dev@10.0.0.5 QLAB_SPARK_SSH_PORT=2223 QLAB_SPARK_JUMP=song@10.110.12.99
    QLAB_SPARK_WORKDIR=/home/dev/quant
    QLAB_SPARK_PYTHON=/home/dev/.local/share/mamba/envs/quant/bin/python
  - 流程：git archive 打包（**只含已提交文件，新 spec/执行器必须先 commit 再 submit**）→
    scp → 远端解包到**每 job 独立目录** jobs/job_<id>/repo（cache/ 软链共享
    /home/dev/quant/cache，venv 共享 executor_venvs）→ 远端 harness --compute-only
    （取数/执行器/契约/固定测试器，只算不记账）→ 远端 tar → scp 回传 → 本地
    harness import 记账。成功即删 job 目录；失败保留现场 + run 目录 tar 保回
    results/remote_fail/；executor.log 边跑边流式落盘。
  - 配额实测（2026-08-24）：CPU 16 核硬配额；内存 48GiB 硬上限（free -h 的 121Gi 是
    宿主视图，重活前看 /sys/fs/cgroup/memory.current）；/dev/shm 仅 64MB（torch
    多进程取数必挂，统一 n_jobs=0）；GPU 1×GB10 无配额、显存是统一内存并计入内存配额；
    CUDA 13.0 / torch 2.10.0+cu128，有 capability 12.1 vs 支持上限 12.0 的警告
    （PTX JIT 前向兼容，训练正常，已接受；下次升级 torch 复验）。
  - 远端环境：micromamba 环境 quant（pyqlib 0.9.7 源码编译，aarch64 无 wheel；
    PyPI 上 qlib 停更在 0.0.2.dev*，0.9.x 以 pyqlib 包名发布）；数据 bins 在
    /home/dev/quant/qlib_data；pip 走清华镜像，torch CUDA 轮子走阿里 cu128 镜像；
    无 rsync/sudo/bzip2/strace（重建教程 docs/remote_rebuild_runbook.md）。
  - 路由：runner 默认 auto = spark 优先、不可达自动回退本地（事件 spark_fallback）；
    runner=spark 显式远程，不可达 blocked；runner=local 强制本地。
- **GitHub 备份**：blue-buff/quant-backtest。**main 只推干净代码**（GIT_PATHS，不含任何
  results/ 产物）；数据快照（snaps/*.zip + manifest + board.csv + registry_export.json）
  由 push 打全量 commit **force-push 到 backup 分支**（refs/heads/backup，main 不受影响）。
  token 从主机拿（gh auth token），容器备用文件 /root/quant/.qlab_github_token（600，
  gitignored），本机备用 .qlab_github_token（仓库根，gitignored）。网络恢复后补推。
  已知债务：旧 snap zip 仍留在 main 历史里（历史无法缩；如需彻底清除需 filter-repo 重写 + 强推）。
- **人类看板（WebUI）**：主机 `node scripts/webui.js`（默认 http://127.0.0.1:8099，
  端口/间隔可配）。只读旁观：总览/队列/台账/claims/事件，无操作入口，
  不写任何容器文件、容器内零常驻进程——研究链路不受影响。

## 2. 铁律（不可违背）

1. 诚实：禁止编造、美化、篡改任何实验数据或结论。用户会派另一个 agent 复查。
2. 双备份：每次实验或重要变更后必须本地快照 + commit + push GitHub（§6）。
3. 机器可读：实验产出必须进 MLflow 台账（标准 tags/metrics），不靠散装日志。
4. 烟雾测试先行：新脚本/新管线先跑最小验证，通过后才上真实任务。
5. 不跑长任务：任何训练/全市场任务必须显式经用户批准，且先给耗时估算。
6. 不碰远程机：除非用户明确说"上远程"。

## 3. 台账、队列与实验操作

**台账（MLflow）**
- sqlite:////root/quant/mlflow-server/mlflow.db。agent 默认 sqlite 直连
  （registry 无探测、无 2 秒税）；UI 服务仅人类看板时按需启动
  （scripts/mlflow_server.sh start/stop，启动 ~40 秒、占 ~2GB 内存）。
- 写入防丢：NaN/Inf 指标跳过打 qlab.dropped_metrics；超长参数截断打 qlab.truncated；
  非法 key 捕获打 qlab.invalid_keys。看正式结果用 board --json --formal
  （只含 FINISHED 非 smoke 行）。
- 查账：python -m pipeline.board（导出 results/board.csv，主入口）。

**队列（results/queue/jobs.db，WAL）**
- 命令：submit / status / run / retry / unblock / cancel / heal / show / events。要点：
  · 幂等：同 exp_id+spec_hash 已 done 且 data_rev 相同 → submit 自动跳过；
    同一 spec_hash 只允许一个活跃行（部分唯一索引），并发双提交不会重复执行。
  · run --once 只领一轮（concurrency 个）即退，排空用 run --watch；
    同一时刻只允许一个 dispatcher；重资源任务 --concurrency 1；纯排队 FIFO。
  · retry：默认重排 failed（attempts<3）；--blocked 只重排 blocked（不再连坐 failed）；
    --blocked --failed 两者都排；同 hash 多失败行只重排最新一行。
  · unblock：blocked→queued 强制回 local；spark 任务用 retry --blocked 保留 runner
    （可回远端重试）。
  · cancel <job_id...>：终态 cancelled（queued/blocked 直接转、running 先杀进程组）；
    批次汇总计入 cancelled。废弃任务永久取消 = status='failed' + attempts=3 +
    note 写原因。
- 故障排查三板斧：show <job_id>（error+日志尾+事件链）/ events --since N / heal
  （dispatcher 死后对账：验活 PID 才动手，僵尸进程视为已死；心跳 >3×周期且 PID
  已验证死亡 → 立即 heal，不必等满 5 分钟窗口）。
- 强制检查点：① 每次 run --once 一轮返回后立刻 status --json；② 每回合开始
  events --since <上次 id>；③ 修完代码重投前先 show 看错误原因；重投 = 重新 submit
  （幂等，只补失败的）。
- 卡死由 timeout_min 兜底（默认 120 分钟）：超时先 SIGTERM 等 10 秒再 SIGKILL
  整组杀（无孤儿），台账 run 显式置 FAILED（qlab.failed_reason 记原因）；
  测试用 QLAB_QUEUE_TIMEOUT_SECONDS 压到秒级。

**实验流程（train action，执行器契约）**
- 写 spec（必填 exp_id + base；推荐补 changes/hypothesis/expectation）→ 放进 batch →
  submit → run --once → status 核验 → 入账完成。
  spec 规则：exp_id 全局唯一且必须单路径段（^[A-Za-z0-9][A-Za-z0-9_.-]*$，防路径穿越）；
  base 用 "ref:xxx" 引用 experiments/refs/；允许多变量变更，但必须写 changes 说明。
- 管线只管五样：spec、队列、取数、固定测试器、入库。执行器内部是什么管线丝毫不管。
  spec 写 {"action": {"kind": "train", "executor": "executors/<name>", "task": ...}}；
  不写 executor 默认 executors/_example_lgb。执行器契约见 executors/README.md：
  main.py --config <json> --train <pq> --test <pq> --out <dir> → <out>/pred.pkl
  （(datetime,instrument) MultiIndex，列 "score"；交易型另出 portfolio.pkl，列 "weight"，
  非负、行和 ≤1）；有 requirements.txt 自动建独立 venv（results/venvs/<name>，
  stamp 保证依赖变更自动重装）。
- 数据固定菜单：pool（all/hs300/zz500）× handler（Alpha158/Alpha360）× 标签 × 窗口，
  由 pipeline.data 建成 float32 parquet 缓存；执行器只许读，不许自造特征（保证可比）。
- 链：取数 → 执行器 → 契约检查（不过则 QLAB_CONTRACT_FAIL）→ 固定测试器
  pipeline.metrics（唯一指标来源，回归=IC 族/分类=AUC 族/交易=四族+attribution）→
  台账自动入库（tags 记 qlab.executor/qlab.handler/qlab.task/qlab.data_key）。
- 产物：results/runs/<exp_id>/（pred.pkl、label_matrix.pkl、executor.log、
  contract_report.json、metrics.json、core_metrics.json、work.json、spec.json），
  run 目录内**每个文件**（模型、run_info.json、portfolio.pkl、spec 原件）全部入
  台账 artifacts。测试器 bootstrap 种子固定 42（params.tester_seed 可见）。
- expectation 通用化：{"rankic_mean_min": 0.05, "p_le0_max": 0.5}（旧格式）或
  {"metric": "rankic_mean", "min": 0.05} / 列表形式，自动对照给结论。
- backfill 仅用于历史实验入账；新实验一律走 train action。

**通知系统（主机 notify_bridge.js，两阶段协议）**
- 常驻：node notify_bridge.js --interval 15（日志 notify/bridge.log）；测试单次 --once。
- 两阶段：notify/notify-done 默认 peek（只读 marker 之后的行），--ack <id> 才推进
  marker（只进不退）；桥必须"先 POST 到 DSH、成功后 ack"，POST 失败下轮重发。
- 通知类型：任务失败【QLab 队列通知】/ 任务完成（done.log 行）/ --once 轮结束
  【本轮结束：done X / failed Y / blocked Z，队列剩余 N】/ blocked 任务每 job 只提醒
  一次（离开 blocked 后重置）/ 批次全部终态汇总（done/failed/blocked/cancelled）/
  【QLab 备份】未推送提醒。全部追加 notify/inbox.jsonl，失败另弹 Windows toast。
- 自动备份挂钩：队列排空一批后自动 snap；有 token 则 push（失败 1h 退避并记事件），
  无 token 记 backup_pending（同 1h 退避，不刷屏）；QLAB_AUTO_BACKUP=0 可关。
- 心跳/自动 heal：dispatcher 心跳 = 后台线程每 5 秒写 '<epoch> <pid>'；桥发现可疑先
  读容器时钟复核再调 heal（验活 PID 才动手，长任务不会被误杀）；心跳文件缺失 →
  heal 拒绝自动判定（unknown），桥发"人工核查 dispatcher"通知；dispatcher 确认死亡 →
  自动 heal（杀孤儿进程组 + 台账置 FAILED）+ 通知。
- 会话登记：负责研究的 agent 每个新会话第一件事 = 把会话 id 写入
  D:\quant_backup\notify\session.txt（Windows 主机；否则通知找不到家）。
  本机（无容器）默认写 <repo>/notify/session.txt。做实验环节不需要通知；
  允许忽略单个完成通知等一整组实验，batch_completed 到达才必须开启分析。

## 4. 数据与版本

- 数据 v3：2021-06-01 起，尾部随 scripts/update_tail.py 滚动（增量 ≥2 行才更新；
  指数成分用 index_stock_cons_sina）；bins 在 /root/.qlib/qlib_data（hs300/zz500/all）。
  caveat：v3 成分是当前名单，存在幸存者偏差（已退市 2 只补齐至退市日），结论必须带此标注。
- 特征缓存：cache/*.parquet（train/test 按配置 hash 命名），重跑直读；改 handler/label/
  窗口自动新 key。数据修订号跟随 update_tail 的 invalidate；run 入账带 qlab.data_rev
  （取数时记录，非入账时现读）。
- 所有 run 必须带 qlab.data_version 和 qlab.git tag（harness 自动写，git=取数前 pin 的 commit）。

## 5. 评审与知识库

- 默认 advisory 检查：python -m pipeline.review run <run_id>（确定性 checklist，随 run 入库）。
- 三种场景做深审（P2 上 LLM red-team）：结论要汇报用户 / 配置要升 refs 基线 / 结果好得可疑。
- 知识库 knowledge/claims.jsonl：新读资料提炼 claims 入库
  （python -m pipeline.kb add --text ... --source ...）；实验结论回写 claim 状态
  （confirmed/falsified/partial）；检索 pipeline.kb search <词>。
- 知识闭环（自动）：每个 train run 入库自动追加一行 auto-claim（untested，tags=["auto"]）；
  spec 可带 "claims": ["c-0005"]，跑完自动回写（met→confirmed、not_met→falsified、
  n/a→不动）。首次使用先 pipeline.kb init。

## 6. 收尾动作（每轮 / 每次实验后，强制）

~~~
.venv/bin/python -m pipeline.backup snap      # 本机直跑；容器环境用 docker exec 等价命令
    # 含 jobs.db/mlflow.db 在线一致性副本 + run 制品（>50MB 除外，清单在 zip 内 manifest.json）
    # snap 只写本地 results/backup/，不再提交任何 git（main 保持纯代码）
.venv/bin/python -m pipeline.backup push --message "..."   # 代码推 main + 数据快照推 backup 分支
    # token：QLAB_GITHUB_TOKEN env / 仓库根 .qlab_github_token（本机）/ 容器 /root/quant/.qlab_github_token
    # 网络不通时 push_watch.sh 守望自动补推（主机暂存仓库 push_stage.git）
    # 已知债务：旧 snap zip 仍留在 main 历史里（历史无法缩；彻底清除需 filter-repo 重写 + 强推）
~~~

- commit message 必须带 batch_id / exp_id 或本轮主题。
- 汇报格式：跑了什么、结果数字（带窗口与 p 值）、台账位置、备份状态。

## 7. 已趟平的坑（别再踩）

**工具链（主机侧）**
- pwsh → docker → sh → ssh 引号吞噬/CRLF 污染：**跨容器/跨机一律 scripts/qexec.sh /
  qremote.sh（脚本文件模式，零内层引号，参数 argv 直传）**；工具不可用时退化
  "写脚本文件（LF 结尾）+ docker cp/scp + 按路径执行"。禁止单行嵌套多级引号/管道；
  heredoc 里写换行用 chr(10) 别写 \n；grep 用 -e 模式别用引号包 pattern。
- MSYS 吃反斜杠；远程 Windows 路径一律正斜杠；docker cp 到已存在目录会嵌套一层，
  镜像备份后核对落点。
- Git Bash 下 `> NUL` 会创建字面文件 NUL（Aug 21 实锤漏进一行 ssh host key）：
  丢弃输出一律 `>/dev/null`，根目录发现 NUL 文件直接删。
- 主机进程操作：**禁止 taskkill //IM node.exe**（DSH 运行时和桥接都是 node 进程，
  已实锤杀过自己）。重启桥接：powershell -File D:/quant_backup/scripts/probe_node.ps1
  -KillBridge 精确杀，再 cd /d/quant_backup && nohup node notify_bridge.js --interval 15
  >> notify/bridge.log 2>&1 &。
- 远程 Windows（4090D）：OpenSSH 会话关闭会杀子进程，必须 Scheduled Task；
  权限用 icacls；远程 pyqlib 配 numpy 1.26.4（numpy 2.5 会崩）。

**容器/平台**
- 本地容器 12GB 内存 + pids.max=256（线程也计入）：全市场整批加载 OOM、官方
  num_threads=20 模型 fork 失败；大活走远端。
- 常驻 dispatcher 是长命进程：改 pipeline/*.py 后必须重启 watch dispatcher——旧进程
  跑旧代码，会用旧行为记账/备份（2026-08-24 实锤：04:05 启动的旧 dispatcher 在脱敏
  修复后仍把 token 原样写进事件）；重启后 ps 确认只有一个 dispatcher，且带齐
  QLAB_SPARK_* 环境变量。
- spark dispatch 只打包已提交文件（git archive HEAD）：新/改 spec、batch、执行器必须
  **先 git commit 再 submit**，否则远端 FileNotFoundError（本地 submit 读工作树所以能过）。
- 仓库根目录放临时 .py 会触发脏代码门禁挡 import：scratch 放 /tmp、容器外或非代码后缀。
- MLflow 3.x 废弃文件目录存储，用 sqlite 后端（旧数据已 migrate-filestore）；
  容器 numpy 2.5.2 正常，不要动。
- qlib 特征/标签窗口：特征用到未来信息是泄露；te 段预测 infer_processors=[] 只取特征。
- 排队期间改 spec 文件 → harness 拒跑（QLAB_SPEC_DRIFT，写明新旧 hash）；改回原文件后
  retry，或重新 submit 记录新配置。hang action 自然结束必失败（预期行为，专测超时）；
  测队列机制用 sleep_ok action（入账标 smoke）。

**远端（DGX Spark）**
- /dev/shm 64MB（docker 默认配额，与内存配额无关）：num_workers>0 且 batch 较大时
  torch collate 共享内存分配失败/挂死（GPU-Util 0%、CPU 近 0%、etime 只增 = 卡死不是慢）；
  torch 任务一律 n_jobs/num_workers=0。GB10 nvidia-smi 显存列 Not Supported 正常，
  GPU-Util 才是活性指标。
- 无 rsync/sudo/bzip2/strace：回传用 tar+scp 单流；要恢复多进程取数/装工具按
  docs/remote_rebuild_runbook.md 重建容器（--shm-size + strace）。
- executor.log 已流式落盘（失败/超时现场在盘上）；远端计算失败时 run 目录 tar 保回
  results/remote_fail/；每 job 独立目录失败不删、成功才删。调试仍建议用 /home/dev 下
  不随 repo 重建的路径 + 独立复现脚本。
- qlib 的 R.start 是 contextmanager：裸调是空操作，必须 `with R.start(...)`。

**安全/卫生**
- GitHub token 禁止明文落盘：push 暂存仓库 remote url、队列事件 error、任何日志都不得
  出现 oauth2:gho_***；写事件/日志前脱敏（`https://oauth2:[^@]+@` → `https://***@`）。
  旧 token 已扩散进 git 历史与快照，**轮换（gh auth refresh）是唯一根治**（挂账 P0）。

## 8. 结论表述纪律

- 措辞不得强于证据；显著不等于能用；报指标必须带样本窗口和 p 值。
- 零结果同样入账（falsified 也是成果）。
- 单票方向 hit 期望约 0.5 + IC/pi，0.49-0.50 的 hit 是正常数学现象，不是 bug。
- 试了 N 个变体后，解读 p 值必须考虑多重检验：board 同 base_ref 的 n_variants /
  p_bonf / multiplicity_risk 列（p_bonf = p×n_variants；smoke 行与同 exp 重复行不计入）；
  p_bonf>0.05 时结论必须降档为"有信号需确认"，不得宣称显著。
- 预注册（expectation）写了就要对照，没写就标记 exploratory，不许事后改口径。
- board 对比只在同 source 内（--source 过滤）；hit_rate 口径：绝对收益方向 + 截面均值
  定方向（sign(0) 记错的可忽略偏差已注明）。

## 8.5 交易实验 SOP（P8 交易闭环）

- 交易型 spec：metrics 勾 ["rankic", "bootstrap", "portfolio", "backtest",
  "attribution"]（缺省 metrics = prediction 全族，向后兼容）；
  expectation 可用别名：sharpe_min / excess_ann_min / mdd_max / turnover_max /
  cost_drag_max（与 _min/_max 语法一致）。
- 成本模型默认：佣金双边万2.5、印花税卖出千1、滑点1bp、收盘价成交次日生效；
  spec action.costs 可覆盖（commission/stamp/slippage）。
- benchmark：hs300→sh000300、zz500→sh000905（数据已有）；all 池 = 等权全体持仓收益。
- 统计纪律（防 p-hacking，铁律）：测试段月频约 20 个收益点，Sharpe/MDD 估计误差极大；
  backtest 数字不得单独支撑 refs 升级，必须 expectation 预注册 + n_variants 一起看；
  对着 backtest 反复调参 = p-hacking，正式结论必须走一次未碰过的配置。
- attribution.json 是定位责任的读数：pred（rankic/p）/ align（weight_ic、turnover）/
  cost（年化成本拖累）/ market（超额、beta）四层各给一个 verdict + 数值；
  成绩差先看 attribution，不要默认怪信号。

## 9. P7 平台冻结线（2026-08-23 生效）

- 冻结原则：管线只拥有"可比性三件套"——同一份数据、同一个评分口径、同一本账；
  模型/预处理/集成/训练协议/特征工程/交易策略全部属于执行器自由，管线不 inspect。
- 功能准入门槛三条件：痛感≥2 次 / 是 ≤3 个实验的硬前置 / 净增行数≤0（新功能要么带
  测试、要么删等量旧代码）——三条都不满足的新想法写进 won't-do；
  未经批准新增 pipeline 模块（现有 12 个 .py 之外）= 违规。
- 维护预算：pipeline/*.py 总行数上限 3600（2026-08-23 封板目标）；每个新功能带测试或
  删等量旧代码；连续 3 个真实实验零平台改动 = 平台完成。预算审计（实际增量全部是
  用户明令的功能）：
  · P7 +538 / P8 +547，两阶段合计 +1085、删旧 -125，封板终态 3709；
  · qb 官方 benchmark 复现批（2026-08-24，用户明令"测试阶段可改管线"）+57 → 4067；
  · 审计修复批 A（2026-08-24 晚，15 项审计按用户裁决执行：spec_hash 口径、并发锁、
    exp_id 校验、data_rev 语义、board 去重、退避、--once 语义、heal 守卫、流式日志、
    spark 每 job 目录、cancel、SIGTERM 探针、snap 自提交）+184 → **当前实测 4251**。
  未来新功能仍按准入三条件，净增必须带审计说明。
- won't-do（明确不做）：dev/holdout 拆考场、分块 bootstrap、walk-forward 定期任务、
  数据 v4 重建、资源感知并发、执行器级独立超时、KB sqlite/向量检索、ideas 队列、
  ref 版本化、deviations 表、队列 DAG/优先级/自动重试、模型注册、deflated Sharpe、
  假说生成器、supervisor 常驻、灾难恢复演练。
- 各能力定格表：

  | 能力 | 定格位置 | 明确不做 |
  |---|---|---|
  | 训练/跑分 | executors 契约 + pipeline.harness/metrics 固定测试器（P8 四族+attribution 已上线） | 统计口径再改动需重新批准 |
  | 数据 | pipeline.data 固定菜单 + cache manifest/修订号 | 数据 v4 重建（先标注 caveat） |
  | 队列 | pipeline.queue FIFO 单 dispatcher（--once/--watch/cancel/retry/unblock/heal） | 优先级/DAG/自动重试/资源感知调度 |
  | 知识 | knowledge/claims.jsonl + auto-claim/linkback | KB 换 sqlite/向量检索、三层知识库 |
  | 评审 | pipeline.review advisory + review.json 入库 | LLM red-team 自动化（手动深审保留） |
  | 通知 | notify_bridge.js 两阶段 + done/轮结束/批次汇总/blocked 提醒 | supervisor 常驻 |
  | 备份 | backup snap/push + 队列排空自动挂钩 | 灾难恢复演练 |

- 数据变更规则：唯一入口 scripts/update_tail.py（--start/--end 参数化）；更新自动
  invalidate+修订号；手工改数据必须 python -m pipeline.data invalidate --pool <pool>；
  改窗口 = 改 spec = 新 spec_hash。
- 队列运行规则：排空用 run --watch（--once 只领一轮并写 round_end 通知；watch 每完成
  一个任务发完成通知；blocked 每 job 只提醒一次）；同一时刻只允许一个 dispatcher；
  重新 submit 即重跑（无 --force 概念）；重资源任务必须 --concurrency 1；
  纯排队 FIFO，无资源分配/调度。
- 术语表（消除歧义）：harness 一律指 DeepSeek Harness 运行时；管线的实验执行进程叫
  "run 进程（pipeline.harness run）"；文档禁止写"harness 子进程"。
- 通知驱动的研究循环（核心 SOP）：任务完成通知（单任务 done 或 batch_completed 汇总）
  → 唤醒 agent 读数据（board diff）→ 搜资料/读资料 → 提炼假说（claims）→ 修改笔记
  （knowledge/notes/*.md，自由 markdown）→ 设计下一批实验。允许 agent 忽略单个任务的
  完成通知以等一整组实验；batch_completed 到达才必须开启分析。做实验环节不需要任何通知。
- 知识库分工：claims.jsonl = 假说与结论（管线自动回写）；knowledge/notes/ = 自由笔记
  （agent 维护 markdown）；不做三层知识库。

## 10. qlib 官方 benchmark 复现经验（qb 批次，2026-08-24）

- 官方参数唯一来源 = microsoft/qlib 的 examples/benchmarks/<Model>/workflow_config_*.yaml。
  主机直连 GitHub 常不通（梯子依赖），用 gitee 镜像克隆：git clone --depth 1
  https://gitee.com/mirrors/qlib.git。本项目已 vendor：yaml 在 experiments/qlib_official/；
  官方预训练 LSTM 检查点（GATs/HIST/IGMTF 的 base）在 executors/qlib_bench/benchmarks/
  ——*.pkl 会被 .gitignore 挡，必须 git add -f 才进 git archive。
- 执行器模式（executors/qlib_bench，复用于今后任何 qlib 模型）：官方模型类原样 import +
  官方 fit/predict 直跑；特征经 DataHandlerLP 兼容 shim（ParquetHandler.fetch）从管线
  parquet 转接，执行器不自造特征。0.9.7 接口坑位与 torch 2.10 兼容全部在 shim 里做
  （禁止改官方模型代码）；shim 逐点清单 + vendor 版本钉死见 executors/qlib_bench/VENDOR.md，
  逐点回归测试 tests/test_qlib_shim.py。
- 环境：pyqlib 无 aarch64 wheel → qlib 从环境解释器 site-packages 注入；requirements
  venv 只装 torch（阿里 CUDA 镜像）/xgboost/catboost/pytorch-tabnet（清华源均有轮子）。
- 口径对齐最小集（与官方榜单可比的基础）：官方 label Ref($close,-2)/Ref($close,-1)-1
  （horizon 1）；官方 learn/infer processors 原样写进 spec；test 用 DK_I 取数；
  infer 处理器统计量必须拟合在训练期——test 缓存从 fit_start 整期加载再按 selector 切窗
  （data.py 已实现；按切片直建 = 全零特征）。
- 官方配置就是慢：200 epochs + 早停，35 模型单 GPU 串行纯计算 5-6 小时。批量前先统一
  跑一遍全部 spec 的 data ensure（prewarm 全部缓存 key）再提交批次；spark 已支持每 job
  独立目录 + 共享 cache 软链，--concurrency>1 有真实增益（历史串行锁已移除）。
- 已核实不可部署（别重试）：TFT（tensorflow-gpu 1.15 vs py3.12）、
  HIST（概念矩阵需 GitHub 下载 + stock_index 与官方 2020 成分绑定，网络恢复后单独立项）、
  TCTS（0.9.7 官方实现维度 bug，修复需改模型数学）。
- 结果与全量偏差记录：knowledge/notes/qb_benchmark_repro.md（执行/偏差/摩擦账/挂账）、
  knowledge/notes/qb_benchmark_results.md（35 模型全表）；未决问题清单在
  qb_benchmark_repro.md「未解决问题挂账」节。
