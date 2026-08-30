#!/usr/bin/env python3
"""backfill_delisted.py — all-pool delisted-stock backfill.

Plan: knowledge/notes/backfill_delisted_plan.md (executed 2026-08-29).
Source deviation (user-approved 2026-08-29): eastmoney stock_zh_a_hist was
WAF-blocked (push2his TLS reset from host and container); switched to baostock,
Step 0-calibrated against sina on sh600000 (65 overlap days):
  - volume: baostock == sina EXACT (both in shares)
  - amount: EXACT (CNY)
  - raw close: EXACT
  - turn: baostock percent == sina turnover*100 within 4-dp rounding (max diff 5e-5)
  - hfq close / factor: differ by a per-stock constant (hfq base date differs) —
    the same caveat class plan section 6 anticipated for eastmoney.

CSV format (identical to qlib_scripts/fetch_sina.py convert_stock), 10 cols:
    date,open,high,low,close,volume,amount,vwap,turn,factor
  - close = hfq close (baostock adjustflag=1, 后复权)
  - factor = close_hfq / close_raw (real adjustment factor, baostock base)
  - vwap = (amount/volume)*factor; volume==0 -> close
  - turn = percent (2.5 means 2.5%)
  - volume in shares

Only touches qlib_data_src_all. Never modifies existing CSVs. Stocks with no
data are recorded as no_data (never fails the run). B-shares are recorded as
no_data because the all pool has no B-share universe.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
START = "2021-06-01"


def _to_sym(c):
    c = str(c).zfill(6)
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


def _bs_code(sym):
    return sym[:2] + "." + sym[2:]


def fetch_delist_lists():
    """Step A: SH/SZ termination lists via akshare. Returns DataFrame
    [code, name, delist, sym]. SH column '暂停上市日期' is akshare's mislabel
    of DELIST_DATE (verified 2026-08-29)."""
    import akshare as ak
    frames = []
    sh = None
    for attempt in range(4):
        try:
            sh = ak.stock_info_sh_delist()
            break
        except Exception as e:  # noqa: BLE001
            print("SH delist list attempt %d failed: %r" % (attempt + 1, e), flush=True)
            time.sleep(4 + attempt)
    if sh is None:
        raise RuntimeError("SH delist list unavailable after retries")
    sh = sh.copy()
    sh["code"] = sh["公司代码"].astype(str).str.zfill(6)
    sh["delist"] = pd.to_datetime(sh["暂停上市日期"], errors="coerce").dt.date
    frames.append(sh.rename(columns={"公司简称": "name"})[["code", "name", "delist"]])

    sz = None
    for attempt in range(4):
        try:
            sz = ak.stock_info_sz_delist()
            break
        except Exception as e:  # noqa: BLE001
            print("SZ delist list attempt %d failed: %r" % (attempt + 1, e), flush=True)
            time.sleep(4 + attempt)
    if sz is None:
        raise RuntimeError("SZ delist list unavailable after retries")
    sz = sz.copy()
    sz["code"] = sz["证券代码"].astype(str).str.zfill(6)
    sz["delist"] = pd.to_datetime(sz["终止上市日期"], errors="coerce").dt.date
    frames.append(sz.rename(columns={"证券简称": "name"})[["code", "name", "delist"]])

    df = pd.concat(frames, ignore_index=True)
    cutoff = pd.Timestamp(START).date()
    df = df[df["delist"] >= cutoff]
    # A/B double listings under one code: keep first (delist dates identical)
    df = df.sort_values(["code", "delist"]).drop_duplicates(subset="code", keep="first")
    df["sym"] = df["code"].map(_to_sym)
    df = df.sort_values(["delist", "sym"]).reset_index(drop=True)
    return df


def csv_up_to_date(fp, delist_date):
    """Pool CSV already covers through the delist date -> skip."""
    if not fp.exists():
        return False
    try:
        last = str(pd.read_csv(fp, usecols=["date"])["date"].iloc[-1])
        return last >= str(delist_date)
    except Exception:  # noqa: BLE001
        return False


def convert(hfq, raw):
    """baostock hfq/raw -> project 10-column CSV frame."""
    COLS = ["date", "open", "high", "low", "close", "volume", "amount",
            "vwap", "turn", "factor"]

    def _empty():
        return pd.DataFrame(columns=COLS)

    h = hfq.rename(columns={"date": "date"})
    m = h.merge(raw[["date", "close"]].rename(columns={"close": "close_raw"}),
                on="date", how="inner")
    if m.empty:
        return _empty()
    for c in ["open", "high", "low", "close", "volume", "amount", "turn"]:
        m[c] = pd.to_numeric(m[c], errors="coerce")
    m["close_raw"] = pd.to_numeric(m["close_raw"], errors="coerce")
    m = m[m["close"].notna() & m["close_raw"].notna()]
    # drop no-trade (suspended) rows: volume missing -> same trading-day
    # convention as sina stock_zh_a_daily (which omits them)
    m = m[m["volume"].notna() & (m["volume"] != 0)]
    if m.empty:
        return _empty()
    m["factor"] = m["close"] / m["close_raw"].replace(0, pd.NA)
    m["vwap"] = (m["amount"] / m["volume"].replace(0, pd.NA)) * m["factor"]
    m["vwap"] = m["vwap"].fillna(m["close"])
    return m[COLS].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试）")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--out", default=str(REPO / "qlib_data_src_all"))
    ap.add_argument("--manifest", default=str(REPO / "data" / "extra" / "delisted_manifest.json"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    import baostock as bs

    cand = fetch_delist_lists()
    print("Step A: 候选 %d 只（终止上市 >= %s）" % (len(cand), START), flush=True)
    if args.limit > 0:
        cand = cand.head(args.limit)
        print("--limit: 只处理 %d 只" % len(cand), flush=True)

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError("baostock login failed: %s %s" % (lg.error_code, lg.error_msg))
    print("baostock login ok", flush=True)

    B_SHARE_PREFIXES = ("sh9", "sz2")  # 900xxx / 200xxx
    results = []
    ok = skipped = nodata = 0
    t0 = time.time()

    def query(code, adjustflag):
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount,turn",
            start_date=START, end_date="2030-12-31", frequency="d", adjustflag=adjustflag)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                           "volume", "amount", "turn"])

    for i, row in cand.iterrows():
        sym = row["sym"]
        name = str(row["name"])
        delist = str(row["delist"])
        fp = out / (sym + ".csv")
        entry = {"sym": sym, "name": name, "delist_date": delist, "source": "baostock"}
        try:
            if csv_up_to_date(fp, delist):
                entry.update(status="skipped")
                skipped += 1
                print("SKIP %s (csv 已覆盖 %s)" % (sym, delist), flush=True)
            elif sym.startswith(B_SHARE_PREFIXES):
                entry.update(status="no_data", reason="B股不在 all 池 universe（池内无 B 股代码），且 baostock 无覆盖")
                nodata += 1
                print("NO_DATA %s %s (B股)" % (sym, name), flush=True)
            else:
                code = _bs_code(sym)
                hfq = query(code, "1")
                raw = query(code, "3")
                if hfq.empty or raw.empty:
                    entry.update(status="no_data", reason="baostock 无历史（吸收合并/转板/无覆盖）")
                    nodata += 1
                    print("NO_DATA %s %s (empty)" % (sym, name), flush=True)
                else:
                    df = convert(hfq, raw)
                    df = df[(df["date"] >= START) & (df["date"] <= delist)]
                    if df.empty:
                        entry.update(status="no_data", reason="窗口内无成交（长期停牌至退市）")
                        nodata += 1
                        print("NO_DATA %s %s (empty window)" % (sym, name), flush=True)
                    else:
                        df.to_csv(fp, index=False)
                        entry.update(status="added", rows=len(df), start=str(df["date"].iloc[0]),
                                     end=str(df["date"].iloc[-1]))
                        ok += 1
            if ok % 10 == 0 or i == len(cand) - 1 or entry.get("status") == "no_data":
                print("  %d/%d added=%d no_data=%d skip=%d (%.0fs) last=%s %s"
                      % (i + 1, len(cand), ok, nodata, skipped, time.time() - t0,
                         sym, entry.get("status")), flush=True)
        except Exception as e:  # noqa: BLE001
            entry.update(status="no_data", reason="%s: %s" % (type(e).__name__, e))
            nodata += 1
            print("NO_DATA %s %s (%s)" % (sym, name, entry["reason"]), flush=True)
        results.append(entry)
        time.sleep(args.sleep)
    bs.logout()

    # Step C: manifest (atomic)
    man = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pool": "all",
        "data_dir": "qlib_data_src_all",
        "cutoff": START,
        "source": "baostock",
        "source_deviation": "eastmoney stock_zh_a_hist WAF-blocked (push2his TLS reset, host+container, 2026-08-29); user approved baostock. Step 0 calibration vs sina on sh600000: volume/amount/raw exact, turn within 4-dp rounding (percent), hfq/factor differ by per-stock constant (hfq base).",
        "caveats": [
            "hfq 后复权基准与 sina 不同（差每只股票常数倍）；退市股与现存股无同日重叠，对收益率/截面特征影响有限",
            "turn 为 baostock 百分数口径，与 sina turnover*100 在 4 位小数内一致",
            "B 股（200xxx/900xxx）不在 all 池 universe，记 no_data 不入池",
            "北交所退市不在本计划范围（akshare 1.18.91 无 bj 退市接口）",
            "暂停上市期间无成交行已按 sina 口径剔除（仅保留有成交交易日）",
        ],
        "stocks": sorted(results, key=lambda x: (x["delist_date"], x["sym"])),
    }
    mp = Path(args.manifest)
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(man, ensure_ascii=False, indent=1))
    os.replace(tmp, mp)
    print("manifest written: %s" % mp, flush=True)
    print("SUMMARY added=%d skipped=%d no_data=%d total=%d (%.0fs)"
          % (ok, skipped, nodata, len(results), time.time() - t0), flush=True)
    from collections import Counter
    print("no_data reasons:", dict(Counter(e.get("reason", "?") for e in results if e.get("status") == "no_data")), flush=True)


if __name__ == "__main__":
    main()