"""远程夜间队列：全市场多方案训练（特征缓存复用 + 失败方案重试）。

方案（全部留痕 results/）：
  A. 10d MSE 种子 100/200（与已完成的 seed42 组成 3 种子集成）
  B. 20d MSE（长标签全市场）
  C. LambdaRank 10d（重试：更大数据下排序学习是否有效）
  D. 时间衰减×收益归因加权 10d（重试）
每步写独立日志 results/queue_log.txt。
"""
import json
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("C:/Users/song/qbt_work")
URI = str(BASE / "qlib_data/cn_data_all")
OUT = BASE / "results"
LOG = OUT / "queue_log.txt"


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def all_insts():
    insts = []
    with open(BASE / "qlib_data/cn_data_all/instruments/all.txt") as f:
        for ln in f:
            if ln.strip():
                insts.append(ln.split()[0])
    return insts


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


def get_features(label, learn_proc, cache_name, kind):
    """kind: tr+va 合并载入后切分 / te"""
    cache = OUT / f"cache_{cache_name}_{kind}.parquet"
    if cache.exists():
        log(f"缓存命中 {cache.name}")
        return pd.read_parquet(cache)
    insts = all_insts()
    parts = []
    CH = 400
    chunks = [insts[i:i + CH] for i in range(0, len(insts), CH)]
    for ci, ch in enumerate(chunks):
        df = load_chunk(ch, label, learn_proc, kind)
        parts.append(df)
        log(f"{kind} chunk {ci}/{len(chunks)} OK ({len(df)} 行)")
    full = pd.concat(parts)
    full.to_parquet(cache)
    log(f"{kind} 缓存写入 {cache.name} ({full.shape})")
    return full


LEARN = [
    {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
    {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
     "kwargs": {"fields_group": "label"}},
]


def train_mse(feats, tr, va, te, name, seed, lr=0.2, weight=None, ndcg=None):
    import lightgbm as lgb
    d_tr = lgb.Dataset(tr[feats], label=tr["y"].astype(np.float32))
    d_va = lgb.Dataset(va[feats], label=va["y"].astype(np.float32), reference=d_tr)
    params = {"objective": "regression", "metric": "l2", "colsample_bytree": 0.8879,
              "learning_rate": lr, "subsample": 0.8789, "lambda_l1": 205.6999, "lambda_l2": 580.9768,
              "max_depth": 8, "num_leaves": 210, "num_threads": 28, "verbosity": -1, "seed": seed}
    t0 = time.time()
    model = lgb.train(params, d_tr, num_boost_round=1000, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50)])
    log(f"{name} 训练 {time.time()-t0:.0f}s best_iter={model.best_iteration}")
    scores = []
    for s in range(0, len(te), 200000):
        scores.append(model.predict(te.iloc[s:s + 200000][feats], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=te.index)
    return pred, model.best_iteration


def eval_rankic(pred, label_te, csi300, csi500):
    from scipy import stats
    def sub(mask):
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
                "rankic_ir": float(ics.mean() / ics.std()) if ics.std() > 0 else None}
    inst = pred.index.get_level_values(1)
    return {"all": sub(np.ones(len(pred), dtype=bool)),
            "csi300_subset": sub(inst.isin(csi300)),
            "csi500_subset": sub(inst.isin(csi500))}


def pool_sets():
    csi300, csi500 = set(), set()
    for f, s in [("cn_data/instruments/csi300.txt", csi300), ("cn_data_zz500/instruments/csi500.txt", csi500)]:
        with open(BASE / "qlib_data" / f) as fh:
            for ln in fh:
                if ln.strip():
                    s.add(ln.split()[0])
    return csi300, csi500


def main():
    import qlib
    qlib.init(provider_uri=URI, region="cn", kernels=4)
    log("=== 夜间队列启动 ===")
    csi300, csi500 = pool_sets()
    label10 = ["Ref($close, -11)/Ref($close, -1) - 1"]
    label20 = ["Ref($close, -21)/Ref($close, -1) - 1"]

    # ---- 10d 特征（缓存）----
    df_tr = get_features(label10, LEARN, "a10d", "tr")
    df_te = get_features(label10, LEARN, "a10d", "te")
    feats = [c for c in df_tr.columns if c != "y"]
    tr = df_tr[(df_tr.index.get_level_values(0) >= "2021-06-01") & (df_tr.index.get_level_values(0) < "2024-06-30")]
    va = df_tr[(df_tr.index.get_level_values(0) >= "2024-07-01") & (df_tr.index.get_level_values(0) < "2024-12-31")]
    te = df_te[(df_te.index.get_level_values(0) >= "2025-01-01") & (df_te.index.get_level_values(0) < "2026-08-14")]
    label_te = te["y"]
    log(f"10d 特征: train {tr.shape} valid {va.shape} test {te.shape}")

    results = {}
    # A. 种子 100 / 200
    for seed in (100, 200):
        name = f"all10d_seed{seed}"
        pred, it = train_mse(feats, tr, va, te, name, seed)
        pred.to_pickle(OUT / f"{name}_pred.pkl")
        results[name] = {"best_iter": it, "eval": eval_rankic(pred, label_te, csi300, csi500)}
        log(f"{name}: {json.dumps(results[name]['eval'], ensure_ascii=False)}")

    # D. 时间衰减加权（seed 42）
    dates = tr.index.get_level_values(0)
    age = np.asarray((dates.max() - dates).days, dtype=float)
    w_time = np.exp(-age / 365.0)
    w_ret = 0.2 + 0.8 * np.minimum(np.abs(tr["y"].to_numpy(float)) / 3.0, 1.0)
    w = (w_time * w_ret).astype(np.float32)
    import lightgbm as lgb
    d_tr = lgb.Dataset(tr[feats], label=tr["y"].astype(np.float32), weight=w)
    d_va = lgb.Dataset(va[feats], label=va["y"].astype(np.float32), reference=d_tr)
    params = {"objective": "regression", "metric": "l2", "colsample_bytree": 0.8879,
              "learning_rate": 0.2, "subsample": 0.8789, "lambda_l1": 205.6999, "lambda_l2": 580.9768,
              "max_depth": 8, "num_leaves": 210, "num_threads": 28, "verbosity": -1, "seed": 42}
    model = lgb.train(params, d_tr, num_boost_round=1000, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50)])
    log(f"all10d_weighted 训练完成 best_iter={model.best_iteration}")
    scores = []
    for s in range(0, len(te), 200000):
        scores.append(model.predict(te.iloc[s:s + 200000][feats], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=te.index)
    pred.to_pickle(OUT / "all10d_weighted_pred.pkl")
    results["all10d_weighted"] = {"best_iter": model.best_iteration,
                                  "eval": eval_rankic(pred, label_te, csi300, csi500)}
    log(f"all10d_weighted: {json.dumps(results['all10d_weighted']['eval'], ensure_ascii=False)}")

    # C. LambdaRank（10d，分位离散）
    def quantize_daily(y, n_buckets=20):
        df_ = y.to_frame("y")
        df_["dt"] = df_.index.get_level_values(0)
        out = []
        for dt, g in df_.groupby("dt"):
            g = g.dropna()
            if len(g) < 30:
                continue
            pct = g["y"].rank(pct=True)
            g["yb"] = (pct * n_buckets).astype(int).clip(upper=n_buckets - 1)
            out.append(g["yb"])
        return pd.concat(out)
    y_tr = quantize_daily(tr["y"]).astype(np.int32)
    y_va = quantize_daily(va["y"]).astype(np.int32)
    X_tr = tr[feats].loc[y_tr.index].sort_index()
    y_tr = y_tr.loc[X_tr.index]
    X_va = va[feats].loc[y_va.index].sort_index()
    y_va = y_va.loc[X_va.index]
    groups_tr = X_tr.index.get_level_values(0).value_counts().sort_index().to_numpy()
    groups_va = X_va.index.get_level_values(0).value_counts().sort_index().to_numpy()
    rparams = {"objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [50],
               "boosting_type": "gbdt", "num_leaves": 128, "learning_rate": 0.05,
               "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
               "min_child_samples": 100, "lambda_l1": 0.1, "lambda_l2": 1.0,
               "max_depth": -1, "seed": 42, "num_threads": 28, "verbosity": -1}
    d_tr = lgb.Dataset(X_tr, label=y_tr, group=groups_tr)
    d_va = lgb.Dataset(X_va, label=y_va, group=groups_va, reference=d_tr)
    t0 = time.time()
    model = lgb.train(rparams, d_tr, num_boost_round=800, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(100)])
    log(f"all10d_ranker 训练 {time.time()-t0:.0f}s best_iter={model.best_iteration}")
    scores = []
    for s in range(0, len(te), 200000):
        scores.append(model.predict(te.iloc[s:s + 200000][feats], num_iteration=model.best_iteration))
    pred = pd.DataFrame({"score": np.concatenate(scores)}, index=te.index)
    pred.to_pickle(OUT / "all10d_ranker_pred.pkl")
    results["all10d_ranker"] = {"best_iter": model.best_iteration,
                                "eval": eval_rankic(pred, label_te, csi300, csi500)}
    log(f"all10d_ranker: {json.dumps(results['all10d_ranker']['eval'], ensure_ascii=False)}")

    # B. 20d（单独特征缓存 + 训练）
    df_tr20 = get_features(label20, LEARN, "b20d", "tr")
    df_te20 = get_features(label20, LEARN, "b20d", "te")
    feats20 = [c for c in df_tr20.columns if c != "y"]
    tr20 = df_tr20[(df_tr20.index.get_level_values(0) >= "2021-06-01") & (df_tr20.index.get_level_values(0) < "2024-06-30")]
    va20 = df_tr20[(df_tr20.index.get_level_values(0) >= "2024-07-01") & (df_tr20.index.get_level_values(0) < "2024-12-31")]
    te20 = df_te20[(df_te20.index.get_level_values(0) >= "2025-01-01") & (df_te20.index.get_level_values(0) < "2026-08-14")]
    label_te20 = te20["y"]
    pred, it = train_mse(feats20, tr20, va20, te20, "all20d_seed42", 42)
    pred.to_pickle(OUT / "all20d_seed42_pred.pkl")
    results["all20d_seed42"] = {"best_iter": it, "eval": eval_rankic(pred, label_te20, csi300, csi500)}
    log(f"all20d_seed42: {json.dumps(results['all20d_seed42']['eval'], ensure_ascii=False)}")

    with open(OUT / "queue_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log("=== 队列全部完成 ===")
    log(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        log(traceback.format_exc())
        raise
