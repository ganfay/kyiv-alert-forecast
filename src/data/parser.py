"""Parsers for Kyiv Digital alerts and volunteer regional datasets."""

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import List, Union
import zoneinfo
from bs4 import BeautifulSoup
import pandas as pd

logger = logging.getLogger(__name__)

KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
UTC_TZ = timezone.utc

NEIGHBOR_REGIONS: List[str] = [
    "Київська область",
    "Чернігівська область",
    "Сумська область",
    "Полтавська область",
    "Черкаська область",
    "Житомирська область",
]


def parse_kyiv_digital_html(html_source: Union[str, Path]) -> pd.DataFrame:
    """Parse Kyiv Digital HTML into a structured DataFrame of alert intervals in UTC.

    Columns returned:
        started_at: datetime64[ns, UTC]
        finished_at: datetime64[ns, UTC] (NaT if ongoing)
        duration_minutes: float (np.nan if ongoing)

    Sorted strictly ascending by started_at.
    """
    if isinstance(html_source, Path) or (isinstance(html_source, str) and "\n" not in html_source and Path(html_source).exists()):
        html_content = Path(html_source).read_text(encoding="utf-8")
    else:
        html_content = str(html_source)

    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table")
    if not table:
        raise ValueError("No table found in Kyiv Digital HTML.")

    rows = table.find_all("tr")
    raw_events = []

    # Table rows format:
    # <td>17:45 12.10.26</td> <td>🟢 Відбій тривоги</td> <td> 7 хвилин </td>
    # <td>17:38 12.10.26</td> <td>🔴 Повітряна тривога!</td> <td></td>
    time_regex = re.compile(r"(\d{2}):(\d{2})\s+(\d{2})\.(\d{2})\.(\d{2})")

    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        time_text = tds[0].get_text(strip=True)
        type_text = tds[1].get_text(strip=True)

        m = time_regex.match(time_text)
        if not m:
            continue
        hh, mm, dd, month, yy = map(int, m.groups())
        year = 2000 + yy

        dt_kyiv = datetime(year, month, dd, hh, mm, tzinfo=KYIV_TZ)
        dt_utc = dt_kyiv.astimezone(UTC_TZ)

        raw_events.append({
            "dt_utc": dt_utc,
            "is_start": "Повітряна тривога" in type_text,
            "is_end": "Відбій" in type_text,
        })

    # Rows in HTML are in reverse chronological order (newest first).
    # Reverse to process chronologically:
    events_chrono = list(reversed(raw_events))

    intervals = []
    current_start = None

    for ev in events_chrono:
        if ev["is_start"]:
            if current_start is not None:
                # Previous start was not closed by an explicit all-clear.
                # Cap it at min(ev.dt_utc, current_start + 4h) to avoid multi-month span.
                cap_end = min(ev["dt_utc"], current_start["dt_utc"] + pd.Timedelta(hours=4))
                dur = (cap_end - current_start["dt_utc"]).total_seconds() / 60.0
                intervals.append({
                    "started_at": current_start["dt_utc"],
                    "finished_at": cap_end,
                    "duration_minutes": dur,
                })
            current_start = ev
        elif ev["is_end"]:
            if current_start is not None:
                dur = (ev["dt_utc"] - current_start["dt_utc"]).total_seconds() / 60.0
                intervals.append({
                    "started_at": current_start["dt_utc"],
                    "finished_at": ev["dt_utc"],
                    "duration_minutes": dur,
                })
                current_start = None

    # If the alert is currently active at the newest moment:
    if current_start is not None:
        intervals.append({
            "started_at": current_start["dt_utc"],
            "finished_at": pd.NaT,
            "duration_minutes": float("nan"),
        })

    df = pd.DataFrame(intervals)
    if df.empty:
        return pd.DataFrame(columns=["started_at", "finished_at", "duration_minutes"])

    df["started_at"] = pd.to_datetime(df["started_at"], utc=True)
    df["finished_at"] = pd.to_datetime(df["finished_at"], utc=True)
    df["duration_minutes"] = df["duration_minutes"].astype(float)

    # Sort and reset index
    df = df.sort_values("started_at").reset_index(drop=True)
    return df


def parse_volunteer_neighbor_starts(
    csv_source: Union[str, Path, pd.DataFrame],
    regions: List[str] = NEIGHBOR_REGIONS,
) -> pd.DataFrame:
    """Parse volunteer dataset to extract alert start timestamps for neighbor regions.

    Discards finished_at and naive flags to avoid fabricated duration artifacts.
    Returns:
        region: str
        started_at: datetime64[ns, UTC]
    Sorted ascending by started_at.
    """
    if isinstance(csv_source, pd.DataFrame):
        df = csv_source.copy()
    else:
        df = pd.read_csv(csv_source, usecols=["region", "started_at"])

    # Filter to requested neighbor regions
    df = df[df["region"].isin(regions)].copy()
    df["started_at"] = pd.to_datetime(df["started_at"], utc=True)
    df = df[["region", "started_at"]].dropna(subset=["started_at"])
    df = df.sort_values("started_at").reset_index(drop=True)
    return df
