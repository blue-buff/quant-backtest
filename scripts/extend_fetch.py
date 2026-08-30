"""数据扩展：为每个已有 CSV 补拉 2021-06-01 起的历史（新浪 hfq+raw），前置合并。

不动 2022 之后的数据（新数据若与旧数据重叠以新为准——两段日期不重叠，直接 concat）。
用法: python scripts/extend_fetch.py --pool hs300 [--pool 可多值] --start 2021-06-01
"""
import argparse
import os
import sys
import time

import pandas as pd

sys.path.insert(0, "/root/quant")
from qlib_scripts.fetch_sina import _fetch_pair, convert_stock  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pools", default="hs300,zz500")
    ap.add_argument("--start", default="2021-06-01")
    args = ap.parse_args()
    import akshare as ak
    os.environ.setdefault("NO_PROXY", "*")
    for pool in args.pools.split(","):
        out = f"/root/quant/qlib_data_src" if pool == "hs300" else "/root/quant/qlib_data_src_zz500"
        files = sorted(f for f in os.listdir(out) if f.endswith(".csv"))
        ok, skip, fail = 0, 0, 0
        t0 = time.time()
        for i, f in enumerate(files):
            sym = f[:-4]
            if sym.startswith("sh000"):  # 指数
                continue
            p = os.path.join(out, f)
            old = pd.read_csv(p, dtype={"date": str})
            first = old["date"].min()
            if first <= args.start:
                skip += 1
                continue
            end = "2022-01-03"
            try:
                hfq, raw = _fetch_pair(ak, sym, args.start, end)
                add = convert_stock(hfq, raw)
                add["date"] = add["date"].astype(str)
                add = add[add["date"] < first]
                new = pd.concat([add, old], ignore_index=True).drop_duplicates(subset=["date"], keep="last")
                new = new.sort_values("date").reset_index(drop=True)
                new.to_csv(p, index=False)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                if fail <= 5:
                    print(f"  FAIL {sym}: {e}")
            if (i + 1) % 50 == 0:
                print(f"{pool} {i+1}/{len(files)} ok={ok} skip={skip} fail={fail} ({time.time()-t0:.0f}s)", flush=True)
            time.sleep(0.2)
        print(f"{pool} DONE: ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    main()
