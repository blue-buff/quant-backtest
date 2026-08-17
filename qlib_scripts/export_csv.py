"""baostock 导出沪深300 日线为 qlib dump 格式 CSV（前复权，2023-2026）"""
import sys
import os
import pandas as pd
import baostock as bs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DATA_SRC_DIR

OUT = DATA_SRC_DIR
os.makedirs(OUT, exist_ok=True)
START, END = "2023-01-01", "2026-08-15"

lg = bs.login()
print("login:", lg.error_code, lg.error_msg)

rs = bs.query_hs300_stocks()
codes = []
while rs.error_code == "0" and rs.next():
    row = rs.get_row_data()  # [updateDate, code, code_name]，code 如 sh.600519
    codes.append(row[1])
print(f"沪深300 成分: {len(codes)} 只（当前成分，含幸存者偏差，简化处理）")

ok = 0
skipped = 0
failed = 0
for i, code in enumerate(codes):
    fname = code.replace(".", "")  # sh.600519 -> sh600519
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount,turn",
            start_date=START, end_date=END, frequency="d", adjustflag="2")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if len(rows) < 100:
            skipped += 1
            if skipped <= 5:
                print(f"  [{i}] {code} 数据不足({len(rows)}行)", flush=True)
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                         "volume", "amount", "turn"])
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            df[c] = df[c].astype(float)
        # vwap = 成交额/成交量（元/股），成交量为 0 时回退到收盘价
        df["vwap"] = (df["amount"] / df["volume"].replace(0, pd.NA)).fillna(df["close"])
        df["factor"] = 1.0  # 价格已前复权，factor=1
        df = df[["date", "open", "high", "low", "close", "volume", "vwap", "factor"]]
        df.to_csv(f"{OUT}/{fname}.csv", index=False)
        ok += 1
    except Exception as e:
        failed += 1
        print(f"  [{i}] {code} 异常: {type(e).__name__}: {e}", flush=True)
    if (i + 1) % 30 == 0:
        print(f"  {i + 1}/{len(codes)} 完成 (ok={ok} skip={skipped} fail={failed})", flush=True)

bs.logout()
print(f"导出完成: ok={ok} skip={skipped} fail={failed}，目录 {OUT}", flush=True)
