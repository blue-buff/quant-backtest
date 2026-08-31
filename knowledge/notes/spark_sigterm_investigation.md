# DGX Spark 远端 SIGTERM 杀手——调查计划（交办版）

> 交办状态：已获用户明确授权（"上远程"排查该问题），本文件是给接单 agent 的
> 自包含调查计划。权限边界：只读排查 + 最小复现实验，不杀他人进程、不改
> 宿主/容器系统配置、不动 4090D、不占 GPU。所有发现回写到本文件。
> 发起会话记录见 knowledge/notes/kronos_deploy.md §8（2026-08-24 Kronos 部署）。

## 1. 问题定义（已确认的现象）

**一句话**：DGX Spark 计算容器（`ssh -J song@10.110.12.99 -p 2223 dev@10.0.0.5`）
上，qlib 数据取数（pipeline.harness 的 data ensure 阶段）会在启动后 **3 秒 ~ 7 分钟**
内被**不明来源的 SIGTERM** 整组杀死。其余阶段（pip 装 venv、GPU torch 推理、
多小时 torch 训练、普通 python 脚本）全部存活。

**4 次致死记录（2026-08-24，均为同一 spec kronos_hs300_30d 的取数阶段）：**

| # | 时间 | 形态 | 取数方式 | 存活时长 | 死亡时活动 |
|---|---|---|---|---|---|
| 1 | 16:33:10 | 队列 dispatch（`timeout 18000` 包裹，ssh attached） | loky 14 进程 | ~3s | qlib init 后、Loading data 中 |
| 2 | 16:39:21 | 手动 ssh attached 重跑 | loky 14 进程 | ~8s | 第 1 个 cache slice 写入后 |
| 3 | 16:40:47 | `setsid nohup` detached | loky 14 进程 | ~8s | 同上（排除会话关闭） |
| 4 | 16:44:40 | `setsid nohup` detached + 1s 级 watcher 录像 | loky 14 进程 | ~8s | worker 大面积僵尸化瞬间 |
| 5 | ~16:57 | `setsid nohup` detached + kernels=1 串行 | 串行 1 worker | **~5-7 min** | 写到第 6/7 个 slice（183MB）时 |

- 信号为 **SIGTERM（15）**，不是 SIGKILL/OOM(9)、不是 SIGSEGV/SIGABRT：
  harness 的 SIGTERM handler 写了 sig_probe 文件（`"signum": 15`），随后
  `os.killpg(0, SIGTERM)` 把 TERM 扩散给所有子进程（所以 worker 们同一秒
  收到 TERM 并留下各自的 probe——**扩散是 harness 自己做的，原始信号只打在
  harness 主进程上**）。
- 每次死亡时 cgroup 内存 ~9.9GB / 上限 51.5GB；容器 uptime 3+ 天（无重启）；
  无 core dump；日志无 traceback（干净地被 TERM 掉）。

## 2. 证据地址（还存在的）

远端（10.0.0.5:2223，用户 dev）：
- `/tmp/kronos_watch.log`（39KB）：死亡瞬间前 1s 分辨率的 ps 快照，能看到
  worker 从 R 到 Z 的僵尸化浪潮（16:44:40 前后）。
- **PID 100 的 2 天龄交互会话**：`sshd: dev@pts/0`（PID 99）→ `-bash`（PID 100），
  `SSH_CLIENT=10.0.0.4 7801 22`、cwd=/home/dev、无子进程。**这是最可疑的
  外方入口**（我们走 2223，它走 22 端口、来自 10.0.0.4 的 7801）。
- `/home/dev/quant/cache/`：kronos_ohlcv.parquet + 预置的 train/test 缓存
  （**注意：有这两个缓存时 data ensure 会跳过取数，复现前需移走**）。
- `/home/dev/quant/executor_venvs/kronos/`：完整 venv（torch 2.10 cu128）。
- `/tmp/kronos_models.tgz` 已删；HF 缓存 `/home/dev/.cache/huggingface/hub/` 完整。

本地（容器 hermes-1679f5b2，/root/quant）：
- `results/remote_fail/fail_kronos_hs300_30d_20260824_163313.tar.gz`：第 1 次
  失败的 run 目录现场（executor_config/spec/日志）。
- `results/queue/jobs.db`（events 表）：job84 全部 4 次 blocked/failed 的
  error 全文（含 rc=143 的 stdout 尾巴）。
- `knowledge/notes/kronos_deploy.md` §8：发起会话的完整分析。
- `scratch_probe/`（主机侧 D:\quant_backup\scratch_probe\）：
  kronos_remote_watch.py（watcher）、kronos_remote_detached.py、
  kronos_remote_matrix.py、kronos_remote_peek.py、kronos_sitecustomize/、
  kronos_ship_remote*.py 等全部复现工具与 ssh 封装（注意 ssh 用 `-p`、
  scp 用 `-P`，混用会踩坑）。

已消失：远端 jobs/job_84/（成功后被 remote.py 删除），其中 sig_probe_*.json
的完整内容已引用在上方笔记与本文件第 1 节。

## 3. 已排除项（别重复排查）

cgroup OOM（内存远未到顶）；CPU 配额（cgroup cpu 只节流不杀，且多小时 torch
训练存活）；ssh 会话关闭（setsid detached 照样死，且死亡时会话还活着）；
RLIMIT（nproc/openfiles 等全 unlimited）；容器重启（uptime 3+ 天）；qlib 代码
崩溃（信号是 TERM 不是 ABRT/SEGV，且 handler 正常执行）；`timeout` 包裹器
（无包裹也死）；HF/网络（死亡时无网络活动）；容器内 cron/systemd（不存在）；
sshd 配置（干净，仅 UsePAM=yes、无 ForceCommand）。

**串行（kernels=1）仍死是重要线索**：说明不是"14 个 loky 进程触发了某个
进程数阈值"这么简单；死亡与**持续 CPU 负载 + 运行时长**的组合相关
（3 秒死亡发生在负载峰值，串行死亡发生在 ~5-7 分钟）。

## 4. 假设清单（按先验概率排序）

- **H1 宿主级 watchdog**：DGX 宿主（10.0.0.5 主机）上有对计算容器的监控/
  资源回收机制（docker healthcheck、宿主 cron、GPU 租户调度器、或人工写的
  守护脚本），检测到"容器内持续高 CPU / 特定进程模式"后向容器内进程组发
  SIGTERM。需要宿主侧权限。
- **H2 共享使用者的进程**：10.0.0.4:22 的 2 天龄会话属于另一使用者，其侧
  运行着 pkill/回收类脚本。可通过会话归属 + 对侧排查验证。
- **H3 容器编排层**：容器由某守护进程管理（如带 init 的 docker run 的
  healthcheck、compose、或 4090D/DGX 间的调度脚本），向容器内 PID 发 TERM。
  查宿主 `docker inspect`（init/healthcheck/resources/创建命令）。
- **H4 宿主内核层**：宿主 kernel 机制（seccomp/cgroup freezer/auditd）在
  特定触发下杀进程组。查宿主 dmesg/journalctl 在死亡时刻的条目。
- **H5（弱）网络/跳板**：已被 detached 实验大幅弱化，仅备查（死亡时无 ssh
  会话附着）。

## 5. 复现配方（给接单 agent，直接照抄）

环境（本地容器内执行，密钥已配好）：
```
ssh  = ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new -J song@10.110.12.99 -p 2223 dev@10.0.0.5
scp  = scp  ...  -J song@10.110.12.99 -P 2223  <src> dev@10.0.0.5:<dst>
PY   = /home/dev/.local/share/mamba/envs/quant/bin/python
```

**步骤 0：基线矩阵（5 分钟）**
1. `$PY -c "import time; time.sleep(30); print('ok')"` → 预期存活。
2. qlib init + 日历 + sleep 30 → 预期存活。
3. 纯 CPU 负载机（numpy matmul 循环，10 分钟，detached）→ **判别实验 E1**：
   死 = 宿主级 CPU watchdog；活 = 与 qlib/harness 有关。命令：
   `setsid nohup $PY -c "import numpy as np,time; t=time.time(); [np.dot(np.random.rand(2000,2000),np.random.rand(2000,2000)) for _ in range(2000)]; print('SURVIVED', time.time()-t)" > /tmp/cpu_burn.log 2>&1 < /dev/null &`

**步骤 1：规范复现（harness 取数，10 分钟）**
1. 远端 `mv /home/dev/quant/cache/train_bb21444d6eeb66e4.parquet /tmp/ && mv /home/dev/quant/cache/test_f15160a7066ea0ff.parquet /tmp/`（移走预置缓存，逼出取数）。
2. 准备仓库副本（本地容器）：`cp -r /root/quant /root/quant_invest 2>/dev/null` 或直接
   用 git worktree；在副本根目录 `mv sitecustomize.py sitecustomize.py.bak`
   （**恢复 14 进程默认行为**，复现原始死亡）。
3. 打包上远端（或复用 remote.py 的 pack 逻辑）：
   `git -C <副本> archive --format=tar.gz -o /tmp/invest.tar.gz HEAD` →
   scp → 远端解包到 `/tmp/invest_repo`。
4. 远端 detached 启动 + watcher 双开（脚本见 scratch_probe/kronos_remote_watch.py 的
   watcher 段，1s 快照写入 /tmp/kronos_watch2.log）：
   ```
   cd /tmp/invest_repo && setsid nohup env QLAB_ROOT=/tmp/invest_repo \
     QLAB_QLIB_DATA=/home/dev/quant QLAB_VENV_DIR=/home/dev/quant/executor_venvs \
     OMP_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8 \
     $PY -m pipeline.harness run experiments/specs/kronos_hs300_30d.json --compute-only \
     > /tmp/invest_run.log 2>&1 < /dev/null &
   ```
   预期：14 进程取数 3-40 秒内死（sig_probe 出现在 /tmp/invest_repo/results/queue/）。
5. 清场：杀掉残留、`mv /tmp/*.parquet` 还原缓存、删 /tmp/invest_repo。

**步骤 2：判别实验（按需）**
- **E2 进程名免疫测试**：`$PY -c "import runpy,sys; sys.argv=['harness']; runpy.run_module('pipeline.harness', run_name='__main__')" run <spec> --compute-only`
  （cmdline 不含 "harness run" 字样）→ 死 = 不看名字；活 = pkill -f 类机制。
- **E3 会话归属**：远端 `who -a` / `last -a` / `cat /var/log/wtmp|strings`；
  确认 PID 100 会话的所有者与来源主机身份；请用户配合确认 10.0.0.4 是谁。
- **E4 宿主侧**（需用户提供）：死亡时刻的 `docker logs <容器>`、`docker events`、
  宿主 `dmesg -T`、宿主 crontab、`docker inspect <容器> | grep -i -e init -e health -e oom`。
- **E5 高频录像**：把 watcher 轮询提到 0.2s 并记录**全部**进程快照 diff，
  抓死亡瞬间的瞬态进程（杀手若为短命进程可被拍到）。

## 6. 成功判据（DoD）

1. 定位发送 SIGTERM 的实体（进程/机制/配置项），或给出排他性结论（如
   "宿主 XX 服务的 YY 配置在 Z 条件下发 TERM"）。
2. 能解释第 1 节 5 次死亡的时间分布（3s / 8s / 8s / 8s / ~5-7min）。
3. 给出修复方案（白名单 / 关 watchdog / 隔离容器 / 换端口等），并验证：
   **qlib 默认 14 进程取数在远端连续运行 >30 分钟不死**。
4. 结论回写本文件"调查结果"节 + knowledge/notes/kronos_deploy.md §8；
   若修复涉及宿主配置，由用户执行，agent 只给操作清单。

## 7. 预算与分工

- agent 容器内实验：~1 小时（复现 20 min + 判别实验 30 min + 取证 10 min）。
- 用户侧（宿主/会话归属）：~30 分钟（E3/E4 所需的权限与信息）。
- 风险控制：所有复现实验限 10 分钟级、不占 GPU、不碰 qlib_data 与共享缓存
  之外的东西、结束必清场（残留进程/临时文件/tmp 仓库）。
## 8. 调查结果（2026-08-25 定案）

**结论：不是基础设施杀手，是管线自伤级联。** 内核级信号追踪证明，整个死亡链中
**没有任何外部进程向 harness 主进程发送过 SIGTERM**；第一个 SIGTERM 事件是
harness 主进程自己在 joblib 池正常关闭时向 worker 逐个发 TERM（Pool.terminate）。

### 8.1 真凶链（bpftrace + strace + sig_probe 交叉验证）

远端取证环境：宿主 spark-c294 上以 kaiser 进入（docker group + sudo），
devbox = 计算容器（2223→22）。宿主侧无 crontab/watchdog/oomd，内核日志无 OOM。

忠实复现 4 次（14 进程取数）全部死亡，死亡时间 5s/13s/10s/9s。宿主 bpftrace
（sys_enter_kill/tgkill/tkill + signal_generate）记录到的死亡链：

1. qlib 取数使用 joblib.ParallelExt(n_jobs=14, backend="multiprocessing")
   （qlib 默认 joblib_backend="multiprocessing"）→ multiprocessing.Pool，
   fork 出 14 个 worker。
2. 每个 processor 的并行 groupby 结束后，joblib 调 backend.terminate()
   → Pool.terminate() → **例行地给每个 worker 发 SIGTERM**（bpftrace 中
   主进程 pid=729025 的 KILL 事件，这正是死亡前第一条 sig=15）。
3. worker 是 fork 出来的，**继承了 harness 的 SIGTERM handler**
   （_term_handler：写探针 → killpg(0, SIGTERM) → exit(143)）。收到池关闭
   TERM 的 worker 执行 handler → killpg(0) 把 **整个进程组（包括 harness 主进程）
   一起 TERM 掉**。
4. 主进程再走自己的 handler → killpg(0) → exit(143)。整组瞬间死绝。

bpftrace 铁证：15:50:09.910259 主进程 kill worker 36589 → 15:50:09.911398
worker 36589 kill(0, SIGTERM)（killpg）→ 15:50:09.911458 该 worker 的 killpg
把信号送到 t_pid=729025（主进程）→ 15:50:09.916059 主进程 kill(0, SIGTERM)
→ 全员死亡。整个链路没有外部发送者。

这解释了全部现象：
- 为什么只有 qlib 取数阶段死（唯一用 joblib multiprocessing 池的阶段）；
- 为什么 detached 也死（与 ssh 会话无关，信号源在进程组内部）；
- 为什么 kernels=1 能绕过（n_jobs=1 → joblib 回落 SequentialBackend，无池无 worker）；
- 为什么死亡时间散布在 3~40s（对应各 processor 池完成的时机，不是固定阈值）。

### 8.2 修复（commit b9d123d）

pipeline/harness.py 的 _term_handler 增加 fork 子进程守卫：_install_term_handler
记录主进程 pid；handler 在 os.getpid() != _TERM_MAIN_PID 时（即 fork 出的
joblib/loky/multiprocessing worker）**只按默认方式死于 SIGTERM，绝不 killpg**。
主进程收到 TERM 时的杀组语义保持不变（队列 timeout/cancel 仍有效）。

回归测试：tests/test_harness.py::test_term_handler_forked_child_never_killpg（5 passed）。

### 8.3 验证

- **修复后 14 进程取数完整存活**：data ready 46s，train/test cache 全部写完
  （历史上同一路径 3~40s 必死）。bpftrace 显示池关闭仍在给 worker 发 TERM，但不再
  出现 worker 的 kill(0, SIGTERM) 级联。
- **第二次修复后完整取数存活**：data ready 48s。
- **30 分钟持续验证通过**：远端后台跑 31 分钟 joblib multiprocessing 池循环
  （与 qlib 相同的 backend），修复后的 handler 下持续存活——
  SUSTAIN_SURVIVED iter=658 elapsed=1861s（658 轮池循环，0 死亡）。

### 8.4 遗留说明

- 历史死亡 #5（kernels=1 串行 5~7 分钟死）：修复后未再复现；且本轮验证发现
  当时的 sitecustomize.py kernels=1 兜底在该启动路径下并**不可靠**（C2 运行日志
  bpftrace 仍出现 worker 池）。#5 最可能是同一自伤级联的变体（部分 kernels 覆盖
  未生效时仍创建了 multiprocessing 池），而非第二个外部杀手。
- sitecustomize.py 的 kernels=1 兜底已无存在必要（真修复已合入），已移除（2026-08-26 收尾提交），
  恢复远端默认 14 进程取数性能。
- 顺带发现（与本问题无关，已澄清为 HF 网络问题次生症状，非独立缺陷）：kronos executor
  在远端加载 NeoQuasar/Kronos-Tokenizer-base 时曾报 KronosTokenizer.__init__()
  missing 16 required positional arguments。经复核（2026-08-26），该报错不是模型/版本问题，
  而是 from_pretrained 走 online 模式下载 config.json 时远端无外网、SSL EOF 断连，
  拿不到 config 导致空参调用 __init__；config.json 的 16 个键与 __init__ 的 16 个参数
  一一对应。修复 = f4e0758（main.py 强制 HF_HUB_OFFLINE=1 + 权重/config 预缓存），
  修复后 smoke/hs300 均稳定加载成功，无需单独立项。
