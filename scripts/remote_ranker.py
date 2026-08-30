"""远程 LambdaRank 重试（全市场数据，用户要求不给失败方法判死刑）。
标签 10d，每日截面分位离散 20 桶，NDCG@50。
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("C:/Users/song/qbt_work")
URI = str(BASE / "qlib_data/cn_data_all")
OUT = BASE / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="ranker_all_10d")
    ap.add_argument("--label-days", type=int, default=10)
    ap.add_argument("--ndcg", type=int, default=50)
    ap.add_argument("--num-leaves", type=int, default=128)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--rounds", type=int, default=800)
    args = ap.parse_args()

    import lightgbm as lgb
    import qlib
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.contrib.data.handler import Alpha158
    from scipy import stats

    label = [f"Ref($close, -{args.label_days + 1})/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    qlib.init(provider_uri=URI, region="cn")
    h_tr = Alpha158(instruments="all", start_time="2021-06-01", end_time="2024-12-31",
                    fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                    learn_processors=learn_proc, label=label)
    data = h_tr.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    feat = data["feature"].astype(np.float32)
    lab = data["label"].iloc[:, 0]
    df = feat.join(lab.rename("y"))
    del feat, lab, data
    tr = df[(df.index.get_level_values(0) >= "2021-06-01") & (df.index.get_level_values(0) < "2024-06-30")]
    va = df[(df.index.get_level_values(0) >= "2024-07-01") & (df.index.get_level_values(0) < "2024-12-31")]
    te = df[(df.index.get_level_values(0) >= "2025-01-01") & (df.index.get_level_values(0) < "2026-08-14")]
    feats = [c for c in df.columns if c != "y"]
    print(f"数据就绪 train {tr.shape} valid {va.shape} test {te.shape}", flush=True)

    def quantize_daily(y, n_buckets=20):
        df_ = y.to_frame("y")
        df_["dt"] = df_.index.get_level_values(0)
        out = []
        for dt, g in df_.groupby("dt"):
            pct = g["y"].rank(pct=True)
            g["yb"] = (pct * n_buckets).astype(int).clip(upper=n_buckets - 1)
            out.append(g["yb"])
        return pd.concat(out)

    y_tr = quantize_daily(tr["y"]).astype(np.int32)
    y_va = quantize_daily(va["y"]).astype(np.int32)
    X_tr, X_va, X_te = tr[feats], va[feats], te[feats]
    X_tr = X_tr.sort_index()
    y_tr = y_tr.loc[X_tr.index]
    X_va = X_va.sort_index()
    y_va = y_va.loc[X_va.index]
    X_te = X_te.sort_index()
    groups_tr = X_tr.index.get_level_values(0).value_counts().sort_index().to_numpy()
    groups_va = X_va.index.get_level_values(0).value_counts().sort_index().to_numpy()

    params = {
        "objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [args.ndcg],
        "boosting_type": "gbdt", "num_leaves": args.num_leaves, "learning_rate": args.lr,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
        "min_child_samples": 100, "lambda_l1": 0.1, "lambda_l2": 1.0,
        "max_depth": -1, "seed": 42, "num_threads": 28, "verbosity": -1,
    }
    d_tr = lgb.Dataset(X_tr, label=y_tr, group=groups_tr)
    d_va = lgb.Dataset(X_va, label=y_va, group=groups_va, reference=d_tr)
    t0 = time.time()
    model = lgb.train(params, d_tr, num_boost_round=args.rounds, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(100), lgb.log_evaluation(100)])
    print(f"训练 {time.time()-t0:.0f}s best_iter={model.best_iteration}", flush=True)

    scores = []
    for s in range(0, len(X_te), 200000):
        scores.append(model.predict(X_te.iloc[s:s + 200000], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=X_te.index)
    label_te = te["y"].loc[pred.index]
    ics = []
    for dt, g in pred.groupby(level=0):
        if len(g) < 30:
            continue
        v = stats.spearmanr(g["score"], label_te.loc[g.index])[0]
        if np.isfinite(v):
            ics.append(v)
    ics = np.array(ics)
    out = OUT / args.name
    out.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(out / "pred_matrix.pkl")
    label_te.to_pickle(out / "label_matrix.pkl")
    meta = {"name": args.name, "label_days": args.label_days, "ndcg": args.ndcg,
            "num_leaves": args.num_leaves, "lr": args.lr, "best_iter": model.best_iteration,
            "test_rankic_mean": float(ics.mean()), "test_rankic_std": float(ics.std()),
            "test_rankic_ir": float(ics.mean() / ics.std()) if ics.std() > 0 else None,
            "ts": datetime.now().isoformat(timespec="seconds")}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
