"""Time-series hourly grid builder and causal target generator."""

from typing import Tuple
import numpy as np
import pandas as pd

# Exact split boundaries (half-open intervals [start, end)):
TRAIN_START = pd.Timestamp("2022-03-01 00:00:00", tz="UTC")
VAL_START   = pd.Timestamp("2026-08-17 14:00:00", tz="UTC")
TEST_START  = pd.Timestamp("2026-09-14 14:00:00", tz="UTC")
TEST_END    = pd.Timestamp("2026-10-12 14:00:00", tz="UTC")


def make_hourly_grid(start_time: pd.Timestamp, end_time: pd.Timestamp) -> pd.DatetimeIndex:
    """Generate hourly timestamp grid [start_time, end_time) in UTC.

    Half-open interval: start_time is included, end_time is excluded.
    """
    if start_time.tz is None:
        start_time = start_time.tz_localize("UTC")
    if end_time.tz is None:
        end_time = end_time.tz_localize("UTC")

    return pd.date_range(start_time, end_time, freq="1h", inclusive="left")


def compute_target(grid: pd.DatetimeIndex, alerts_df: pd.DataFrame) -> pd.Series:
    """Compute binary target Y_T for each hour T in the grid.

    Y_T = 1 if any alert in alerts_df intersects [T, T + 1h), else 0.
    An alert intersects [T, T + 1h) if:
        started_at < T + 1h AND (finished_at > T OR finished_at is NaT)
    """
    if alerts_df.empty or len(grid) == 0:
        return pd.Series(0, index=grid, name="target_in_1h", dtype=int)

    # Sort alerts by started_at
    alerts_sorted = alerts_df.sort_values("started_at").reset_index(drop=True)
    starts = alerts_sorted["started_at"].to_numpy()
    far_future = pd.Timestamp("2100-01-01", tz="UTC")
    finishes = alerts_sorted["finished_at"].fillna(far_future).to_numpy()

    grid_arr = grid.to_numpy()
    grid_next = (grid + pd.Timedelta(hours=1)).to_numpy()

    n = len(grid)
    targets = np.zeros(n, dtype=int)

    for i in range(n):
        t = grid_arr[i]
        t_next = grid_next[i]

        # Candidates: alerts starting before t_next
        idx_end = np.searchsorted(starts, t_next, side="left")
        if idx_end > 0:
            cands_fin = finishes[:idx_end]
            if np.any(cands_fin > t):
                targets[i] = 1

    return pd.Series(targets, index=grid, name="target_in_1h")


def split_time_series(
    df: pd.DataFrame,
    train_start: pd.Timestamp = TRAIN_START,
    val_start: pd.Timestamp = VAL_START,
    test_start: pd.Timestamp = TEST_START,
    test_end: pd.Timestamp = TEST_END,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split time-series DataFrame into strictly chronological Train, Val, and Test splits.

    Ensures zero gap and zero overlap:
        Train: [train_start, val_start)
        Val:   [val_start, test_start)
        Test:  [test_start, test_end)
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("DataFrame must be indexed by pd.DatetimeIndex.")

    train_df = df[(df.index >= train_start) & (df.index < val_start)]
    val_df   = df[(df.index >= val_start) & (df.index < test_start)]
    test_df  = df[(df.index >= test_start) & (df.index < test_end)]

    return train_df, val_df, test_df
