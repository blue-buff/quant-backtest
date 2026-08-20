"""LambdaRank 排序学习训练（P1-7，西南证券实证：夏普+66%、换手-46%）。

与 MSE 回归不同：objective=lambdarank，每个交易日截面一个 group，
直接优化"同截面股票相对排序"（NDCG），与"选 TopK 多头"的任务对齐。

数据管线完全复用训练 yaml 的 Alpha158 handler（含 learn/infer processors），
标签用多日收益（默认 20 日，匹配月频调仓），输出预测 + 评估 JSON。

用法:
  python scripts/train_ranker.py --pool hs300 --yaml qlib_examples/experiments/e01_off_1d_hs300.yaml \
      --label-days 20 --name ranker20 --ndcg 50
产物:
  results/exps/<name>_<pool>/pred.pkl 预测矩阵（date × instrument）
  results/exps/<name>_<pool>/label.pkl 标签矩阵（同口径，评估用）
  results/exps/<name>_<pool>/meta.json 训练摘要
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


def label_expr(h: int) -> list:
    return [f"Ref($close, -{h + 1})/Ref($close, -1) - 1"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", default="hs300")
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--label-days", type=int, default=20)
    ap.add_argument("--ndcg", type=int, default=50)
    ap.add_argument("--mode", default="rank", choices=["rank", "bin"],
                    help="rank=lambdarank 排序学习；bin=每日截面 top/bottom 30% 二分类")
    ap.add_argument("--name", default="ranker")
    ap.add_argument("--num-leaves", type=int, default=128)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--rounds", type=int, default=800)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    import qlib
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.contrib.data.handler import Alpha158

    cfg = yaml.safe_load(Path(args.yaml).read_text(encoding="utf-8"))
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn")
    hc = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    label = label_expr(args.label_days)

    t0 = time.time()
    handler = Alpha158(
        instruments=hc["instruments"],
        start_time=hc["start_time"],
        end_time=hc["end_time"],
        fit_start_time=hc["fit_start_time"],
        fit_end_time=hc["fit_end_time"],
        learn_processors=[
            {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
            {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
             "kwargs": {"fields_group": "label"}},
        ],
        label=label,
    )
    seg = cfg["task"]["dataset"]["kwargs"]["segments"]
    train_seg = (seg["train"][0], seg["train"][1])
    valid_seg = (seg["valid"][0], seg["valid"][1])
    test_seg = (seg["test"][0], seg["test"][1])

    # 特征/标签（DK_L=learn 处理器：标签已截面 z-score）
    data = handler.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    feat = data["feature"]
    lab = data["label"].iloc[:, 0]
    df = feat.join(lab.rename("y"))
    df_tr = df.loc[(df.index.get_level_values(0) >= train_seg[0]) & (df.index.get_level_values(0) < train_seg[1])]
    df_va = df.loc[(df.index.get_level_values(0) >= valid_seg[0]) & (df.index.get_level_values(0) < valid_seg[1])]
    df_te = df.loc[(df.index.get_level_values(0) >= test_seg[0]) & (df.index.get_level_values(0) < test_seg[1])]
    print(f"数据就绪 {time.time()-t0:.0f}s | train {df_tr.shape} valid {df_va.shape} test {df_te.shape}")

    feats = [c for c in df.columns if c != "y"]
    X_tr, y_tr = df_tr[feats], df_tr["y"]
    X_va, y_va = df_va[feats], df_va["y"]
    X_te = df_te[feats]

    def quantize_daily(y: pd.Series, n_buckets=20) -> pd.Series:
        """每个日期截面内按排名切成 0..n_buckets-1 整数标签（lambdarank 需要整数）。"""
        df_ = y.to_frame("y")
        df_["dt"] = df_.index.get_level_values(0)
        out = []
        for dt, g in df_.groupby("dt"):
            pct = g["y"].rank(pct=True)
            g["yb"] = (pct * n_buckets).astype(int).clip(upper=n_buckets - 1)
            out.append(g["yb"])
        return pd.concat(out)

    def binarize_daily(y: pd.Series, keep=0.3) -> pd.Series:
        """每日截面：top 30% = 1，bottom 30% = 0，中间丢弃（NaN）。"""
        df_ = y.to_frame("y")
        df_["dt"] = df_.index.get_level_values(0)
        out = []
        for dt, g in df_.groupby("dt"):
            pct = g["y"].rank(pct=True)
            g["yb"] = pd.NA
            g.loc[pct >= 1 - keep, "yb"] = 1
            g.loc[pct <= keep, "yb"] = 0
            out.append(g["yb"].dropna())
        return pd.concat(out)

    if args.mode == "bin":
        y_tr = binarize_daily(y_tr)
        y_va = binarize_daily(y_va)
    else:
        y_tr = quantize_daily(y_tr)
        y_va = quantize_daily(y_va)
    # 按日期排序（组连续性）
    X_tr = X_tr.sort_index()
    y_tr = y_tr.loc[X_tr.index]
    X_va = X_va.sort_index()
    y_va = y_va.loc[X_va.index]
    X_te = X_te.sort_index()
    tr_dates = X_tr.index.get_level_values(0)
    va_dates = X_va.index.get_level_values(0)
    groups_tr = tr_dates.value_counts().sort_index().values
    groups_va = va_dates.value_counts().sort_index().values

    import lightgbm as lgb
    if args.mode == "bin":
        params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": args.num_leaves,
            "learning_rate": args.lr,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "min_child_samples": 100,
            "lambda_l1": 0.1,
            "lambda_l2": 1.0,
            "max_depth": -1,
            "seed": args.seed,
            "num_threads": 20,
            "verbosity": -1,
        }
        d_tr = lgb.Dataset(X_tr, label=y_tr)
        d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    else:
        params = {
            "objective": "lambdarank",
            "metric": "ndcg",
            "ndcg_eval_at": [args.ndcg],
            "boosting_type": "gbdt",
            "num_leaves": args.num_leaves,
            "learning_rate": args.lr,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "min_child_samples": 100,
            "lambda_l1": 0.1,
            "lambda_l2": 1.0,
            "max_depth": -1,
            "seed": args.seed,
            "num_threads": 20,
            "verbosity": -1,
        }
        d_tr = lgb.Dataset(X_tr, label=y_tr, group=groups_tr)
        d_va = lgb.Dataset(X_va, label=y_va, group=groups_va, reference=d_tr)
    t0 = time.time()
    model = lgb.train(params, d_tr, num_boost_round=args.rounds,
                      valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(100), lgb.log_evaluation(100)])
    print(f"训练完成 {time.time()-t0:.0f}s best_iter={model.best_iteration}")

    score = model.predict(X_te, num_iteration=model.best_iteration)
    pred = pd.DataFrame({"score": score}, index=X_te.index)
    label_te = df_te["y"].loc[pred.index]
    out = EXPS / f"{args.name}_{args.pool}"
    out.mkdir(parents=True, exist_ok=True)
    pred.to_pickle(out / "pred_matrix.pkl")
    label_te.to_pickle(out / "label_matrix.pkl")
    meta = {
        "name": f"{args.name}_{args.pool}", "pool": args.pool,
        "label_days": args.label_days, "ndcg": args.ndcg,
        "num_leaves": args.num_leaves, "lr": args.lr, "rounds": args.rounds,
        "best_iter": model.best_iteration,
        "valid_ndcg": {str(k): v for k, v in model.best_score.get("valid_0", {}).items()},
        "train_shape": list(df_tr.shape), "valid_shape": list(df_va.shape),
        "test_shape": list(df_te.shape), "ts": datetime.now().isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    # 直接计算测试段 RankIC（原始连续标签）
    from scipy import stats
    ics = []
    for dt, g in pred.groupby(level=0):
        if len(g) < 30:
            continue
        ics.append(stats.spearmanr(g["score"], label_te.loc[g.index])[0])
    ics = np.array([x for x in ics if np.isfinite(x)])
    print(f"测试段 RankIC: mean={ics.mean():.4f} std={ics.std():.4f} ICIR={ics.mean()/ics.std():.3f} n={ics.size}")
    print(f"产物目录: {out}")


if __name__ == "__main__":
    main()
