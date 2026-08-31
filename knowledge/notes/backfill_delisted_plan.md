# 退市数据补齐 开发计划（backfill delisted stocks）

> 状态：待执行（交给专门会话办理）。本笔记自包含，执行者无需其它上下文。
> 缘起：all 池（全市场 5265 只）存在幸存者偏差——2021-06-01 之后退市、且
> 最初建池时就不在名单里的股票，历史行情完全缺失，导致历史收益系统性偏高。
> 本计划把「退市股消失」这一半偏差补掉；另一半（指数成分调入调出）另走 tushare 路径。

## 1. 环境与位置
- 权威仓库/实验环境 = 容器 hermes-1679f5b2:/root/quant（下称 repo），宿主镜像 D:\\quant_backup 无 git。
- 数据源目录（CSV）：
  - all → /root/quant/qlib_data_src_all（**本计划只动这个**）
  - hs300 → /root/quant/qlib_data_src（301 文件，不动）
  - zz500 → /root/quant/qlib_data_src_zz500（501 文件，不动）
- akshare 已在容器内（1.18.91）。已核验接口存在：stock_info_sh_delist、
  stock_info_sz_delist、stock_zh_a_hist（东方财富历史行情）。
- 现有取数口径唯一真源：qlib_scripts/fetch_sina.py 的 convert_stock()。

## 2. 统一 CSV 口径（错一列 qlib dump 就废，必须逐字对齐）
10 列、逗号分隔、date 为 YYYY-MM-DD 字符串：
```
date,open,high,low,close,volume,amount,vwap,turn,factor
```
- close = **后复权**收盘
- factor = close_hfq / close_raw（真实复权因子）
- vwap = (amount/volume) * factor，volume=0 时填 close
- turn = 换手率，**百分数口径**（2.5 表示 2.5%）

## 3. 数据源与接口（Step 0 先核验）
- 退市清单：
  - 上海终止上市 ak.stock_info_sh_delist()
  - 深圳终止/暂停 ak.stock_info_sz_delist()
  - 列名执行时用 df.columns 核验（预期含：证券代码/证券简称/终止上市日期）。
- 退市股历史（新浪 stock_zh_a_daily 对退市股返回空，**必须改用东方财富**）：
  - ak.stock_zh_a_hist(symbol, period=daily, start_date=20210601, end_date=..., adjust=hfq) → 后复权
  - 同接口 adjust=（空）→ 不复权（算 factor 用）
- **Step 0 必做（本计划最大坑）**：用一只能在两个源都查到的票（如 sh600000）同时拉
  sina 与 eastmoney，核对：volume 单位（eastmoney 成交量单位是「手」，可能需 ×100 对齐 sina 的「股」）、
  turn 口径、hfq close 是否只差常数倍。核对通过才能进 Step B。

## 4. 实施步骤
1. 写 scripts/backfill_delisted.py（放 repo，容器内跑）。
2. Step A：拉沪深退市清单，过滤「终止上市日期 >= 2021-06-01」；6 位代码转前缀
   （复用 fetch_sina 的 _to_sym：6/9→sh，0/3→sz，4/8→bj）。
3. Step B：逐只退市股：若 qlib_data_src_all/<sym>.csv 已存在且末行日期 ≥ 退市日，跳过；
   否则拉 hfq+raw（eastmoney）→ 转 10 列 → 写 qlib_data_src_all/<sym>.csv。
   加 sleep + 重试防限频；查不到历史（吸收合并/转板）记 no_data，不失败。
4. Step C：写 data/extra/delisted_manifest.json（tracked，用于复现）：
   每只 {sym, name, delist_date, source:eastmoney, rows, start, end, status:added/skipped/no_data}。
5. Step D：python -m pipeline.data invalidate --pool all（推进数据修订号，下次 run 自动重建缓存）。
6. Step E：冒烟验证（§5）。

## 5. 验收标准
- 一只已知退市股（2021-06 之后退市的沪深主板票）CSV 存在，覆盖 2021-06-01..退市日，10 列齐全，close 无 NaN。
- 现存股（如 sh600000）的 CSV 未被改动（diff 为空）。
- volume/turn 口径与现有 CSV 一致（Step 0 核验通过）。
- all 池 CSV 总数增加；python -m pipeline.board 或 data ensure 显示 data_rev 已推进。
- manifest 记录完整。

## 6. 风险与诚实标注
- **后复权基准可能不同**：eastmoney 与 sina 的 hfq 基准或差常数倍。退市股与现存股无同日重叠，
  对收益率/截面特征影响有限；manifest 记 source，结论注明口径。
- 本计划只修「退市股消失」；**未修**：hs300/zz500 成分调入调出、暂停上市期间、北交所退市
  （akshare 1.18.91 无 bj 退市接口）。这些留待 tushare 路径，结论继续带相应 caveat。
- eastmoney 限频：控制并发 + sleep。

## 7. 交付物
- scripts/backfill_delisted.py
- data/extra/delisted_manifest.json
- 池数据更新 + 缓存 invalidate + board 冒烟记录
- 回填本笔记「实际结果」段

## 8. 实际结果（执行者回填，2026-08-29）

**执行偏差（用户已批准）**：Step 0 计划为新浪/东财对账，但当日东财历史 K 线
`push2his.eastmoney.com` 被 WAF 阻断（宿主与容器均 TLS 断连，重试/浏览器头/
编号子域/http/jsonp 均无效；realtime `push2` 正常）。经用户确认改用 **baostock**
（同 10 列口径，manifest `source: baostock`），东财方向字面对账未能完成，记为偏差。

- 退市清单数量 / 过滤后数量 / added / skipped / no_data：
  - SH 终止上市清单 159（akshare `stock_info_sh_delist`，其「暂停上市日期」列实为
    DELIST_DATE，已核验）；SZ 终止上市清单 208（`stock_info_sz_delist`）。
  - 过滤「终止上市日期 >= 2021-06-01」并按代码去重（A/B 双上市同码）后 **205** 只。
  - **added=196 / skipped=0 / no_data=9**（8 只 B 股 200xxx/900xxx 不入池：
    池 universe 无 B 股且 baostock 无覆盖；1 只 sh600485 *ST信威：窗口内无成交、
    长期停牌至退市）。
- 新增 CSV 行数合计：**117,815 行**（196 只）。
- Step 0 口径核验结论（baostock vs 新浪，sh600000，2021-06-01..08-31，65 重叠日）：
  - volume：**逐日完全相等**（单位同为「股」，无需 ×100）；
  - amount：**逐日完全相等**（元）；raw close：**完全相等**；
  - turn：baostock 为**百分数口径**（2.5 = 2.5%），与新浪 `turnover×100`
    在 4 位小数内一致（|diff| ≤ 5e-5，baostock 4 位小数舍入）；
  - hfq close / factor：与新浪差**常数倍 0.768677**（后复权基准日不同）——
    即计划 §6 预判的 caveat 类别；退市股与现存股无同日重叠，对收益率/截面特征影响有限。
  - vwap 公式（amount/volume×factor，volume=0 填 close）196 只全量复核通过。
- 冒烟验证 data_rev / board 结果：
  - `python -m pipeline.data invalidate --pool all`：data_rev **0 → 1**，
    移除缓存 `prices_all_a3feca3dd2d9e6bb.parquet`（下次 run 自动重建）；
  - all 池 CSV **5266 → 5462**（+196；目录另含非 CSV 的 `.fetch_meta.json`/`stock_list.json`）；
  - 196 只新增 CSV 全量校验：10 列齐全、close 无 NaN、date 在 [2021-06-01, 退市日]、
    与 manifest rows/start/end 一致；
  - 现存股未改动：sh600000.csv sha256 前后一致（`8f1762d3…`）；
  - board 未重跑（数据修订号已推进，缓存重建由下次 run 触发）。
- 额外 caveat：10 只新增股首行晚于 2021-06-01（停牌至复牌/退市整理期，或
  `sh688287 退市观典` 2022-05-25 才上市）；停牌无成交日按新浪口径剔除（baostock
  空量行不写入）。manifest 见 `data/extra/delisted_manifest.json`（205 条，逐只
  sym/name/delist_date/source/rows/start/end/status/reason）。