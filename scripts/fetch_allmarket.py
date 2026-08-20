"""全市场 A 股日线拉取（新浪 hfq+raw，2021-06-01 起，断点续传）。

- 股票列表来自 sina spot（ak.stock_zh_a_spot），剔除 ST/*ST/退
- 每只拉 hfq + raw 两套 → 标准 CSV（与池数据同口径）
- 已下载且 >=900 行的跳过（断点续传）
用法: python scripts/fetch_allmarket.py
"""
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, "/root/quant")
from qlib_scripts.fetch_sina import _fetch_pair, convert_stock  # noqa: E402

OUT = "/root/quant/qlib_data_src_all"
START, END = "2021-06-01", "2026-08-15"


def main():
    import akshare as ak
    os.environ.setdefault("NO_PROXY", "*")
    os.makedirs(OUT, exist_ok=True)
    spot = ak.stock_zh_a_spot()
    spot = spot[~spot["名称"].str.contains("ST|退", na=False)]
    syms = sorted(spot["代码"].astype(str).str.lower().tolist())
    print(f"全市场（剔 ST/退）: {len(syms)} 只")
    with open(os.path.join(OUT, "stock_list.json"), "w", encoding="utf-8") as f:
        json.dump({"n": len(syms), "syms": syms}, f)

    def done(sym):
        fp = os.path.join(OUT, sym + ".csv")
        if not os.path.exists(fp):
            return False
        try:
            with open(fp, encoding="utf-8") as fh:
                return sum(1 for _ in fh) - 1 >= 900
        except OSError:
            return False

    todo = [s for s in syms if not done(s)]
    print(f"续传: 跳过 {len(syms) - len(todo)}，待拉 {len(todo)}")
    ok, fail = 0, 0
    t0 = time.time()
    for i, sym in enumerate(todo):
        try:
            hfq, raw = _fetch_pair(ak, sym, START, END)
            convert_stock(hfq, raw).to_csv(os.path.join(OUT, sym + ".csv"), index=False)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            if fail <= 10:
                print(f"  FAIL {sym}: {type(e).__name__} {str(e)[:80]}", flush=True)
        if (i + 1) % 100 == 0:
            print(f"{i+1}/{len(todo)} ok={ok} fail={fail} ({time.time()-t0:.0f}s)", flush=True)
        time.sleep(0.15)
    print(f"DONE ok={ok} fail={fail} 耗时 {time.time()-t0:.0f}s")
    meta = {"source": "sina", "start": START, "end": END, "fields_version": "v2",
            "stocks": ok, "fails": fail, "note": "全市场（剔 ST/退）"}
    with open(os.path.join(OUT, ".fetch_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
