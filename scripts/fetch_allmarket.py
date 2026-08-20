"""全市场 A 股日线拉取（4 线程并行版，断点续传）。

- 列表来自 sina spot，剔除 ST/*ST/退
- 每只 hfq+raw → 标准 CSV；已下载且 >=900 行跳过
- 4 线程各取一段 todo，互不重叠
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, "/root/quant")
from qlib_scripts.fetch_sina import _fetch_pair, convert_stock  # noqa: E402

OUT = "/root/quant/qlib_data_src_all"
START, END = "2021-06-01", "2026-08-15"
WORKERS = 4
COUNTER_LOCK = threading.Lock()


def done(sym):
    fp = os.path.join(OUT, sym + ".csv")
    if not os.path.exists(fp):
        return False
    try:
        with open(fp, encoding="utf-8") as fh:
            return sum(1 for _ in fh) - 1 >= 900
    except OSError:
        return False


def fetch_one(ak, sym, stats):
    try:
        hfq, raw = _fetch_pair(ak, sym, START, END)
        convert_stock(hfq, raw).to_csv(os.path.join(OUT, sym + ".csv"), index=False)
        stats["ok"] += 1
    except Exception as e:  # noqa: BLE001
        stats["fail"] += 1
        if stats["fail"] <= 10:
            print(f"  FAIL {sym}: {type(e).__name__} {str(e)[:80]}", flush=True)
    finally:
        time.sleep(0.15)


def worker(ak, chunk, stats, wi, n_chunks):
    for i, sym in enumerate(chunk):
        fetch_one(ak, sym, stats)
        if (i + 1) % 50 == 0:
            print(f"w{wi} {i+1}/{len(chunk)} ok={stats['ok']} fail={stats['fail']}", flush=True)


def main():
    import akshare as ak
    os.environ.setdefault("NO_PROXY", "*")
    os.makedirs(OUT, exist_ok=True)
    spot = ak.stock_zh_a_spot()
    spot = spot[~spot["名称"].str.contains("ST|退", na=False)]
    syms = sorted(spot["代码"].astype(str).str.lower().tolist())
    with open(os.path.join(OUT, "stock_list.json"), "w", encoding="utf-8") as f:
        json.dump({"n": len(syms), "syms": syms}, f)
    todo = [s for s in syms if not done(s)]
    print(f"全市场（剔 ST/退）: {len(syms)} 只，跳过 {len(syms)-len(todo)}，待拉 {len(todo)}")
    stats = {"ok": 0, "fail": 0}
    t0 = time.time()
    chunks = [todo[i::WORKERS] for i in range(WORKERS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(worker, ak, c, stats, wi, WORKERS) for wi, c in enumerate(chunks) if c]
        for f in as_completed(futs):
            f.result()
    print(f"DONE ok={stats['ok']} fail={stats['fail']} 耗时 {time.time()-t0:.0f}s")
    with open(os.path.join(OUT, ".fetch_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "sina", "start": START, "end": END, "fields_version": "v2",
                   "stocks": stats["ok"], "fails": stats["fail"],
                   "note": "全市场（剔 ST/退）"}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
