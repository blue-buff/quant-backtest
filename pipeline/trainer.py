"""Real training for the harness train action (P0-4).

Memory-friendly port of the proven scripts/train_allmarket.py path:
Alpha158 handler -> float32 features -> LightGBM (one model per seed) -> chunked
out-of-sample prediction -> rank_mean ensemble -> full metrics (pipeline.metrics).

Feature cache: cache/<key>.parquet keyed by the sha256 of (pool, instruments,
fit/window config, label, learn processors) so repeated runs skip data building.
Artifacts land in results/runs/<exp_id>/ (pred_matrix.pkl, label_matrix.pkl,
meta.json, metrics.json, work.json).
"""
import hashlib, json, time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import QLAB_ROOT, DATA_VERSION
from . import metrics as metricsmod

RUNS_DIR = QLAB_ROOT / "results" / "runs"
CACHE_DIR = QLAB_ROOT / "cache"
EVAL_DIR = QLAB_ROOT / "results" / "eval"

POOL_MAP = {
    "all": ("/root/.qlib/qlib_data/cn_data_all", "all"),
    "hs300": ("/root/.qlib/qlib_data/cn_data", "csi300"),
    "zz500": ("/root/.qlib/qlib_data/cn_data_zz500", "csi500"),
}

DEFAULTS = {
    "fit_start_time": "2021-06-01",
    "fit_end_time": "2024-06-30",
    "train": ["2021-06-01", "2024-06-30"],
    "valid": ["2024-07-01", "2024-12-31"],
    "test_start": "2025-01-01",
    "test_end": "2026-08-20",
    "rounds": 1000,
    "early_stopping": 50,
    "num_threads": 20,
}

LEARN_PROCESSORS = [
    {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
    {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
     "kwargs": {"fields_group": "label"}},
]


def _cfg_key(eff, pool):
    """Deterministic cache key for a feature matrix window."""
    canon = json.dumps({
        "pool": pool,
        "instruments": POOL_MAP[pool][1],
        "fit": [eff.get("dataset", {}).get("handler", {}).get("fit_start_time", DEFAULTS["fit_start_time"]),
                eff.get("dataset", {}).get("handler", {}).get("fit_end_time", DEFAULTS["fit_end_time"])],
        "window_end": DEFAULTS["valid"][1],
        "label": eff.get("label", {}).get("formula"),
        "learn_processors": LEARN_PROCESSORS,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _resolve(eff, spec):
    """Effective training config from resolved spec (ref+overrides) + action defaults."""
    h = eff.get("dataset", {}).get("handler", {})
    lab = eff.get("label", {})
    act = spec.get("action", {})
    cfg = {
        "pool": eff.get("universe", "all"),
        "instruments": h.get("instruments"),
        "fit_start_time": h.get("fit_start_time", DEFAULTS["fit_start_time"]),
        "fit_end_time": h.get("fit_end_time", DEFAULTS["fit_end_time"]),
        "train": DEFAULTS["train"], "valid": DEFAULTS["valid"],
        "test_start": act.get("test_start", DEFAULTS["test_start"]),
        "test_end": act.get("test_end", DEFAULTS["test_end"]),
        "label_formula": lab.get("formula"),
        "horizon": int(lab.get("horizon", 10)),
        "model": dict(eff.get("model", {})),
        "seeds": list(eff.get("seeds", [42])),
        "ensemble": eff.get("ensemble", "rank_mean(seeds)"),
        "rounds": int(act.get("rounds", DEFAULTS["rounds"])),
        "early_stopping": int(act.get("early_stopping", DEFAULTS["early_stopping"])),
        "num_threads": int(act.get("num_threads", DEFAULTS["num_threads"])),
        "save_models": bool(act.get("save_models", False)),
    }
    provider, instruments_default = POOL_MAP[cfg["pool"]]
    cfg["provider_uri"] = provider
    if not cfg["instruments"]:
        cfg["instruments"] = instruments_default
    if not cfg["label_formula"]:
        hz = cfg["horizon"]
        cfg["label_formula"] = "Ref(" + "$close" + ",-%d)/Ref(" + "$close" + ",-1)-1" % (hz + 1)
    return cfg


def _fetch_matrix(cfg, start_time, end_time, cache_name):
    """Fetch (feature|label) -> DataFrame with y column, via parquet cache.

    Memory strategy (container cgroup = 12GB): the all-market float64 feature
    matrix (~4GB) is copied several times inside qlib's processor pipeline, which
    OOMs when fetched in one piece. So the window is fetched in calendar slices
    (one handler per slice; qlib caches the raw loaded data in-process, so only
    the first handler pays the loading cost), each slice converted to float32
    and appended to the parquet file immediately."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / (cache_name + ".parquet")
    if cache_path.exists():
        return pd.read_parquet(cache_path)
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP
    import pyarrow as pa
    import pyarrow.parquet as pq
    days = pd.bdate_range(start_time, end_time)
    SLICE_DAYS = 180
    writer = None
    for i in range(0, len(days), SLICE_DAYS):
        s0 = days[i]
        s1 = days[min(i + SLICE_DAYS, len(days)) - 1]
        h = Alpha158(instruments=cfg["instruments"], start_time=s0,
                     end_time=s1, fit_start_time=cfg["fit_start_time"],
                     fit_end_time=cfg["fit_end_time"], learn_processors=LEARN_PROCESSORS,
                     label=[cfg["label_formula"]])
        data = h.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
        feat64 = data["feature"]
        lab = data["label"].iloc[:, 0]
        del data, h
        CHUNK = 400000
        for s in range(0, len(feat64), CHUNK):
            sub = feat64.iloc[s:s + CHUNK].astype(np.float32)
            chunk = sub.join(lab.iloc[s:s + CHUNK].rename("y"))
            table = pa.Table.from_pandas(chunk)
            if writer is None:
                writer = pq.ParquetWriter(cache_path, table.schema)
            writer.write_table(table)
            del sub, chunk, table
        del feat64, lab
        print("cache slice %s..%s done" % (s0.date(), s1.date()), flush=True)
    writer.close()
    return pd.read_parquet(cache_path)


def _split(df, cfg):
    lv = df.index.get_level_values(0)
    tr = df[(lv >= cfg["train"][0]) & (lv < cfg["train"][1])]
    va = df[(lv >= cfg["valid"][0]) & (lv < cfg["valid"][1])]
    feats = [c for c in df.columns if c != "y"]
    return tr[feats], tr["y"], va[feats], va["y"], feats


def _train_seed(X_tr, y_tr, X_va, y_va, cfg, seed):
    import lightgbm as lgb
    params = {"objective": "regression", "metric": "l2",
              "num_threads": cfg["num_threads"], "verbosity": -1, "seed": int(seed)}
    for k in ("loss", "num_leaves", "learning_rate", "max_depth", "colsample_bytree",
              "subsample", "lambda_l1", "lambda_l2"):
        if k in cfg["model"]:
            params[k] = cfg["model"][k]
    d_tr = lgb.Dataset(X_tr, label=y_tr)
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    model = lgb.train(params, d_tr, num_boost_round=cfg["rounds"], valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(cfg["early_stopping"]), lgb.log_evaluation(0)])
    best = model.best_score.get("valid_0", {}).get("l2")
    return model, best


def _predict(model, X_te, cfg):
    scores = []
    for s in range(0, len(X_te), 200000):
        scores.append(model.predict(X_te.iloc[s:s + 200000], num_iteration=model.best_iteration))
    return np.concatenate(scores)


def run_train(spec, eff):
    """Run the train action. Returns (run_dir, full_metrics, meta)."""
    import qlib

    cfg = _resolve(eff, spec)
    exp_id = spec.get("exp_id")
    run_dir = RUNS_DIR / exp_id
    if run_dir.exists():
        for p in sorted(run_dir.glob("*")):
            if p.is_file():
                p.unlink()
    run_dir.mkdir(parents=True, exist_ok=True)
    qlib.init(provider_uri=cfg["provider_uri"], region="cn")

    t0 = time.time()
    key = _cfg_key(eff, cfg["pool"])
    df = _fetch_matrix(cfg, cfg["fit_start_time"], DEFAULTS["valid"][1], "train_" + key)
    X_tr, y_tr, X_va, y_va, feats = _split(df, cfg)
    del df
    print("data ready %.0fs | train %s valid %s | %d features" %
          (time.time() - t0, X_tr.shape, X_va.shape, len(feats)), flush=True)

    # ---- one model per seed (kept in memory for prediction) ----
    seed_best = []
    models = []
    for seed in cfg["seeds"]:
        t1 = time.time()
        model, valid_l2 = _train_seed(X_tr, y_tr, X_va, y_va, cfg, seed)
        seed_best.append({"seed": int(seed), "best_iter": int(model.best_iteration),
                          "valid_l2": float(valid_l2) if valid_l2 is not None else None,
                          "seconds": round(time.time() - t1, 1)})
        print("seed %s done %.0fs best_iter=%s valid_l2=%s" %
              (seed, time.time() - t1, model.best_iteration, valid_l2), flush=True)
        if cfg["save_models"]:
            model.save_model(str(run_dir / ("model_seed%s.txt" % seed)))
        models.append(model)
    del df, X_tr, y_tr, X_va, y_va

    # ---- out-of-sample prediction per seed + ensemble ----
    te_key = _cfg_key(eff, cfg["pool"]) + "_te_%s_%s" % (cfg["test_start"], cfg["test_end"])
    df_te = _fetch_matrix(cfg, cfg["test_start"], cfg["test_end"], "test_" + te_key)
    X_te = df_te[feats]
    label_te = df_te["y"]
    del df_te
    print("test features %s" % (X_te.shape,), flush=True)

    preds = []
    for i, seed in enumerate(cfg["seeds"]):
        model = models[i]
        score = _predict(model, X_te, cfg)
        preds.append(pd.Series(score, index=X_te.index, name="score"))
        del model

    if len(preds) == 1:
        ens = preds[0]
    else:
        # rank_mean(seeds): per-day cross-sectional rank average
        ranks = []
        for s in preds:
            dfr = s.to_frame("score")
            dfr["dt"] = dfr.index.get_level_values(0)
            dfr["r"] = dfr.groupby("dt")["score"].rank(pct=True)
            ranks.append(dfr["r"])
        ens_rank = pd.concat(ranks, axis=1).mean(axis=1)
        ens = pd.Series(ens_rank.to_numpy(), index=X_te.index, name="score")

    # ---- artifacts + metrics ----
    pred_path = run_dir / "pred_matrix.pkl"
    label_path = run_dir / "label_matrix.pkl"
    pred = ens.to_frame("score")
    pred.to_pickle(pred_path)
    label_te.to_pickle(label_path)
    (run_dir / "work.json").write_text(
        json.dumps({"exp_id": exp_id,
                    "cfg": {k: v for k, v in cfg.items() if k != "model"},
                    "model": cfg["model"], "seeds": cfg["seeds"],
                    "ensemble": cfg["ensemble"], "data_version": DATA_VERSION},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    full = metricsmod.compute_full(pred_path, label_path, h=cfg["horizon"])
    (run_dir / "metrics.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / (exp_id + ".json")).write_text(
        json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    meta = {"exp_id": exp_id, "pool": cfg["pool"], "instruments": cfg["instruments"],
            "data_version": DATA_VERSION, "label": cfg["label_formula"],
            "horizon": cfg["horizon"], "seeds": cfg["seeds"], "ensemble": cfg["ensemble"],
            "model": cfg["model"], "fit": [cfg["fit_start_time"], cfg["fit_end_time"]],
            "train": cfg["train"], "valid": cfg["valid"],
            "test": [cfg["test_start"], cfg["test_end"]],
            "feature_cache_keys": [key, te_key],
            "seed_runs": seed_best, "total_seconds": round(time.time() - t0, 1),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "note": ("train action P0-4: memory-friendly port of scripts/train_allmarket.py "
                     "(learn-processor feature path, no infer processors; legacy parity)")}
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(json.dumps({"exp_id": exp_id, "total_seconds": meta["total_seconds"],
                      "rankic_mean": full["nonoverlap_mean_rank_ic"],
                      "p_le0": full["bootstrap_rankic"]["p_le0"],
                      "n_days": full["n_days"]}, ensure_ascii=False), flush=True)
    return run_dir, full, meta


if __name__ == "__main__":
    import argparse
    from . import spec as specmod
    ap = argparse.ArgumentParser(prog="pipeline.trainer")
    ap.add_argument("spec_path")
    a = ap.parse_args()
    spec = specmod.load_spec(a.spec_path)
    eff = specmod.resolve(spec)
    run_train(spec, eff)
