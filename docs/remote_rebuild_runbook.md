# DGX Spark 远端容器重建教程（--shm-size + strace/rsync/bzip2）

> 用途：解决挂账 #7（/dev/shm=64MB）与 #8（无 strace/rsync/bzip2）。
> 前置条件：需要 **DGX 宿主机（10.0.0.5 所在主机）的 docker 权限**——容器内无 sudo，
> 这些参数只能在创建层改。本教程在宿主机上操作，不是通过 ssh 在容器里操作。

## 第 1 步：记录现行配置（重建前必须做）

```bash
docker inspect <容器名/ID> --format '
image={{.Config.Image}}
shm={{.HostConfig.ShmSize}}
restart={{.HostConfig.RestartPolicy.Name}}
network={{.HostConfig.NetworkMode}}
user={{.Config.User}}
' 
docker inspect <容器名/ID> --format '{{json .Mounts}}'          # 卷挂载清单
docker inspect <容器名/ID> --format '{{json .Config.Env}}'       # 环境变量
docker port <容器名/ID>                                          # 端口映射
```

把输出原样存一份（回滚用）。重点确认：
- **/home/dev 或 /home/dev/quant 是不是卷挂载**（Mounts 里 Type=volume/bind）。
  如果是容器层数据且无挂载，rm 容器会丢数据/venv——重建前必须先 tar 备份。

## 第 2 步：重建容器（模板，按第 1 步输出替换）

```bash
docker stop <容器名> && docker rm <容器名>
docker run -d --name <容器名> \
  --restart unless-stopped \
  --shm-size 16g \
  --cap-add SYS_PTRACE \
  -p <原端口映射> \
  -v <原卷挂载逐项照抄> \
  --network <原网络> \
  -e <原环境变量逐项照抄> \
  <原镜像> <原启动命令>
```

说明：
- `--shm-size 16g`：解决 #7。给 DataLoader worker 足够共享内存
  （官方 n_jobs=20 峰值约 100-200MB，16g 富余；保守 4g 也够，按需）。
- `--cap-add SYS_PTRACE`：strace 需要 ptrace 权限（解决 #8 的 strace 部分）。
- 镜像/启动命令照抄原配置，**不要顺手升级镜像**（远端环境刚稳定，一次只变一个变量）。

## 第 3 步：容器内装缺失工具（二选一）

方式 A（容器内有 root 且能 apt）：
```bash
docker exec -u 0 <容器名> bash -c "apt-get update && apt-get install -y strace rsync bzip2"
```

方式 B（无 root/无 apt）：宿主机下载静态 strace 单文件丢进容器
（rsync 可先用 tar+scp 代替，bzip2 非必需——bzip2 只是历史管线用过，
当前 tar czf 已是 gzip，此项可选）。

## 第 4 步：验收清单（全部通过才算完成）

```bash
# 容器内：
df -h /dev/shm          # 期望 16G（原来 64M）
strace -V               # 期望有版本号
rsync --version         # 可选
free -h                 # 常规内存应和原来一致（121Gi 量级）
# 数据/venv 完整性（若依赖卷挂载则自动保留）：
ls /home/dev/quant/qlib_data /home/dev/quant/executor_venvs
/home/dev/.local/share/mamba/envs/quant/bin/python -c "import qlib; print(qlib.__version__)"
# 链路回归：宿主机/主机跑一单最小任务
python -m pipeline.queue submit experiments/batches/b-p8-spark-kinds.json
python -m pipeline.queue run --batch b-p8-spark-kinds --once --concurrency 1
```

## 回滚

重建失败时按第 1 步留存的配置原样 docker run（旧参数，--shm-size 不加），
容器内环境恢复原状；若第 1 步发现数据无卷挂载，备份的 tar 解回 /home/dev 即可。

## 备注

- 与本次变更无关的项一律照抄原配置（端口/网络/环境变量），保持"一次只变一个变量"。
- 重建成功后把 strace 可用的消息回填本文件与 REMOTE_ENV.md，并给 #7/#8 挂账销项。
