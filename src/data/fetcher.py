"""Data fetcher with snapshot archiving for Kyiv Digital and volunteer datasets."""

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Optional
import requests

logger = logging.getLogger(__name__)

KYIV_DIGITAL_URL = "https://kyiv.digital/storage/air-alert/stats.html"
VOLUNTEER_CSV_URL = (
    "https://raw.githubusercontent.com/Vadimkin/ukrainian-air-raid-sirens-dataset/"
    "main/datasets/volunteer_data_uk.csv"
)


def get_latest_snapshot(snapshots_dir: Path = Path("data/snapshots")) -> Optional[Path]:
    """Return path to the most recent snapshot in snapshots_dir by name, or None."""
    if not snapshots_dir.exists():
        return None
    snapshots = sorted(snapshots_dir.glob("stats_*.html"))
    return snapshots[-1] if snapshots else None


def fetch_kyiv_digital_snapshot(
    snapshots_dir: Path = Path("data/snapshots"),
    force_fetch: bool = False,
    timeout: int = 30,
) -> Path:
    """Fetch Kyiv Digital alerts HTML page, archiving a timestamped snapshot.

    If force_fetch is False and an existing snapshot exists, returns the newest snapshot.
    Otherwise fetches from the live URL, validates the content, writes atomically,
    and returns the new snapshot path.
    """
    snapshots_dir = Path(snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    if not force_fetch:
        latest = get_latest_snapshot(snapshots_dir)
        if latest is not None:
            logger.info("Using cached Kyiv Digital snapshot: %s", latest)
            return latest

    logger.info("Fetching fresh Kyiv Digital snapshot from %s", KYIV_DIGITAL_URL)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(KYIV_DIGITAL_URL, headers=headers, timeout=timeout)
    response.raise_for_status()

    content = response.text
    if "<table" not in content or "Повітряна тривога" not in content:
        raise ValueError("Response does not appear to be a valid Kyiv Digital alert page.")

    now_utc = datetime.now(timezone.utc)
    filename = f"stats_{now_utc.strftime('%Y%m%d_%H%M%SZ')}.html"
    snapshot_path = snapshots_dir / filename

    # Atomic write via temporary file
    temp_path = snapshots_dir / f".tmp_{filename}"
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(snapshot_path)

    logger.info("Saved fresh Kyiv Digital snapshot to %s", snapshot_path)
    return snapshot_path


def fetch_volunteer_dataset(
    cache_path: Path = Path("data/raw/volunteer_data_uk.csv"),
    force_fetch: bool = False,
    timeout: int = 60,
) -> Path:
    """Download volunteer dataset from GitHub repository or return cached path."""
    cache_path = Path(cache_path)
    if cache_path.exists() and not force_fetch:
        logger.info("Using cached volunteer dataset at %s", cache_path)
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading volunteer dataset from %s", VOLUNTEER_CSV_URL)
    response = requests.get(VOLUNTEER_CSV_URL, timeout=timeout)
    response.raise_for_status()

    temp_path = cache_path.parent / f".tmp_{cache_path.name}"
    temp_path.write_bytes(response.content)
    temp_path.replace(cache_path)

    logger.info("Saved volunteer dataset to %s", cache_path)
    return cache_path
