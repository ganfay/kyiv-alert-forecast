"""Tests for timezone handling, Kyiv local time conversions, and DST transitions."""

import numpy as np
import pandas as pd
import pytest
import zoneinfo

from src.data.grid import make_hourly_grid
from src.features.builder import build_feature_matrix

KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")


def test_utc_grid_is_continuous_and_strictly_monotonic():
    """UTC grid over 1 full year must have exactly 1-hour difference with zero jumps."""
    grid = make_hourly_grid(
        pd.Timestamp("2025-01-01 00:00:00", tz="UTC"),
        pd.Timestamp("2026-01-01 00:00:00", tz="UTC"),
    )
    diffs = grid.to_series().diff().dropna()
    assert (diffs == pd.Timedelta(hours=1)).all()
    assert len(grid) == 8760  # non-leap year hours


def test_kyiv_hour_summer_vs_winter():
    """Verify offset: UTC+3 in summer (EEST), UTC+2 in winter (EET)."""
    # Winter test: 2026-01-15 12:00:00 UTC -> 14:00 Kyiv
    t_winter = pd.Timestamp("2026-01-15 12:00:00", tz="UTC")
    grid_winter = pd.DatetimeIndex([t_winter])
    empty_alerts = pd.DataFrame(columns=["started_at", "finished_at"])
    empty_neigh = pd.DataFrame(columns=["region", "started_at"])

    feats_w = build_feature_matrix(grid_winter, empty_alerts, empty_neigh)
    assert feats_w.loc[t_winter, "hour_of_day_kyiv"] == 14.0

    # Summer test: 2026-07-15 12:00:00 UTC -> 15:00 Kyiv
    t_summer = pd.Timestamp("2026-07-15 12:00:00", tz="UTC")
    grid_summer = pd.DatetimeIndex([t_summer])
    feats_s = build_feature_matrix(grid_summer, empty_alerts, empty_neigh)
    assert feats_s.loc[t_summer, "hour_of_day_kyiv"] == 15.0


def test_dst_spring_transition_2026():
    """In March 2026 (last Sunday, March 29), Kyiv switches from UTC+2 to UTC+3."""
    # 2026-03-29 00:00 UTC is 02:00 EET
    # 2026-03-29 01:00 UTC is 04:00 EEST (03:00 jumped over)
    grid = make_hourly_grid(
        pd.Timestamp("2026-03-28 22:00:00", tz="UTC"),
        pd.Timestamp("2026-03-29 04:00:00", tz="UTC"),
    )
    empty_alerts = pd.DataFrame(columns=["started_at", "finished_at"])
    empty_neigh = pd.DataFrame(columns=["region", "started_at"])

    feats = build_feature_matrix(grid, empty_alerts, empty_neigh)

    # UTC grid is completely seamless (1h each)
    assert len(feats) == 6
    # 00:00 UTC -> 02:00 Kyiv
    assert feats.loc[pd.Timestamp("2026-03-29 00:00:00", tz="UTC"), "hour_of_day_kyiv"] == 2.0
    # 01:00 UTC -> 04:00 Kyiv (DST shift applied!)
    assert feats.loc[pd.Timestamp("2026-03-29 01:00:00", tz="UTC"), "hour_of_day_kyiv"] == 4.0
    # 02:00 UTC -> 05:00 Kyiv
    assert feats.loc[pd.Timestamp("2026-03-29 02:00:00", tz="UTC"), "hour_of_day_kyiv"] == 5.0


def test_dst_autumn_transition_2026():
    """In October 2026 (last Sunday, October 25), Kyiv switches from UTC+3 to UTC+2."""
    grid = make_hourly_grid(
        pd.Timestamp("2026-10-24 22:00:00", tz="UTC"),
        pd.Timestamp("2026-10-25 04:00:00", tz="UTC"),
    )
    empty_alerts = pd.DataFrame(columns=["started_at", "finished_at"])
    empty_neigh = pd.DataFrame(columns=["region", "started_at"])

    feats = build_feature_matrix(grid, empty_alerts, empty_neigh)
    # 23:00 UTC (Oct 24) is 02:00 EEST (UTC+3)
    assert feats.loc[pd.Timestamp("2026-10-24 23:00:00", tz="UTC"), "hour_of_day_kyiv"] == 2.0
    # 00:00 UTC (Oct 25) is 03:00 EEST (UTC+3)
    assert feats.loc[pd.Timestamp("2026-10-25 00:00:00", tz="UTC"), "hour_of_day_kyiv"] == 3.0
    # 01:00 UTC (Oct 25) is 03:00 EET (UTC+2) (clock set back)
    assert feats.loc[pd.Timestamp("2026-10-25 01:00:00", tz="UTC"), "hour_of_day_kyiv"] == 3.0
    # 02:00 UTC (Oct 25) is 04:00 EET (UTC+2)
    assert feats.loc[pd.Timestamp("2026-10-25 02:00:00", tz="UTC"), "hour_of_day_kyiv"] == 4.0


def test_cyclical_hour_encodings():
    """Verify trigonometric properties of hour_sin and hour_cos."""
    grid = make_hourly_grid(
        pd.Timestamp("2026-08-01 00:00:00", tz="UTC"),
        pd.Timestamp("2026-08-02 00:00:00", tz="UTC"),
    )
    empty_alerts = pd.DataFrame(columns=["started_at", "finished_at"])
    empty_neigh = pd.DataFrame(columns=["region", "started_at"])

    feats = build_feature_matrix(grid, empty_alerts, empty_neigh)
    sin_sq = feats["hour_sin"] ** 2
    cos_sq = feats["hour_cos"] ** 2

    # sin^2 + cos^2 == 1.0 everywhere
    np.testing.assert_allclose(sin_sq + cos_sq, 1.0, atol=1e-5)


def test_day_of_week_and_weekend():
    """Verify correct Monday=0 .. Sunday=6 mapping and weekend indicators."""
    # 2026-08-07 is Friday (dow=4), 2026-08-08 is Saturday (dow=5), 2026-08-09 is Sunday (dow=6), 2026-08-10 is Monday (dow=0)
    # Pick 12:00 UTC = 15:00 Kyiv on each day
    days = [
        pd.Timestamp("2026-08-07 12:00:00", tz="UTC"),
        pd.Timestamp("2026-08-08 12:00:00", tz="UTC"),
        pd.Timestamp("2026-08-09 12:00:00", tz="UTC"),
        pd.Timestamp("2026-08-10 12:00:00", tz="UTC"),
    ]
    grid = pd.DatetimeIndex(days)
    empty_alerts = pd.DataFrame(columns=["started_at", "finished_at"])
    empty_neigh = pd.DataFrame(columns=["region", "started_at"])

    feats = build_feature_matrix(grid, empty_alerts, empty_neigh)

    # Friday
    assert feats.loc[days[0], "day_of_week_kyiv"] == 4.0
    assert feats.loc[days[0], "is_weekend_kyiv"] == 0.0

    # Saturday
    assert feats.loc[days[1], "day_of_week_kyiv"] == 5.0
    assert feats.loc[days[1], "is_weekend_kyiv"] == 1.0

    # Sunday
    assert feats.loc[days[2], "day_of_week_kyiv"] == 6.0
    assert feats.loc[days[2], "is_weekend_kyiv"] == 1.0

    # Monday
    assert feats.loc[days[3], "day_of_week_kyiv"] == 0.0
    assert feats.loc[days[3], "is_weekend_kyiv"] == 0.0
