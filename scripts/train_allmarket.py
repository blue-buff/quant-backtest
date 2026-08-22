"""全市场训练（内存友好版）：分段 fetch + float32 + 分块预测。

- 训练/验证：handler 只建到 2024-12-31，fetch 后转 float32（≈1.6GB）
- 测试预测：独立 handler（2025-01 起，raw 特征与训练一致），分块 predict
- 训练用全部训练段样本（2021-06~2024-06）
用法: python scripts/train_allmarket.py --name e17b_all_10d21
产物: results/exps/<name>/pred_matrix.pkl, label_matrix.pkl, meta.json
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
    ap.add_argument("--name", default="allmarket_10d21")
    ap.add_argument("--label-days", type=int, default=10)
    ap.add_argument("--num-leaves", type=int, default=210)
    ap.add_argument("--lr", type=float, default=0.2)
    ap.add_argument("--rounds", type=int, default=1000)
    args = ap.parse_args()

    import lightgbm as lgb
    import qlib
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.contrib.data.handler import Alpha158
    from scipy import stats

    uri = "/root/.qlib/qlib_data/cn_data_all"
    label = [f"Ref($close, -{args.label_days + 1})/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    qlib.init(provider_uri=uri, region="cn")

    # 训练+验证（数据只建到 2024-12-31，控制内存）
    t0 = time.time()
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
    feats = [c for c in df.columns if c != "y"]
    print(f"数据就绪 {time.time()-t0:.0f}s | train {tr.shape} valid {va.shape}")
    X_tr, y_tr = tr[feats], tr["y"].astype(np.float32)
    X_va, y_va = va[feats], va["y"].astype(np.float32)
    del df, tr, va

    d_tr = lgb.Dataset(X_tr, label=y_tr)
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    params = {
        "objective": "regression", "metric": "l2",
        "colsample_bytree": 0.8879, "learning_rate": args.lr, "subsample": 0.8789,
        "lambda_l1": 205.6999, "lambda_l2": 580.9768, "max_depth": 8,
        "num_leaves": args.num_leaves, "num_threads": 20, "verbosity": -1, "seed": 42,
    }
    t0 = time.time()
    model = lgb.train(params, d_tr, num_boost_round=args.rounds, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)])
    print(f"训练 {time.time()-t0:.0f}s best_iter={model.best_iteration}")
    del X_tr, y_tr, X_va, y_va, d_tr, d_va

    # 测试段（独立 handler，分块预测）
    h_te = Alpha158(instruments="all", start_time="2025-01-01", end_time="2026-08-14",
                    fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                    learn_processors=learn_proc, label=label)
    data_te = h_te.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    X_te = data_te["feature"].astype(np.float32)
    label_te = data_te["label"].iloc[:, 0]
    del data_te
    print(f"测试特征 {X_te.shape}")
    scores = []
    for s in range(0, len(X_te), 200000):
        scores.append(model.predict(X_te.iloc[s:s + 200000], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=X_te.index)

    out = EXPS / args.name
    out.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(out / "pred_matrix.pkl")
    label_te.to_pickle(out / "label_matrix.pkl")

    def eval_subset(uname, mask):
        p = pred[mask]
        l = label_te.loc[p.index]
        ics = []
        for dt, g in p.groupby(level=0):
            if len(g) < 30:
                continue
            v = stats.spearmanr(g["score"], l.loc[g.index])[0]
            if np.isfinite(v):
                ics.append(v)
        ics = np.array(ics)
        return {"n_days": int(len(ics)), "rankic_mean": float(ics.mean()),
                "rankic_std": float(ics.std()),
                "rankic_ir": float(ics.mean() / ics.std()) if ics.std() > 0 else None}

    insts = pred.index.get_level_values(1)
    sub_all = eval_subset("all", np.ones(len(pred), dtype=bool))
    with open("/root/.qlib/qlib_data/cn_data/instruments/csi300.txt") as f:
        csi300 = {ln.split()[0] for ln in f if ln.strip()}
    with open("/root/.qlib/qlib_data/cn_data_zz500/instruments/csi500.txt") as f:
        csi500 = {ln.split()[0] for ln in f if ln.strip()}
    sub300 = eval_subset("csi300-subset", insts.isin(csi300))
    sub500 = eval_subset("csi500-subset", insts.isin(csi500))
    meta = {"name": args.name, "label_days": args.label_days, "best_iter": model.best_iteration,
            "test_shape": list(X_te.shape), "ts": datetime.now().isoformat(timespec="seconds"),
            "eval": {"all": sub_all, "csi300_subset": sub300, "csi500_subset": sub500}}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
