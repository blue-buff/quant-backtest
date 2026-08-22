"""Example executor (contract reference implementation): LightGBM ranker.

This directory is the reference for the executor contract. Copy it, keep the
CLI, change train() freely -- the pipeline does not inspect executor internals.

Contract:
  main.py --config <json> --train <pq> --test <pq> --out <dir>
  outputs: <out>/pred.pkl  (MultiIndex (datetime, instrument), column "score")
The pipeline then runs the fixed tester on pred.pkl and imports the result.

config json (written by pipeline.data/harness) contains: pool, instruments,
handler_class, fit/train/valid/test windows, label_formula, horizon, task,
model params, seeds, ensemble, rounds, early_stopping, num_threads.
"""
import argparse, json, time
from pathlib import Path

import numpy as np
import pandas as pd


def load(cfg, train_pq, test_pq):
    tr = pd.read_parquet(train_pq)
    te = pd.read_parquet(test_pq)
    lv_tr = tr.index.get_level_values(0)
    trn = tr[(lv_tr >= cfg["train"][0]) & (lv_tr < cfg["train"][1])]
    val = tr[(lv_tr >= cfg["valid"][0]) & (lv_tr < cfg["valid"][1])]
    feats = [c for c in tr.columns if c != "y"]
    return trn[feats], trn["y"], val[feats], val["y"], te[feats], te["y"], feats


def train_seed(X_tr, y_tr, X_va, y_va, cfg, seed):
    import lightgbm as lgb
    params = {"objective": "regression", "metric": "l2",
              "num_threads": cfg.get("num_threads", 20), "verbosity": -1, "seed": int(seed)}
    for k in ("loss", "num_leaves", "learning_rate", "max_depth", "colsample_bytree",
              "subsample", "lambda_l1", "lambda_l2"):
        if k in cfg.get("model", {}):
            params[k] = cfg["model"][k]
    d_tr = lgb.Dataset(X_tr, label=y_tr)
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    model = lgb.train(params, d_tr, num_boost_round=cfg.get("rounds", 1000),
                      valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(cfg.get("early_stopping", 50)),
                                 lgb.log_evaluation(0)])
    best = model.best_score.get("valid_0", {}).get("l2")
    return model, best


def main():
    ap = argparse.ArgumentParser(prog="executors/_example_lgb")
    ap.add_argument("--config", required=True)
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    X_tr, y_tr, X_va, y_va, X_te, label_te, feats = load(cfg, a.train, a.test)
    print("data loaded: train %s valid %s test %s feats=%d" %
          (X_tr.shape, X_va.shape, X_te.shape, len(feats)), flush=True)
    seeds = cfg.get("seeds") or [42]
    preds, seed_runs = [], []
    for seed in seeds:
        t1 = time.time()
        model, best = train_seed(X_tr, y_tr, X_va, y_va, cfg, seed)
        seed_runs.append({"seed": int(seed), "best_iter": int(model.best_iteration),
                          "valid_l2": float(best) if best is not None else None,
                          "seconds": round(time.time() - t1, 1)})
        if cfg.get("save_models"):
            model.save_model(str(out / ("model_seed%s.txt" % seed)))
        scores = []
        for s in range(0, len(X_te), 200000):
            scores.append(model.predict(X_te.iloc[s:s + 200000],
                                        num_iteration=model.best_iteration))
        preds.append(pd.Series(np.concatenate(scores), index=X_te.index, name="score"))
        print("seed %s done %.0fs best_iter=%s valid_l2=%s" %
              (seed, time.time() - t1, model.best_iteration, best), flush=True)
        del model
    if len(preds) == 1:
        ens = preds[0]
    else:
        ranks = []
        for s in preds:
            dfr = s.to_frame("score")
            dfr["dt"] = dfr.index.get_level_values(0)
            dfr["r"] = dfr.groupby("dt")["score"].rank(pct=True)
            ranks.append(dfr["r"])
        ens = pd.Series(pd.concat(ranks, axis=1).mean(axis=1).to_numpy(),
                        index=X_te.index, name="score")
    ens.to_frame("score").to_pickle(out / "pred.pkl")
    info = {"seed_runs": seed_runs, "seconds": round(time.time() - t0, 1),
            "features": len(feats)}
    (out / "run_info.json").write_text(json.dumps(info, indent=2))
    print(json.dumps({"done": True, "seconds": info["seconds"], "seed_runs": seed_runs}),
          flush=True)


if __name__ == "__main__":
    main()
