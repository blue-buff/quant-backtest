"""baostock 导出中证500 日线为 qlib dump 格式 CSV（后复权 + 真实 factor + turn，2023-2026）

P2-3: adjustflag=1 后复权，factor 取真实复权因子（不再硬编码 1.0）
P2-5: turn（换手率）字段保留导出，Alpha158 的流动性类因子不再缺失
"""
import sys
import os
import pandas as pd
import baostock as bs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DATA_SRC_ZZ500_DIR

OUT = DATA_SRC_ZZ500_DIR
os.makedirs(OUT, exist_ok=True)
START, END = "2023-01-01", "2026-08-15"

lg = bs.login()
print("login:", lg.error_code, lg.error_msg)

rs = bs.query_zz500_stocks()
codes = []
while rs.error_code == "0" and rs.next():
    row = rs.get_row_data()  # [updateDate, code, code_name]，code 如 sh.600519
    codes.append(row[1])
print(f"中证500 成分: {len(codes)} 只（当前成分，含幸存者偏差，简化处理）")

ok = 0
skipped = 0
failed = 0
for i, code in enumerate(codes):
    fname = code.replace(".", "")  # sh.600519 -> sh600519
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount,turn,factor",
            start_date=START, end_date=END, frequency="d", adjustflag="1")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if len(rows) < 100:
            skipped += 1
            if skipped <= 5:
                print(f"  [{i}] {code} 数据不足({len(rows)}行)", flush=True)
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                         "volume", "amount", "turn", "factor"])
        for c in ["open", "high", "low", "close", "volume", "amount", "turn", "factor"]:
            df[c] = df[c].astype(float)
        # vwap = 成交额/成交量（元/股），成交量为 0 时回退到收盘价
        df["vwap"] = (df["amount"] / df["volume"].replace(0, pd.NA)).fillna(df["close"])
        # P2-3: 真实复权因子（后复权口径）；P2-5: 保留 turn 供 Alpha158 流动性因子
        df = df[["date", "open", "high", "low", "close", "volume", "vwap", "turn", "factor"]]
        df.to_csv(f"{OUT}/{fname}.csv", index=False)
        ok += 1
    except Exception as e:
        failed += 1
        print(f"  [{i}] {code} 异常: {type(e).__name__}: {e}", flush=True)
    if (i + 1) % 30 == 0:
        print(f"  {i + 1}/{len(codes)} 完成 (ok={ok} skip={skipped} fail={failed})", flush=True)

bs.logout()
print(f"导出完成: ok={ok} skip={skipped} fail={failed}，目录 {OUT}", flush=True)
