"""QLab systems-style topK executor (MIT vendor + GPLv3 cleanroom impl).

The prediction half reuses the established LightGBM executor pattern.  The
portfolio half replaces fixed equal weights with the first systems release:
forecast scale/cap, volatility-inverse sizing, schedule-aware rebalancing, and
simple execution feasibility checks.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from impl.forecast_scale_cap import fit_absolute_scale, scale_and_cap
from impl.portfolio_state import (apply_position_band, apply_trade_feasibility,
                                  assert_portfolio_vector, complete_target_vector,
                                  select_top_k)
from impl.position_sizing import (fill_missing_volatility,
                                  rolling_annual_volatility,
                                  vol_inverse_weights)
from qstrader.risk_model.risk_model import RiskModel
from qstrader.system.rebalance.daily import DailyRebalance
from qstrader.system.rebalance.end_of_month import EndOfMonthRebalance
from qstrader.system.rebalance.weekly import WeeklyRebalance


def load(cfg, train_pq, test_pq):
    tr = pd.read_parquet(train_pq)
    te = pd.read_parquet(test_pq)
    lv_tr = tr.index.get_level_values(0)
    trn = tr[(lv_tr >= cfg["train"][0]) & (lv_tr < cfg["train"][1])]
    val = tr[(lv_tr >= cfg["valid"][0]) & (lv_tr < cfg["valid"][1])]
    feats = [c for c in tr.columns if c != "y"]
    return trn[feats], trn["y"], val[feats], val["y"], te[feats], te["y"], feats


def train_seed(X_tr, y_tr, X_va, y_va, cfg, seed):
    import lightgbm as lgb
    params = {"objective": "regression", "metric": "l2",
              "num_threads": cfg.get("num_threads", 20), "verbosity": -1,
              "seed": int(seed)}
    if (cfg.get("params") or {}).get("deterministic"):
        params["deterministic"] = True
        params["force_row_wise"] = True
    for k in ("loss", "num_leaves", "learning_rate", "max_depth",
              "colsample_bytree", "subsample", "lambda_l1", "lambda_l2"):
        if k in cfg.get("model", {}):
            params[k] = cfg["model"][k]
    d_tr = lgb.Dataset(X_tr, label=y_tr)
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    model = lgb.train(params, d_tr, num_boost_round=cfg.get("rounds", 1000),
                      valid_sets=[d_tr, d_va],
                      callbacks=[lgb.early_stopping(cfg.get("early_stopping", 50)),
                                 lgb.log_evaluation(0)])
    best = model.best_score.get("valid_0", {}).get("l2")
    return model, best


def _predict_chunks(model, frame):
    chunks = []
    for start in range(0, len(frame), 200000):
        chunks.append(model.predict(frame.iloc[start:start + 200000],
                                    num_iteration=model.best_iteration))
    return pd.Series(np.concatenate(chunks), index=frame.index, name="score")


class ExposureCapRiskModel(RiskModel):
    """Small qstrader-compatible hook protecting the portfolio contract."""

    def __init__(self, max_weight: float):
        if max_weight <= 0:
            raise ValueError("max_weight must be positive")
        self.max_weight = float(max_weight)

    def __call__(self, dt, weights):
        return pd.Series(weights, dtype=float).clip(upper=self.max_weight)


def rebalance_days(days: pd.DatetimeIndex, params: dict) -> set[pd.Timestamp]:
    """Use the vendored MIT schedule objects; intersect with real trade days."""
    kind = str(params.get("rebalance", "daily")).lower()
    start, end = days[0], days[-1]
    if kind == "daily":
        times = DailyRebalance(start, end).rebalances
    elif kind == "weekly":
        weekday = int(params.get("rebalance_weekday", 4))
        if not 0 <= weekday <= 4:
            raise ValueError("rebalance_weekday must be 0..4")
        abbrev = ("MON", "TUE", "WED", "THU", "FRI")[weekday]
        times = WeeklyRebalance(start, end, abbrev).rebalances
    elif kind == "monthly":
        times = EndOfMonthRebalance(start, end).rebalances
    else:
        raise ValueError("rebalance must be daily, weekly, or monthly")
    times = pd.DatetimeIndex(times)
    if times.tz is not None:
        times = times.tz_localize(None)
    wanted = set(times.normalize())
    return wanted.intersection(set(days))


def _pivot_price(prices: pd.DataFrame, column: str) -> pd.DataFrame:
    return prices[column].unstack("instrument").sort_index()


def build_portfolio(scores: pd.Series, params: dict,
                    prices: pd.DataFrame | None = None) -> pd.DataFrame:
    """Forecast -> volatility-aware full target vectors -> contract weights."""
    top_k = int(params.get("top_k", 50))
    invest_frac = float(params.get("invest_frac", 0.95))
    buffer_size = float(params.get("buffer", 0.10))
    rank_buffer = int(params.get("rank_buffer", 0))
    warmup = int(params.get("warmup_days", 0))
    max_weight = params.get("max_weight", 0.10)
    mode = str(params.get("mode", "strategy")).lower()
    if mode not in {"strategy", "baseline"}:
        raise ValueError("mode must be strategy or baseline")

    if prices is not None and "close" in prices:
        close = _pivot_price(prices, "close")
        vol_raw = rolling_annual_volatility(
            close,
            span=int(params.get("vol_span", 35)),
            min_periods=int(params.get("vol_min_periods", 10)),
            floor_window=int(params.get("vol_floor_window", 500)),
            floor_min_periods=int(params.get("vol_floor_min_periods", 100)),
            floor_quantile=float(params.get("vol_floor_quantile", 0.05)))
        vol = fill_missing_volatility(vol_raw)
    else:
        close = None
        vol = None

    days = pd.DatetimeIndex(scores.index.get_level_values(0).unique()).sort_values()
    schedule = rebalance_days(days, params)
    rows = []
    previous = pd.Series(dtype=float)
    previous_picks: set = set()
    if max_weight is not None and mode == "strategy":
        risk_model = ExposureCapRiskModel(float(max_weight))
    else:
        risk_model = None

    for day_no, dt in enumerate(days):
        day = scores.xs(dt, drop_level=True).dropna()
        if day.empty:
            continue
        if mode == "baseline":
            picks = select_top_k(day, top_k)
            target = pd.Series(dtype=float)
            if len(picks):
                weight = float(invest_frac) / top_k
                target = pd.Series(weight, index=picks, dtype=float)
                assert_portfolio_vector(target, budget=1.0)
                for inst, value in target.items():
                    rows.append((dt, inst, float(value)))
            previous = target
            previous_picks = set(picks)
            continue

        if day_no < warmup:
            continue
        if dt not in schedule and len(previous):
            for inst, value in previous[previous > 0].items():
                rows.append((dt, inst, float(value)))
            continue
        if dt not in schedule:
            continue

        picks = select_top_k(day, top_k)
        if rank_buffer and previous_picks:
            churn = len(set(picks).symmetric_difference(previous_picks))
            if churn < rank_buffer and len(previous):
                for inst, value in previous[previous > 0].items():
                    rows.append((dt, inst, float(value)))
                previous_picks = set(previous_picks)
                continue

        if len(picks) == 0:
            previous = pd.Series(dtype=float)
            previous_picks = set()
            continue

        selected = pd.Series(False, index=day.index)
        selected.loc[picks] = True
        if vol is None:
            day_vol = pd.Series(1.0, index=day.index)
        else:
            day_vol = vol.reindex(index=[dt], columns=day.index).iloc[0]
            day_vol = day_vol.fillna(day_vol.median())
            day_vol = day_vol.fillna(1.0)
        signals = pd.DataFrame([day.to_dict()], index=[dt])
        vol_frame = pd.DataFrame([day_vol.to_dict()], index=[dt])
        selected_frame = pd.DataFrame([selected.to_dict()], index=[dt])
        target = vol_inverse_weights(signals, vol_frame, selected_frame,
                                     invest_frac=invest_frac,
                                     max_weight=max_weight).iloc[0]
        held_universe = day.index.union(previous.index)
        target = complete_target_vector(target, previous, held_universe)
        if risk_model is not None:
            target = risk_model(dt, target)
        target = apply_position_band(previous, target, buffer_size)

        if prices is not None:
            inst_index = pd.Index(target.index)
            flags = {}
            for name in ("suspended", "limit_up", "limit_down"):
                if name in prices:
                    flags[name] = _pivot_price(prices, name).reindex(
                        index=[dt], columns=inst_index).iloc[0].fillna(False)
            amount = _pivot_price(prices, "amount").reindex(
                index=[dt], columns=inst_index).iloc[0] if "amount" in prices else None
            target = apply_trade_feasibility(
                previous, target, suspended=flags.get("suspended"),
                limit_up=flags.get("limit_up"), limit_down=flags.get("limit_down"),
                amount=amount,
                capital=float(params.get("capital", 1_000_000.0)),
                participation_rate=float(params.get("participation_rate", 0.05)))
        exposure = float(target.clip(lower=0).sum())
        if exposure > invest_frac + 1e-10:
            target = target * (invest_frac / exposure)
        assert_portfolio_vector(target, budget=1.0)
        for inst, value in target[target > 0].items():
            rows.append((dt, inst, float(value)))
        previous = target
        previous_picks = set(picks)

    pf = pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"])
    return pf


def main():
    ap = argparse.ArgumentParser(prog="executors/systems_topk")
    ap.add_argument("--config", required=True)
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    X_tr, y_tr, X_va, y_va, X_te, label_te, feats = load(cfg, a.train, a.test)
    print("data loaded: train %s valid %s test %s feats=%d" %
          (X_tr.shape, X_va.shape, X_te.shape, len(feats)), flush=True)
    seeds = cfg.get("seeds") or [42]
    preds, valid_preds, seed_runs = [], [], []
    for seed in seeds:
        t1 = time.time()
        model, best = train_seed(X_tr, y_tr, X_va, y_va, cfg, seed)
        seed_runs.append({"seed": int(seed),
                          "best_iter": int(model.best_iteration),
                          "valid_l2": float(best) if best is not None else None,
                          "seconds": round(time.time() - t1, 1)})
        valid_preds.append(_predict_chunks(model, X_va))
        preds.append(_predict_chunks(model, X_te))
        print("seed %s done %.0fs best_iter=%s" %
              (seed, time.time() - t1, model.best_iteration), flush=True)
        del model
    if len(preds) == 1:
        ens = preds[0]
    else:
        ranks = []
        for s in preds:
            dfr = s.to_frame("score")
            dfr["dt"] = dfr.index.get_level_values(0)
            dfr["r"] = dfr.groupby("dt")["score"].rank(pct=True)
            ranks.append(dfr["r"])
        ens = pd.Series(pd.concat(ranks, axis=1).mean(axis=1).to_numpy(),
                        index=X_te.index, name="score")
    ens.to_frame("score").to_pickle(out / "pred.pkl")

    params = cfg.get("params") or {}
    scalar = fit_absolute_scale(valid_preds[0],
                                float(params.get("forecast_target", 10.0)))
    scores = scale_and_cap(ens, scalar, float(params.get("forecast_target", 10.0)),
                           float(params.get("forecast_cap", 20.0)))
    prices = pd.read_parquet(cfg["price_pq"]) if cfg.get("price_pq") else None
    pf = build_portfolio(scores, params, prices)
    pf.to_pickle(out / "portfolio.pkl")
    info = {"seed_runs": seed_runs, "seconds": round(time.time() - t0, 1),
            "features": len(feats),
            "portfolio": {
                "strategy": "forecast scale + vol-inverse topK",
                "params_used": {k: params.get(k) for k in (
                    "top_k", "invest_frac", "buffer", "rebalance", "mode",
                    "warmup_days", "rank_buffer", "max_weight")},
                "forecast_scalar": scalar,
                "n_days": int(pf.index.get_level_values(0).nunique()),
                "mean_held": float(pf.groupby(level=0).size().mean())
                if len(pf) else 0.0}}
    (out / "run_info.json").write_text(json.dumps(info, indent=2))
    print(json.dumps({"done": True, "seconds": info["seconds"],
                      "portfolio": info["portfolio"]}), flush=True)


if __name__ == "__main__":
    main()
