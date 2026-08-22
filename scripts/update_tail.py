"""增量更新：拉最近区间（2026-08-05 ~ 2026-08-21）合并进现有 CSV（新日期覆盖旧）。

- 股票：hfq+raw 双拉 → convert_stock → 合并去重（keep=last）
- 指数：stock_zh_index_daily 最近区间合并
- 已退市股（新浪返回空）跳过保留原数据
用法: python scripts/update_tail.py [--pools hs300,zz500,all]
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, "/root/quant")
from qlib_scripts.fetch_sina import _fetch_pair, convert_stock  # noqa: E402

POOL_DIRS = {"hs300": "/root/quant/qlib_data_src",
             "zz500": "/root/quant/qlib_data_src_zz500",
             "all": "/root/quant/qlib_data_src_all"}
POOL_INDEX = {"hs300": "sh000300", "zz500": "sh000905"}
START, END = "2026-08-05", "2026-08-21"


def fetch_tail(ak, sym, start, end, retries=3):
    last = None
    for i in range(retries):
        try:
            hfq = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""), adjust="hfq")
            raw = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""), adjust="")
            if hfq is None or len(hfq) < 2:
                last = RuntimeError(f"数据不足({0 if hfq is None else len(hfq)}行)")
                time.sleep(1 + i)
                continue
            return hfq, raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise last


def update_one(ak, out, sym, stats):
    fp = os.path.join(out, f"{sym}.csv")
    try:
        hfq, raw = fetch_tail(ak, sym, START, END)
        add = convert_stock(hfq, raw)
        add["date"] = add["date"].astype(str)
        old = pd.read_csv(fp, dtype={"date": str})
        new = pd.concat([old, add], ignore_index=True).drop_duplicates(subset=["date"], keep="last")
        new = new.sort_values("date").reset_index(drop=True)
        new.to_csv(fp, index=False)
        stats["ok"] += 1
    except Exception as e:  # noqa: BLE001
        stats["fail"] += 1
        if stats["fail"] <= 5:
            print(f"  FAIL {sym}: {type(e).__name__} {str(e)[:80]}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pools", default="hs300,zz500,all")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    import akshare as ak
    os.environ.setdefault("NO_PROXY", "*")
    for pool in args.pools.split(","):
        out = POOL_DIRS[pool]
        syms = sorted(f[:-4] for f in os.listdir(out) if f.endswith(".csv") and not f.startswith("_"))
        syms = [s for s in syms if s not in POOL_INDEX.values()]
        stats = {"ok": 0, "fail": 0}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(update_one, ak, out, s, stats) for s in syms]
            done = 0
            for f in as_completed(futs):
                f.result()
                done += 1
                if done % 200 == 0:
                    print(f"{pool} {done}/{len(syms)} ok={stats['ok']} fail={stats['fail']} ({time.time()-t0:.0f}s)", flush=True)
        # 指数
        idx = POOL_INDEX.get(pool)
        if idx:
            try:
                df = ak.stock_zh_index_daily(symbol=idx)
                df = df[df["date"] >= START]
                df["date"] = df["date"].astype(str)
                old = pd.read_csv(os.path.join(out, f"{idx}.csv"), dtype={"date": str})
                new = pd.concat([old, df], ignore_index=True).drop_duplicates(subset=["date"], keep="last")
                new.to_csv(os.path.join(out, f"{idx}.csv"), index=False)
                print(f"{idx} 指数更新 OK", flush=True)
            except Exception as e:
                print(f"{idx} 指数更新失败: {e}", flush=True)
        print(f"{pool} DONE ok={stats['ok']} fail={stats['fail']} 耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
