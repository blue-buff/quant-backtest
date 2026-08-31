# QLab 轻量 WebUI 计划（2026-08-24 拟定，待人类批准后实施）

## 目标

人类一眼看全实验室状态，替代"按需启动的 MLflow UI"（启动 ~40s、占 ~2GB）。
只读、零侵入、单文件、无框架。

## 硬约束：不干扰研究进行

1. 不动 pipeline/*.py（冻结线：未经批准新增 pipeline 模块=违规）——UI 完全独立于管线。
2. 只读：不写 jobs.db / mlflow.db / knowledge；不创建、不修改任何文件（除自身日志）。
3. 不在容器内常驻新进程；不重启、不依赖 dispatcher / 通知桥 / 研究 agent。
4. 数据获取只复用现有只读 CLI：queue status --json / events / board --json /
   kb claims / heal（只读输出）/ cat heartbeat——这些本来就是日常命令，零新访问模式。
5. 不提供任何操作按钮（submit/retry/cancel/unblock 仍走 agent 或命令行）；
   UI 是旁观者，不是控制器。

## 技术方案

- 主机侧单文件 Node 服务 scripts/webui.js（与 notify_bridge.js 同风格，零依赖）。
- 监听 http://127.0.0.1:8099（端口可配，回环绑定，无认证需求）。
- 后台每 10s（可配）docker exec 拉取快照缓存于内存；浏览器请求只吃缓存，
  不触发容器调用——轮询放大效应为零。
- 一个静态 HTML 单页，内联 CSS，无构建、无框架、无图表库。

## 页面（四个区块）

1. 总览：队列各状态计数（queued/running/blocked/done/failed/cancelled）、
   dispatcher 心跳（epoch/pid/新鲜度）、桥状态（notify/bridge_state.json）、
   备份推送状态（backup_pending 计数 + 上次 push 时间）。
2. 队列：jobs 表（按 batch/状态过滤；行内展开 events 链 + error 摘要）。
3. 台账：board 表（exp/状态/rankic/p/base_ref/n_variants/p_bonf/sharpe…），
   --formal 开关，最近 200 行。
4. 知识：claims 列表（status 过滤 + 关键词搜索）。

## 明确不做

操作按钮、用户认证（仅本机回环）、图表库、移动端适配、超 200 行历史分页
（更老的直接看 results/board.csv）。

## 验收标准

- curl 三个 JSON 端点正常；浏览器人工过一遍四个区块。
- 全程 dispatcher / 桥 / 研究循环零变化；pytest 仍全绿（无代码改动，仅确认）。
- 刷新间隔内浏览器负载 ≈ 0，容器负载增量 ≈ 每 10s 三次只读 CLI（秒级）。

## 预估

~200 行 Node + ~150 行 HTML，实现 ~1h + 冒烟。实施前等人类批准。
