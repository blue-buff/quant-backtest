# qlib 官方 Benchmark 复现（qb 批次）执行记录

> 2026-08-24 | 主线任务：复制 qlib 官方 benchmark 测试参数，在项目 v3 数据上
> 按同一口径复跑，测出指标表。参数只改时间窗，不做任何调参。

## 切分纪律（全部模型统一）

- 股票池：hs300（csi300，对齐官方 CSI300）
- 特征：Alpha158 / Alpha360（pipeline.data 固定菜单，qlib 官方 handler 构建）
- label：官方 `Ref($close,-2)/Ref($close,-1)-1`（未来 1 日收益，horizon=1）
- 切分：train 2021-06-01~2024-06-30 / valid 2024-07-01~2024-12-31 /
  test 2025-01-01~2026-08-20（严格样本外；官方 2008-2014/2015-2016/2017-2020
  的切分结构不变，只换时间）
- 评分：全部模型同一固定测试器（pipeline.metrics）+ 同一回测口径
  （TopkDropout topk=50/n_drop=5 复刻，统一成本模型）
- 官方配置里每个模型的 learn/infer processors（FilterCol 20 特征、
  RobustZScoreNorm、CSRankNorm/CSZScoreNorm 等）原样照搬

## 模型清单（36 个，来自官方 workflow_config_*.yaml，vendor 于
experiments/qlib_official/）

- Alpha158：LightGBM, XGBoost, CatBoost, Linear, DoubleEnsemble, MLP,
  GRU(20feat,ts), LSTM(20feat,ts), ALSTM(20feat,ts), GATs(20feat,ts),
  TCN(20feat,ts), Transformer(20feat,ts), Localformer(20feat,ts),
  TabNet, TRA
- Alpha360：LightGBM, XGBoost, CatBoost, DoubleEnsemble, MLP, GRU, LSTM,
  ALSTM, GATs, IGMTF, TCN, Transformer, Localformer, TabNet, SFM, KRNN,
  Sandwich, ADD, ADARNN, TCTS, TRA
- GATs(A158/A360)、IGMTF 的 LSTM base 用官方预训练检查点
  （executors/qlib_bench/benchmarks/LSTM/*.pkl，官方 2010-2020 数据训出）

## 不可部署（上报）

1. TFT：官方实现依赖 tensorflow-gpu==1.15 + pandas==1.1，与 Python 3.12
   不兼容，本轮跳过。
2. HIST：概念矩阵 qlib_csi300_stock2concept.npy 需从 GitHub release 下载，
   本机/容器/远端 GitHub 全不通（梯子故障期）；且其 stock_index 与官方
   2020 年成分绑定，与当前 hs300 成分存在错位风险。跳过。

## 管线改动（测试阶段授权，报告清单）

1. `pipeline/data.py`：
   - spec 支持 `processors.infer`（官方 infer 处理器：FilterCol/
     RobustZScoreNorm/Fillna 等，作用于全部段），并纳入缓存 key；
   - spec 支持 `processors.process_type`（MLP 官方的 independent）；
   - test 缓存改以 DK_I（infer 视图）取数：测试标签为原始 label，
     与官方 SigAnaRecord 口径一致；对 CSZScoreNorm 标签模型 IC/RankIC 不变
     （仿射/单调不变），CSRankNorm 标签模型的 IC 口径修正为官方一致；
   - 带 infer 处理器时单切片取数（infer 统计量覆盖整窗，避免逐切片差异）；
   - **修复（04:00）**：test 缓存的 handler 从 fit_start 起整期加载、按
     selector 切测试窗——按切片起 handler 时 fit 区间为空，infer 统计量在
     空集上拟合 → 特征全 NaN → Fillna 填 0（lin_a158 常数预测事故根因）。
2. `pipeline/harness.py`：SIGTERM handler 防重入（killpg 会把信号打回自己，
   远端复现出 RecursionError；现在 handler 内先 SIG_IGN 再 killpg）。
3. `pipeline/remote.py`：**spark dispatch 全流程加进程内锁**——并发 2 时两个
   dispatch 共享一个远端 repo 目录，extract 的 rm -rf+tar 互相撕（job 44/45
   事故根因：Directory not empty / No module named pipeline）。
4. `executors/qlib_bench/main.py`：torch 2.10 兼容 shim——qlib 0.9.7 给
   ReduceLROnPlateau 传 verbose=，torch≥2.2 已移除该参数（MLP 等 scheduler
   模型初始化必炸，已全局补丁）；torch.load 默认 weights_only=True 会拒绝
   官方旧版预训练 pkl，shim 里默认 weights_only=False（仅限自家 vendor 文件）。
5. `executors/qlib_bench/main.py`（执行器侧，不改模型数学的运行时适配）：
   - ts 模型 n_jobs 强制 0：远端容器 /dev/shm 仅 64MB，官方 n_jobs=20 的
     DataLoader worker 共享内存分配失败（真实规模下挂死，GRU 卡 38 分钟，
     GPU/CPU 双空闲；规模实验证实）——n_jobs 只影响取数并行度，
     相同 batch、相同顺序、训练数学不变；
   - qlib.init 时预建默认 experiment + `with R.start(...)` 包裹 fit：
     非 ts 模型的 fit 结尾会调 R.get_recorder()（create=False），官方 qrun
     编排里 recorder 已激活；注意 R.start 是 contextmanager，裸调是空操作
     （踩过）；
   - ParquetHandler 补 data_loader.fields + _learn 视图（MTSDatasetH/TRA
     直接读这两个属性）；
   - TRA spec 显式 horizon=1（官方 label 的推导值；qlib 从列名 "LABEL0"
     猜不出 horizon）；
   - IGMTF 补 metric=ic（官方 yaml 第 57 行有，首次转写遗漏）。
6. vendor 修正：官方预训练 LSTM 检查点（GATs/HIST/IGMTF base）曾被
   .gitignore（*.pkl）挡在仓库外 → git archive 缺文件 → 远端 FileNotFoundError；
   已 git add -f（df96034）。

## 取消/不可部署（上报，非失败）

- TFT：tensorflow-gpu 1.15 与 Python 3.12 不兼容（官方代码自身）。
- HIST：概念矩阵需从 GitHub release 下载（梯子故障期不可达）；stock_index
  与官方 2020 年成分绑定。
- TCTS：qlib 0.9.7 官方实现维度 bug（init_pred(batch,3) 与 label 广播
  不匹配，官方原码如此），修复需改模型数学，超出参数保真范围。

## 摩擦账（复盘用，2026-08-24 记录）

- 时间线：容器 02:00（主机 10:00）开跑 → 09:41（主机 17:41）收尾 ≈ 8 小时。
  其中纯训练计算（官方 200 epochs × 35 模型，共享 repo 串行 dispatch）约
  5-6 小时；诊断与修复 ≈ 2-3 小时。
- 主要摩擦见主会话复盘：官方库×新环境兼容问题 6 项、平台真 bug 2 项、
  shell 引号/CRLF 税、无实时 executor 日志、全量 status 轮询。

## 未解决问题挂账（非操作失误所致，逐项待办）

1. 工具链：pwsh→docker→sh→ssh 引号吞噬/CRLF 污染——已用"脚本文件模式"绕开并写进
   AGENTS.md，根因（pwsh 原生命令参数传递）未修。
2. 网络：主机/容器到 GitHub 全断（schannel/openssl/gh/IWR 四种方式全失败；
   容器 git 配着指向 127.0.0.1:7897 的本地代理，代理已死）——GitHub 备份 push
   欠账（backup_pending 积压），HIST 概念矩阵无法下载。
3. 管线：远端 executor.log 只在任务结束落盘，失败现场随下一次 extract 丢失——
   未改代码（只写规则），是 8 小时成本的最大杠杆点。
4. 管线：spark dispatch 共享单个远端 repo → 串行锁只是止血，--concurrency>1
   对 spark 无效；每 job 多付 scp/extract 开销；根治（每 job 独立工作目录）未做。
5. 管线：队列无 cancel 命令；retry --blocked 连坐全部 failed(attempts<3)。
6. 管线：heal 对"心跳过期但 pid 已死"判定滞后（等 100s+ 仍报 ok），手动 sqlite 翻行。
7. 远端：/dev/shm=64MB 无法从容器内改（创建层参数、无 sudo）；n_jobs=0 兜底。
8. 远端：无 strace/rsync/sudo/bzip2；strace 缺失拖慢 SIGTERM 溯源。
9. 远端：首轮 3 次神秘 SIGTERM 的发送者未定位（干净环境不复现，handler 防重入
   已止血）——悬案。
10. 远端：GB10 CUDA capability 12.1 vs torch 2.10 支持上限 12.0 的警告未消除
    （训练正常，升级 torch/驱动时需注意）。
11. 上游：TFT（TF1.15 vs py3.12）/ TCTS（维度 bug）/ HIST（下载+成分错位）不可部署。
12. 上游：qlib API 陷阱全部靠执行器 shim 兜底，上游未修（R.start 语义、
    ReduceLROnPlateau、torch.load、MTSDatasetH 接口、TRA horizon 解析）。
13. ~~悬案~~已闭环（2026-08-24）：qlib LinearModel 系数 std 8.56 vs 手工 sklearn
    同数据 0.35 之谜已解释——非 bug，是标签预处理口径（qlib 拟合 CSRankNorm 标签
    std≈1，手工用 raw 标签 std≈0.024）+ 截距开关经 158 共线特征放大 ≈24×；
    执行 run 系数 std 实测 6.93（pred.pkl/IC 双指纹验证），8.56 为早期数据态测量。
    详见 knowledge/notes/linear_coeff_investigation.md「结论」节。
14. 口径：官方成本模型/Linear long-short/官方预训练 base 的口径差异已文档化
    （永久 caveat，非 bug）。

第二轮复查新增（2026-08-24 晚，此前未上报）：

15. 密钥卫生（根因已修、现场已清理，仍建议轮换 token）：pipeline/backup.py 的
    git() 只对 stdout/stderr 脱敏，args 前缀里的 token URL 原样进 RuntimeError，
    经 queue.py 写进事件。泄露面实测：容器 jobs.db events 8 行、events.log 8 处、
    主机 notify/inbox.jsonl 12 处、主机 push_stage.git/config 1 处——全部已清理
    （<REDACTED>，jobs.db 补审计事件 secret_scrub）；根因已修（整条错误信息脱敏，
    见 commit）。.qlab_github_token 文件本身是设计内（gitignored、600），不算泄露。
    遗留：历史快照 zip（snap_20260824_093734/094108 含未脱敏 jobs.db 副本，
    已入 git 历史与主机镜像）仍有明文——清理需改写 git 历史，不划算；请轮换
    token（gh auth refresh），旧 token 全部副本随即失效。
16. 主机残留：push_stage.git 半成品暂存仓库 + push_watch.log（60 次尝试 502，
    06:24 放弃）。网络恢复后可从该仓库续推，暂保留；log 可归档。
17. 容器仓库 dirty：results/backup/snaps/manifest.sha256 有 1 行自动备份追加的
    快照哈希未提交（自动备份不 commit）——本轮已提交（见后续 commit）。
18. 主机 docs/qlib_benchmark_模型排行榜.md 追加段混入 1 处 CRLF（pwsh 追加税），
    主机文档无 git 保护；本轮已统一 LF。
19. 主机根目录字面文件 NUL（94B，内容为 10.110.12.99 的 ssh-ed25519 host key 行）：
    Git Bash 下 `> NUL` 会创建真实文件而非丢弃输出（Aug 21 旧会话遗留）。
    本轮已删除；丢弃输出一律 `>/dev/null`（已写入 AGENTS.md）。

## 两批分流（2026-08-24，应人类要求整理，执行前先过目）

### 批次 A：明确纯正面，修复计划已定（人类批准后动手）

A1. 【#3】executor.log 流式落盘 + 失败现场保回：
    harness.run_executor 用读线程边跑边写 run_dir/executor.log（超时/杀组后部分日志
    已在盘上）；remote.dispatch 远端计算 rc!=0 时先把 run 目录 tar+scp 保回本地再报错
    （下次 extract 清场不再丢现场）。
A2. 【#4】spark 每 job 独立工作目录：workdir/jobs/job_<id>/repo，cache 软链共享
    workdir/cache（配 #2 的 per-file flock 防并发建缓存），venv 仍共享 executor_venvs；
    移除串行锁，--concurrency 2 起效；成功任务目录即删、失败保留。
A3. 【#5a】queue cancel <job_id...>：新增终态 status='cancelled'；queued/blocked 直接
    转 cancelled；running 先 killpg 再转 cancelled 并关台账 run；批次汇总计入 cancelledN。
A4. 【#5b】retry --blocked 只重排 blocked（failed 需显式 --include-failed），语义防呆。
A5. 【#6】heal 提速：心跳 age > 3×心跳周期即验证 pid，pid 死则立即 heal（原 5 分钟
    门限造成 100s+ 滞后）；pid 活着仍走 alive_but_stale 不动。
A6. 【#9】SIGTERM/SIGINT 溯源日志：handler 落盘 ppid/cmdline、/proc/self/status、
    运行目录到 results/queue/sig_probe_<pid>.json——悬案下次发生即可定位，不额外立项。
A7. 【#16】网络恢复后：从 push_stage.git 续推（git push origin HEAD:main）→ 推完
    归档 push_watch.log、删除半成品暂存仓库。
A8. 【#17 复发根因】snap 的 manifest.sha256 追加行自提交：manifest.sha256 已被
    force-add 成 tracked 文件，每次自动快照追加一行→树长期 dirty；改为 snap 末尾
    对该文件 git add+commit（无变更容忍 nothing-to-commit），push 逻辑不变。
（#17/#18/#19 本轮已闭环：manifest 行已提交、排行榜 CRLF 已统一 LF、NUL 已删+规则入库。）

### 批次 B：需与人类讨论后定夺

B1. 【#15，P0，人类操作】轮换 GitHub token（gh auth refresh 或换新 token 并更新
    /root/.qlab_github_token + push_stage 配置）。旧 token 已扩散进 git 历史与快照，
    轮换是唯一根治；历史不必改写，旧 token 全部副本随轮换失效。
B2. 【#2，人类操作】修梯子/代理后恢复 push 与 HIST 下载；容器 git 的
    127.0.0.1:7897 代理配置是删是留（梯子恢复后是否自动可用）由人类定。
B3. 【#1】工具链引号/CRLF 税：根治需写主机侧封装（所有 docker exec 走脚本文件），
    成本 vs 维持"脚本文件模式"纪律的收益需讨论。
B4. 【#7+#8】远端容器重建（--shm-size 调大 + 装 strace，顺手加 rsync/bzip2）：
    需要容器创建层权限，是否值得为此重建一次环境，由人类定。
B5. 【#10】CUDA capability 12.1 vs torch 2.10 支持上限 12.0：训练正常；接受现状
    or 等 torch 支持 sm_120 的新版本再升级（不建议压警告）。
B6. 【#9】悬案本体：A6 溯源日志上线后等下次发生即可定位；是否另花时间做专门
    复现实验由人类定（倾向不做）。
B7. 【#11】TFT/TCTS/HIST 不可部署：永久放弃 or 将来换环境（TF1.x 容器/下载通道）
    重试，由人类定。
B8. 【#12】qlib API 陷阱 shim 层：接受为永久补丁层 or 评估换库/上游提交，由人类定。
B9. 【#13】Linear 系数 std 8.56 vs sklearn 0.35 之谜：~~是否立项调查~~已调查闭环
    （2026-08-24），结论=预处理口径差异、非 bug，见
    knowledge/notes/linear_coeff_investigation.md「结论」节。
B10. 【#14】口径差异三件套：维持文档 caveat or 给台账历史行加口径 tag，由人类定。

## 执行器

- `executors/qlib_bench/main.py`：官方模型类原样导入（module_path/class/
  kwargs 来自 spec），官方 fit/predict 直跑；特征经 ParquetHandler 转接
  （DataHandlerLP 接口）喂给官方 DatasetH/TSDatasetH/MTSDatasetH；
  执行器不建特征、不报指标。
- requirements.txt：torch cu128(阿里镜像)/xgboost/catboost/pytorch-tabnet；
  qlib 从环境解释器 site-packages 注入（pyqlib 无本平台 wheel）。
- 训练=官方代码；早期停止/学习率/批次/优化器全部官方值。
- 评分只认 pipeline.metrics 固定测试器。

## 与官方榜单的可比性 caveat

- 官方榜单数据期 ~2010-2020、含官方成本模型（open_cost 0.0005 /
  close_cost 0.0015 / min_cost 5 / limit 0.095）；本项目为 2021-06~2026-08、
  管线统一成本模型（佣金双边万2.5+印花税卖出千1+滑点1bp）。绝对数字不可横比，
  本批次的价值 = 同一数据同一管道上 36 个官方模型的横向排序 + 与自研基线对齐。
- Linear 官方回测为 long-short，本批统一 long-only（跨模型可比优先）。
- GATs/HIST/IGMTF 的 base LSTM 用官方预训练权重（官方历史数据），与官方流程一致。

## 运行状态

- 本地冒烟（qb_lgb_a158）：rankic 0.0234 / p 0.002 / n=394 天 ✓
- 远端冒烟（job 39）：同数字 ✓（spark 全链验证）
- 全量批次 b-qb-official：35 任务排队，spark 并发 2，进行中
