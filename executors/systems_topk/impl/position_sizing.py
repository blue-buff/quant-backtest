"""Cleanroom volatility-aware long-only sizing primitives."""

from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_annual_volatility(close: pd.DataFrame, span: int = 35,
                              min_periods: int = 10, floor_window: int = 500,
                              floor_min_periods: int = 100,
                              floor_quantile: float = 0.05,
                              periods: int = 252) -> pd.DataFrame:
    """Per-name EWMA volatility with a long-history quantile floor."""
    if min_periods < 2 or span < 2:
        raise ValueError("volatility span/min_periods must be at least 2")
    returns = close.sort_index().pct_change(fill_method=None)
    vol = returns.ewm(span=span, min_periods=min_periods).std() * np.sqrt(periods)
    if floor_window > 0:
        floor = returns.rolling(floor_window, min_periods=floor_min_periods).quantile(
            floor_quantile) * np.sqrt(periods)
        vol = vol.clip(lower=floor)
    return vol


def fill_missing_volatility(vol: pd.DataFrame) -> pd.DataFrame:
    """Use the same-day cross-section, then the global median, then unity."""
    filled = vol.copy()
    cross_med = filled.median(axis=1, skipna=True)
    filled = filled.apply(lambda col: col.fillna(cross_med))
    global_med = float(filled.stack().dropna().median())
    if not np.isfinite(global_med) or global_med <= 0:
        global_med = 1.0
    return filled.fillna(global_med).clip(lower=1e-6)


def vol_inverse_weights(signals: pd.DataFrame, vol: pd.DataFrame,
                        selected: pd.DataFrame, invest_frac: float = 0.95,
                        max_weight: float | None = None) -> pd.DataFrame:
    """Turn positive forecasts into normalized long-only target weights.

    ``selected`` is a boolean frame with the same shape; names outside it stay
    zero. The output is normalized to ``invest_frac`` on days with at least one
    eligible name, leaving the remainder as explicit cash.
    """
    if not 0 < float(invest_frac) <= 1:
        raise ValueError("invest_frac must be in (0, 1]")
    positive = signals.clip(lower=0.0)
    raw = positive / vol.replace([np.inf, -np.inf], np.nan)
    raw = raw.where(selected, 0.0).fillna(0.0)
    out = raw.copy()
    totals = out.sum(axis=1)
    scale = float(invest_frac) / totals.where(totals > 0)
    out = out.mul(scale, axis=0).fillna(0.0)
    if max_weight is not None:
        max_weight = float(max_weight)
        if max_weight <= 0:
            raise ValueError("max_weight must be positive")
        # Iterative water-filling: capped names stay at the cap; their unused
        # budget is redistributed proportionally to the uncapped raw weights.
        for _, row in out.iterrows():
            raw_row = raw.loc[row.name]
            fixed = row.clip(upper=max_weight)
            capped = row.gt(max_weight)
            for _cycle in range(20):
                if not capped.any():
                    break
                remaining = float(invest_frac) - fixed.loc[capped].sum()
                free_mass = float(raw_row.loc[~capped].sum())
                if free_mass <= 0 or remaining <= 0:
                    row.loc[capped] = fixed.loc[capped]
                    row.loc[~capped] = 0.0
                    break
                row.loc[~capped] = raw_row.loc[~capped] / free_mass * remaining
                newly_capped = row.loc[~capped].gt(max_weight)
                if not newly_capped.any():
                    break
                capped |= newly_capped
                fixed = row.clip(upper=max_weight)
            out.loc[row.name] = row.clip(lower=0.0, upper=max_weight)
    return out
