"""新浪财经数据拉取器：完整字段（后复权价 + amount + 真实 factor + 真实换手率）

OPTIMIZATION.md P2-3/P2-5 的数据源实现：
- baostock 服务端故障期间（2026-08-19）改走新浪财经 ak.stock_zh_a_daily
- 每只股票拉 hfq（后复权）与 raw（不复权）两套：
    factor = close_hfq / close_raw            # 真实复权因子
    vwap   = (amount / volume) * factor       # 与后复权价同口径
    turn   = turnover * 100                   # 百分数口径，与 baostock 一致
- 指数用新浪 stock_zh_index_daily（vwap 以 close 填充、turn 空、factor=1.0）
- 产出 CSV 列: date,open,high,low,close,volume,amount,vwap,turn,factor
  （与 qbt data fetch / export_csv.py 的 P2-3/P2-5 口径完全一致）
- 写完写 .fetch_meta.json，qbt data fetch 缓存命中可跳过网络

用法:
  python qlib_scripts/fetch_sina.py --pool hs300 --out qlib_data_src
"""
import argparse
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL_INDEX = {"hs300": "sh000300", "zz500": "sh000905"}
START, END = "2023-01-01", "2026-08-15"


def _fetch_pair(ak, sym, start, end, retries=3):
    """拉 hfq + raw 两套日线，返回 (hfq_df, raw_df)；失败抛最后一次异常"""
    last = None
    for i in range(retries):
        try:
            hfq = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""), adjust="hfq")
            raw = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""), adjust="")
            if hfq is None or len(hfq) < 100:
                last = RuntimeError(f"数据不足({0 if hfq is None else len(hfq)}行)")
                time.sleep(1 + i)
                continue
            return hfq, raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise last


def convert_stock(hfq, raw):
    """新浪 hfq/raw → 项目统一 CSV 格式（后复权 + 真实 factor + turn 百分数）"""
    m = hfq.merge(raw[["date", "close"]], on="date", suffixes=("", "_raw"))
    if m.empty:
        raise RuntimeError("hfq 与 raw 日期无交集")
    df = pd.DataFrame({
        "date": m["date"],
        "open": m["open"],
        "high": m["high"],
        "low": m["low"],
        "close": m["close"],
        "volume": m["volume"],
        "amount": m["amount"],
        "vwap": (m["amount"] / m["volume"].replace(0, pd.NA)) * (m["close"] / m["close_raw"]),
        "turn": m["turnover"] * 100.0,   # 新浪为小数 → 百分数口径（baostock 一致）
        "factor": m["close"] / m["close_raw"],
    })
    df["vwap"] = df["vwap"].fillna(df["close"])
    return df[["date", "open", "high", "low", "close", "volume", "amount",
               "vwap", "turn", "factor"]]


def convert_index(df, start, end):
    """新浪指数日线 → 项目 CSV（vwap 用 close 填充；指数无 turn/factor）"""
    df = df.copy()
    df["date"] = df["date"].astype(str)
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    df["amount"] = 0.0
    df["vwap"] = df["close"]
    df["turn"] = pd.NA
    df["factor"] = 1.0
    return df[["date", "open", "high", "low", "close", "volume", "amount",
               "vwap", "turn", "factor"]]


def stock_symbols_from_dir(out_dir, exclude_index=True):
    """从已有 CSV 文件列表取股票 symbol（保证与旧数据股票集合一致）"""
    syms = []
    for f in sorted(os.listdir(out_dir)):
        if not f.endswith(".csv"):
            continue
        sym = f[:-4]
        if exclude_index and sym in ("sh000300", "sh000905"):
            continue
        syms.append(sym)
    return syms


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", default="hs300", choices=list(POOL_INDEX))
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--out", default="qlib_data_src")
    ap.add_argument("--sleep", type=float, default=0.2, help="请求间隔秒（防限频）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试）")
    args = ap.parse_args()

    import akshare as ak

    os.environ.setdefault("NO_PROXY", "*")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.out)
    os.makedirs(out, exist_ok=True)

    syms = stock_symbols_from_dir(out)
    if not syms:
        print("目录无旧 CSV，改用成分接口（index_stock_cons / sina）")
        cons = ak.index_stock_cons(symbol="000300" if args.pool == "hs300" else "000905")
        codes = cons["品种代码"].astype(str).str.zfill(6)

        def _to_sym(c):
            if c.startswith(("6", "9")):
                return "sh" + c
            if c.startswith(("4", "8")):
                return "bj" + c
            return "sz" + c

        syms = sorted(_to_sym(c) for c in codes)
    if args.limit > 0:
        syms = syms[: args.limit]
    print(f"股票数: {len(syms)}（{syms[0]} ~ {syms[-1]}）")

    ok, fails = 0, []
    t0 = time.time()
    for i, sym in enumerate(syms):
        fname = f"{sym}.csv"
        try:
            hfq, raw = _fetch_pair(ak, sym, args.start, args.end)
            df = convert_stock(hfq, raw)
            df.to_csv(os.path.join(out, fname), index=False)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fails.append((sym, f"{type(e).__name__}: {e}"))
        if (i + 1) % 25 == 0 or i == len(syms) - 1:
            print(f"  {i + 1}/{len(syms)} ok={ok} fail={len(fails)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
        time.sleep(args.sleep)

    # 指数
    idx_sym = POOL_INDEX[args.pool]
    try:
        idx = ak.stock_zh_index_daily(symbol=idx_sym)
        convert_index(idx, args.start, args.end).to_csv(os.path.join(out, f"{idx_sym}.csv"), index=False)
        print(f"指数 {idx_sym} OK")
    except Exception as e:  # noqa: BLE001
        fails.append((idx_sym, f"{type(e).__name__}: {e}"))
        print(f"指数失败: {e}")

    # 缓存元数据（与 qbt data fetch 兼容：口径一致即命中跳过）
    meta = {"pool": args.pool, "start": args.start, "end": args.end,
            "adjust": "1", "fields_version": "v2", "stocks": ok,
            "source": "sina", "note": "新浪财经完整字段（hfq+factor+turn）"}
    with open(os.path.join(out, ".fetch_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"完成: ok={ok} fail={len(fails)} 耗时 {time.time() - t0:.0f}s")
    for sym, err in fails[:20]:
        print(f"  FAIL {sym}: {err}")
    if len(fails) > 20:
        print(f"  ...共 {len(fails)} 个失败")
    if fails:
        with open(os.path.join(out, ".fetch_fails.json"), "w", encoding="utf-8") as f:
            json.dump(fails, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
