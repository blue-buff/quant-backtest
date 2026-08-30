"""Cleanroom full-vector portfolio state, bands, and trade feasibility."""

from __future__ import annotations

import numpy as np
import pandas as pd


def complete_target_vector(picks: pd.Series, held: pd.Series,
                           universe: pd.Index) -> pd.Series:
    """Build a complete long-only target snapshot over universe plus holdings."""
    full_index = universe.union(held.index).union(picks.index)
    target = pd.Series(0.0, index=full_index, dtype=float)
    aligned = picks.reindex(full_index).fillna(0.0)
    target.loc[aligned.index] = aligned.to_numpy(float)
    # Any current holding absent from picks is explicitly liquidated, even when
    # it has already fallen out of today's universe.
    return target


def assert_portfolio_vector(target: pd.Series, budget: float = 1.0) -> None:
    values = pd.to_numeric(target, errors="coerce").to_numpy(float)
    if not np.isfinite(values).all():
        raise ValueError("portfolio target contains non-finite weights")
    if (values < -1e-12).any():
        raise ValueError("portfolio target contains negative weights")
    if float(values.sum()) > float(budget) + 1e-8:
        raise ValueError("portfolio target exceeds budget")


def position_band(previous: float, target: float, buffer_size: float) -> float:
    """A relative no-trade band based on the midpoint of old/new exposure."""
    return float(buffer_size * (abs(float(previous)) + abs(float(target))) / 2.0)


def apply_position_band(previous: pd.Series, target: pd.Series,
                        buffer_size: float) -> pd.Series:
    """Keep old weights inside the no-trade band; trade to the new target outside."""
    if buffer_size <= 0:
        return target
    idx = target.index
    prev = previous.reindex(idx).fillna(0.0)
    diff = target - prev
    band = pd.Series([position_band(p, t, buffer_size) for p, t in zip(prev, target)],
                     index=idx)
    return target.where(diff.abs() > band, prev)


def apply_trade_feasibility(previous: pd.Series, target: pd.Series,
                            suspended: pd.Series | None = None,
                            limit_up: pd.Series | None = None,
                            limit_down: pd.Series | None = None,
                            amount: pd.Series | None = None,
                            capital: float = 1_000_000.0,
                            participation_rate: float = 0.05) -> pd.Series:
    """Clamp a target to simple A-share execution constraints.

    The tester remains the metric authority. This is executor-side realism:
    suspended names cannot trade, limit-up cannot be bought, limit-down cannot
    be sold, and a one-day trade cannot exceed a configured fraction of turnover.
    """
    feasible = target.copy()
    prev = previous.reindex(feasible.index).fillna(0.0)
    if suspended is not None:
        feasible = feasible.mask(suspended.reindex(feasible.index).fillna(False), prev)
    if limit_up is not None:
        blocked_buy = limit_up.reindex(feasible.index).fillna(False) & feasible.gt(prev)
        feasible = feasible.mask(blocked_buy, prev)
    if limit_down is not None:
        blocked_sell = limit_down.reindex(feasible.index).fillna(False) & feasible.lt(prev)
        feasible = feasible.mask(blocked_sell, prev)
    if amount is not None and participation_rate > 0:
        max_change = (pd.to_numeric(amount, errors="coerce").reindex(feasible.index)
                      .fillna(0.0) * float(participation_rate) / float(capital))
        desired = feasible - prev
        desired = desired.clip(lower=-max_change, upper=max_change)
        feasible = prev + desired
    return feasible.clip(lower=0.0)


def select_top_k(scores: pd.Series, top_k: int) -> pd.Index:
    """Deterministically select the highest scores, breaking ties by label."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    return scores.dropna().sort_values(ascending=False, kind="mergesort").head(
        top_k).index
