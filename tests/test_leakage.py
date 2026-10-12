"""Tests for temporal leakage prevention and strict causality."""

import numpy as np
import pandas as pd
import pytest

from src.data.grid import compute_target, make_hourly_grid
from src.features.builder import build_feature_matrix, extract_single_timestamp_features


@pytest.fixture
def sample_data():
    """Create controlled sample alert history and neighbor starts."""
    kyiv_alerts = pd.DataFrame(
        [
            {
                "started_at": pd.Timestamp("2026-08-01 10:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 11:30:00", tz="UTC"),
                "duration_minutes": 90.0,
            },
            {
                "started_at": pd.Timestamp("2026-08-01 15:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 18:00:00", tz="UTC"),
                "duration_minutes": 180.0,
            },
            {
                "started_at": pd.Timestamp("2026-08-02 02:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-02 03:15:00", tz="UTC"),
                "duration_minutes": 75.0,
            },
            {
                "started_at": pd.Timestamp("2026-08-02 12:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-02 16:30:00", tz="UTC"),
                "duration_minutes": 270.0,
            },
        ]
    )

    neighbor_starts = pd.DataFrame(
        [
            {"region": "Київська область", "started_at": pd.Timestamp("2026-08-01 09:30:00", tz="UTC")},
            {"region": "Чернігівська область", "started_at": pd.Timestamp("2026-08-01 14:15:00", tz="UTC")},
            {"region": "Київська область", "started_at": pd.Timestamp("2026-08-02 01:45:00", tz="UTC")},
            {"region": "Сумська область", "started_at": pd.Timestamp("2026-08-02 11:10:00", tz="UTC")},
        ]
    )

    return kyiv_alerts, neighbor_starts


def test_leakage_immunity_on_future_corruption(sample_data):
    """Features for T <= T_cut must remain bit-for-bit identical when future data is corrupted."""
    kyiv_alerts, neighbor_starts = sample_data

    # Cutoff point
    t_cut = pd.Timestamp("2026-08-01 20:00:00", tz="UTC")
    grid = make_hourly_grid(pd.Timestamp("2026-08-01 00:00:00", tz="UTC"), t_cut)

    # 1. Original features
    feats_orig = build_feature_matrix(grid, kyiv_alerts, neighbor_starts)

    # 2. Corrupt future: add 50 fake new alerts and neighbor starts AFTER t_cut
    future_fake_alerts = pd.DataFrame(
        [
            {
                "started_at": t_cut + pd.Timedelta(hours=i),
                "finished_at": t_cut + pd.Timedelta(hours=i, minutes=45),
                "duration_minutes": 45.0,
            }
            for i in range(1, 20)
        ]
    )
    corrupted_alerts = pd.concat([kyiv_alerts, future_fake_alerts], ignore_index=True)

    future_fake_neigh = pd.DataFrame(
        [
            {
                "region": "Київська область",
                "started_at": t_cut + pd.Timedelta(minutes=30 * i),
            }
            for i in range(1, 30)
        ]
    )
    corrupted_neigh = pd.concat([neighbor_starts, future_fake_neigh], ignore_index=True)

    # Recompute features on the same grid <= t_cut
    feats_after_corruption = build_feature_matrix(grid, corrupted_alerts, corrupted_neigh)

    # Assert exact equality
    pd.testing.assert_frame_equal(feats_orig, feats_after_corruption)


def test_leakage_immunity_when_future_truncated(sample_data):
    """Features for T <= T_cut must match when all future records (> T_cut) are deleted."""
    kyiv_alerts, neighbor_starts = sample_data
    t_cut = pd.Timestamp("2026-08-01 20:00:00", tz="UTC")
    grid = make_hourly_grid(pd.Timestamp("2026-08-01 00:00:00", tz="UTC"), t_cut)

    feats_full = build_feature_matrix(grid, kyiv_alerts, neighbor_starts)

    # Drop everything after t_cut
    truncated_alerts = kyiv_alerts[kyiv_alerts["started_at"] <= t_cut].copy()
    truncated_neigh = neighbor_starts[neighbor_starts["started_at"] <= t_cut].copy()

    feats_truncated = build_feature_matrix(grid, truncated_alerts, truncated_neigh)
    pd.testing.assert_frame_equal(feats_full, feats_truncated)


def test_leakage_ongoing_alert_future_masking():
    """If an alert started <= T and ends > T, changing its future finish time must NOT affect features at T."""
    t = pd.Timestamp("2026-08-01 12:00:00", tz="UTC")
    grid = pd.DatetimeIndex([t])
    neigh = pd.DataFrame(columns=["region", "started_at"])

    # Alert starts at 11:00. In version A, it ends at 13:00.
    alerts_a = pd.DataFrame(
        [
            {
                "started_at": pd.Timestamp("2026-08-01 11:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 13:00:00", tz="UTC"),
                "duration_minutes": 120.0,
            }
        ]
    )

    # In version B, the alert ends at 20:00 (7 hours later!).
    alerts_b = pd.DataFrame(
        [
            {
                "started_at": pd.Timestamp("2026-08-01 11:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
                "duration_minutes": 540.0,
            }
        ]
    )

    # In version C, the alert is still ongoing (finished_at is NaT).
    alerts_c = pd.DataFrame(
        [
            {
                "started_at": pd.Timestamp("2026-08-01 11:00:00", tz="UTC"),
                "finished_at": pd.NaT,
                "duration_minutes": np.nan,
            }
        ]
    )

    feats_a = build_feature_matrix(grid, alerts_a, neigh)
    feats_b = build_feature_matrix(grid, alerts_b, neigh)
    feats_c = build_feature_matrix(grid, alerts_c, neigh)

    # At moment T = 12:00:00, all three cases MUST have identical feature values:
    # is_active = 1, active_duration = 60 min, minutes_last_1h = 60 min.
    pd.testing.assert_frame_equal(feats_a, feats_b)
    pd.testing.assert_frame_equal(feats_a, feats_c)

    assert feats_a.loc[t, "kyiv_is_active_at_T"] == 1.0
    assert feats_a.loc[t, "kyiv_active_duration_min_at_T"] == 60.0
    assert feats_a.loc[t, "kyiv_minutes_last_1h"] == 60.0


def test_single_extractor_matches_vectorized_matrix(sample_data):
    """Single timestamp extractor and vectorized matrix builder must yield identical features."""
    kyiv_alerts, neighbor_starts = sample_data
    grid = make_hourly_grid(
        pd.Timestamp("2026-08-01 00:00:00", tz="UTC"),
        pd.Timestamp("2026-08-02 18:00:00", tz="UTC"),
    )

    mat = build_feature_matrix(grid, kyiv_alerts, neighbor_starts)

    # Check 10 sample points across the timeline
    sample_points = grid[::4]
    for t in sample_points:
        single = extract_single_timestamp_features(t, kyiv_alerts, neighbor_starts)
        vec = mat.loc[t].to_dict()
        for col in single:
            diff = abs(single[col] - vec[col])
            assert diff < 1e-4, f"Feature {col} mismatch at {t}: single={single[col]}, vec={vec[col]}"


def test_target_strict_interval_bounds():
    """Target Y_T in [T, T+1h) must only be 1 if an alert actually intersects [T, T+1h)."""
    alerts = pd.DataFrame(
        [
            # Alert 1: strictly before [12:00, 13:00)
            {
                "started_at": pd.Timestamp("2026-08-01 10:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 11:59:59", tz="UTC"),
            },
            # Alert 2: strictly inside [14:00, 15:00)
            {
                "started_at": pd.Timestamp("2026-08-01 14:15:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 14:45:00", tz="UTC"),
            },
            # Alert 3: strictly after [16:00, 17:00)
            {
                "started_at": pd.Timestamp("2026-08-01 17:00:00", tz="UTC"),
                "finished_at": pd.Timestamp("2026-08-01 18:00:00", tz="UTC"),
            },
        ]
    )

    grid = make_hourly_grid(
        pd.Timestamp("2026-08-01 11:00:00", tz="UTC"),
        pd.Timestamp("2026-08-01 18:00:00", tz="UTC"),
    )
    targets = compute_target(grid, alerts)

    # T = 11:00 -> [11:00, 12:00) intersects Alert 1 (10:00-11:59:59) -> 1
    assert targets.loc[pd.Timestamp("2026-08-01 11:00:00", tz="UTC")] == 1
    # T = 12:00 -> [12:00, 13:00) no alert -> 0
    assert targets.loc[pd.Timestamp("2026-08-01 12:00:00", tz="UTC")] == 0
    # T = 13:00 -> [13:00, 14:00) no alert -> 0
    assert targets.loc[pd.Timestamp("2026-08-01 13:00:00", tz="UTC")] == 0
    # T = 14:00 -> [14:00, 15:00) intersects Alert 2 (14:15-14:45) -> 1
    assert targets.loc[pd.Timestamp("2026-08-01 14:00:00", tz="UTC")] == 1
    # T = 15:00 -> [15:00, 16:00) no alert -> 0
    assert targets.loc[pd.Timestamp("2026-08-01 15:00:00", tz="UTC")] == 0
    # T = 16:00 -> [16:00, 17:00) does NOT intersect Alert 3 (starts at 17:00) -> 0
    assert targets.loc[pd.Timestamp("2026-08-01 16:00:00", tz="UTC")] == 0
    # T = 17:00 -> [17:00, 18:00) intersects Alert 3 -> 1
    assert targets.loc[pd.Timestamp("2026-08-01 17:00:00", tz="UTC")] == 1
