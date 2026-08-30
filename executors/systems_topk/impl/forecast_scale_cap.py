"""Cleanroom forecast normalization for QLab signals.

The numerical contract is intentionally small: map any model score to a common
absolute scale, then prevent a handful of extreme scores from dominating the
sizing step.  This implementation is written from the QLab cleanroom
specification and has no dependency on GPL source code.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def fit_absolute_scale(scores: pd.Series, target_abs: float = 10.0) -> float:
    """Return the multiplier that puts mean(|score|) at ``target_abs``."""
    values = pd.to_numeric(pd.Series(scores), errors="coerce").to_numpy(float)
    values = values[np.isfinite(values)]
    mean_abs = float(np.mean(np.abs(values))) if values.size else 0.0
    if not np.isfinite(mean_abs) or mean_abs <= 1e-12:
        return 1.0
    return float(target_abs / mean_abs)


def scale_and_cap(scores: pd.Series, scalar: float, target_abs: float = 10.0,
                  cap: float = 20.0) -> pd.Series:
    """Apply an estimated scale and symmetric cap without changing the index."""
    cap = float(cap)
    if cap <= 0:
        raise ValueError("forecast cap must be positive")
    scalar = float(scalar)
    if not np.isfinite(scalar) or scalar < 0:
        raise ValueError("forecast scalar must be finite and non-negative")
    scaled = pd.to_numeric(scores, errors="coerce").astype(float) * scalar
    return scaled.clip(-cap, cap)
