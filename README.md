# A股/港股 Python 回测练习

用 Python 做 A股/港股回测的练习项目，含两个版本的回测链路与完整踩坑记录。

## 目录结构

```
quant/
├── demo_backtest.py     # backtesting.py 版：无 A股规则（双均线 5/20）
├── rqalpha/
│   ├── strategy.py      # rqalpha 版：真实 A股规则（T+1/涨跌停/印花税/100股整数倍）
│   ├── rq_run.py        # rqalpha Python API 跑回测并输出绩效
│   ├── config.yml       # rqalpha 配置（bundle 路径/日期/资金）
│   └── diag_rq.py       # 调试脚本
└── diag*.py             # backtesting.py 调试脚本
```

## 数据源（中国网络环境验证）

| 用途 | 方案 | 说明 |
|---|---|---|
| A股日线 | baostock | 免费无需注册，`adjustflag="2"` 前复权 |
| 港股日线 | 腾讯行情接口 | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` |
| rqalpha 回测数据 | 米筐 bundle | 月度更新 ~1GB，解压 3.3G，含分红/复权/ST/停牌 |
| ⚠️ 东财接口(akshare) | 云服务器/容器 IP 会被断连 | 仅本地可用 |

## 回测结果（5/20 双均线 × 茅台 600519，2023-01 ~ 2026-08）

| 版本 | 交易 | 胜率 | 总收益 | 最大回撤 |
|---|---|---|---|---|
| backtesting.py（无规则） | 28 | 25.0% | -40.06% | -46.40% |
| rqalpha（真实规则） | 57 | 16.9% | -35.23% | -40.74% |

结论：双均线在茅台震荡阴跌市大幅亏损、胜率极低——回测的意义是实盘前发现策略不可行。

## 关键坑（详见 skill: cn-stock-backtesting）

1. **backtesting.py 0.6.x**：全仓买入后裸 `sell()` 被 margin 逻辑静默取消 → 平仓必须 `self.position.close()`
2. **rqalpha 6.x**：必须显式 `subscribe()`；`data_bundle_path` 指向 bundle 内容目录；A股 100 股整数倍（高价股资金不足 1 手时订单静默失败）
3. **回测可信度**：交易数异常（0 或过少）= 策略有 bug；核对胜率非 nan

## 运行

```bash
# backtesting.py 版
python3 demo_backtest.py 2>/dev/null

# rqalpha 版（需先下载 bundle 到 /root/.rqalpha/bundle）
cd rqalpha && python3 rq_run.py 2>/dev/null
```
