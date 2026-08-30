#!/usr/bin/env python3
"""Kronos zero-shot executor (foundation model for financial K-lines).

Contract (executors/README.md):
  main.py --config <json> --train <pq> --test <pq> --out <dir>
  outputs <out>/pred.pkl -- DataFrame, MultiIndex (datetime, instrument),
  column "score"; and <out>/portfolio.pkl -- MultiIndex (datetime,
  instrument), column "weight" (qlib TopkDropoutStrategy semantics, topk /
  n_drop from spec.params).

Model: Kronos (shiyu-coder/Kronos, MIT; vendored in ./model, upstream commit
67b630e). Zero-shot: no training on the train parquet. For each test date d,
each instrument's last `lookback` daily K-lines (open/high/low/close/volume/
amount, hfq prices) are fed to the tokenizer + model, which predicts the
next-day K-line; score = pred_close(d) / close(d-1) - 1 (predicted 1-day
return), cross-sectionally rankable against the pipeline label
Ref($close,-2)/Ref($close,-1)-1.

Input data: data/extra/kronos_ohlcv.parquet (extra-feature bypass, contract
item 8; declared in run_info.json). It is built from the same hfq price CSVs
(qlib_data_src) the pipeline price cache uses -- raw OHLCV is the foundation
model's input, not an engineered feature.

All sampling/params come from spec.params (transparent passthrough).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]  # executors/kronos -> repo root
PRICE_COLS = ["open", "high", "low", "close", "volume", "amount"]


def load_extra(path):
    df = pd.read_parquet(path)
    if not all(c in df.columns for c in PRICE_COLS):
        raise ValueError("extra parquet missing columns %s" % PRICE_COLS)
    return df[PRICE_COLS]


def resolve_extra(repo_root):
    """Extra OHLCV parquet: repo data/extra/ (local), else the shared cache
    dir (remote dispatch symlinks <repo>/cache -> <workdir>/cache)."""
    cands = [repo_root / "data" / "extra" / "kronos_ohlcv.parquet",
             repo_root / "cache" / "kronos_ohlcv.parquet"]
    for p in cands:
        if p.exists():
            return p
    raise FileNotFoundError(
        "kronos_ohlcv.parquet not found (tried %s); build it with "
        "scratch_probe/kronos_build_extra.py and place it in data/extra/ "
        "or the shared cache dir" % [str(c) for c in cands])


def build_portfolio(score, topk=50, n_drop=5):
    """Convert cross-sectional scores into daily target weights.

    Replicates qlib's official TopkDropoutStrategy semantics (identical to
    executors/qlib_bench._portfolio): each day hold the top-`topk` names by
    score at equal weight 1/topk, sell at most `n_drop` stale holdings (lowest
    score first), refill the rest from the new top-k. Returns a Series with
    MultiIndex (datetime, instrument) named "weight"; weights >= 0, daily sum
    <= 1 (cash = 1 - sum), unheld rows omitted -- matches check_portfolio.
    """
    score = score[~score.index.duplicated(keep="last")]
    w = score.unstack("instrument")
    holdings = set()
    rows = []
    for dt in w.index:
        s = w.loc[dt].dropna().sort_values(ascending=False)
        top = set(s.index[:topk])
        drop_cand = sorted([i for i in holdings if i not in top],
                           key=lambda i: s.get(i, -np.inf))
        sell = set(drop_cand[:n_drop])
        hold_now = (holdings - sell) & set(s.index)
        need = max(0, topk - len(hold_now))
        buys = [i for i in s.index if i not in hold_now][:need]
        holdings = set(hold_now) | set(buys)
        row = pd.Series(0.0, index=s.index)
        row[list(holdings)] = 1.0 / topk
        rows.append(row)
    pf = pd.concat(rows, axis=1).T
    pf.index = w.index
    pf = pf.stack().rename("weight")
    pf = pf[pf > 0]
    pf.index = pf.index.set_names(["datetime", "instrument"])
    # pandas 3.x (executor venv) serializes a stack()-derived MultiIndex with a
    # datetime64 level in a format pandas 2.x (the harness interpreter) cannot
    # unpickle (NDArrayBacked.__setstate__ NotImplementedError). Rebuild the
    # index via set_index -- the same construction pred.pkl uses, which round-
    # trips across the version gap.
    pf = pf.reset_index().set_index(["datetime", "instrument"])["weight"]
    return pf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    params = dict(cfg.get("params") or {})

    model_id = str(params.get("kronos_model", "NeoQuasar/Kronos-base"))
    tok_id = str(params.get("kronos_tokenizer", "NeoQuasar/Kronos-Tokenizer-base"))
    lookback = int(params.get("lookback", 512))
    max_context = int(params.get("max_context", 512))
    min_history = int(params.get("min_history", 60))
    temperature = float(params.get("temperature", 1.0))
    top_p = float(params.get("top_p", 0.9))
    sample_count = int(params.get("sample_count", 1))
    seed = int(params.get("seed", 42))
    max_dates = int(params.get("max_dates", 0))  # 0 = all test dates
    torch_threads = int(params.get("torch_threads", 4))

    # weights are pre-cached at deploy time (huggingface.co AND hf-mirror.com
    # are both unreachable from the DGX Spark container; online mode would try
    # an etag HEAD and die on SSL). Offline mode resolves purely from the cache.
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ["HF_HUB_OFFLINE"] = "1"
    # Container pids.max=256: cap BLAS/OpenMP pools hard BEFORE numpy/torch
    # import. The harness sets OMP/OPENBLAS/MKL to QLAB_EXECUTOR_THREADS (8);
    # under pids pressure a failed libgomp/OpenBLAS thread spawn deadlocks the
    # whole batch on a barrier (observed 2026-08-24, kronos_smoke job82).
    blas_threads = str(max(1, min(torch_threads, 2)))
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[k] = blas_threads

    import torch
    torch.set_num_threads(torch_threads)
    torch.manual_seed(seed)

    sys.path.insert(0, str(Path(__file__).parent))
    from model import Kronos, KronosTokenizer, KronosPredictor

    print("loading tokenizer %s" % tok_id, flush=True)
    t0 = time.time()
    tokenizer = KronosTokenizer.from_pretrained(tok_id)
    print("loading model %s" % model_id, flush=True)
    model = Kronos.from_pretrained(model_id)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    predictor = KronosPredictor(model, tokenizer, device=device,
                                max_context=max_context)
    print("models loaded in %.1fs, device=%s" % (time.time() - t0, device),
          flush=True)

    extra_path = resolve_extra(REPO_ROOT)
    extra = load_extra(extra_path)
    test_df = pd.read_parquet(args.test, columns=["y"])
    test_dates = sorted(test_df.index.get_level_values(0).unique())
    test_insts = sorted(test_df.index.get_level_values(1).unique())
    if max_dates:
        test_dates = test_dates[:max_dates]

    extra_insts = set(extra.index.get_level_values(1).unique())
    insts = [i for i in test_insts if i in extra_insts]
    print("test window %s..%s, %d dates, %d/%d instruments in extra data"
          % (test_dates[0].date(), test_dates[-1].date(), len(test_dates),
             len(insts), len(test_insts)), flush=True)

    by_inst = {}
    for i in insts:
        by_inst[i] = extra.xs(i, level="instrument").sort_index()

    rows = []
    n_skipped_dates = 0
    t1 = time.time()
    for di, d in enumerate(test_dates):
        df_list, x_ts_list, y_ts_list, used, last_close = [], [], [], [], []
        for i in insts:
            dfi = by_inst[i]
            pos = int((dfi.index < d).sum())
            if pos < lookback:
                # full-window requirement: predict_batch needs identical
                # series lengths; skip instruments with short history
                continue
            w = dfi.iloc[pos - lookback:pos]
            if w[PRICE_COLS].isnull().values.any():
                continue
            df_list.append(w[PRICE_COLS])
            x_ts_list.append(pd.Series(w.index))
            y_ts_list.append(pd.Series([d]))
            used.append(i)
            last_close.append(float(w["close"].iloc[-1]))
        if not df_list:
            n_skipped_dates += 1
            print("day %s: no instruments with history, skipped" % d.date(),
                  flush=True)
            continue
        preds = predictor.predict_batch(
            df_list=df_list, x_timestamp_list=x_ts_list,
            y_timestamp_list=y_ts_list, pred_len=1,
            T=temperature, top_p=top_p, sample_count=sample_count,
            verbose=False)
        for pred_df, i, lc in zip(preds, used, last_close):
            pc = float(pred_df["close"].iloc[-1])
            if not (np.isfinite(pc) and np.isfinite(lc) and lc > 0):
                continue
            rows.append((d, i, pc / lc - 1.0))
        print("day %s: %d instruments, elapsed %.1fs"
              % (d.date(), len(preds), time.time() - t1), flush=True)

    if not rows:
        sys.exit("no predictions produced -- check extra data / history depth")

    pred = pd.DataFrame(rows, columns=["datetime", "instrument", "score"])
    pred = pred.set_index(["datetime", "instrument"])
    pred.index.names = ["datetime", "instrument"]
    pred["score"] = pred["score"].astype(np.float64)
    pred.to_pickle(out / "pred.pkl")

    # ---- signal -> trading positions (TopkDropout semantics) ----
    topk = int(params.get("topk", 50))
    n_drop = int(params.get("n_drop", 5))
    portfolio = build_portfolio(pred["score"], topk=topk, n_drop=n_drop)
    portfolio.to_frame().to_pickle(out / "portfolio.pkl")

    info = {
        "executor": "kronos",
        "extra_features": ["kronos_ohlcv"],
        "model": model_id,
        "tokenizer": tok_id,
        "lookback": lookback,
        "max_context": max_context,
        "min_history": min_history,
        "temperature": temperature,
        "top_p": top_p,
        "sample_count": sample_count,
        "seed": seed,
        "device": device,
        "extra_source": str(extra_path.relative_to(REPO_ROOT)),
        "torch_threads": torch_threads,
        "n_dates_predicted": int(pred.index.get_level_values(0).nunique()),
        "n_instruments": int(pred.index.get_level_values(1).nunique()),
        "n_rows": int(len(pred)),
        "n_skipped_dates": n_skipped_dates,
        "score_formula": "pred_close(t+1)/close(t) - 1",
        "upstream": "shiyu-coder/Kronos (gitee mirror), MIT, vendored @ 67b630e",
        "topk": topk,
        "n_drop": n_drop,
        "portfolio": {
            "strategy": "qlib TopkDropoutStrategy (daily top-k equal weight 1/topk, max n_drop stale sells)",
            "topk": topk,
            "n_drop": n_drop,
            "n_weight_rows": int(len(portfolio)),
            "n_dates_held": int(portfolio.index.get_level_values(0).nunique()),
        },
    }
    (out / "run_info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    print("pred.pkl written: %d rows over %d dates; portfolio %d rows over %d dates"
          % (len(pred), info["n_dates_predicted"], len(portfolio),
             info["portfolio"]["n_dates_held"]), flush=True)


if __name__ == "__main__":
    main()

