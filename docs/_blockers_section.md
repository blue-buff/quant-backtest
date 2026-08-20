

## 8. 硬阻碍清单（供资源方解决）

1. **容器内存上限 5GB**（Docker Desktop 默认）：全市场训练特征矩阵（5341 股 × 1100 日 × 159 特征）峰值内存超 12GB（float32 方案也 OOM）。已用 docker update 提到 12GB 仍不够；**已改到远程机（31.8GB）执行**。
2. **远程机实为 Windows**（iLucas，Win10，远程默认 shell 是 cmd，PowerShell 可用）：已用便携版 Python 3.12.10（零系统改动，仅在 C:\Users\song\qbt_work 内）+ pyqlib 0.9.7 官方 win wheel 搭建环境。Windows 无 WSL、无 rsync。
3. **容器↔远程 scp 大文件传输反复中断**（~140MB 处断）：已改 20MB 分块 + 重试传输。
4. **新浪成分接口 index_stock_cons 返回重复行**（hs300 300 行仅 288 唯一）：导致首批数据缺 12 只，已改用 index_stock_cons_sina 修复并核对补全。
5. **csindex 官网接口（akshare index_stock_cons_csindex）在容器内挂死**（90s+ 无响应）：已弃用。
6. **东财接口被断连**（Connection aborted）：行业分类/市值等数据源受限，行业中性化实验顺延；建议后续申请 tushare pro token（股本/行业/分析师预期）。
7. **宿主 Windows shell 对 ssh 远程命令的引号/管道处理异常**（cmd 拆分 ; | " 且吃反斜杠）：已改用"脚本文件 + powershell -File"模式规避；反斜杠需写 4 个到达远程 1 个。
8. **baostock 服务端故障**（历史遗留）：新浪替代已稳定。
