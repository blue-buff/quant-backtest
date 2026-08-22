"""事后验证：10 个交易日后，用真实收益对照前瞻预测。

用法（2026-09-07 之后）:
  python scripts/verify_future.py --pred results/future/pred_future.pkl \
      --label <真实 10 日收益 pkl/long csv> --top 50

输出：前瞻样本外 RankIC（全市场/300/500）、hit rate、Top50 平均收益 vs 全市场基准。
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pred", required=True, help="predict_future.py 输出的 pred_future.pkl（含 score）")
    ap.add_argument("--label", required=True, help="真实标签：long-format csv(date,instrument,ret10) 或 pkl")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    from scipy import stats

    pred = pd.read_pickle(args.pred)
    p = args.label
    if p.endswith(".pkl"):
        lab = pd.read_pickle(p)
        if isinstance(lab, pd.DataFrame) and "ret10" in lab.columns:
            lab = lab[["ret10"]]
        lab = lab.iloc[:, 0]
    else:
        lab = pd.read_csv(p, dtype={"date": str})
        lab.index = pd.MultiIndex.from_arrays([lab["date"], lab["instrument"]])
        lab = lab["ret10"]
    common = pred.index.intersection(lab.index)
    df = pd.DataFrame({"s": pred.loc[common, "score"], "l": lab.loc[common].to_numpy(float)})
    ric = stats.spearmanr(df["s"], df["l"])[0]
    hit = float(np.mean(np.sign(df["s"] - df["s"].mean()) == np.sign(df["l"])))
    top = df.nlargest(args.top, "s")
    bench = float(df["l"].mean())
    print(f"前瞻验证结果（{len(df)} 只，{args.top} 名单）")
    print(f"  RankIC = {ric:.4f}")
    print(f"  hit rate = {hit:.4f}")
    print(f"  Top{args.top} 平均 10 日收益 = {top['l'].mean()*100:.2f}%  全市场均值 = {bench*100:.2f}%  超额 = {(top['l'].mean()-bench)*100:.2f}%")
    out = Path("results/future/verify_result.json")
    out.write_text(json.dumps({"rankic": ric, "hit": hit, "top_mean": float(top["l"].mean()),
                               "bench_mean": float(bench)}, indent=2), encoding="utf-8")
    print("已写入", out)


if __name__ == "__main__":
    main()
