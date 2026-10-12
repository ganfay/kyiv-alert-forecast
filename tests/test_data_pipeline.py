"""Tests for data fetching, parsing, and grid splitting pipeline."""

from pathlib import Path
import pandas as pd
import pytest

from src.data.fetcher import get_latest_snapshot
from src.data.grid import (
    TRAIN_START,
    VAL_START,
    TEST_START,
    TEST_END,
    compute_target,
    make_hourly_grid,
    split_time_series,
)
from src.data.parser import NEIGHBOR_REGIONS, parse_kyiv_digital_html, parse_volunteer_neighbor_starts


def test_real_snapshot_parsing():
    """Verify parsing on the actual downloaded Kyiv Digital snapshot."""
    snapshot = get_latest_snapshot(Path("data/snapshots"))
    assert snapshot is not None, "Snapshot must exist in data/snapshots"

    alerts = parse_kyiv_digital_html(snapshot)
    assert not alerts.empty
    assert list(alerts.columns) == ["started_at", "finished_at", "duration_minutes"]

    # Monotonicity check
    assert alerts["started_at"].is_monotonic_increasing

    # Duration sanity check
    closed = alerts.dropna(subset=["finished_at"])
    assert (closed["finished_at"] >= closed["started_at"]).all()
    assert (closed["duration_minutes"] >= 0.0).all()


def test_volunteer_neighbor_parsing():
    """Verify parsing on volunteer dataset for neighbor regions."""
    csv_path = Path("data/raw/volunteer_data_uk.csv")
    if not csv_path.exists():
        pytest.skip("Volunteer CSV not present locally.")

    neigh = parse_volunteer_neighbor_starts(csv_path)
    assert not neigh.empty
    assert set(neigh["region"].unique()).issubset(set(NEIGHBOR_REGIONS))
    assert neigh["started_at"].dt.tz is not None
    assert neigh["started_at"].is_monotonic_increasing


def test_grid_partition_integrity():
    """Verify that hourly grid and time-series split have zero gaps and zero overlaps."""
    grid = make_hourly_grid(TRAIN_START, TEST_END)
    assert len(grid) == 40478

    df = pd.DataFrame({"dummy": 1}, index=grid)
    train, val, test = split_time_series(df)

    assert len(train) == 39134
    assert len(val) == 672
    assert len(test) == 672
    assert len(train) + len(val) + len(test) == len(df)

    # Check boundaries
    assert train.index.min() == TRAIN_START
    assert train.index.max() == VAL_START - pd.Timedelta(hours=1)

    assert val.index.min() == VAL_START
    assert val.index.max() == TEST_START - pd.Timedelta(hours=1)

    assert test.index.min() == TEST_START
    assert test.index.max() == TEST_END - pd.Timedelta(hours=1)
