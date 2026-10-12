"""Feature extraction pipeline for Kyiv air alert forecasting.

All features are strictly causal: at forecast step T, only information
with timestamp <= T is observed or used.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional
import zoneinfo
import numpy as np
import pandas as pd

KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
UTC_TZ = timezone.utc
CAP_HOURS_SINCE_EVENT = 720.0  # 30 days cap for elapsed hours when no event yet


def _ensure_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """Ensure timestamp is timezone-aware in UTC."""
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def extract_single_timestamp_features(
    t: pd.Timestamp,
    kyiv_alerts: pd.DataFrame,
    neighbor_starts: pd.DataFrame,
) -> Dict[str, float]:
    """Causal feature extractor for a single moment T.

    Strictly masks and discards any data after T:
    - Alerts with started_at > T do not exist.
    - If an alert started <= T and finished > T (or not yet finished),
      it is ongoing at T; its future finished_at is masked.
    - Neighbor starts with started_at > T do not exist.
    """
    t_utc = _ensure_utc(t)

    # 1. Filter Kyiv alerts known at T
    known_alerts = kyiv_alerts[kyiv_alerts["started_at"] <= t_utc].copy()

    # Alert state right at moment T
    is_active = 0
    active_duration_min = 0.0
    hours_since_start = CAP_HOURS_SINCE_EVENT
    hours_since_end = CAP_HOURS_SINCE_EVENT

    if not known_alerts.empty:
        # Most recent start
        last_start = known_alerts["started_at"].max()
        hours_since_start = min(CAP_HOURS_SINCE_EVENT, (t_utc - last_start).total_seconds() / 3600.0)

        # Most recent end strictly <= T
        closed_alerts = known_alerts[known_alerts["finished_at"] <= t_utc]
        if not closed_alerts.empty:
            last_end = closed_alerts["finished_at"].max()
            hours_since_end = min(CAP_HOURS_SINCE_EVENT, (t_utc - last_end).total_seconds() / 3600.0)

        # Active check: started <= T and (finished > T or finished is NaT)
        active_candidates = known_alerts[
            known_alerts["finished_at"].isna() | (known_alerts["finished_at"] > t_utc)
        ]
        if not active_candidates.empty:
            is_active = 1
            # Current ongoing alert started at:
            curr_start = active_candidates.iloc[-1]["started_at"]
            active_duration_min = (t_utc - curr_start).total_seconds() / 60.0

    # 2. Historical minutes under alert in [T - W, T)
    def minutes_in_window(window_hours: int) -> float:
        if known_alerts.empty:
            return 0.0
        w_start = t_utc - pd.Timedelta(hours=window_hours)
        total_sec = 0.0
        for _, row in known_alerts.iterrows():
            st = max(row["started_at"], w_start)
            # Truncate end at t_utc because future after T is unknown
            fin = t_utc if pd.isna(row["finished_at"]) else min(row["finished_at"], t_utc)
            if fin > st:
                total_sec += (fin - st).total_seconds()
        return total_sec / 60.0

    min_1h = minutes_in_window(1)
    min_3h = minutes_in_window(3)
    min_6h = minutes_in_window(6)
    min_12h = minutes_in_window(12)
    min_24h = minutes_in_window(24)

    # 3. Neighbor starts known at T
    known_neighbors = neighbor_starts[neighbor_starts["started_at"] <= t_utc]
    neighbor_starts_1h = 0
    neighbor_starts_3h = 0
    neighbor_starts_6h = 0
    kyiv_obl_starts_1h = 0
    kyiv_obl_starts_3h = 0
    neighbor_hours_since_start = CAP_HOURS_SINCE_EVENT

    if not known_neighbors.empty:
        last_neigh_start = known_neighbors["started_at"].max()
        neighbor_hours_since_start = min(
            CAP_HOURS_SINCE_EVENT, (t_utc - last_neigh_start).total_seconds() / 3600.0
        )

        n_1h = known_neighbors[known_neighbors["started_at"] >= t_utc - pd.Timedelta(hours=1)]
        n_3h = known_neighbors[known_neighbors["started_at"] >= t_utc - pd.Timedelta(hours=3)]
        n_6h = known_neighbors[known_neighbors["started_at"] >= t_utc - pd.Timedelta(hours=6)]

        neighbor_starts_1h = len(n_1h)
        neighbor_starts_3h = len(n_3h)
        neighbor_starts_6h = len(n_6h)

        kyiv_obl_starts_1h = (n_1h["region"] == "Київська область").sum()
        kyiv_obl_starts_3h = (n_3h["region"] == "Київська область").sum()

    # 4. Kyiv local time features
    t_kyiv = t_utc.astimezone(KYIV_TZ)
    hour = t_kyiv.hour
    dow = t_kyiv.dayofweek
    is_weekend = 1 if dow >= 5 else 0

    return {
        "kyiv_is_active_at_T": float(is_active),
        "kyiv_active_duration_min_at_T": float(active_duration_min),
        "kyiv_hours_since_last_start": float(hours_since_start),
        "kyiv_hours_since_last_end": float(hours_since_end),
        "kyiv_minutes_last_1h": float(min_1h),
        "kyiv_minutes_last_3h": float(min_3h),
        "kyiv_minutes_last_6h": float(min_6h),
        "kyiv_minutes_last_12h": float(min_12h),
        "kyiv_minutes_last_24h": float(min_24h),
        "neighbor_starts_last_1h": float(neighbor_starts_1h),
        "neighbor_starts_last_3h": float(neighbor_starts_3h),
        "neighbor_starts_last_6h": float(neighbor_starts_6h),
        "kyiv_oblast_starts_last_1h": float(kyiv_obl_starts_1h),
        "kyiv_oblast_starts_last_3h": float(kyiv_obl_starts_3h),
        "neighbor_hours_since_last_start": float(neighbor_hours_since_start),
        "hour_of_day_kyiv": float(hour),
        "hour_sin": float(np.sin(2.0 * np.pi * hour / 24.0)),
        "hour_cos": float(np.cos(2.0 * np.pi * hour / 24.0)),
        "day_of_week_kyiv": float(dow),
        "is_weekend_kyiv": float(is_weekend),
    }


def build_feature_matrix(
    grid: pd.DatetimeIndex,
    kyiv_alerts: pd.DataFrame,
    neighbor_starts: pd.DataFrame,
) -> pd.DataFrame:
    """Build feature matrix for all timestamps in the hourly grid.

    Optimized implementation with exact causal equivalence to
    extract_single_timestamp_features.
    """
    if len(grid) == 0:
        return pd.DataFrame()

    grid = pd.DatetimeIndex([_ensure_utc(t) for t in grid])
    grid_start = grid.min()
    grid_end = grid.max()

    # Extended raster range back 48h to calculate full lags
    raster_start = grid_start - pd.Timedelta(hours=48)
    full_hourly_range = pd.date_range(raster_start, grid_end, freq="1h", tz="UTC")

    # 1. Precompute hourly alert minutes for Kyiv
    # Each hourly bin is [h, h + 1h)
    h_starts = full_hourly_range.to_numpy()
    h_ends = (full_hourly_range + pd.Timedelta(hours=1)).to_numpy()
    n_h = len(full_hourly_range)
    hourly_kyiv_minutes = np.zeros(n_h, dtype=float)

    if not kyiv_alerts.empty:
        alerts_sorted = kyiv_alerts.sort_values("started_at").reset_index(drop=True)
        a_starts = alerts_sorted["started_at"].to_numpy()
        far_future = pd.Timestamp("2100-01-01", tz="UTC")
        a_ends = alerts_sorted["finished_at"].fillna(far_future).to_numpy()

        for j in range(n_h):
            bin_s = h_starts[j]
            bin_e = h_ends[j]
            # Alerts that overlap with [bin_s, bin_e)
            idx_e = np.searchsorted(a_starts, bin_e, side="left")
            if idx_e > 0:
                cur_s = a_starts[:idx_e]
                cur_e = a_ends[:idx_e]
                mask = cur_e > bin_s
                if np.any(mask):
                    overlap_s = np.maximum(cur_s[mask], bin_s)
                    overlap_e = np.minimum(cur_e[mask], bin_e)
                    sec = (overlap_e - overlap_s) / np.timedelta64(1, "s")
                    hourly_kyiv_minutes[j] = np.sum(sec) / 60.0

    s_kyiv_minutes = pd.Series(hourly_kyiv_minutes, index=full_hourly_range)

    # 2. Precompute hourly starts for neighbor regions
    hourly_neighbor_starts = pd.Series(0, index=full_hourly_range)
    hourly_kyiv_obl_starts = pd.Series(0, index=full_hourly_range)

    if not neighbor_starts.empty:
        n_df = neighbor_starts.copy()
        n_df["hour_bin"] = n_df["started_at"].dt.floor("h")
        counts_all = n_df.groupby("hour_bin").size()
        hourly_neighbor_starts = hourly_neighbor_starts.add(counts_all, fill_value=0)

        obl_df = n_df[n_df["region"] == "Київська область"]
        counts_obl = obl_df.groupby("hour_bin").size()
        hourly_kyiv_obl_starts = hourly_kyiv_obl_starts.add(counts_obl, fill_value=0)

    # 3. Rolling lags: for moment T, past window [T - W, T) is shift(1).rolling(W).sum()
    lag_1h = s_kyiv_minutes.shift(1).reindex(grid).fillna(0.0).to_numpy()
    lag_3h = s_kyiv_minutes.shift(1).rolling(3).sum().reindex(grid).fillna(0.0).to_numpy()
    lag_6h = s_kyiv_minutes.shift(1).rolling(6).sum().reindex(grid).fillna(0.0).to_numpy()
    lag_12h = s_kyiv_minutes.shift(1).rolling(12).sum().reindex(grid).fillna(0.0).to_numpy()
    lag_24h = s_kyiv_minutes.shift(1).rolling(24).sum().reindex(grid).fillna(0.0).to_numpy()

    neigh_1h = hourly_neighbor_starts.shift(1).reindex(grid).fillna(0.0).to_numpy()
    neigh_3h = hourly_neighbor_starts.shift(1).rolling(3).sum().reindex(grid).fillna(0.0).to_numpy()
    neigh_6h = hourly_neighbor_starts.shift(1).rolling(6).sum().reindex(grid).fillna(0.0).to_numpy()

    obl_1h = hourly_kyiv_obl_starts.shift(1).reindex(grid).fillna(0.0).to_numpy()
    obl_3h = hourly_kyiv_obl_starts.shift(1).rolling(3).sum().reindex(grid).fillna(0.0).to_numpy()

    # 4. Instantaneous status at moment T
    n_points = len(grid)
    is_active_arr = np.zeros(n_points, dtype=float)
    active_dur_arr = np.zeros(n_points, dtype=float)
    hours_since_start_arr = np.full(n_points, CAP_HOURS_SINCE_EVENT, dtype=float)
    hours_since_end_arr = np.full(n_points, CAP_HOURS_SINCE_EVENT, dtype=float)

    if not kyiv_alerts.empty:
        a_starts_ts = alerts_sorted["started_at"].to_numpy()
        a_ends_ts = alerts_sorted["finished_at"].to_numpy()

        # Closed alerts with non-null finished_at
        closed_mask = ~alerts_sorted["finished_at"].isna()
        closed_ends = alerts_sorted.loc[closed_mask, "finished_at"].sort_values().to_numpy()

        grid_ts = grid.to_numpy()

        for i in range(n_points):
            t = grid_ts[i]
            # Recent start <= T
            idx_s = np.searchsorted(a_starts_ts, t, side="right")
            if idx_s > 0:
                last_s = a_starts_ts[idx_s - 1]
                hours_since_start_arr[i] = min(
                    CAP_HOURS_SINCE_EVENT, (t - last_s) / np.timedelta64(1, "h")
                )

                # Check if this alert or any preceding active alert is active at T
                # Alert active at T: started <= t and (finished > t or finished is NaT)
                # Since alerts are non-overlapping in Kyiv Digital, only the last start candidate can be active
                cand_end = a_ends_ts[idx_s - 1]
                if pd.isna(cand_end) or cand_end > t:
                    is_active_arr[i] = 1.0
                    active_dur_arr[i] = (t - last_s) / np.timedelta64(1, "m")

            # Recent end <= T
            if len(closed_ends) > 0:
                idx_e = np.searchsorted(closed_ends, t, side="right")
                if idx_e > 0:
                    last_e = closed_ends[idx_e - 1]
                    hours_since_end_arr[i] = min(
                        CAP_HOURS_SINCE_EVENT, (t - last_e) / np.timedelta64(1, "h")
                    )

    # 5. Hours since neighbor start
    hours_since_neigh_arr = np.full(n_points, CAP_HOURS_SINCE_EVENT, dtype=float)
    if not neighbor_starts.empty:
        n_starts_ts = neighbor_starts["started_at"].sort_values().to_numpy()
        grid_ts = grid.to_numpy()
        for i in range(n_points):
            t = grid_ts[i]
            idx_n = np.searchsorted(n_starts_ts, t, side="right")
            if idx_n > 0:
                last_n = n_starts_ts[idx_n - 1]
                hours_since_neigh_arr[i] = min(
                    CAP_HOURS_SINCE_EVENT, (t - last_n) / np.timedelta64(1, "h")
                )

    # 6. Kyiv local calendar features
    grid_kyiv = grid.tz_convert(KYIV_TZ)
    hours_kyiv = grid_kyiv.hour.to_numpy(dtype=float)
    dows_kyiv = grid_kyiv.dayofweek.to_numpy(dtype=float)
    is_weekend_kyiv = (dows_kyiv >= 5).astype(float)
    hour_sin = np.sin(2.0 * np.pi * hours_kyiv / 24.0)
    hour_cos = np.cos(2.0 * np.pi * hours_kyiv / 24.0)

    features = pd.DataFrame(
        {
            "kyiv_is_active_at_T": is_active_arr,
            "kyiv_active_duration_min_at_T": active_dur_arr,
            "kyiv_hours_since_last_start": hours_since_start_arr,
            "kyiv_hours_since_last_end": hours_since_end_arr,
            "kyiv_minutes_last_1h": lag_1h,
            "kyiv_minutes_last_3h": lag_3h,
            "kyiv_minutes_last_6h": lag_6h,
            "kyiv_minutes_last_12h": lag_12h,
            "kyiv_minutes_last_24h": lag_24h,
            "neighbor_starts_last_1h": neigh_1h,
            "neighbor_starts_last_3h": neigh_3h,
            "neighbor_starts_last_6h": neigh_6h,
            "kyiv_oblast_starts_last_1h": obl_1h,
            "kyiv_oblast_starts_last_3h": obl_3h,
            "neighbor_hours_since_last_start": hours_since_neigh_arr,
            "hour_of_day_kyiv": hours_kyiv,
            "hour_sin": hour_sin,
            "hour_cos": hour_cos,
            "day_of_week_kyiv": dows_kyiv,
            "is_weekend_kyiv": is_weekend_kyiv,
        },
        index=grid,
    )

    return features
