"""按历史调仓日拉取指数成分，消除幸存者偏差（OPTIMIZATION.md A2）

原理：baostock query_hs300_stocks(date=YYYY-MM-DD) / query_zz500_stocks(date=...)
支持历史日期参数（官方 pythonAPI 文档 12.3）。对每个调仓日（每年 1 月、7 月的
首个交易日）拉取【当时的】成分，而不是当前快照：
  - 日线：成分并集的全区间后复权 CSV（含真实 factor 与 turn，与 export_csv*.py 同口径）
  - 成分清单：csi300.txt / csi500.txt（每股票可多行区间，qlib instruments 格式）

用法:
  python qlib_scripts/export_csv_hist.py --pool hs300 --start 2023-01-01 --end 2026-08-15
      --out qlib_data_src_hist --instruments instruments_hist
      [--limit-dates 2]   # 只处理前 N 个调仓日（调试）
验证:
  1) 2023 年成分与当年公告抽查对比
  2) 找一只 2024 年被剔除的股票，确认其在当年池内、剔除后不在
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import PROJECT_ROOT  # noqa: E402

POOLS = {
    "hs300": {"query": "query_hs300_stocks", "index": "sh.000300", "universe": "csi300"},
    "zz500": {"query": "query_zz500_stocks", "index": "sh.000905", "universe": "csi500"},
}
# 指数半年调仓月：1 月、7 月（首个交易日为调仓日）
REBALANCE_MONTHS = (1, 7)
START, END = "2023-01-01", "2026-08-15"


def first_trading_days_of_month(days, months=REBALANCE_MONTHS):
    """给定交易日列表（YYYY-MM-DD），返回每月 months 中首个交易日（调仓日）"""
    by_month = {}
    for d in days:
        y, m = int(d[:4]), int(d[5:7])
        by_month.setdefault((y, m), d)  # days 升序，首个即最早
    return [d for (y, m), d in sorted(by_month.items()) if m in months]


def fetch_members(bs, query_fn, dates):
    """每个调仓日的成分列表 → {date: [symbol, ...]}（symbol 形如 sh.600519）"""
    members = {}
    for d in dates:
        rs = getattr(bs, query_fn)(date=d)
        codes = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if len(row) >= 2:
                codes.append(row[1])
        members[d] = codes
    return members


def build_instruments(members, symbols=None):
    """成分清单：每股票按连续调仓期合并为 (SYMBOL, start, end) 区间行

    qlib instruments 格式（制表符分隔，同 symbol 可多行不同区间）。
    """
    dates = sorted(members)
    by_sym = {}
    for i, d in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else "2099-12-31"
        for s in members[d]:
            if symbols is not None and s not in symbols:
                continue
            by_sym.setdefault(s, []).append((d, nxt))
    rows = []
    for s in sorted(by_sym):
        merged = []
        for start, end in sorted(by_sym[s]):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            rows.append(f"{s.replace('.', '').upper()}\t{start}\t{end}")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", default="hs300", choices=list(POOLS))
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--out", default="qlib_data_src_hist", help="日线 CSV 输出目录")
    ap.add_argument("--instruments", default="instruments_hist", help="成分清单输出目录")
    ap.add_argument("--limit-dates", type=int, default=0, help="只处理前 N 个调仓日（调试）")
    args = ap.parse_args()

    import baostock as bs

    pool = POOLS[args.pool]
    out_dir = os.path.join(PROJECT_ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)

    lg = bs.login()
    print("login:", lg.error_code, lg.error_msg)

    # 1) 交易日历 → 调仓日
    rs = bs.query_trade_dates(start_date=args.start, end_date=args.end)
    days = []
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        if row[1] == "1":
            days.append(row[0])
    rebs = first_trading_days_of_month(days)
    if args.limit_dates > 0:
        rebs = rebs[: args.limit_dates]
    print(f"调仓日 {len(rebs)} 个: {rebs[0]} ~ {rebs[-1]}")

    # 2) 每个调仓日的当时成分
    members = fetch_members(bs, pool["query"], rebs)
    for d, codes in members.items():
        print(f"  {d}: {len(codes)} 只")
    all_syms = sorted({s for codes in members.values() for s in codes})
    print(f"成分并集: {len(all_syms)} 只（跨 {len(rebs)} 个调仓期）")

    # 3) 导出成分并集全区间日线（后复权 + 真实 factor + turn，与 export_csv*.py 同口径）
    ok = fail = 0
    for i, code in enumerate(all_syms):
        fname = code.replace(".", "")
        try:
            rs = bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume,amount,turn,factor",
                start_date=args.start, end_date=args.end, frequency="d", adjustflag="1")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if len(rows) < 100:
                continue
            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                             "volume", "amount", "turn", "factor"])
            for c in ["open", "high", "low", "close", "volume", "amount", "turn", "factor"]:
                df[c] = df[c].astype(float)
            df["vwap"] = (df["amount"] / df["volume"].replace(0, pd.NA)).fillna(df["close"])
            df.to_csv(os.path.join(out_dir, f"{fname}.csv"), index=False)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  [{i}] {code} 异常: {type(e).__name__}: {e}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(all_syms)} ok={ok} fail={fail}", flush=True)
    print(f"日线导出完成: ok={ok} fail={fail} → {out_dir}")

    # 4) 成分清单（instruments，多区间）
    inst_dir = os.path.join(os.path.dirname(out_dir), args.instruments)
    os.makedirs(inst_dir, exist_ok=True)
    rows = build_instruments(members)
    uni = f"{pool['universe']}.txt"
    with open(os.path.join(inst_dir, uni), "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    print(f"成分清单: {len(rows)} 行 → {inst_dir}/{uni}（幸存者偏差已消除，按调仓日成分）")

    bs.logout()


if __name__ == "__main__":
    main()
