"""远程冒烟测试：pool 级 10d 训练（300 股），验证 Windows 环境可用。"""
import traceback
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("C:/Users/song/qbt_work")
URI = str(BASE / "qlib_data/cn_data")
OUT = BASE / "results"


def main():
    import lightgbm as lgb
    import qlib
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.contrib.data.handler import Alpha158
    from scipy import stats

    label = ["Ref($close, -11)/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    qlib.init(provider_uri=URI, region="cn")
    print("qlib init OK", flush=True)
    h = Alpha158(instruments="csi300", start_time="2021-06-01", end_time="2026-08-14",
                 fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                 learn_processors=learn_proc, label=label)
    print("handler created", flush=True)
    t0 = time.time()
    data = h.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    print(f"fetch done {time.time()-t0:.0f}s", flush=True)
    feat = data["feature"].astype(np.float32)
    lab = data["label"].iloc[:, 0]
    df = feat.join(lab.rename("y"))
    tr = df[(df.index.get_level_values(0) >= "2021-06-01") & (df.index.get_level_values(0) < "2024-06-30")]
    va = df[(df.index.get_level_values(0) >= "2024-07-01") & (df.index.get_level_values(0) < "2024-12-31")]
    te = df[(df.index.get_level_values(0) >= "2025-01-01") & (df.index.get_level_values(0) < "2026-08-14")]
    feats = [c for c in df.columns if c != "y"]
    print(f"train {tr.shape} valid {va.shape} test {te.shape}", flush=True)
    d_tr = lgb.Dataset(tr[feats], label=tr["y"].astype(np.float32))
    d_va = lgb.Dataset(va[feats], label=va["y"].astype(np.float32), reference=d_tr)
    params = {"objective": "regression", "metric": "l2", "colsample_bytree": 0.8879,
              "learning_rate": 0.2, "subsample": 0.8789, "lambda_l1": 205.6999, "lambda_l2": 580.9768,
              "max_depth": 8, "num_leaves": 210, "num_threads": 28, "verbosity": -1, "seed": 42}
    model = lgb.train(params, d_tr, num_boost_round=1000, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(200)])
    print(f"best_iter {model.best_iteration}", flush=True)
    scores = []
    for s in range(0, len(te), 100000):
        scores.append(model.predict(te.iloc[s:s + 100000][feats], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=te.index)
    label_te = te["y"].loc[pred.index]
    ics = []
    for dt, g in pred.groupby(level=0):
        if len(g) < 30:
            continue
        v = stats.spearmanr(g["score"], label_te.loc[g.index])[0]
        if np.isfinite(v):
            ics.append(v)
    ics = np.array(ics)
    print(f"SMOKE RankIC mean={ics.mean():.4f} std={ics.std():.4f} n={ics.size}", flush=True)
    out = OUT / "smoke_hs300"
    out.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(out / "pred_matrix.pkl")
    label_te.to_pickle(out / "label_matrix.pkl")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
