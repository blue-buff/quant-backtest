"""诚实评估脚本：对 pred.pkl / label.pkl 计算预测力与稳定性指标。

指标（全部基于样本外测试段）：
- IC / ICIR（皮尔逊，日频序列）；RankIC / RankICIR（斯皮尔曼）
- 非重叠 IC（多日标签按 stride=h 取日，避免重叠样本虚增 ICIR）
- hit rate：方向命中率（score 与 label 符号一致比例，跨日汇总）
- 成对正确率：同截面股票对排序正确的比例（=(Kendall tau+1)/2）
- 月度 IC 表：每个月的 IC 与样本天数（稳定性证据）
- 分位单调性：按 score 日截面 10 等分，各分位平均 label，Spearman(分位, 平均label)
- bootstrap 95% CI + p 值（H0: 非重叠 IC 均值 <= 0，10000 次）
- 大盘情景拆分：不同市场阶段（涨/跌/震荡）的 IC

用法:
  python scripts/eval_pred.py --run-dir qlib_examples/mlruns/<exp>/<run_id> [--run-dir ...] [--h 1]
  多个 run-dir 时 score 取平均（多种子集成评估），label 用第一个。
输出: results/eval/<name>.json + 控制台 markdown 表
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent


def load_pred_label(run_dirs, h=1):
    """长表 pkl → 宽表（date × instrument）。

    多 run 语义：每个 run 覆盖不同（或相同）日期段的预测，
    score 按日期分组取均值（跳过 NaN），label 取全部窗口并集（重复日期保留最后一个）。
    """
    preds, labels = [], []
    for rd in run_dirs:
        rd = Path(rd)
        pred = pd.read_pickle(rd / "artifacts" / "pred.pkl")
        lab = pd.read_pickle(rd / "artifacts" / "label.pkl")
        if not isinstance(pred.index, pd.MultiIndex) or "instrument" not in pred.index.names:
            raise ValueError(f"pred.pkl 结构异常: {rd} index={pred.index.names}")
        preds.append(pred["score"].unstack("instrument"))
        labels.append(lab.iloc[:, 0].unstack("instrument"))
    cat_p = pd.concat(preds)
    cat_l = pd.concat(labels)
    score = cat_p.groupby(level=0).mean()  # 同日多 run 平均（同 run 同日不重复）
    label = cat_l[~cat_l.index.duplicated(keep="last")]
    common_idx = score.index.intersection(label.index)
    common_cols = score.columns.intersection(label.columns)
    score = score.loc[common_idx, common_cols]
    label = label.loc[common_idx, common_cols]
    return score, label


def per_day_stats(score, label, min_n=20):
    """返回 DataFrame: date, n, ic, rank_ic, tau, hit_rate"""
    rows = []
    for dt in score.index:
        s = score.loc[dt].to_numpy(float)
        l = label.loc[dt].to_numpy(float)
        mask = np.isfinite(s) & np.isfinite(l)
        if mask.sum() < min_n:
            continue
        s, l = s[mask], l[mask]
        ic = stats.pearsonr(s, l)[0] if np.std(s) > 0 and np.std(l) > 0 else np.nan
        ric = stats.spearmanr(s, l)[0] if np.unique(s).size > 1 else np.nan
        tau, _ = stats.kendalltau(s, l)
        sign_s = s - np.mean(s)
        hit = float(np.mean(np.sign(sign_s) == np.sign(l)))  # 标签 z-score 后零为中心
        rows.append({"date": dt, "n": mask.sum(), "ic": ic, "rank_ic": ric,
                     "tau": tau, "hit_rate": hit})
    return pd.DataFrame(rows)


def decile_monotonicity(score, label, q=10):
    """每日截面按 score 百分位切固定 q 桶（桶 0=最低分，q-1=最高分），
    平均 label 后跨日取均值。top_minus_bottom = 最高分桶 - 最低分桶。"""
    mat = np.full((score.shape[0], q), np.nan)
    n_ok = 0
    for i, dt in enumerate(score.index):
        s = score.loc[dt].to_numpy(float)
        l = label.loc[dt].to_numpy(float)
        mask = np.isfinite(s) & np.isfinite(l)
        if mask.sum() < q * 3:
            continue
        s, l = s[mask], l[mask]
        pct = pd.Series(s).rank(pct=True).to_numpy()
        bucket = np.minimum((pct * q).astype(int), q - 1)
        for b in range(q):
            if (bucket == b).sum() > 0:
                mat[i, b] = np.mean(l[bucket == b])
        n_ok += 1
    if n_ok < 5:
        return None
    dec = np.nanmean(mat, axis=0)
    top_minus_bottom = float(dec[-1] - dec[0])
    top_gt_bottom_days = float(np.nanmean(mat[:, -1] > mat[:, 0])) if n_ok else None
    mono, _ = stats.spearmanr(np.arange(len(dec)), dec)
    return {"decile_means": [float(x) for x in dec], "top_minus_bottom": top_minus_bottom,
            "monotonicity_spearman": float(mono), "top_gt_bottom_day_frac": top_gt_bottom_days,
            "n_days": int(n_ok)}


def bootstrap_ci(x, n_boot=10000, seed=42):
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(x, size=x.size, replace=True).mean()
    lo, hi = np.quantile(means, [0.025, 0.975])
    p_le0 = float(np.mean(means <= 0))
    return {"mean": float(x.mean()), "ci95_lo": float(lo), "ci95_hi": float(hi), "p_le0": p_le0}


def regime_table(daily, h=1):
    """按季度拆分 IC（不同大盘情景的证据）。"""
    df = daily.copy()
    df["q"] = df["date"].dt.to_period("Q").astype(str)
    out = []
    for q, g in df.groupby("q"):
        out.append({"quarter": q, "n_days": int(len(g)), "ic": float(g["ic"].mean()),
                    "rank_ic": float(g["rank_ic"].mean()), "hit_rate": float(g["hit_rate"].mean())})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", type=str, action="append", dest="run_dirs", required=True)
    ap.add_argument("--h", type=int, default=1, help="标签周期（重叠样本 stride）")
    ap.add_argument("--universe-file", type=str, default=None,
                    help="只评估该文件列出的股票（每行一个 SH600000 格式代码）")
    ap.add_argument("--name", type=str, default=None)
    ap.add_argument("--out-json", type=str, default=None)
    args = ap.parse_args()
    name = args.name or Path(args.run_dirs[0]).parent.name + "_" + Path(args.run_dirs[0]).name[:8]

    score, label = load_pred_label(args.run_dirs, args.h)
    if args.universe_file:
        codes = {ln.strip() for ln in open(args.universe_file, encoding="utf-8") if ln.strip()}
        keep = [c for c in score.columns if c in codes]
        score = score[keep]
        label = label[keep]
        print(f"universe 过滤后: {len(keep)} 只（{args.universe_file}）")
    print(f"样本矩阵: {score.shape[0]} 天 × {score.shape[1]} 只股票（run_dirs={len(args.run_dirs)}）")

    daily = per_day_stats(score, label)
    # 非重叠：从第一个日期起按 stride=h 取
    daily_ng = daily.iloc[:: args.h] if args.h > 1 else daily
    result = {
        "name": name, "h": args.h, "n_days": int(len(daily)),
        "mean_ic": float(daily["ic"].mean()), "ic_std": float(daily["ic"].std()),
        "icir": float(daily["ic"].mean() / daily["ic"].std()) if daily["ic"].std() > 0 else None,
        "mean_rank_ic": float(daily["rank_ic"].mean()),
        "rank_ic_std": float(daily["rank_ic"].std()),
        "rank_icir": float(daily["rank_ic"].mean() / daily["rank_ic"].std()) if daily["rank_ic"].std() > 0 else None,
        "hit_rate": float(daily["hit_rate"].mean()),
        "mean_tau": float(daily["tau"].mean()),
        "pairwise_acc": float((daily["tau"].mean() + 1) / 2),
        "n_nonoverlap": int(len(daily_ng)),
        "nonoverlap_mean_ic": float(daily_ng["ic"].mean()),
        "nonoverlap_icir": float(daily_ng["ic"].mean() / daily_ng["ic"].std()) if daily_ng["ic"].std() > 0 else None,
        "nonoverlap_mean_rank_ic": float(daily_ng["rank_ic"].mean()),
        "nonoverlap_rank_icir": float(daily_ng["rank_ic"].mean() / daily_ng["rank_ic"].std()) if daily_ng["rank_ic"].std() > 0 else None,
    }
    dec = decile_monotonicity(score, label)
    result["deciles"] = dec
    result["bootstrap_ic"] = bootstrap_ci(daily_ng["ic"].to_numpy())
    result["bootstrap_rankic"] = bootstrap_ci(daily_ng["rank_ic"].to_numpy())
    result["quarters"] = regime_table(daily, args.h)
    # 月度 IC
    m = daily.copy()
    m["ym"] = m["date"].dt.to_period("M").astype(str)
    result["monthly_ic"] = [{"month": k, "ic": float(v["ic"].mean()), "n": int(len(v))}
                            for k, v in m.groupby("ym")]

    out = args.out_json or str(ROOT / "results" / "eval" / f"{name}.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n== {name} ==（h={args.h}, 日频 {result['n_days']} 天, 非重叠 {result['n_nonoverlap']} 天）")
    print(f"IC      mean={result['mean_ic']:.4f}  std={result['ic_std']:.4f}  ICIR={result['icir']:.3f}")
    print(f"RankIC  mean={result['mean_rank_ic']:.4f}  std={result['rank_ic_std']:.4f}  RankICIR={result['rank_icir']:.3f}")
    print(f"非重叠   IC={result['nonoverlap_mean_ic']:.4f} (ICIR {result['nonoverlap_icir']:.3f})  "
          f"RankIC={result['nonoverlap_mean_rank_ic']:.4f} (RankICIR {result['nonoverlap_rank_icir']:.3f})")
    print(f"hit_rate={result['hit_rate']:.4f}  成对正确率={result['pairwise_acc']:.4f}")
    b = result["bootstrap_rankic"]
    print(f"bootstrap RankIC 95% CI=[{b['ci95_lo']:.4f}, {b['ci95_hi']:.4f}]  p(<=0)={b['p_le0']:.4f}")
    if dec:
        print(f"分位单调性 Spearman={dec['monotonicity_spearman']:.3f}  top-bottom={dec['top_minus_bottom']*100:.3f}%/日")
        print("分位均值:", " ".join(f"{x*100:.3f}" for x in dec["decile_means"]))
    print("\n季度 IC:")
    for q in result["quarters"]:
        print(f"  {q['quarter']}: 天={q['n_days']}  IC={q['ic']:+.4f}  RankIC={q['rank_ic']:+.4f}  hit={q['hit_rate']:.3f}")
    print(f"\nJSON: {out}")


if __name__ == "__main__":
    main()
