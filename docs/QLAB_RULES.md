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
  agent 操作走 sqlite 直连，不需要服务常驻；UI 服务仅供人类看浏览器界面时按需启动
  （sh /root/quant/scripts/mlflow_server.sh start，用完 stop；启动慢约 40 秒且占约 2GB 内存）。
- 队列 = results/queue/jobs.db（sqlite 任务表）。三个命令：

~~~
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue submit experiments/batches/xxx.json'
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue status --json'
docker exec -i hermes-1679f5b2 sh -c 'cd /root/quant && python -m pipeline.queue run --batch xxx --once'
~~~

- 查台账：python -m pipeline.board（导出 results/board.csv）或
  docker exec -i -w /root/quant hermes-1679f5b2 mlflow runs search --experiment-name <name>。
- 新实验流程：写 spec（最小必填 exp_id + base，推荐补 changes 和 expectation）
  -> 放进 batch -> submit -> run --once -> status 核验 -> 入账完成。
- spec 规则：exp_id 全局唯一；base 用 "ref:xxx" 引用 experiments/refs/；
  允许多变量变更，但必须写 changes 一句话说明；runner 默认 local。
- 幂等：同 exp_id + 同 spec_hash 且已 done 的任务 submit 时自动跳过；
  --force 重跑需用户批准。失败任务 retry 命令只重跑 failed 且 attempts<3。
- 故障排查三板斧（任务报错/卡死时）：
  show <job_id>（一行看 error+日志尾部+事件链）/ events --since N（增量事件）/ heal（dispatcher 挂掉后对账，running->failed）。
- 检查点（防漏失败，强制）：① 每次 run --once 排空返回后立刻 status --json；
  ② 每回合开始时 events --since <上次看到的 id>；③ 修完代码重投前先 show 看错误原因；
  重投 = 重新 submit（幂等，只补 failed 的）。
- 卡死任务由 timeout_min 兜底：超时整组杀进程（无孤儿）；测试可用
  QLAB_QUEUE_TIMEOUT_SECONDS 环境变量把超时压到秒级。
- 通知机制（已上线，主机 notify_bridge.js）：
  · 常驻运行：node notify_bridge.js --interval 15（日志 notify/bridge.log）；测试单次：--once；
  · 任务失败 -> 自动 POST 到 DSH 会话（/api/session.prompt，mode=queue，agent 会被自动唤醒），
    同时追加 D:\quant_backup\notify\inbox.jsonl 并弹 Windows toast 提醒人类；
  · dispatcher 心跳失联超 5 分钟且有 running 任务 -> 自动 heal（任务按挂掉处理）+ 通知会话；
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
docker cp hermes-1679f5b2:/root/quant/results/backup D:/quant_backup/backup   # 主机镜像
docker exec -i -w /root/quant hermes-1679f5b2 python -m pipeline.backup push --message "..."
    # push 需要 token：先在本机跑 gh auth token 拿值，再传进容器
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

## 8. 结论表述纪律

- 措辞不得强于证据；显著不等于能用；报指标必须带样本窗口和 p 值。
- 零结果同样入账（falsified 也是成果）。
- 单票方向 hit 期望约 0.5 + IC/pi，0.49-0.50 的 hit 是正常数学现象，不是 bug。
- 试了 N 个变体后，解读 p 值必须考虑多重检验（board 里同假说变体数）。
- 预注册（expectation）写了就要对照，没写就标记 exploratory，不许事后改口径。
