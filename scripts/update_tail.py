"""增量更新：拉指定区间合并进现有 CSV（新日期覆盖旧）。

- 股票：hfq+raw 双拉 → convert_stock → 合并去重（keep=last）
- 指数：stock_zh_index_daily 区间合并
- 已退市股（新浪返回空）跳过保留原数据
- 每个池新增 ≥1 个日期且未传 --keep-cache 时，自动 invalidate 该池特征缓存并
  推进数据修订号（pipeline.data.invalidate —— "数据变了" 的唯一信号源）
用法: python scripts/update_tail.py [--pools hs300,zz500,all] [--start 2026-07-24]
      [--end 2026-08-23] [--keep-cache]
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# 仓库根：env QLAB_ROOT 优先，否则脚本所在目录上一级（容器内即 /root/quant）
REPO = os.environ.get("QLAB_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from qlib_scripts.fetch_sina import _fetch_pair, convert_stock  # noqa: E402

POOL_DIRS = {"hs300": os.path.join(REPO, "qlib_data_src"),
             "zz500": os.path.join(REPO, "qlib_data_src_zz500"),
             "all": os.path.join(REPO, "qlib_data_src_all")}
POOL_INDEX = {"hs300": "sh000300", "zz500": "sh000905"}


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


def update_one(ak, out, sym, start, end, stats):
    fp = os.path.join(out, f"{sym}.csv")
    try:
        hfq, raw = fetch_tail(ak, sym, start, end)
        add = convert_stock(hfq, raw)
        add["date"] = add["date"].astype(str)
        old = pd.read_csv(fp, dtype={"date": str})
        old_dates = set(old["date"])
        stats["new_dates"] += len(set(add["date"]) - old_dates)
        new = pd.concat([old, add], ignore_index=True).drop_duplicates(subset=["date"], keep="last")
        new = new.sort_values("date").reset_index(drop=True)
        new.to_csv(fp, index=False)
        stats["ok"] += 1
    except Exception as e:  # noqa: BLE001
        stats["fail"] += 1
        if stats["fail"] <= 5:
            print(f"  FAIL {sym}: {type(e).__name__} {str(e)[:80]}", flush=True)


def main():
    today = time.strftime("%Y-%m-%d")
    default_start = (pd.Timestamp(today) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pools", default="hs300,zz500,all")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--start", default=default_start)
    ap.add_argument("--end", default=today)
    ap.add_argument("--keep-cache", action="store_true",
                    help="新增日期后不 invalidate 特征缓存（默认会 invalidate 对应池）")
    args = ap.parse_args()
    import akshare as ak
    os.environ.setdefault("NO_PROXY", "*")
    for pool in args.pools.split(","):
        out = POOL_DIRS[pool]
        syms = sorted(f[:-4] for f in os.listdir(out) if f.endswith(".csv") and not f.startswith("_"))
        syms = [s for s in syms if s not in POOL_INDEX.values()]
        stats = {"ok": 0, "fail": 0, "new_dates": 0}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(update_one, ak, out, s, args.start, args.end, stats) for s in syms]
            done = 0
            for f in as_completed(futs):
                f.result()
                done += 1
                if done % 200 == 0:
                    print(f"{pool} {done}/{len(syms)} ok={stats['ok']} fail={stats['fail']} ({time.time()-t0:.0f}s)", flush=True)
        # 指数
        idx_new = 0
        idx = POOL_INDEX.get(pool)
        if idx:
            try:
                df = ak.stock_zh_index_daily(symbol=idx)
                df = df[(df["date"] >= args.start) & (df["date"] <= args.end)]
                df["date"] = df["date"].astype(str)
                old = pd.read_csv(os.path.join(out, f"{idx}.csv"), dtype={"date": str})
                idx_new = len(set(df["date"]) - set(old["date"]))
                new = pd.concat([old, df], ignore_index=True).drop_duplicates(subset=["date"], keep="last")
                new.to_csv(os.path.join(out, f"{idx}.csv"), index=False)
                print(f"{idx} 指数更新 OK（新增 {idx_new} 天）", flush=True)
            except Exception as e:
                print(f"{idx} 指数更新失败: {e}", flush=True)
        added = stats["new_dates"] + idx_new
        if added > 0 and not args.keep_cache:
            from pipeline import data  # noqa: E402
            res = data.invalidate(pool)
            print(f"{pool} 新增 {added} 个日期：已 invalidate 缓存，数据修订号 -> {res['revision']}",
                  flush=True)
        elif added > 0:
            print(f"{pool} 新增 {added} 个日期：--keep-cache 未 invalidate 缓存（手动执行 "
                  f"python -m pipeline.data invalidate --pool {pool}）", flush=True)
        print(f"{pool} DONE ok={stats['ok']} fail={stats['fail']} 新增日期={added} 耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
