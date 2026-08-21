"""矩阵评估：远程全市场预测（long-format pkl）的完整评估。

支持多 pred 平均（集成）、universe 子集过滤、季度分解、bootstrap、分位单调性。
用法:
  python scripts/eval_matrix.py --pred results/remote/e17r_seed42_pred.pkl \
      --pred results/remote/all10d_seed100_pred.pkl \
      --label results/remote/e17r_label.pkl \
      --pool hs300 --h 10 --name all10d_ens3_hs300
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pred", action="append", dest="preds", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--pool", default="hs300", choices=["hs300", "zz500", "all"])
    ap.add_argument("--h", type=int, default=10)
    ap.add_argument("--name", default="matrix_eval")
    args = ap.parse_args()

    from scipy import stats

    preds = [pd.read_pickle(p) for p in args.preds]
    label = pd.read_pickle(args.label)
    if not isinstance(label.index, pd.MultiIndex):
        label = label.iloc[:, 0] if isinstance(label, pd.DataFrame) else label
    score = pd.concat(preds).groupby(level=[0, 1]).mean()
    # 对齐
    common = score.index.intersection(label.index)
    score = score.loc[common]
    label = label.loc[common]
    if args.pool != "all":
        f = Path("/root/.qlib/qlib_data/cn_data/instruments/csi300.txt" if args.pool == "hs300"
                 else "/root/.qlib/qlib_data/cn_data_zz500/instruments/csi500.txt")
        codes = {ln.split()[0] for ln in f.read_text().splitlines() if ln.strip()}
        inst = score.index.get_level_values(1)
        mask = inst.isin(codes)
        score = score[mask]
        label = label.loc[score.index]
    s_arr = score.iloc[:, 0].to_numpy() if isinstance(score, pd.DataFrame) else score.to_numpy()
    l_arr = label.iloc[:, 0].to_numpy(float) if isinstance(label, pd.DataFrame) else label.to_numpy(float)
    df = pd.DataFrame({"s": s_arr, "l": l_arr}, index=score.index)
    rows = []
    for dt, g in df.groupby(level=0):
        if len(g) < 20:
            continue
        g = g.dropna()
        if len(g) < 20:
            continue
        ric = stats.spearmanr(g["s"], g["l"])[0]
        if np.isfinite(ric):
            rows.append({"date": dt, "ric": ric, "n": len(g)})
    daily = pd.DataFrame(rows).set_index("date")
    non = daily.iloc[:: args.h]
    mean = daily["ric"].mean()
    std = daily["ric"].std()
    mean_n = non["ric"].mean()
    std_n = non["ric"].std()
    # bootstrap（非重叠序列）
    rng = np.random.default_rng(42)
    arr = non["ric"].to_numpy()
    means = np.empty(10000)
    for i in range(10000):
        means[i] = rng.choice(arr, size=arr.size, replace=True).mean()
    lo, hi = np.quantile(means, [0.025, 0.975])
    p_le0 = float(np.mean(means <= 0))
    # 季度
    q = daily.copy()
    q["q"] = q.index.to_period("Q").astype(str)
    quarters = {k: {"n": int(len(g)), "rankic": float(g["ric"].mean())} for k, g in q.groupby("q")}
    # 分位（前1/5 vs 后1/5）
    top_bot = []
    for dt, g in df.groupby(level=0):
        g = g.dropna()
        if len(g) < 30:
            continue
        top = g.nlargest(max(5, len(g) // 5), "s")["l"].mean()
        bot = g.nsmallest(max(5, len(g) // 5), "s")["l"].mean()
        top_bot.append(top - bot)
    out = {"name": args.name, "pool": args.pool, "h": args.h,
           "n_days": int(len(daily)), "n_nonoverlap": int(len(non)),
           "rankic_mean": float(mean), "rankic_std": float(std),
           "rankic_ir": float(mean / std) if std > 0 else None,
           "nonoverlap_rankic_mean": float(mean_n),
           "nonoverlap_rankic_ir": float(mean_n / std_n) if std_n > 0 else None,
           "bootstrap": {"ci95": [float(lo), float(hi)], "p_le0": p_le0},
           "quarters": quarters,
           "top_minus_bottom_mean": float(np.mean(top_bot)) if top_bot else None,
           "top_gt_bottom_frac": float(np.mean(np.array(top_bot) > 0)) if top_bot else None}
    out_path = ROOT / "results" / "eval" / f"{args.name}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
