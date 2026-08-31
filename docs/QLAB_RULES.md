# QLab 实验室使用规范（AGENTS.md / QLAB_RULES.md）

> 本文件是 DeepSeek Harness 在本量化项目中的行为规范，会话开始时自动注入。
> 目的：防止 agent 遗忘项目规则后瞎折腾。铁律不可违背；其余为默认做法，偏离时记一行偏差记录。

## 1. 环境与位置（谁在哪）

- 权威仓库与实验环境：容器 hermes-1679f5b2 内 /root/quant。
  所有实验代码、数据、台账、队列都在容器里，用 docker exec 操作：

~~~
docker exec -i hermes-1679f5b2 sh -c '...'          # 单条命令
docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.queue status
~~~

- 主机 D:\quant_backup：harness 工作目录，文件暂存 + 备份镜像（无 git 历史）。
  往容器传文件用 docker cp，例如 docker cp "D:/quant_backup/pipeline" hermes-1679f5b2:/root/quant/。
- 4090D 远程机（song@10.110.12.99）：默认不动。只有用户明确说"上远程"才可碰，
  且只碰 C:/Users/song/qbt_work，不碰系统、不用 GPU、不杀别的进程。
- DGX Spark 远程机（GB10 128GB，容器内 nvidia-smi 可见，CUDA 13.0）：已实测跑通
  （p6_spark_hs300_10d, job 33）。链路 = ssh -J song@10.110.12.99 -p 2223 dev@10.0.0.5，
  落点直接是计算容器（无 docker exec）。上传 ~2MB/s、下载 ~6MB/s（家用带宽）。
  · 容器 cgroup 配额实测（2026-08-24）：CPU 16 核硬配额（cpu.max=1600000/100000，
    cpu.stat 显示已被节流 320+ 秒——是真的会撞顶）；内存 48GiB 硬上限
    （memory.max=51539607552，memory.high=max 无软节流；free -h 显示 121Gi 是
    宿主视图，别信，重活前看 memory.current）；pids.max=153547；/dev/shm 仅 64MB；
    磁盘宿主共享 ~3TB 可用；GPU 1×GB10 无配额机制（无 MIG、无利用率上限），
    与宿主其他任务纯抢用；GB10 显存是统一内存，GPU 显存分配计入 48GiB 内存配额。
    配额 = "天花板+按用计费"（空闲让得出、满载拿不全），不是预留，不会动态调整。
  · dispatcher 启动须带环境变量（host 侧 MSYS_NO_PATHCONV=1 防路径转换）：
    QLAB_SPARK_SSH=dev@10.0.0.5 QLAB_SPARK_SSH_PORT=2223 QLAB_SPARK_JUMP=song@10.110.12.99
    QLAB_SPARK_WORKDIR=/home/dev/quant
    QLAB_SPARK_PYTHON=/home/dev/.local/share/mamba/envs/quant/bin/python
  · 流程 = git archive 打包 → scp → 远端解包 → 远端 harness --compute-only（取数/执行器/
    契约检查/固定测试器，只算不记账）→ 远端 tar → scp 回传 → 本地 harness import 记账
    （sqlite 单写者不变）。
  · 远端环境：micromamba 环境 quant（conda-forge + qlib/pyqlib 0.9.7 源码编译，aarch64；
    注意 PyPI 上 qlib 项目停更在 0.0.2.dev*，0.9.x 以 pyqlib 包名发布且无 aarch64 wheel）；
    数据 bins 在 /home/dev/quant/qlib_data（QLAB_QLIB_DATA=/home/dev/quant）；
    远端 pip 走清华镜像（~/.config/pip/pip.conf）；torch CUDA aarch64 轮子走
    阿里 pytorch-wheels 镜像（下载约 13MB/s，2.10.0+cu128 支持 sm_120）。
  · 执行器依赖 venv（P8）：远端用 QLAB_VENV_DIR=/home/dev/quant/executor_venvs
    （repo 每次 dispatch 会被清空重建，venv 放外面才持久）。
  · 数据同步路径：特征缓存 = 远端自建（qlib bins 在 QLAB_QLIB_DATA）；
    价格缓存 = 本地从 qlib_data_src 构建 + scp 预放 repo/cache；
    两种缓存都随 repo/cache 跨 dispatch 持久。
  · 坑位记录：远端无 rsync（回传用 tar+scp 单流）；远端无 sudo、无 bzip2；
    远端宿主可能有邻居任务（配额外的 4 核 CPU / 73GiB 内存 / GPU 留给别人，
    不是"空闲可借"）；跑重活前看 cgroup 实占：
    `cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.stat`；
    retry --blocked 对 spark 任务保留 runner（不再强制回 local）。
  · 路由（P8 D6）：runner 默认 auto = spark 优先、连不上自动回退本地（事件
    spark_fallback 注明原因）；runner=spark 显式远程，不可达才 blocked；
    SSH 未配置时 runner="spark" 任务 blocked（占位行为，不是故障）。
- GitHub 备份：blue-buff/quant-backtest（main）。push token 从主机拿：gh auth token。

## 2. 铁律（不可违背）

1. 诚实：禁止编造、美化、篡改任何实验数据或结论。用户会派另一个 agent 复查。
2. 双备份：每次实验或重要变更后必须本地快照 + commit + push GitHub（见 §6 收尾动作）。
3. 机器可读：实验产出必须进 MLflow 台账（标准 tags/metrics），不靠散装日志。
4. 烟雾测试先行：新脚本/新管线先跑最小验证，通过后才上真实任务。
5. 不跑长任务：任何训练/全市场任务必须显式经用户批准，且先给耗时估算。
6. 不碰远程机：除非用户明确说"上远程"。

## 3. 台账与队列（QLab 操作规范）

- 台账 = MLflow，存储 sqlite:////root/quant/mlflow-server/mlflow.db。
  agent 操作默认 sqlite 直连（registry 无探测、无 2 秒税）；只有设了 QLAB_USE_SERVER=1 才会走 UI 服务。
  UI 服务仅供人类看浏览器界面时按需启动（sh /root/quant/scripts/mlflow_server.sh start，
  用完 stop；启动慢约 40 秒且占约 2GB 内存）。
  台账写入防丢：NaN/Inf 指标跳过并打 qlab.dropped_metrics tag；超长参数截断打 qlab.truncated；
  非法 key 捕获打 qlab.invalid_keys（不再终点线前炸任务）。看正式结果用 board --json --formal
  （只含 FINISHED 非 smoke 行）。
- 队列 = results/queue/jobs.db（sqlite 任务表，WAL 模式）。常用命令：

~~~
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue submit experiments/batches/xxx.json'
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue status --json'
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue run --batch xxx --once'
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue retry [job_id ...] [--batch xxx] [--blocked]'
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue unblock [job_id ...]'   # blocked→queued 并强制回 local
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.registry heal-zombies [--dry-run]'  # 台账僵尸 RUNNING 清理
~~~

- 并发是真的：run --concurrency N（默认 2）用线程池并行执行 N 个任务（原子认领，
  同一任务只会被一个 dispatcher 执行）。重资源训练建议 --concurrency 1，避免内存竞争。

- 查台账：python -m pipeline.board（导出 results/board.csv，主入口）或 mlflow CLI
  （3.x 语法注意：先 export MLFLOW_TRACKING_URI=sqlite:////root/quant/mlflow-server/mlflow.db，
  用 mlflow experiments search 找 id，再 mlflow runs list --experiment-id <id>）。
- 新实验流程：写 spec（最小必填 exp_id + base，推荐补 changes 和 expectation）
  -> 放进 batch -> submit -> run --once -> status 核验 -> 入账完成。
- spec 规则：exp_id 全局唯一；base 用 "ref:xxx" 引用 experiments/refs/；
  允许多变量变更，但必须写 changes 一句话说明；runner 默认 auto
  （spark 优先、失败回退本地），runner=spark 显式远程、runner=local 强制本地。
- 幂等：同 exp_id + 同 spec_hash 且已 done 的任务 submit 时自动跳过；
  --force 重跑需用户批准。数据库有部分唯一索引：同一 spec_hash 只允许一个活跃行
  （queued/running/blocked），并发双提交/重排不会产生重复执行。
- retry：重排 failed（attempts<3）任务，可指定 job_id 或 --batch；--blocked 连 blocked 一起重排；
  重排时清空 error/finished_at。同一 hash 多个失败行只重排最新一行。blocked 任务必须走
  unblock（或 retry --blocked），它会把 runner 强制回 local——这是 blocked 唯一的出路。
- 故障排查三板斧（任务报错/卡死时）：
  show <job_id>（一行看 error+日志尾部+事件链）/ events --since N（增量事件）/
  heal（dispatcher 死后对账：先验活 PID 再动手，running->failed 并杀孤儿进程组）。
  heal 输出 JSON 状态：ok（无 running）/ unknown（心跳文件缺失，拒绝动手，需人工 ps 核查）/
  alive_but_stale（心跳过期但 PID 活着，多半是时钟漂移，不动）/ healed（已确认死亡并善后）。
- 检查点（防漏失败，强制）：① 每次 run --once 一轮返回后立刻 status --json；
  ② 每回合开始时 events --since <上次看到的 id>；③ 修完代码重投前先 show 看错误原因；
  重投 = 重新 submit（幂等，只补 failed 的）。
- 卡死任务由 timeout_min 兜底（默认 120 分钟）：超时先 SIGTERM 等 10 秒再 SIGKILL
  整组杀进程（无孤儿），并把台账里已开的 run 显式置 FAILED（qlab.failed_reason 记录原因）；
  测试可用 QLAB_QUEUE_TIMEOUT_SECONDS 环境变量把超时压到秒级。
- 通知机制（已上线，主机 notify_bridge.js，两阶段协议）：
  · 常驻运行：node notify_bridge.js --interval 15（日志 notify/bridge.log）；测试单次：--once；
    环境变量带默认值：QLAB_NOTIFY_DIR（D:/quant_backup/notify）、QLAB_CONTAINER
    （hermes-1679f5b2）、QLAB_DSH_URL（http://127.0.0.1:3080）。
  · 两阶段协议：notify / notify-done 默认 peek（只读 marker 之后的行、不推进），
    --ack <id> 才推进 marker（只前进不回退）；桥必须"先 POST 到 DSH、成功后 ack"，
    POST 失败不 ack，下轮重发。
  · 任务失败 -> 【QLab 队列通知】；任务完成 -> done.log 行组装【QLab 队列】完成 N 个任务
    （exp#job: rankic/p/expectation）；批次全部终态时追加"批次 <id> done N / failed M"汇总；
    backup_pending 事件单独发【QLab 备份】"未推送"通知；
    全部同时追加 D:\quant_backup\notify\inbox.jsonl，失败另弹 Windows toast。
  · 自动备份挂钩：队列排空一批后自动 snap；有 QLAB_GITHUB_TOKEN 则 push，
    否则写 backup_pending 事件（不阻塞队列）；QLAB_AUTO_BACKUP=0 可关。
  · dispatcher 心跳是后台线程每 5 秒写的 '<epoch> <pid>'；桥发现可疑时先读容器时钟复核，
    再调 heal——heal 验活 PID（僵尸进程视为已死）后才动手，所以长任务不可能被误杀；
  · 心跳文件缺失/不可读 -> heal 拒绝自动判定（unknown），桥发"请人工核查 dispatcher"通知，不自动 heal；
  · dispatcher 确认死亡 -> 自动 heal（杀孤儿进程组 + 台账 run 置 FAILED）+ 通知会话；
  · 收到【QLab 队列通知】消息时按本节三板斧排查，修复后重新 submit；
  · 会话 id 存在 D:\quant_backup\notify\session.txt，换新会话时要更新（否则通知找不到家）。
- 真实训练走 train action（P5 执行器契约，2026-08 上线）：管线只管五样——spec、队列、
  取数、固定测试器、入库。执行器内部是什么管线丝毫不管，写执行器是 agent 的事。
  · spec 写 {"action": {"kind": "train", "executor": "executors/<name>", "task": ...}}，
    base 用 "ref:xxx" 继承基线配置（dataset/label/model/seeds/ensemble），overrides 改单变量；
    不写 executor 则默认 executors/_example_lgb。
  · 数据固定菜单：pool（all/hs300/zz500）× handler（Alpha158/Alpha360）× 标签 × 窗口，
    由 pipeline.data 建成 float32 parquet 缓存（cache/<key>.parquet，重跑直读）；
    执行器只许读，不许自造特征（保证 board 可比）。
  · 执行器契约见 executors/README.md：main.py --config <json> --train <pq> --test <pq>
    --out <dir>，输出 <out>/pred.pkl（(datetime, instrument) MultiIndex，列 "score"）；
    有 requirements.txt 自动建独立 venv（results/venvs/<name>）。
  · 链：取数 → 执行器 → 契约检查（schema/覆盖度，不过则 QLAB_CONTRACT_FAIL）→
    固定测试器 pipeline.metrics（唯一指标来源，回归=IC 族/分类=AUC 族）→ 台账自动入库
    （metrics 全来自测试器，tags 记 qlab.executor/qlab.handler/qlab.task/qlab.data_key）。
  · 产物：results/runs/<exp_id>/{pred.pkl, label_matrix.pkl, executor.log,
    contract_report.json, metrics.json, core_metrics.json, work.json, spec.json}；
    run 目录内**每一个文件**（模型、run_info.json、portfolio.pkl、spec 原件）
    全部入台账 artifacts——快照恢复不丢执行器任何产出。
    测试器 bootstrap 种子固定 42（由 qlab.git 钉死），入库 params.tester_seed 可见。
  · expectation 通用化：{"rankic_mean_min": 0.05, "p_le0_max": 0.5}（旧格式）或
    {"metric": "rankic_mean", "min": 0.05} / 列表形式，自动对照给结论。
  · 远程 runner="spark" 为 v1 占位：pipeline/remote.py 读 QLAB_SPARK_SSH（当前留空），
    未配置时任务 blocked；DGX Spark 传输层等机器信息到位后实现（P6）。
  参考示例：experiments/specs/p3_exec_hs300_10d.json（执行器契约冒烟）。
- backfill 仅用于历史实验入账；新实验一律走 train action，不要再用旧脚本。

## 4. 数据与版本

- 数据 v3 = 2021-06-01 ~ 2026-08-20；bin 在 /root/.qlib/qlib_data（含 hs300/zz500/all 三池）。
- 尾部更新用 scripts/update_tail.py（增量，>=2 行才更新）；指数成分用 index_stock_cons_sina。
- 特征缓存（已上线）：/root/quant/cache/*.parquet（train/test 两套矩阵，按配置 hash 命名），
  重跑直读不重算；改 handler/label/窗口后自动生成新 key。
- 所有 run 必须带 qlab.data_version 和 qlab.git tag（harness 自动写）。

## 5. 评审与知识库

- 默认跑 advisory 检查：python -m pipeline.review run <run_id>（确定性 checklist）。
- 三种场景做深审（P2 上 LLM red-team）：结论要汇报给用户 / 配置要升 refs 基线 / 结果好得可疑。
- 知识库 knowledge/claims.jsonl：新读的资料提炼 claims 入库
  （python -m pipeline.kb add --text ... --source ...），实验结论回写 claim 状态
  （confirmed / falsified / partial）。检索：python -m pipeline.kb search <关键词>。
- 知识闭环（自动）：每个 train run 入库时自动追加一行 auto-claim（status=untested，
  tags=["auto"]，linked 到该 exp）供审读；spec 可带 "claims": ["c-0005"]，
  跑完自动回写：linked_exp_ids 追加 exp_id，expectation met -> confirmed、
  not_met -> falsified、n/a -> 状态不动（写 updated_at）。
- 首次使用先 python -m pipeline.kb init（种子 claims 来自本项目已 pin 的结论）。

## 6. 收尾动作（每轮 / 每次实验后，强制）

~~~
docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.backup snap
    # snap 含 jobs.db/mlflow.db 在线一致性副本（sqlite backup API）+ run 制品（>50MB 文件除外，
    # 例外清单写在 zip 内 manifest.json），不再是纯导出件
docker cp hermes-1679f5b2:/root/quant/results/backup D:/quant_backup/backup   # 主机镜像
docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.backup push --message "..."
    # push 需要 token：先在本机跑 gh auth token 拿值，再传进容器（或直接用
    # /root/quant/.qlab_github_token 文件，backup 会自动读）
    # push 只把最新一个 snap zip 加进 git（旧 zip 留盘、已在历史里），仓库不会无限膨胀
    # 容器网络访问 GitHub 不通时（2026-08-23 实锤：外网被墙，pypi/github 全 SSL reset），
    # 从主机推：docker cp 容器内 .git 到主机临时目录，再 gh auth setup-git 后 git push
~~~

- commit message 必须带 batch_id / exp_id 或本轮主题。
- 汇报格式：跑了什么、结果数字（带窗口与 p 值）、台账位置、备份状态。

## 7. 已趟平的坑（别再踩）

- 主机 shell 引号地狱：docker exec 外层用单引号，内层不要再用单引号；
  复杂脚本用 heredoc（docker exec -i ... python - <<'EOF' ... EOF）；
  heredoc 里写换行符不要用 \n（会被传输层吃成真换行），用 chr(10)。
- MSYS 吃反斜杠；远程 Windows 路径一律用正斜杠。
- 远程 Windows：OpenSSH 会话关闭会杀子进程，必须用 Scheduled Task；
  权限问题用 icacls 授权；远程 pyqlib 配 numpy 1.26.4（numpy 2.5 会崩）。
- 容器里 numpy 2.5.2 正常，不要动。
- MLflow 3.x 废弃文件目录式存储，用 sqlite 后端；旧数据已用 migrate-filestore 迁移。
- qlib 特征/标签窗口：特征用到未来信息是泄露，te 段预测用 infer_processors=[] 只取特征。
- 容器内存 12GB：全市场整批加载会 OOM，要分块或走远程（远程需用户批准）。
- 队列坑位（本轮评审修复后）：
  · 同一 spec_hash 只允许一个活跃行（部分唯一索引）；retry/unblock 自动只重排每 hash 最新一行；
  · heal 验活含僵尸判定：/proc/<pid> 状态为 Z 的进程视为已死（kill(pid,0) 对僵尸仍返回成功）；
  · 排队期间改 spec 文件 → harness 拒跑（stderr QLAB_SPEC_DRIFT，任务 failed 并写明新旧 hash），
    改回原文件后 retry 即可，或重新 submit 记录新配置；
  · hang action 自然结束必失败（"hang action ended without timeout"是预期行为，专测超时路径）；
    测队列机制/并发用 sleep_ok action（延时 N 秒正常成功，入账标 smoke）；
  · 桥发现心跳可疑时先读容器时钟复核再 heal，Docker 睡眠唤醒的时钟漂移不会误杀。
- 主机进程操作铁律：**禁止 taskkill //IM node.exe**——DSH 运行时和桥接都是 node 进程，
  无差别击杀会杀掉自己的运行环境（已实锤发生一次）。重启桥接只用：
  powershell -NoProfile -ExecutionPolicy Bypass -File D:/quant_backup/scripts/probe_node.ps1 -KillBridge
  （先不带 -KillBridge 看进程清单，再精确杀桥接 PID），然后
  cd /d/quant_backup && nohup node notify_bridge.js --interval 15 >> notify/bridge.log 2>&1 &。
- 主机 pwsh → docker exec → sh → ssh 链路的引号吞噬与 CRLF 污染（2026-08-24 qb 批次踩穿）：
  pwsh 会剥掉内层双引号、管道会把 here-string 重编码成 CRLF（`2>/dev/null` 变成
  `/dev/null\r`）。跨机执行一律走"写脚本文件（LF 结尾）+ docker cp/scp + 远端执行"，
  禁止在单行里嵌套多级引号与管道；grep 用 -e 模式，别用引号包 pattern。
- docker cp 目录到已存在的目标目录会嵌套一层（backup/backup）；镜像备份后核对落点。
- 队列 retry --blocked 会连坐所有 failed(attempts<3) 的历史行：废弃任务要
  status='failed' + attempts=3 + note 写原因，才是"永久取消"；failed 本身不是取消态
  （qb 批次被历史故障测试行连坐两次）。
- 仓库根目录放临时 .py 会触发脏代码门禁挡 import（_smoke_driver.py 事故）；
  scratch 脚本放 /tmp、容器外或非代码后缀，别进 repo 根。
- qlib 的 R.start 是 contextmanager：裸调 R.start(...) 是空操作，必须
  `with R.start(...)`（qb 批次 gru_a360 为此失败两轮）。
- 远端容器 /dev/shm 仅 64MB（与 128GB 主存无关，是 docker 默认 shm 配额，
  装不下 DataLoader 共享内存）：num_workers>0 且 batch 较大时 torch collate
  分配共享内存失败/挂死——表现是 GPU-Util 持续 0%、CPU 近 0%、主线程 poll、
  大量 futex 线程、etime 只增不长（=卡死不是慢）。远端 torch 任务一律
  单进程加载（n_jobs/num_workers=0）；GB10 的 nvidia-smi 显存列显示
  Not Supported 是正常现象，GPU-Util 才是活性指标。
- 远端失败任务的 executor.log 只在成功回传后可见，现场会被下一次 dispatch 的
  extract 清掉（run 目录在 repo 内）；调试远端任务用 /home/dev 下不随 repo
  重建的路径 + 独立复现脚本，别在 repo 里留调试文件。
- 本地容器 pids.max=256（线程也计入）：官方 num_threads=20 的模型在本地跑会
  fork/线程创建失败（libgomp/Cannot fork）；官方参数任务走远端。
- Git Bash 下 `> NUL` 不是 Windows 空设备，会创建字面文件 NUL（Aug 21 实锤，
  文件里漏进一行 ssh host key）；丢弃输出一律 `>/dev/null`，主机根目录发现
  名为 NUL 的文件直接删（内容大概率是重定向事故残留）。
- GitHub token 禁止明文落盘：push 暂存仓库的 remote url、队列事件 error 字段、
  任何日志都不得出现 oauth2:gho_***（2026-08-24 在 push_stage.git/config 和
  jobs.db events 两处实锤）。管道写事件/日志前必须对 secret 脱敏（正则把
  `https://oauth2:[^@]+@` 换成 `https://***@`）；需要 token 时现取现用，
  用完不落盘。
- spark dispatch 只打包已提交文件（remote.py 用 git archive HEAD）：
  新建/修改的 spec、batch、执行器必须**先 git commit 再 submit**，否则远端
  FileNotFoundError（本地 submit 读工作树所以能过——t_spark_smoke2 job78 实锤）。

## 8. 结论表述纪律

- 措辞不得强于证据；显著不等于能用；报指标必须带样本窗口和 p 值。
- 零结果同样入账（falsified 也是成果）。
- 单票方向 hit 期望约 0.5 + IC/pi，0.49-0.50 的 hit 是正常数学现象，不是 bug。
- 试了 N 个变体后，解读 p 值必须考虑多重检验：board 里同 base_ref 的
  n_variants / p_bonf / multiplicity_risk 列（p_bonf = p*n_variants）；
  p_bonf>0.05 时结论必须降档为"有信号需确认"，不得宣称显著。
- 预注册（expectation）写了就要对照，没写就标记 exploratory，不许事后改口径。

## 8.5 交易实验 SOP（P8 交易闭环）

- 交易型 spec：metrics 勾 ["rankic", "bootstrap", "portfolio", "backtest",
  "attribution"]（缺省 metrics = 现状 prediction 全族，向后兼容）；
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
- 维护预算：pipeline/*.py 总行数上限 3600（2026-08-23 封板实测终态）；
  每个新功能带测试或删等量旧代码；连续 3 个真实实验零平台改动 = 平台完成。
  预算审计（P7 原估 2750、P8 原估 +300=3050；实际增量全部是两份任务单明令的功能）：
  P7 清单 +538 行（T1 spec 校验 / T2 数据修订+原子写 / T3 契约+extra / T4 通知两阶段+
  自动备份 / T5 多重检验+summary / T6 知识闭环 / T7 auto review+写锁），
  P8 清单 +547 行（价格缓存 / portfolio 契约 / 四族测试器+attribution / token 持久化 /
  auto 路由 / 远端杀组与 venv 持久化）；两阶段合计 +1085、顺手删旧 -125，
  收尾修正（孤儿台账行防护 / venv 印章 / push 退避 / board 分族列）后终态 3709。
  qb 官方 benchmark 复现批（2026-08-24，用户明令"测试阶段可改管线"，改动已上报）：
  data.py +36（spec 级 infer_processors/process_type、test DK_I 取数、fit_start
  整期加载修零特征）、remote.py +14（spark dispatch 串行锁）、harness.py +7
  （SIGTERM 防重入）——净增 +57，当前 wc -l 实测 4067。
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
  | 队列 | pipeline.queue FIFO 单 dispatcher | 优先级/DAG/自动重试/资源感知调度 |
  | 知识 | knowledge/claims.jsonl + auto-claim/linkback | KB 换 sqlite/向量检索、三层知识库 |
  | 评审 | pipeline.review advisory + review.json 入库 | LLM red-team 自动化（手动深审保留） |
  | 通知 | notify_bridge.js 两阶段 + done 通知 + 批次汇总 | supervisor 常驻 |
  | 备份 | backup snap/push + 队列排空自动挂钩 | 灾难恢复演练 |

- 数据变更规则：唯一入口 scripts/update_tail.py（--start/--end 参数化）；更新自动
  invalidate+修订号；手工改数据必须 python -m pipeline.data invalidate --pool <pool>；
  改窗口 = 改 spec = 新 spec_hash。
- 队列运行规则：排空用 run --watch（--once 只领 concurrency 个、一轮结束写 round_end
  通知；watch 每完成一个任务发完成通知；blocked 任务每 job 只提醒一次，离开 blocked 后重置）；
  同一时刻只允许一个
  dispatcher；重新 submit 即重跑（无 --force 概念）；重资源任务必须 --concurrency 1；
  无资源分配、纯排队（FIFO），不做资源感知调度。
- 术语表（消除歧义）：harness 一律指 DeepSeek Harness 运行时；管线的实验执行进程叫
  "run 进程（pipeline.harness run）"；文档禁止写"harness 子进程"。
- 通知驱动的研究循环（核心 SOP）：任务完成通知（单任务 done 或 batch_completed 汇总）
  → 唤醒 agent 读数据（board diff）→ 搜资料/读资料 → 提炼假说（claims）→ 修改笔记
  （knowledge/notes/*.md，自由 markdown）→ 设计下一批实验。允许 agent 忽略单个任务的
  完成通知以等一整组实验；batch_completed 到达才必须开启分析。做实验环节不需要任何通知。
- 会话登记：负责研究的 agent 每个新会话的第一件事 = 把会话 id 写入
  D:\quant_backup\notify\session.txt（通知找不到家 = 循环断掉）。
- 知识库分工：claims.jsonl = 假说与结论（管线自动回写）；knowledge/notes/ = 自由笔记
  （agent 维护 markdown）；不做三层知识库。
- 结论纪律补充：board 对比只在同 source 内（--source 过滤）；p_bonf>0.05 降档为
  "有信号需确认"；hit_rate 口径说明（绝对收益方向 + 截面均值定方向；sign(0) 记错的
  可忽略偏差已注明）。
- 数据 caveat：v3 成分是当前名单，存在幸存者偏差（已退市 2 只补齐至退市日）；
  结论必须带此标注；v4(point-in-time) 排期未定。
- 新会话第一件事：更新 D:\quant_backup\notify\session.txt。
- 时钟判定描述与实现对齐：宿主时钟快速判断 + 容器内 heal 验活（心跳 'epoch pid'，
  桥只读不杀，heal 验活 PID 后才动手）。

## 10. qlib 官方 benchmark 复现经验（qb 批次，2026-08-24）

- 官方参数唯一来源 = microsoft/qlib 的 examples/benchmarks/<Model>/workflow_config_*.yaml。
  主机直连 GitHub 常不通（梯子依赖），用 gitee 镜像克隆：
  `git clone --depth 1 https://gitee.com/mirrors/qlib.git`。
  本项目已 vendor：yaml 在 experiments/qlib_official/；官方预训练 LSTM 检查点
  （GATs/HIST/IGMTF 的 base）在 executors/qlib_bench/benchmarks/——*.pkl 会被
  .gitignore 挡，必须 `git add -f` 才进 git archive（漏了远端就 FileNotFound）。
- 执行器模式（executors/qlib_bench，复用于今后任何 qlib 模型）：官方模型类原样
  import + 官方 fit/predict 直跑；特征经 DataHandlerLP 兼容 shim（ParquetHandler.fetch）
  从管线 parquet 转接，执行器不自造特征。0.9.7 实测接口坑位：
  · DatasetH/TSDatasetH 只走 handler.fetch；MTSDatasetH(TRA) 还直接读
    handler.data_loader.fields 与 handler._learn（MultiIndex 列视图），shim 必须提供；
  · TRA 必须显式 horizon=官方 label 推导值（列名 "LABEL0" 无法被 parse_fields 解析）；
  · 非 ts 模型 fit 结尾调 R.get_recorder()：qlib.init 后预建默认 experiment，
    并用 `with R.start(...)` 包裹 fit（contextmanager，裸调是空操作）；
  · HIST/IGMTF 官方 yaml 带 metric: ic——转写参数要 grep 全字段，别凭印象漏；
  · torch 2.10 兼容全部在 shim 里做（ReduceLROnPlateau 无 verbose 参数、
    torch.load 默认 weights_only 拒旧 pkl），禁止改官方模型代码；
  · ts 模型官方 n_jobs=20 在远端 /dev/shm=64MB 下必挂 → shim 强制 n_jobs=0
    （仅取数并行度：同 batch、同顺序，训练数学不变，属已记录的可接受偏差）。
- 环境：pyqlib 无 aarch64 wheel → qlib 从环境解释器 site-packages 注入
  （sys.base_prefix）；requirements venv 只装 torch（阿里 CUDA 镜像）/xgboost/
  catboost/pytorch-tabnet（清华源均有 aarch64 轮子）。
- 口径对齐最小集（与官方榜单可比的基础）：官方 label
  `Ref($close,-2)/Ref($close,-1)-1`（horizon 1）；官方 learn/infer processors
  原样写进 spec（pipeline.data 支持 spec 级 processors）；test 用 DK_I 取数；
  infer 处理器统计量必须拟合在训练期——test 缓存要从 fit_start 整期加载再按
  selector 切窗（data.py 已实现；退回按切片直建 = 全零特征）。
- 官方配置就是慢：200 epochs + 早期停止，约 35 模型单 GPU 串行下纯计算
  5-6 小时。批量前先统一跑一遍全部 spec 的 data ensure（prewarm 全部缓存 key）
  再提交批次；spark dispatch 有进程内串行锁（remote.py），
  --concurrency>1 对 spark 无增益。
- 已核实不可部署（别重试）：TFT（tensorflow-gpu 1.15 vs py3.12）、
  HIST（概念矩阵需 GitHub 下载 + stock_index 与官方 2020 成分绑定）、
  TCTS（0.9.7 官方实现维度 bug，修复需改模型数学）。
- 结果与全量偏差记录：knowledge/notes/qb_benchmark_repro.md（执行/偏差/摩擦账）、
  knowledge/notes/qb_benchmark_results.md（35 模型全表）。
