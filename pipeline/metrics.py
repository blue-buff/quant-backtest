"""Core metrics for trained models (design doc §4).

Logic mirrors scripts/eval_pred.py (legacy-proven): per-day Pearson IC + Spearman
RankIC on the out-of-sample segment, hit rate, decile monotonicity, day-level
bootstrap (H0: mean non-overlap RankIC <= 0), quarterly breakdown.

Core 5 (auto-computed every run): rankic{mean,std,ir,p_value}, hit{rate,top_bottom_mean},
meta, conclusion. CLI: compute (full json from pred/label pkls).
"""
import argparse, json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def load_wide(pred_path, label_path):
    """Long pkl (score column / label column) -> wide date x instrument."""
    pred = pd.read_pickle(pred_path)
    lab = pd.read_pickle(label_path)
    if not isinstance(pred.index, pd.MultiIndex):
        raise ValueError("pred.pkl index must be (date, instrument)")
    score = pred["score"].unstack("instrument")
    label = lab.iloc[:, 0].unstack("instrument")
    label = label[~label.index.duplicated(keep="last")]
    common_idx = score.index.intersection(label.index)
    common_cols = score.columns.intersection(label.columns)
    score = score.loc[common_idx, common_cols]
    label = label.loc[common_idx, common_cols]
    return score, label


def per_day_stats(score, label, min_n=20):
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
        sign_s = s - np.mean(s)
        hit = float(np.mean(np.sign(sign_s) == np.sign(l)))
        rows.append({"date": dt, "n": int(mask.sum()), "ic": ic, "rank_ic": ric,
                     "hit_rate": hit})
    return pd.DataFrame(rows)


def decile_monotonicity(score, label, q=10):
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
    mono, _ = stats.spearmanr(np.arange(len(dec)), dec)
    return {"decile_means": [float(x) for x in dec],
            "top_minus_bottom": float(dec[-1] - dec[0]),
            "monotonicity_spearman": float(mono),
            "top_gt_bottom_day_frac": float(np.nanmean(mat[:, -1] > mat[:, 0])),
            "n_days": int(n_ok)}


def bootstrap_ci(x, n_boot=10000, seed=42):
    x = np.asarray(x, dtype=float)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(x, size=x.size, replace=True).mean()
    lo, hi = np.quantile(means, [0.025, 0.975])
    return {"mean": float(x.mean()), "ci95_lo": float(lo), "ci95_hi": float(hi),
            "p_le0": float(np.mean(means <= 0))}


def compute_full(pred_path, label_path, h=10):
    """Full metrics dict from long pred/label pkls. h = label horizon (non-overlap stride)."""
    score, label = load_wide(pred_path, label_path)
    daily = per_day_stats(score, label)
    daily_ng = daily.iloc[::h] if h > 1 else daily
    out = {
        "h": int(h),
        "n_days": int(len(daily)),
        "n_inst": int(score.shape[1]),
        "sample_window": [str(score.index.min())[:10], str(score.index.max())[:10]],
        "mean_ic": float(daily["ic"].mean()),
        "ic_std": float(daily["ic"].std()),
        "icir": float(daily["ic"].mean() / daily["ic"].std()) if daily["ic"].std() > 0 else None,
        "mean_rank_ic": float(daily["rank_ic"].mean()),
        "rank_ic_std": float(daily["rank_ic"].std()),
        "rank_icir": float(daily["rank_ic"].mean() / daily["rank_ic"].std()) if daily["rank_ic"].std() > 0 else None,
        "hit_rate": float(daily["hit_rate"].mean()),
        "n_nonoverlap": int(len(daily_ng)),
        "nonoverlap_mean_rank_ic": float(daily_ng["rank_ic"].mean()),
        "nonoverlap_rank_icir": float(daily_ng["rank_ic"].mean() / daily_ng["rank_ic"].std())
        if daily_ng["rank_ic"].std() > 0 else None,
        "bootstrap_rankic": bootstrap_ci(daily_ng["rank_ic"].to_numpy()),
        "bootstrap_ic": bootstrap_ci(daily_ng["ic"].to_numpy()),
        "deciles": decile_monotonicity(score, label),
    }
    m = daily.copy()
    m["ym"] = m["date"].dt.to_period("M").astype(str)
    out["monthly_ic"] = [{"month": k, "ic": float(v["ic"].mean()), "n": int(len(v))}
                         for k, v in m.groupby("ym")]
    m["q"] = m["date"].dt.to_period("Q").astype(str)
    out["quarters"] = [{"quarter": k, "n_days": int(len(v)), "ic": float(v["ic"].mean()),
                        "rank_ic": float(v["rank_ic"].mean()), "hit_rate": float(v["hit_rate"].mean())}
                       for k, v in m.groupby("q")]
    return out


def core_metrics(full, exp_id, run_id="", data_version="v3", expectation=None):
    """Design doc §4.1 core-5 shape."""
    text = "RankIC %.4f (p<=0: %.3f, n=%d non-overlap days)" % (
        full["nonoverlap_mean_rank_ic"], full["bootstrap_rankic"]["p_le0"],
        full["n_nonoverlap"])
    check = "n/a"
    if isinstance(expectation, dict):
        ok = True
        if "rankic_mean_min" in expectation and full["nonoverlap_mean_rank_ic"] < expectation["rankic_mean_min"]:
            ok = False
        if "p_le0_max" in expectation and full["bootstrap_rankic"]["p_le0"] > expectation["p_le0_max"]:
            ok = False
        check = "met" if ok else "not_met"
    return {
        "meta": {"exp_id": exp_id, "run_id": run_id, "data_version": data_version,
                 "sample_window": full["sample_window"], "n_days": full["n_days"],
                 "n_inst": full["n_inst"]},
        "rankic": {"mean": full["nonoverlap_mean_rank_ic"], "std": full["rank_ic_std"],
                   "ir": full["nonoverlap_rank_icir"], "p_value": full["bootstrap_rankic"]["p_le0"]},
        "hit": {"rate": full["hit_rate"],
                "top_bottom_mean": (full["deciles"] or {}).get("top_minus_bottom")},
        "conclusion": {"text": text, "expectation_check": check},
    }


def main():
    ap = argparse.ArgumentParser(prog="pipeline.metrics")
    ap.add_argument("--pred", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--h", type=int, default=10)
    ap.add_argument("--name", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    full = compute_full(a.pred, a.label, a.h)
    out = a.out or (str(Path(a.pred).parent / "metrics.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({"rankic_mean": full["nonoverlap_mean_rank_ic"],
                      "rankic_ir": full["nonoverlap_rank_icir"],
                      "p_le0": full["bootstrap_rankic"]["p_le0"],
                      "n_days": full["n_days"], "n_nonoverlap": full["n_nonoverlap"],
                      "hit_rate": full["hit_rate"], "out": out},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
