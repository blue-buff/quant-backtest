# 项目迁移计划（换设备 + DSH 会话记录）

> 目标：把量化项目（容器 /root/quant）+ 行情数据 + 台账 + DSH 会话记录，
> 打包成可直接 LocalSend 的文件，换到另一台设备继续工作。

## 1. 打包产出（两个文件，都在 D:\quant_backup\）

### 文件 1：quant_migrate_20260829.tar.gz（约 2.5–3G）
来源：宿主 C:\Users\ningj\AppData\Local\hermes\sandboxes\docker\default\home\
- quant/ —— 完整仓库（含 .git 942M、代码、specs、知识笔记、台账）
- .qlib/ —— qlib 数据 bins（378M，避免重跑 dump）
- .cache/ —— 772M（含 HuggingFace 的 Kronos 权重，离线可用）

quant/ 内**排除**（可复现 / 垃圾 / 重复，约 12.8G）：
- results/remote_pack 9.2G、results/backup 2.0G、results/venvs 1.2G、cache 1.8G
- qlib_examples/mlruns、qlib_data_src_old23、qlib_data_src_zz500_old
- results/kronos_models.tgz 377M（与 .cache 里的权重重复）

### 文件 2：dsh_sessions_20260829.tar.gz（约 100M）
来源：C:\Users\ningj\.dsh\
- sessions/ 105M（各工作区会话记录，含 --D-quant_backup--）
- profiles/、skills/、superpowers/、settings.yaml、.agent-presets、
  .credentials.yaml（含 DEEPSEEK_API_KEY）

## 2. 另一台设备的恢复步骤
1. 装 Docker Desktop + Git + Node.js + DSH（npm i -g @deepseek-ai/dsh）。
2. 启动 Docker Desktop；DSH 首次运行会创建 sandboxes\docker\default\home 目录。
3. 解压文件 1 到该 home 目录 → 得到 home\quant、home\.qlib、home\.cache。
4. 解压文件 2 到 C:\Users\<用户名>\ → 恢复 .dsh（会话 + API key + 预设）。
5. 启动 DSH Web GUI。注意：容器名可能不同（本机是 hermes-1679f5b2），
   新设备按实际容器名改 AGENTS.md 与脚本里的引用。
6. 验证：git log 有 c07784a；python -m pipeline.board 正常；跑一个 smoke 实验。

## 3. 安全提醒（重要）
两个包里含机密：
- DEEPSEEK_API_KEY（.dsh/.credentials.yaml）
- GitHub token（quant/.qlab_github_token）
只在你自己设备间传，不要发给别人；LocalSend 走局域网，别连公共 Wi-Fi 传。

## 4. 本机打包命令（复现用）
见 D:\quant_backup\scratch_probe\make_migrate_tar.sh。

## 5. 未包含（需要时另取）
- results/remote_pack（spark 远端打包，可重生成）、results/backup（快照，历史已在 .git）
- results/venvs（依赖环境，按 requirements.txt 重建）、cache（从 CSV 重建）
- 指数历史成分 / 退市数据等数据欠账（见 backfill_delisted_plan.md）