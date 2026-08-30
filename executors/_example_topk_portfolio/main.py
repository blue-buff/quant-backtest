"""Example trade executor (P8 T6): LightGBM + topK equal-weight portfolio.

Reference implementation for the trade half of the executor contract: same
LightGBM training as _example_lgb, then a daily topK equal-weight portfolio
with a rebalance buffer. This is the "glue code" sample -- copy it, keep the
CLI, change the strategy freely.

Contract outputs:
  <out>/pred.pkl       (datetime, instrument) MultiIndex, column "score"
  <out>/portfolio.pkl  (datetime, instrument) MultiIndex, column "weight"
                       weights >= 0, daily sum <= 1; unheld rows omitted

Strategy knobs come from config["params"] (spec.params passthrough):
  top_k       number of names held (default 50)
  invest_frac fraction of capital invested (default 0.95; rest is cash)
  buffer      only trade a name when |target - current| > buffer (default 0.005)
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
              "num_threads": cfg.get("num_threads", 20), "verbosity": -1,
              "seed": int(seed)}
    # opt-in determinism only; the default (no force_row_wise/deterministic)
    # matches the historical all10d_ens3 training and reproduces its rankic
    if (cfg.get("params") or {}).get("deterministic"):
        params["deterministic"] = True
        params["force_row_wise"] = True
    for k in ("loss", "num_leaves", "learning_rate", "max_depth", "colsample_bytree",
              "subsample", "lambda_l1", "lambda_l2"):
        if k in cfg.get("model", {}):
            params[k] = cfg["model"][k]
    d_tr = lgb.Dataset(X_tr, label=y_tr)
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    model = lgb.train(params, d_tr, num_boost_round=cfg.get("rounds", 1000),
                      valid_sets=[d_tr, d_va],
                      callbacks=[lgb.early_stopping(cfg.get("early_stopping", 50)),
                                 lgb.log_evaluation(0)])
    best = model.best_score.get("valid_0", {}).get("l2")
    return model, best


def build_portfolio(scores, params):
    """Daily topK equal-weight with a rebalance buffer. scores: long Series with
    (datetime, instrument) MultiIndex (already rank_mean-ensembled)."""
    top_k = int(params.get("top_k", 50))
    invest_frac = float(params.get("invest_frac", 0.95))
    buffer = float(params.get("buffer", 0.005))
    target_w = invest_frac / max(1, top_k)
    rows = []
    current = {}  # instrument -> weight
    for dt, day in scores.groupby(level=0):
        day = day.droplevel(0)  # groupby keeps the MultiIndex; strip the date level
        ranked = day.rank(ascending=False, method="first")
        universe = set(day.index)
        # liquidate names that left the universe
        for inst in [i for i in current if i not in universe]:
            del current[inst]
        picks = set(day[ranked <= top_k].index)
        new_holdings = {}
        for inst, w_cur in current.items():
            w_new = target_w if inst in picks else 0.0
            if abs(w_new - w_cur) > buffer:
                new_holdings[inst] = w_new
            else:
                new_holdings[inst] = w_cur
        for inst in picks:
            if inst not in current:
                new_holdings[inst] = target_w  # new entry trades immediately
        current = {k: v for k, v in new_holdings.items() if v > 0}
        for inst, w in current.items():
            rows.append((dt, inst, float(w)))
    pf = pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"])
    return pf


def main():
    ap = argparse.ArgumentParser(prog="executors/_example_topk_portfolio")
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
        scores = []
        for s in range(0, len(X_te), 200000):
            scores.append(model.predict(X_te.iloc[s:s + 200000],
                                        num_iteration=model.best_iteration))
        preds.append(pd.Series(np.concatenate(scores), index=X_te.index, name="score"))
        print("seed %s done %.0fs best_iter=%s" % (seed, time.time() - t1,
                                                   model.best_iteration), flush=True)
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
    params = cfg.get("params") or {}
    pf = build_portfolio(ens, params)
    pf.to_pickle(out / "portfolio.pkl")
    info = {"seed_runs": seed_runs, "seconds": round(time.time() - t0, 1),
            "features": len(feats),
            "portfolio": {"strategy": "topK equal weight + rebalance buffer",
                          "params_used": {k: params.get(k) for k in
                                          ("top_k", "invest_frac", "buffer")},
                          "n_days": int(pf.index.get_level_values(0).nunique()),
                          "mean_held": float(pf.groupby(level=0).size().mean())}}
    (out / "run_info.json").write_text(json.dumps(info, indent=2))
    print(json.dumps({"done": True, "seconds": info["seconds"],
                      "portfolio": info["portfolio"]}), flush=True)


if __name__ == "__main__":
    main()
