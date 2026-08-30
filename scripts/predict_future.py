"""未来预测：全市场 10d 3 种子集成 → 最新交易日截面 Top 名单 + 事后验证协议。

流程：
1. 训练：全市场 2021-06-01~2026-07-31（留 10d 标签窗口），valid 2026-06-01~07-31
2. 预测：最新交易日（2026-08-20 收盘）全市场截面分数，3 种子平均
3. 输出：Top 50 名单（全市场）+ 各池 Top 30 + 分数分布 + meta.json
4. 验证协议：10 个交易日后用真实收益对照（verify_future.py 模板）

用法: python scripts/predict_future.py [--seed 42,100,200] [--top 50]
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/quant")
URI = "/root/.qlib/qlib_data/cn_data_all"
OUT = ROOT / "results" / "future"


def load_chunk(insts, label, learn_proc, kind):
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP
    if kind == "tr":
        h = Alpha158(instruments=insts, start_time="2021-06-01", end_time="2026-07-31",
                     fit_start_time="2021-06-01", fit_end_time="2026-07-31",
                     learn_processors=learn_proc, label=label)
        data = h.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
        feat = data["feature"].astype(np.float32)
        lab = data["label"].iloc[:, 0]
        return feat.join(lab.rename("y"))
    # te：预测只需特征（未来标签不存在，不能跑 DropnaLabel）
    h = Alpha158(instruments=insts, start_time="2026-08-20", end_time="2026-08-21",
                 fit_start_time="2021-06-01", fit_end_time="2026-07-31",
                 infer_processors=[])
    data = h.fetch(col_set=["feature"], data_key=DataHandlerLP.DK_I)
    return data["feature"].astype(np.float32)


def all_insts():
    insts = []
    with open("/root/.qlib/qlib_data/cn_data_all/instruments/all.txt") as f:
        for ln in f:
            if ln.strip():
                insts.append(ln.split()[0])
    return insts


def load_all(label, learn_proc, kind):
    insts = all_insts()
    parts = []
    CH = 400
    chunks = [insts[i:i + CH] for i in range(0, len(insts), CH)]
    for ci, ch in enumerate(chunks):
        df = load_chunk(ch, label, learn_proc, kind)
        parts.append(df)
        print(f"{kind} chunk {ci}/{len(chunks)} OK ({len(df)})", flush=True)
    return pd.concat(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", default="42,100,200")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    import lightgbm as lgb
    import qlib
    qlib.init(provider_uri=URI, region="cn", kernels=4)
    label = ["Ref($close, -11)/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    t0 = time.time()
    df_tr = load_all(label, learn_proc, "tr")
    df_te = load_all(label, learn_proc, "te")
    print(f"数据加载 {time.time()-t0:.0f}s", flush=True)
    feats = [c for c in df_tr.columns if c != "y"]
    df_te.columns = feats
    tr = df_tr[(df_tr.index.get_level_values(0) >= "2021-06-01") & (df_tr.index.get_level_values(0) < "2026-06-01")]
    va = df_tr[(df_tr.index.get_level_values(0) >= "2026-06-01") & (df_tr.index.get_level_values(0) <= "2026-07-31")]
    print(f"train {tr.shape} valid {va.shape}", flush=True)

    d_va = lgb.Dataset(va[feats], label=va["y"].astype(np.float32))
    params = {"objective": "regression", "metric": "l2", "colsample_bytree": 0.8879,
              "learning_rate": 0.2, "subsample": 0.8789, "lambda_l1": 205.6999, "lambda_l2": 580.9768,
              "max_depth": 8, "num_leaves": 210, "num_threads": 20, "verbosity": -1}

    seeds = [int(s) for s in args.seeds.split(",")]
    scores = None
    meta_seeds = {}
    for seed in seeds:
        d_tr = lgb.Dataset(tr[feats], label=tr["y"].astype(np.float32))
        p = dict(params, seed=seed)
        t1 = time.time()
        model = lgb.train(p, d_tr, num_boost_round=1000, valid_sets=[d_va],
                          callbacks=[lgb.early_stopping(50)])
        print(f"seed {seed} 训练 {time.time()-t1:.0f}s best_iter={model.best_iteration}", flush=True)
        meta_seeds[seed] = model.best_iteration
        # 预测最新一天（取 te 最后日期）
        last_dt = df_te.index.get_level_values(0).max()
        te_last = df_te[df_te.index.get_level_values(0) == last_dt]
        s = model.predict(te_last[feats], num_iteration=model.best_iteration)
        if scores is None:
            scores = np.zeros(len(te_last))
            idx = te_last.index
        scores = scores + s / len(seeds)

    pred = pd.DataFrame({"score": scores}, index=idx)
    pred = pred.sort_values("score", ascending=False)
    pred["rank"] = np.arange(1, len(pred) + 1)

    # 池过滤
    def pool_codes(name):
        f = Path("/root/.qlib/qlib_data/cn_data/instruments/csi300.txt" if name == "hs300"
                 else "/root/.qlib/qlib_data/cn_data_zz500/instruments/csi500.txt")
        return {ln.split()[0] for ln in f.read_text().splitlines() if ln.strip()}

    csi300, csi500 = pool_codes("hs300"), pool_codes("zz500")
    inst = pred.index.get_level_values(1)
    top_all = pred.head(args.top)
    top_300 = pred[inst.isin(csi300)].head(args.top)
    top_500 = pred[inst.isin(csi500)].head(args.top)

    OUT.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(OUT / "pred_future.pkl")
    for name, df in [("all", top_all), ("hs300", top_300), ("zz500", top_500)]:
        out = OUT / f"top_{args.top}_{name}_{last_dt.strftime('%Y%m%d')}.csv"
        df.to_csv(out, encoding="utf-8-sig")
        print(f"写入 {out} ({len(df)} 行)")
    meta = {"date": str(last_dt), "label": "10d", "seeds": seeds, "best_iter": meta_seeds,
            "train": ["2021-06-01", "2026-05-31"], "valid": ["2026-06-01", "2026-07-31"],
            "top_all": [f"{i}:{c}" for i, c in enumerate(top_all.index.get_level_values(1), 1)],
            "ts": datetime.now().isoformat(timespec="seconds")}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("===== Top 50 全市场 =====")
    for r_, (dt, inst_) in enumerate(top_all.index, 1):
        print(f"{r_:3d} {inst_} {top_all.loc[(dt, inst_), 'score']:.4f}")
    print("===== Top 30 沪深300 =====")
    for r_, (dt, inst_) in enumerate(top_300.index, 1):
        print(f"{r_:3d} {inst_} {top_300.loc[(dt, inst_), 'score']:.4f}")
    print("===== Top 30 中证500 =====")
    for r_, (dt, inst_) in enumerate(top_500.index, 1):
        print(f"{r_:3d} {inst_} {top_500.loc[(dt, inst_), 'score']:.4f}")


if __name__ == "__main__":
    main()
