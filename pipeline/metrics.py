"""Fixed tester (P5): the ONLY component that computes ledger metrics.

Runs on pred/label pkl pairs produced by executors + the pipeline data layer.
regression: per-day Pearson IC + Spearman RankIC, hit rate, decile monotonicity,
day-level bootstrap (H0: mean non-overlap RankIC <= 0), quarterly breakdown.
classification: per-day rank-based AUC, bootstrap (H0: mean AUC <= 0.5).
core_metrics: design doc core shape + generic expectation check.

CLI: python -m pipeline.metrics --pred <pkl> --label <pkl> --h <n> [--task cls]
"""
import argparse, json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import norm


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
            "top_minus_universe": float(dec[-1] - np.nanmean(dec)),
            "monotonicity_spearman": float(mono),
            "top_gt_bottom_day_frac": float(np.nanmean(mat[:, -1] > mat[:, 0])),
            "n_days": int(n_ok)}


TESTER_BOOTSTRAP_SEED = 42  # fixed tester seed: pinned per-run by qlab.git


def bootstrap_ci(x, n_boot=10000, seed=TESTER_BOOTSTRAP_SEED, h0=None):
    """Day-level bootstrap of the mean. h0=None -> p_le0 (H0: mean<=0);
    h0=0.5 -> p_le05 (classification H0: mean<=0.5)."""
    x = np.asarray(x, dtype=float)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(x, size=x.size, replace=True).mean()
    lo, hi = np.quantile(means, [0.025, 0.975])
    out = {"mean": float(x.mean()), "ci95_lo": float(lo), "ci95_hi": float(hi)}
    if h0 is None:
        out["p_le0"] = float(np.mean(means <= 0))
    else:
        out["p_le05"] = float(np.mean(means <= h0))
    return out


def nonoverlap_offsets(daily, h):
    """Non-overlap RankIC at every sampling phase 0..h-1 (an offset is the day
    index mod h). A single phase can be lucky; report all phases + their min."""
    out = []
    for o in range(max(1, h)):
        sub = daily.iloc[o::h]["rank_ic"].dropna()
        if len(sub):
            out.append({"offset": o, "n": int(len(sub)),
                        "mean_rank_ic": float(sub.mean()),
                        "rank_icir": float(sub.mean() / sub.std()) if sub.std() > 0 else None})
    return out


def nonoverlap_min_rankic(daily, h):
    offs = nonoverlap_offsets(daily, h)
    means = [o["mean_rank_ic"] for o in offs if o["mean_rank_ic"] is not None]
    return float(min(means)) if means else None


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
        "coverage": float(daily["n"].mean() / score.shape[1]) if score.shape[1] else None,
        "nonoverlap_offsets": nonoverlap_offsets(daily, int(h)),
        "nonoverlap_min_rank_ic": nonoverlap_min_rankic(daily, int(h)),
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
    return bootstrap_ci(aucs, n_boot=n_boot, seed=seed, h0=h0)


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
    # P8 trade aliases (spec expectation: sharpe_min / excess_ann_min /
    # mdd_max / turnover_max / cost_drag_max)
    "sharpe": "backtest.sharpe",
    "excess_ann": "backtest.excess_ann",
    "mdd": "backtest.mdd",
    "turnover": "portfolio.turnover_mean",
    "cost_drag": "backtest.cost_drag_ann",
}

TRADE_FAMILIES = ("portfolio", "backtest", "attribution")
PRED_FAMILIES = ("rankic", "bootstrap", "deciles", "quarters", "hit")
KNOWN_METRICS = set(PRED_FAMILIES) | set(TRADE_FAMILIES)

# 成本模型默认（spec action.costs 可覆盖）：佣金双边万2.5、印花税卖出千1、滑点1bp
DEFAULT_COSTS = {"commission": 0.00025, "stamp": 0.001, "slippage": 0.0001}

PRED_SECTIONS = {
    "rankic": ("mean_ic", "ic_std", "icir", "mean_rank_ic", "rank_ic_std",
               "rank_icir", "nonoverlap_mean_rank_ic", "nonoverlap_rank_icir",
               "n_nonoverlap", "coverage", "nonoverlap_min_rank_ic",
               "nonoverlap_offsets"),
    "bootstrap": ("bootstrap_rankic", "bootstrap_ic"),
    "deciles": ("deciles",),
    "quarters": ("monthly_ic", "quarters"),
    "hit": ("hit_rate",),
}


def filter_pred(full, families):
    """Keep only the requested prediction sections (+ always meta + the trade
    sections the dispatcher appended). families=None = current behavior
    (everything). attribution needs bootstrap p, so selecting attribution
    auto-keeps the bootstrap section."""
    if families is None:
        return full
    keep = ["task", "h", "n_days", "n_inst", "sample_window", "n_nonoverlap",
            "portfolio_missing"]
    for f in families:
        if f in PRED_SECTIONS:
            keep += list(PRED_SECTIONS[f])
        if f in TRADE_FAMILIES:
            keep.append(f)
    if "attribution" in families and "bootstrap" not in families:
        keep += list(PRED_SECTIONS["bootstrap"])
    return {k: v for k, v in full.items() if k in keep}


def _wide_pkl(p, col=None):
    df = pd.read_pickle(p)
    if hasattr(df, "columns"):
        s = df[col] if col else df.iloc[:, 0]
    else:
        s = df
    return s.unstack("instrument")


def compute_portfolio(pred_path, pf_path):
    """Portfolio family: signal -> holdings quality. Daily series + means for
    turnover, HHI, n_held, weight_ic (daily cross-sectional Spearman of weights
    vs pred ranks), top-decile weight fraction, cash fraction."""
    pred = _wide_pkl(pred_path, "score")
    w = _wide_pkl(pf_path, "weight")
    common_idx = pred.index.intersection(w.index)
    common_cols = pred.columns.union(w.columns)
    pred = pred.reindex(index=common_idx, columns=common_cols)
    w = w.reindex(index=common_idx, columns=common_cols).fillna(0.0).clip(lower=0)
    rows = []
    prev = None
    n_univ = len(w.columns)
    for dt in w.index:
        wt = w.loc[dt]
        p = pred.loc[dt]
        held = wt > 0
        n_held = int(held.sum())
        cash = float(1 - wt.sum())
        tot = float(wt.sum())
        if prev is None:
            turnover = tot
            oneway_buy = tot
            oneway_sell = 0.0
        else:
            d = wt - prev
            turnover = float(d.abs().sum())
            oneway_buy = float(d.clip(lower=0).sum())
            oneway_sell = float(d.clip(upper=0).abs().sum())
        hhi = float((wt ** 2).sum()) / (tot * tot) if tot > 0 else 0.0
        top = wt.sort_values(ascending=False)
        top1 = float(top.head(1).sum() / tot) if tot > 0 else 0.0
        top5 = float(top.head(5).sum() / tot) if tot > 0 else 0.0
        top10 = float(top.head(10).sum() / tot) if tot > 0 else 0.0
        active_share = float(0.5 * (wt - (1.0 / n_univ if n_univ else 0.0)).abs().sum()) if n_univ else None
        mask = np.isfinite(p.to_numpy(float))
        if mask.sum() >= 5 and wt[mask].nunique() > 1:
            wic = float(stats.spearmanr(wt[mask].to_numpy(float),
                                        p[mask].to_numpy(float))[0])
        else:
            wic = np.nan
        if mask.sum() >= 20 and tot > 0:
            q = p[mask].rank(pct=True)
            topq = q >= 0.9
            tdf = float(wt[mask][topq].sum() / tot)
        else:
            tdf = np.nan
        rows.append({"date": dt, "turnover": turnover, "hhi": hhi,
                     "n_held": n_held, "cash_frac": cash, "weight_ic": wic,
                     "top_decile_weight_frac": tdf, "oneway_buy": oneway_buy,
                     "oneway_sell": oneway_sell, "top1_frac": top1,
                     "top5_frac": top5, "top10_frac": top10,
                     "active_share": active_share,
                     "rebalanced": float(turnover > 1e-12)})
        prev = wt
    df = pd.DataFrame(rows)
    out = {"n_days": int(len(df)), "n_inst": int(n_univ)}
    for k in ("turnover", "hhi", "n_held", "cash_frac", "weight_ic",
              "top_decile_weight_frac", "oneway_buy", "oneway_sell",
              "top1_frac", "top5_frac", "top10_frac", "active_share"):
        s = df[k]
        out[k + "_mean"] = float(s.mean()) if s.notna().any() else None
        out[k + "_std"] = float(s.std()) if s.notna().any() else None
        out[k + "_last"] = float(s.iloc[-1]) if len(s) and pd.notna(s.iloc[-1]) else None
    reb = df["rebalanced"].iloc[1:] if len(df) > 1 else df["rebalanced"]
    out["rebalance_day_frac"] = float(reb.mean()) if len(reb) else None
    return out


def _max_drawdown_duration(dd):
    """Longest stretch (in days) spent below the previous peak."""
    best = cur = 0
    for x in dd.to_numpy(dtype=float):
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def _quarterly_returns(frame, ann=252):
    out = []
    f = frame.copy()
    f["ym"] = f.index.to_period("Q").astype(str)
    for k, v in f.groupby("ym"):
        net_cum = float((1 + v["net"]).prod() - 1)
        bmk_cum = float((1 + v["benchmark"]).prod() - 1)
        out.append({"quarter": k, "net_total": net_cum,
                    "bmk_total": bmk_cum, "excess_total": net_cum - bmk_cum,
                    "vol": float(v["net"].std() * math.sqrt(ann)), "n": int(len(v))})
    return out


def compute_backtest(pf_path, prices_path, benchmark=None, costs=None, ann=252):
    """Backtest family: daily rebalance at close, effective next day.
    gross = sum_i w_{t,i} * r_{t->t+1,i}; costs = commission*both-side turnover +
    stamp*sells + slippage*turnover. Benchmark: instrument name from the price
    cache (sh000300/sh000905) or equal-weight universe (None)."""
    import math
    c = dict(DEFAULT_COSTS)
    if costs:
        c.update({k: float(v) for k, v in costs.items() if k in c})
    w = _wide_pkl(pf_path, "weight")
    px = pd.read_parquet(prices_path)
    close = px["close"].unstack("instrument")
    close = close.sort_index()
    w_orig_index = w.index
    w = w.reindex(index=close.index, columns=close.columns).fillna(0.0).clip(lower=0)
    bmk_col = None
    if benchmark and benchmark.upper() in close.columns:
        bmk_col = benchmark.upper()
    if bmk_col is not None:
        bmk = close[bmk_col].pct_change().shift(-1)  # 与组合同日口径：t -> t+1
        bmk_name = str(bmk_col)
    else:
        bmk = close.pct_change().mean(axis=1, skipna=True).shift(-1)
        bmk_name = "equal_weight_universe"
    r = close.pct_change().shift(-1)  # r at t = close_{t+1}/close_t - 1
    gross = (w * r).sum(axis=1)
    gross.iloc[-1] = np.nan  # 最后一个收盘日没有次日收益，必须丢弃（skipna 会把全 NaN 加出 0）
    turn = w.diff().abs().sum(axis=1)
    turn.iloc[0] = w.iloc[0].abs().sum()
    sell = (w.shift(1).fillna(0.0) - w).clip(lower=0).sum(axis=1)
    cost = (c["commission"] + c["slippage"]) * turn + c["stamp"] * sell
    net = gross - cost
    frame = pd.DataFrame({"gross": gross, "net": net, "cost": cost,
                          "turnover": turn, "sell": sell, "benchmark": bmk})
    frame = frame.dropna(subset=["gross"])
    # 只统计组合声明了权重的日子（测试窗）；之前的 0 仓位天数不算
    frame = frame.loc[frame.index.isin(w_orig_index)]
    if len(frame) == 0:
        raise ValueError("QLAB_BACKTEST_EMPTY: no overlapping days between portfolio and prices")

    def _sharpe(x):
        return float(x.mean() / x.std() * math.sqrt(ann)) if x.std() > 0 else 0.0

    def _mdd(cum):
        return float((cum - cum.cummax()).min())

    def _ann_ret(cum):
        if len(cum) == 0 or cum.iloc[-1] <= 0:
            return float("nan")
        return float(cum.iloc[-1] ** (ann / len(cum)) - 1)

    cum_net = (1 + frame["net"]).cumprod()
    cum_gross = (1 + frame["gross"]).cumprod()
    cum_bmk = (1 + frame["benchmark"]).cumprod()
    # audit (minor): a cumulative wealth that went <= 0 makes ann_ret NaN; say
    # so explicitly instead of silently showing "no data"
    wealth_negative = bool(len(cum_net) and (cum_net.min() <= 0))
    cov = float(frame["net"].cov(frame["benchmark"]))
    var = float(frame["benchmark"].var())
    beta = cov / var if var > 0 else None
    monthly = []
    m = frame.copy()
    m["ym"] = m.index.to_period("M").astype(str)
    for k, v in m.groupby("ym"):
        monthly.append({"month": k, "net": float(v["net"].mean()), "n": int(len(v))})

    net = frame["net"]
    bmk_ret = frame["benchmark"]
    active = net - bmk_ret
    ann_ret_net = _ann_ret(cum_net)
    ann_ret_bmk = _ann_ret(cum_bmk)
    ann_ret_gross = _ann_ret(cum_gross)
    alpha_ann = (ann_ret_net - beta * ann_ret_bmk) \
        if (beta is not None and not np.isnan(ann_ret_net) and not np.isnan(ann_ret_bmk)) else float("nan")
    downside = net.clip(upper=0)
    d_std = float(np.sqrt((downside ** 2).mean()))
    sortino = float(net.mean() / d_std * math.sqrt(ann)) if d_std > 0 else float("nan")
    mdd_val = _mdd(cum_net)
    calmar = float(ann_ret_net / abs(mdd_val)) if (mdd_val < 0 and not np.isnan(ann_ret_net)) else float("nan")
    dd_series = cum_net / cum_net.cummax() - 1.0
    mdd_duration_days = _max_drawdown_duration(dd_series)
    te = float(active.std() * math.sqrt(ann)) if active.std() > 0 else 0.0
    ir = float(active.mean() / active.std() * math.sqrt(ann)) if active.std() > 0 else float("nan")
    wins = net[net > 0]
    losses = net[net < 0]
    win_rate = float((net > 0).mean())
    profit_factor = float(wins.sum() / abs(losses.sum())) \
        if len(losses) and losses.sum() != 0 else (float("inf") if len(wins) else float("nan"))
    avg_win = float(wins.mean()) if len(wins) else float("nan")
    avg_loss = float(losses.mean()) if len(losses) else float("nan")
    worst_week = float(net.groupby(net.index.to_period("W")).sum().min())
    worst_month = float(net.groupby(net.index.to_period("M")).sum().min())
    q = net.to_numpy(dtype=float)
    var_95 = float(np.quantile(q, 0.05))
    cvar_95 = float(q[q <= var_95].mean())
    skew = float(stats.skew(q))
    kurt = float(stats.kurtosis(q, fisher=True))
    lo_tail = float(q[q <= np.quantile(q, 0.05)].mean())
    hi_tail = float(q[q >= np.quantile(q, 0.95)].mean())
    tail_ratio = float(hi_tail / abs(lo_tail)) if abs(lo_tail) > 0 else float("nan")
    roll_mdd_60d = float(dd_series.rolling(60, min_periods=1).min().min())
    gross_alpha = ann_ret_gross - ann_ret_bmk
    cost_drag_ann = ann_ret_gross - ann_ret_net
    cost_alpha_ratio = float(cost_drag_ann / gross_alpha) \
        if (not np.isnan(gross_alpha) and gross_alpha != 0) else float("nan")
    turnover_mean = float(frame["turnover"].mean())
    turnover_ann = float(turnover_mean * ann)
    expo = w.loc[frame.index].sum(axis=1)
    sharpe_val = _sharpe(net)
    sr0 = _sharpe(bmk_ret)
    n_periods = len(net)
    if n_periods >= 2:
        sk_f = float(stats.skew(q))
        ku_f = float(stats.kurtosis(q, fisher=False))
        denom = 1 - sk_f * sharpe_val + (ku_f - 1) / 4.0 * sharpe_val * sharpe_val
        psr = float(norm.cdf((sharpe_val - sr0) * math.sqrt(n_periods - 1) / math.sqrt(denom))) \
            if denom > 0 else None
    else:
        psr = None
    quarterly_returns = _quarterly_returns(frame, ann)

    # 执行延迟对照（close 成交）：lag=0 前视上界 / lag=1 默认 T 日收盘成交 /
    # lag=2 T+1 收盘成交；lag="open" 是 T+1 开盘成交（T 收盘 -> T+1 开盘）。
    # 成本不随 lag 移动（换手量近似不变）。open 列由扩展价格缓存提供。
    lag_table = []
    for lag in (0, 1, 2):
        r_lag = close.pct_change().shift(-lag)
        b_lag = (close[bmk_col].pct_change().shift(-lag) if bmk_col is not None
                 else close.pct_change().mean(axis=1, skipna=True).shift(-lag))
        g = (w * r_lag).sum(axis=1).where(r_lag.notna().any(axis=1))
        n_lag = g - cost
        f = pd.DataFrame({"net": n_lag, "benchmark": b_lag}).dropna(subset=["net"])
        f = f.loc[f.index.isin(w_orig_index)]
        if len(f) < 2:
            lag_table.append({"lag": lag, "n_days": int(len(f)),
                              "net_ann_ret": None, "excess_ann": None})
            continue
        cn = (1 + f["net"]).cumprod()
        cb = (1 + f["benchmark"]).cumprod()
        arn = _ann_ret(cn)
        arb = _ann_ret(cb)
        lag_table.append({"lag": lag, "n_days": int(len(f)),
                          "net_ann_ret": arn,
                          "excess_ann": (arn - arb) if not np.isnan(arn) else float("nan")})

    # The expanded cache stores raw prices plus the adjustment factor; hfq open
    # is therefore open_raw * factor, not a separately duplicated price column.
    if "open_raw" in px.columns and "factor" in px.columns:
        open_px = (px["open_raw"] * px["factor"]).unstack("instrument").reindex(
            index=close.index, columns=close.columns)
        r_open = open_px.shift(-1) / close - 1.0
        b_open = (open_px[bmk_col].shift(-1) / close[bmk_col] - 1.0
                  if bmk_col is not None
                  else open_px.mean(axis=1, skipna=True).shift(-1) /
                  close.mean(axis=1, skipna=True) - 1.0)
        g_open = (w * r_open).sum(axis=1).where(r_open.notna().any(axis=1))
        f_open = pd.DataFrame({"net": g_open - cost, "benchmark": b_open}).dropna(
            subset=["net"])
        f_open = f_open.loc[f_open.index.isin(w_orig_index)]
        if len(f_open) < 2:
            lag_table.append({"lag": "open", "n_days": int(len(f_open)),
                              "net_ann_ret": None, "excess_ann": None})
        else:
            cn_open = (1 + f_open["net"]).cumprod()
            cb_open = (1 + f_open["benchmark"]).cumprod()
            arn_open = _ann_ret(cn_open)
            arb_open = _ann_ret(cb_open)
            lag_table.append({"lag": "open", "n_days": int(len(f_open)),
                              "net_ann_ret": arn_open,
                              "excess_ann": (arn_open - arb_open)
                              if not np.isnan(arn_open) else float("nan")})

    out = {
        "n_days": int(len(frame)),
        "wealth_negative": wealth_negative,
        "sample_window": [str(frame.index.min())[:10], str(frame.index.max())[:10]],
        "total_return": float(cum_net.iloc[-1] - 1),
        "gross_total_return": float(cum_gross.iloc[-1] - 1),
        "bmk_total_return": float(cum_bmk.iloc[-1] - 1),
        "sharpe": sharpe_val,
        "psr": psr,
        "psr_benchmark_sharpe": sr0,
        "sortino": sortino,
        "calmar": calmar,
        "ann_ret": ann_ret_net,
        "ann_vol": float(net.std() * math.sqrt(ann)),
        "bmk_ann_ret": ann_ret_bmk,
        "mdd": mdd_val,
        "mdd_duration_days": mdd_duration_days,
        "rolling_mdd_60d": roll_mdd_60d,
        "gross_ann_ret": ann_ret_gross,
        "excess_ann": (ann_ret_net - ann_ret_bmk) if not np.isnan(ann_ret_net) else float("nan"),
        "alpha_ann": alpha_ann,
        "beta": beta,
        "information_ratio": ir,
        "tracking_error": te,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "cost_drag_ann": cost_drag_ann,
        "cost_alpha_ratio": cost_alpha_ratio,
        "worst_day": float(net.min()),
        "worst_week": worst_week,
        "worst_month": worst_month,
        "var_95": var_95,
        "cvar_95": cvar_95,
        "skewness": skew,
        "kurtosis": kurt,
        "tail_ratio": tail_ratio,
        "turnover_mean": turnover_mean,
        "turnover_ann": turnover_ann,
        "max_exposure": float(expo.max()),
        "min_exposure": float(expo.min()),
        "total_cost": float(frame["cost"].sum()),
        "cost_components": {
            "commission": float((c["commission"] * frame["turnover"]).sum()),
            "stamp": float((c["stamp"] * frame["sell"]).sum()),
            "slippage": float((c["slippage"] * frame["turnover"]).sum()),
        },
        "costs_used": c,
        "benchmark": bmk_name,
        "monthly": monthly,
        "quarterly_returns": quarterly_returns,
        "execution_lag": lag_table,
    }
    return out


def compute_attribution(full, pf, bt, task="regression"):
    """Attribution family: one verdict + number per layer, so a bad result can be
    blamed on the right layer instead of the signal by default."""
    if task == "classification":
        value = full.get("mean_auc")
        p = (full.get("bootstrap_auc") or {}).get("p_le05")
    else:
        value = full.get("nonoverlap_mean_rank_ic")
        p = (full.get("bootstrap_rankic") or {}).get("p_le0")
    if p is None:
        pred_verdict = "no_p"
    elif p <= 0.05:
        pred_verdict = "strong"
    elif p <= 0.2:
        pred_verdict = "suggestive"
    else:
        pred_verdict = "weak"
    wic = pf.get("weight_ic_mean")
    ratio = (wic / value) if (wic is not None and value not in (None, 0)) else None
    if ratio is None:
        align_verdict = "no_data"
    elif ratio >= 0.8:
        align_verdict = "good"
    elif ratio >= 0.4:
        align_verdict = "lossy"
    else:
        align_verdict = "poor"
    drag = bt.get("cost_drag_ann")
    if drag is None or (isinstance(drag, float) and np.isnan(drag)):
        cost_verdict = "no_data"
    elif drag <= 0.05:
        cost_verdict = "low"
    elif drag <= 0.15:
        cost_verdict = "moderate"
    else:
        cost_verdict = "high"
    excess = bt.get("excess_ann")
    if excess is None or (isinstance(excess, float) and np.isnan(excess)):
        market_verdict = "no_data"
    elif excess >= 0:
        market_verdict = "helped"
    else:
        market_verdict = "hurt"
    return {
        "pred": {"value": value, "p": p, "verdict": pred_verdict,
                 "note": "信号层：rankic/AUC 与 p 值（信号真假）"},
        "align": {"weight_ic": wic, "ratio_vs_signal": ratio,
                  "turnover": pf.get("turnover_mean"), "verdict": align_verdict,
                  "note": "持仓层：权重与信号排名的截面相关 + 换手"},
        "cost": {"cost_drag_ann": drag, "verdict": cost_verdict,
                 "note": "成本层：佣金万2.5双边 + 印花税卖出千1 + 滑点1bp 的年化拖累"},
        "market": {"excess_ann": excess, "beta": bt.get("beta"),
                   "verdict": market_verdict,
                   "note": "市场层：相对基准的超额与 beta"},
    }


def compute_full_trade(full, pred_path, label_path, pf_path, prices_path,
                       families, costs=None, task="regression", h=10,
                       benchmark=None):
    """Dispatcher for the trade families (P8 T3): appends portfolio / backtest /
    attribution sections to the prediction result. Called only for families the
    spec explicitly selected; unselected families are neither computed nor
    logged."""
    out = dict(full)
    has_pf = bool(pf_path) and Path(pf_path).exists()
    has_px = bool(prices_path) and Path(prices_path).exists()
    if "portfolio" in families and has_pf:
        out["portfolio"] = compute_portfolio(pred_path, pf_path)
    if "backtest" in families and has_pf and has_px:
        out["backtest"] = compute_backtest(pf_path, prices_path,
                                           benchmark=benchmark, costs=costs)
    if "attribution" in families and "portfolio" in out and "backtest" in out:
        out["attribution"] = compute_attribution(out, out["portfolio"],
                                                 out["backtest"], task=task)
    return out


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
    # selective families (spec.metrics) may omit hit/deciles/bootstrap sections;
    # core_metrics must tolerate that and only summarize what was computed
    rankic = full.get("nonoverlap_mean_rank_ic")
    pval = (full.get("bootstrap_rankic") or {}).get("p_le0")
    n_no = full.get("n_nonoverlap")
    text = "RankIC %s (p<=0: %s, n=%s non-overlap days)" % (
        ("%.4f" % rankic) if rankic is not None else "n/a",
        ("%.3f" % pval) if pval is not None else "n/a",
        n_no if n_no is not None else "n/a")
    return {
        "meta": meta,
        "rankic": {"mean": rankic, "std": full.get("rank_ic_std"),
                   "ir": full.get("nonoverlap_rank_icir"), "p_value": pval},
        "hit": {"rate": full.get("hit_rate"),
                "top_bottom_mean": (full.get("deciles") or {}).get("top_minus_bottom")},
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
