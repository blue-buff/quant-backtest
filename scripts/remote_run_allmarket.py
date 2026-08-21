"""远程全市场训练 v2：分块加载（400 只/块）+ kernels=4 + 失败隔离。

修复：全市场整批 fetch 在 Windows 上无声崩溃（疑似 joblib multiprocessing spawn
+ 数据量共同触发）；改为分块加载，坏块单独隔离重试。
"""
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("C:/Users/song/qbt_work")
URI = str(BASE / "qlib_data/cn_data_all")
OUT = BASE / "results"


def load_chunk(insts, label, learn_proc, kind):
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP
    if kind == "tr":
        h = Alpha158(instruments=insts, start_time="2021-06-01", end_time="2024-12-31",
                     fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                     learn_processors=learn_proc, label=label)
    else:
        h = Alpha158(instruments=insts, start_time="2025-01-01", end_time="2026-08-14",
                     fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                     learn_processors=learn_proc, label=label)
    data = h.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    feat = data["feature"].astype(np.float32)
    lab = data["label"].iloc[:, 0]
    return feat.join(lab.rename("y"))


def main():
    import qlib
    qlib.init(provider_uri=URI, region="cn", kernels=4)
    print("qlib init OK (kernels=4)", flush=True)
    label = ["Ref($close, -11)/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    # 全市场股票列表
    insts = []
    with open(BASE / "qlib_data/cn_data_all/instruments/all.txt") as f:
        for ln in f:
            if ln.strip():
                insts.append(ln.split()[0])
    print(f"全市场 {len(insts)} 只", flush=True)

    CH = 400
    chunks = [insts[i:i + CH] for i in range(0, len(insts), CH)]

    tr_parts, va_parts, te_parts = [], [], []
    bad = []
    for ci, ch in enumerate(chunks):
        try:
            df = load_chunk(ch, label, learn_proc, "tr")
            tr_parts.append(df[(df.index.get_level_values(0) >= "2021-06-01") & (df.index.get_level_values(0) < "2024-06-30")])
            va_parts.append(df[(df.index.get_level_values(0) >= "2024-07-01") & (df.index.get_level_values(0) < "2024-12-31")])
            print(f"chunk {ci}/{len(chunks)} 训练特征 OK ({len(df)} 行)", flush=True)
        except Exception as e:
            bad.append((ci, str(e)[:120]))
            print(f"chunk {ci} 失败: {type(e).__name__} {e}", flush=True)
    tr = pd.concat(tr_parts) if tr_parts else pd.DataFrame()
    va = pd.concat(va_parts) if va_parts else pd.DataFrame()
    print(f"train {tr.shape} valid {va.shape} bad_chunks={len(bad)}", flush=True)
    if tr.empty:
        print("FATAL: 无训练数据", flush=True)
        sys.exit(1)
    feats = [c for c in tr.columns if c != "y"]

    import lightgbm as lgb
    d_tr = lgb.Dataset(tr[feats], label=tr["y"].astype(np.float32))
    d_va = lgb.Dataset(va[feats], label=va["y"].astype(np.float32), reference=d_tr)
    params = {"objective": "regression", "metric": "l2", "colsample_bytree": 0.8879,
              "learning_rate": 0.2, "subsample": 0.8789, "lambda_l1": 205.6999, "lambda_l2": 580.9768,
              "max_depth": 8, "num_leaves": 210, "num_threads": 28, "verbosity": -1, "seed": 42}
    t0 = time.time()
    model = lgb.train(params, d_tr, num_boost_round=1000, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)])
    print(f"训练 {time.time()-t0:.0f}s best_iter={model.best_iteration}", flush=True)
    del tr, va, d_tr, d_va

    te_parts = []
    for ci, ch in enumerate(chunks):
        try:
            df = load_chunk(ch, label, learn_proc, "te")
            te_parts.append(df[(df.index.get_level_values(0) >= "2025-01-01") & (df.index.get_level_values(0) < "2026-08-14")])
            print(f"chunk {ci}/{len(chunks)} 测试特征 OK ({len(df)} 行)", flush=True)
        except Exception as e:
            print(f"chunk {ci} 测试失败: {type(e).__name__} {e}", flush=True)
    te = pd.concat(te_parts)
    print(f"test {te.shape}", flush=True)
    scores = []
    for s in range(0, len(te), 200000):
        scores.append(model.predict(te.iloc[s:s + 200000][feats], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=te.index)
    label_te = te["y"].loc[pred.index]

    out = OUT / "e17r_all_10d21"
    out.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(out / "pred_matrix.pkl")
    label_te.to_pickle(out / "label_matrix.pkl")

    from scipy import stats
    def eval_subset(mask):
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

    inst_of_pred = pred.index.get_level_values(1)
    csi300, csi500 = set(), set()
    for f, s in [("cn_data/instruments/csi300.txt", csi300), ("cn_data_zz500/instruments/csi500.txt", csi500)]:
        with open(BASE / "qlib_data" / f) as fh:
            for ln in fh:
                if ln.strip():
                    s.add(ln.split()[0])
    meta = {"name": "e17r_all_10d21", "test_shape": list(te.shape), "bad_chunks": bad,
            "best_iter": model.best_iteration,
            "eval": {"all": eval_subset(np.ones(len(pred), dtype=bool)),
                     "csi300_subset": eval_subset(inst_of_pred.isin(csi300)),
                     "csi500_subset": eval_subset(inst_of_pred.isin(csi500))},
            "ts": datetime.now().isoformat(timespec="seconds")}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
