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
  允许多变量变更，但必须写 changes 一句话说明；runner 默认 local。
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
- 检查点（防漏失败，强制）：① 每次 run --once 排空返回后立刻 status --json；
  ② 每回合开始时 events --since <上次看到的 id>；③ 修完代码重投前先 show 看错误原因；
  重投 = 重新 submit（幂等，只补 failed 的）。
- 卡死任务由 timeout_min 兜底（默认 120 分钟）：超时先 SIGTERM 等 10 秒再 SIGKILL
  整组杀进程（无孤儿），并把台账里已开的 run 显式置 FAILED（qlab.failed_reason 记录原因）；
  测试可用 QLAB_QUEUE_TIMEOUT_SECONDS 环境变量把超时压到秒级。
- 通知机制（已上线，主机 notify_bridge.js）：
  · 常驻运行：node notify_bridge.js --interval 15（日志 notify/bridge.log）；测试单次：--once；
  · 任务失败 -> 自动 POST 到 DSH 会话（/api/session.prompt，mode=queue，agent 会被自动唤醒），
    同时追加 D:\quant_backup\notify\inbox.jsonl 并弹 Windows toast 提醒人类；
  · dispatcher 心跳是后台线程每 5 秒写的 '<epoch> <pid>'；桥发现可疑时先读容器时钟复核，
    再调 heal——heal 验活 PID（僵尸进程视为已死）后才动手，所以长任务不可能被误杀；
  · 心跳文件缺失/不可读 -> heal 拒绝自动判定（unknown），桥发"请人工核查 dispatcher"通知，不自动 heal；
  · dispatcher 确认死亡 -> 自动 heal（杀孤儿进程组 + 台账 run 置 FAILED）+ 通知会话；
  · 收到【QLab 队列通知】消息时按本节三板斧排查，修复后重新 submit；
  · 会话 id 存在 D:\quant_backup\notify\session.txt，换新会话时要更新（否则通知找不到家）。
- P1 完成前，真实训练仍用旧脚本 scripts/run_exps.py 跑，跑完用
  python -m pipeline.harness backfill 把结果补进台账（不重跑、不改数字）。

## 4. 数据与版本

- 数据 v3 = 2021-06-01 ~ 2026-08-20；bin 在 /root/.qlib/qlib_data（含 hs300/zz500/all 三池）。
- 尾部更新用 scripts/update_tail.py（增量，>=2 行才更新）；指数成分用 index_stock_cons_sina。
- 特征缓存（P1 起）：/root/quant/cache/*.parquet，重跑直读不重算。
- 所有 run 必须带 qlab.data_version 和 qlab.git tag（harness 自动写）。

## 5. 评审与知识库

- 默认跑 advisory 检查：python -m pipeline.review run <run_id>（确定性 checklist）。
- 三种场景做深审（P2 上 LLM red-team）：结论要汇报给用户 / 配置要升 refs 基线 / 结果好得可疑。
- 知识库 knowledge/claims.jsonl：新读的资料提炼 claims 入库
  （python -m pipeline.kb add --text ... --source ...），实验结论回写 claim 状态
  （confirmed / falsified / partial）。检索：python -m pipeline.kb search <关键词>。
- 首次使用先 python -m pipeline.kb init（种子 claims 来自本项目已 pin 的结论）。

## 6. 收尾动作（每轮 / 每次实验后，强制）

~~~
docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.backup snap
    # snap 含 jobs.db/mlflow.db 在线一致性副本（sqlite backup API）+ run 制品（>50MB 文件除外，
    # 例外清单写在 zip 内 manifest.json），不再是纯导出件
docker cp hermes-1679f5b2:/root/quant/results/backup D:/quant_backup/backup   # 主机镜像
docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.backup push --message "..."
    # push 需要 token：先在本机跑 gh auth token 拿值，再传进容器
    # push 只把最新一个 snap zip 加进 git（旧 zip 留盘、已在历史里），仓库不会无限膨胀
~~~

- commit message 必须带 batch_id / exp_id 或本轮主题。
- 汇报格式：跑了什么、结果数字（带窗口与 p 值）、台账位置、备份状态。

## 7. 已趟平的坑（别再踩）

- 主机 shell 引号地狱：docker exec 外层用单引号，内层不要再用单引号；
  复杂脚本用 heredoc（docker exec -i ... python - <<'EOF' ... EOF）。
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

## 8. 结论表述纪律

- 措辞不得强于证据；显著不等于能用；报指标必须带样本窗口和 p 值。
- 零结果同样入账（falsified 也是成果）。
- 单票方向 hit 期望约 0.5 + IC/pi，0.49-0.50 的 hit 是正常数学现象，不是 bug。
- 试了 N 个变体后，解读 p 值必须考虑多重检验（board 里同假说变体数）。
- 预注册（expectation）写了就要对照，没写就标记 exploratory，不许事后改口径。
