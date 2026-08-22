"""Fixed tester (P5): the ONLY component that computes ledger metrics.

Runs on pred/label pkl pairs produced by executors + the pipeline data layer.
regression: per-day Pearson IC + Spearman RankIC, hit rate, decile monotonicity,
day-level bootstrap (H0: mean non-overlap RankIC <= 0), quarterly breakdown.
classification: per-day rank-based AUC, bootstrap (H0: mean AUC <= 0.5).
core_metrics: design doc core shape + generic expectation check.

CLI: python -m pipeline.metrics --pred <pkl> --label <pkl> --h <n> [--task cls]
"""
import argparse, json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def load_wide(pred_path, label_path):
    """Long pkl (score column / label column) -> wide date x instrument.
    Accepts Series or DataFrame for either file."""
    pred = pd.read_pickle(pred_path)
    lab = pd.read_pickle(label_path)
    if not isinstance(pred.index, pd.MultiIndex):
        raise ValueError("pred.pkl index must be (date, instrument)")
    score = pred["score"] if hasattr(pred, "columns") else pred
    label = lab.iloc[:, 0] if hasattr(lab, "columns") else lab
    score = score.unstack("instrument")
    label = label.unstack("instrument")
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
    """Regression tester. h = label horizon (non-overlap stride)."""
    score, label = load_wide(pred_path, label_path)
    daily = per_day_stats(score, label)
    if len(daily) == 0:
        raise ValueError("no valid evaluation days (empty pred/label intersection?)")
    daily_ng = daily.iloc[::h] if h > 1 else daily
    out = {
        "task": "regression",
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


def per_day_auc(score, label, min_pos=5, min_neg=5):
    """Per-day rank-based AUC (Mann-Whitney U normalized). Top label value = positive."""
    rows = []
    for dt in score.index:
        s = score.loc[dt].to_numpy(float)
        l = label.loc[dt].to_numpy(float)
        mask = np.isfinite(s) & np.isfinite(l)
        if mask.sum() < (min_pos + min_neg):
            continue
        s, l = s[mask], l[mask]
        uniq = np.unique(l)
        if len(uniq) < 2:
            continue
        pos = l == uniq[-1]
        n_pos, n_neg = int(pos.sum()), int((~pos).sum())
        if n_pos < min_pos or n_neg < min_neg:
            continue
        r = stats.rankdata(s)
        auc = float((r[pos].mean() - (n_pos + 1) / 2) / n_neg)
        rows.append({"date": dt, "n": int(mask.sum()), "n_pos": n_pos, "auc": auc})
    return pd.DataFrame(rows)


def _bootstrap_auc(aucs, n_boot=10000, seed=42, h0=0.5):
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(aucs, size=aucs.size, replace=True).mean()
    lo, hi = np.quantile(means, [0.025, 0.975])
    return {"mean": float(aucs.mean()), "ci95_lo": float(lo), "ci95_hi": float(hi),
            "p_le05": float(np.mean(means <= h0))}


def compute_full_cls(pred_path, label_path):
    """Classification tester: per-day AUC + bootstrap (H0: mean AUC <= 0.5)."""
    score, label = load_wide(pred_path, label_path)
    daily = per_day_auc(score, label)
    if len(daily) == 0:
        raise ValueError("no valid classification days (empty pred/label intersection?)")
    aucs = daily["auc"].to_numpy()
    return {
        "task": "classification",
        "n_days": int(len(daily)),
        "n_inst": int(score.shape[1]),
        "sample_window": [str(score.index.min())[:10], str(score.index.max())[:10]],
        "mean_auc": float(aucs.mean()),
        "auc_std": float(aucs.std()),
        "auc_ir": float(aucs.mean() / aucs.std()) if aucs.std() > 0 else None,
        "bootstrap_auc": _bootstrap_auc(aucs),
        "mean_n_per_day": float(daily["n"].mean()),
    }


EXPECT_ALIASES = {
    "rankic_mean": "nonoverlap_mean_rank_ic",
    "rankic_ir": "nonoverlap_rank_icir",
    "rankic_std": "rank_ic_std",
    "p_le0": "bootstrap_rankic.p_le0",
    "ic": "mean_ic",
    "hit_rate": "hit_rate",
    "auc_mean": "mean_auc",
    "auc_p_le05": "bootstrap_auc.p_le05",
}


def _lookup(full, metric):
    if metric in EXPECT_ALIASES:
        metric = EXPECT_ALIASES[metric]
    node = full
    for part in metric.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def _check_expectation(full, expectation):
    """Generic expectation check. Forms:
    {"rankic_mean_min": 0.05, "p_le0_max": 0.05}          (legacy)
    {"metric": "rankic_mean", "min": 0.05}                (generic, dotted paths ok)
    [{"metric": ..., "min": ...}, ...]                    (list of generic checks)"""
    if not expectation:
        return "n/a"
    checks = []
    if isinstance(expectation, dict):
        for k, v in expectation.items():
            if k.endswith("_min"):
                checks.append({"metric": k[:-4], "min": v})
            elif k.endswith("_max"):
                checks.append({"metric": k[:-4], "max": v})
            elif k == "metric":
                checks.append(expectation)
    elif isinstance(expectation, list):
        checks = [c for c in expectation if isinstance(c, dict)]
    if not checks:
        return "n/a"
    ok = True
    for c in checks:
        val = _lookup(full, str(c.get("metric", "")))
        if val is None:
            ok = False
            continue
        if "min" in c and val < float(c["min"]):
            ok = False
        if "max" in c and val > float(c["max"]):
            ok = False
    return "met" if ok else "not_met"


def core_metrics(full, exp_id, run_id="", data_version="v3", expectation=None,
                 task="regression"):
    """Design doc core shape + conclusion vs expectation."""
    meta = {"exp_id": exp_id, "run_id": run_id, "data_version": data_version,
            "sample_window": full["sample_window"], "n_days": full["n_days"],
            "n_inst": full["n_inst"], "task": full.get("task", task)}
    check = _check_expectation(full, expectation)
    if full.get("task") == "classification":
        text = "AUC %.4f (p<=0.5: %.3f, n=%d days)" % (
            full["mean_auc"], full["bootstrap_auc"]["p_le05"], full["n_days"])
        return {"meta": meta,
                "auc": {"mean": full["mean_auc"], "ir": full["auc_ir"],
                        "p_value": full["bootstrap_auc"]["p_le05"]},
                "conclusion": {"text": text, "expectation_check": check}}
    text = "RankIC %.4f (p<=0: %.3f, n=%d non-overlap days)" % (
        full["nonoverlap_mean_rank_ic"], full["bootstrap_rankic"]["p_le0"],
        full["n_nonoverlap"])
    return {
        "meta": meta,
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
    ap.add_argument("--task", choices=["regression", "classification"], default="regression")
    ap.add_argument("--name", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.task == "classification":
        full = compute_full_cls(a.pred, a.label)
    else:
        full = compute_full(a.pred, a.label, a.h)
    out = a.out or (str(Path(a.pred).parent / "metrics.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2, default=str)
    if full.get("task") == "classification":
        print(json.dumps({"auc_mean": full["mean_auc"], "auc_p_le05": full["bootstrap_auc"]["p_le05"],
                          "n_days": full["n_days"], "out": out}, ensure_ascii=False))
    else:
        print(json.dumps({"rankic_mean": full["nonoverlap_mean_rank_ic"],
                          "rankic_ir": full["nonoverlap_rank_icir"],
                          "p_le0": full["bootstrap_rankic"]["p_le0"],
                          "n_days": full["n_days"], "n_nonoverlap": full["n_nonoverlap"],
                          "hit_rate": full["hit_rate"], "out": out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
