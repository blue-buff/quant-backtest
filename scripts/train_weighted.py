"""时间衰减 × 收益归因样本权重训练（调研建议：age-based weighting + return attribution）。

w_i = exp(-(T - t_i) / half_life) * (0.2 + 0.8 * min(|y_i|/3, 1))
- 时间衰减：给近期样本更高权重（缓解概念漂移，half_life 默认 365 天）
- 收益归因：给大波动样本更高权重（高波动期信号更强）

用法:
  python scripts/train_weighted.py --pool hs300 --yaml qlib_examples/experiments/e03_off_10d_hs300.yaml \
      --label-days 10 --name w10d_hs300 --half-life 365
产物: results/exps/<name>_<pool>/pred_matrix.pkl, label_matrix.pkl, meta.json（含测试段 RankIC 与季度分解）
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXPS = ROOT / "results" / "exps"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", default="hs300")
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--label-days", type=int, default=10)
    ap.add_argument("--name", default="weighted")
    ap.add_argument("--half-life", type=int, default=365)
    ap.add_argument("--num-leaves", type=int, default=210)
    ap.add_argument("--lr", type=float, default=0.2)
    ap.add_argument("--rounds", type=int, default=1000)
    args = ap.parse_args()

    import lightgbm as lgb
    import qlib
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.contrib.data.handler import Alpha158
    from scipy import stats

    cfg = yaml.safe_load(Path(args.yaml).read_text(encoding="utf-8"))
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn")
    hc = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    hc2 = dict(hc)
    hc2["label"] = [f"Ref($close, -{args.label_days + 1})/Ref($close, -1) - 1"]
    handler = Alpha158(
        instruments=hc2["instruments"], start_time=hc2["start_time"], end_time=hc2["end_time"],
        fit_start_time=hc2["fit_start_time"], fit_end_time=hc2["fit_end_time"],
        learn_processors=[
            {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
            {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
             "kwargs": {"fields_group": "label"}},
        ],
        label=hc2["label"],
    )
    seg = cfg["task"]["dataset"]["kwargs"]["segments"]
    data = handler.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    feat = data["feature"]
    lab = data["label"].iloc[:, 0]
    df = feat.join(lab.rename("y"))
    tr = df[(df.index.get_level_values(0) >= seg["train"][0]) & (df.index.get_level_values(0) < seg["train"][1])]
    va = df[(df.index.get_level_values(0) >= seg["valid"][0]) & (df.index.get_level_values(0) < seg["valid"][1])]
    te = df[(df.index.get_level_values(0) >= seg["test"][0]) & (df.index.get_level_values(0) < seg["test"][1])]
    feats = [c for c in df.columns if c != "y"]
    print(f"train {tr.shape} valid {va.shape} test {te.shape}")

    def weights(y: pd.Series) -> np.ndarray:
        dates = y.index.get_level_values(0)
        t_max = dates.max()
        age = np.asarray((t_max - dates).days, dtype=float)
        w_time = np.exp(-age / args.half_life)
        w_ret = 0.2 + 0.8 * np.minimum(np.abs(np.asarray(y, dtype=float)) / 3.0, 1.0)
        return (w_time * w_ret).astype(np.float32)

    X_tr, y_tr = tr[feats], tr["y"]
    X_va, y_va = va[feats], va["y"]
    X_te = te[feats]
    d_tr = lgb.Dataset(X_tr, label=y_tr, weight=weights(y_tr))
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    params = {
        "objective": "regression", "metric": "l2",
        "colsample_bytree": 0.8879, "learning_rate": args.lr, "subsample": 0.8789,
        "lambda_l1": 205.6999, "lambda_l2": 580.9768, "max_depth": 8,
        "num_leaves": args.num_leaves, "num_threads": 20, "verbosity": -1, "seed": 42,
    }
    t0 = time.time()
    model = lgb.train(params, d_tr, num_boost_round=args.rounds, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(200)])
    print(f"训练 {time.time()-t0:.0f}s best_iter={model.best_iteration}")
    score = model.predict(X_te, num_iteration=model.best_iteration)
    pred = pd.DataFrame({"score": score}, index=X_te.index)
    label_te = te["y"].loc[pred.index]
    out = EXPS / f"{args.name}_{args.pool}"
    out.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(out / "pred_matrix.pkl")
    label_te.to_pickle(out / "label_matrix.pkl")
    ics = []
    for dt, g in pred.groupby(level=0):
        if len(g) < 30:
            continue
        v = stats.spearmanr(g["score"], label_te.loc[g.index])[0]
        if np.isfinite(v):
            ics.append((dt, v))
    ics_df = pd.DataFrame(ics, columns=["dt", "ric"]).set_index("dt")
    ics_df["q"] = ics_df.index.to_period("Q").astype(str)
    meta = {
        "name": f"{args.name}_{args.pool}", "pool": args.pool,
        "label_days": args.label_days, "half_life": args.half_life,
        "num_leaves": args.num_leaves, "lr": args.lr, "best_iter": model.best_iteration,
        "test_rankic_mean": float(ics_df["ric"].mean()),
        "test_rankic_std": float(ics_df["ric"].std()),
        "test_rankic_ir": float(ics_df["ric"].mean() / ics_df["ric"].std()),
        "quarterly": {q: {"n": int(len(g)), "rankic": float(g["ric"].mean())}
                      for q, g in ics_df.groupby("q")},
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in meta.items() if k != "quarterly"}, ensure_ascii=False, indent=2))
    print("quarterly:", json.dumps(meta["quarterly"], ensure_ascii=False))


if __name__ == "__main__":
    main()
