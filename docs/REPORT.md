# 任务完成报告（2026-08-20 凌晨自动化执行）

## 完成情况（6 项目标全部达成）

### 1. 新数据下载 ✅（270 只全部成功，字段完整）
- baostock 服务端故障（login 挂起 3 分钟+，多轮重试无效），改走**新浪财经**（akshare）
- 每只股票 hfq（后复权）+ raw（不复权）双拉：
  - **factor = close_hfq / close_raw**（真实复权因子，如茅台 7.859）
  - **turn = 换手率 × 100**（百分数口径，与代码一致）
  - **vwap = amount/volume × factor**（与后复权价同口径）
- 270 只股票 + 沪深300 指数，**0 失败**，876 个交易日（2023-01-03 ~ 2026-08-14）
- CSV 列: date,open,high,low,close,volume,amount,vwap,turn,factor（与 P2-3/P2-5 代码完全匹配）
- 已写入 .fetch_meta.json 缓存元数据，后续 qbt all 不再重复拉取

### 2. 本地与 docker 代码同步 ✅
- 58 个代码文件哈希逐一对比一致；容器 pytest 47/47 全绿

### 3. D 盘项目删除 ✅（见文末"删除确认"）
### 4. 代码复查 ✅（发现并修复 4 个真实偏差）
| 问题 | 影响 | 修复 |
|---|---|---|
| train.py 缺 resolve import | qrun 直接崩溃 | 补 import（容器全链路实测暴露） |
| yaml provider_uri 硬编码 | 换目录静默用错数据 | 生成 work yaml 时强制替换为配置 qlib_dir |
| qbt data validate 用前复权对比 | 后复权数据下必然误报 | 改为对比日收益率（复权基准无关） |
| rebalance_plan*.csv 进 git | 同步时旧文件覆盖新计划（曾致 rank-buffer 失效） | 移出 git 跟踪 + gitignore |

### 5. docker 全流程跑通 + HTML ✅
- 六步全 done：fetch（缓存命中）→ dump → train → plan → backtest → report
- **turn.day.bin 已生成**（P2-5 生效）；**plan 持仓 50~56**（rank-buffer N=10 生效）
- 新数据最终指标（v1.1）：
  - 简化层: IC 0.00125 / 超额年化 +2.41%（IR 0.30）
  - 真实规则: 总收益 **+34.84%** / 年化 +21.80% / Sharpe 1.47 / MDD 10.65% / 1274 笔
- 报告: docs/report.html（GitHub 仓库内）+ 桌面 quant_backtest_report.html

### 6. 代码提交 GitHub ✅
- 仓库: github.com/blue-buff/quant-backtest（main，15 commits，push 自 docker 容器）
- 容器内 git 化（safe.directory + 无代理 push）

## 需要你申请的 API Key（可选，本次未使用）
- **tushare pro token**：若后续想用官方数据源（日线含 turnover_rate、adj_factor 复权因子接口）替代新浪/备用 baostock，需注册 tushare.pro 获取 token（日线接口需基础积分）
- baostock 无需 key（免费），故障恢复后可随时切回：qbt data fetch --force --pool hs300

## 风险与遗留
- 新浪后复权基准与 baostock 不同（日收益率口径一致，绝对价不同）——README 已声明跨源差异
- 新数据下 IC/超额（0.00125 / +2.41%）显著低于旧数据（0.013 / +12.8%）：因子在新口径下预测力大幅下降，需研究（这可能就是"真实"水平，也可能是新浪 turn 引入后因子行为变化）
- zz500 全链路未重跑（数据拉取脚本支持 --pool zz500，数据在本地已删除前未拉取，需要时在容器内补拉）
- 滑点仍为 0（qbt.yaml backtest.slippage 可配）
