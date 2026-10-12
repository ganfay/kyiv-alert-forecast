"""CLI inference interface for Kyiv air alert probability forecasting [T, T+1h)."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zoneinfo
import joblib
import pandas as pd

from src.data.fetcher import fetch_kyiv_digital_snapshot, get_latest_snapshot
from src.data.parser import parse_kyiv_digital_html, parse_volunteer_neighbor_starts
from src.features.builder import extract_single_timestamp_features

KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")
UTC_TZ = timezone.utc


def parse_input_timestamp(ts_str: str) -> pd.Timestamp:
    """Parse user-provided timestamp string, handling UTC and Kyiv local time."""
    ts = pd.to_datetime(ts_str)
    if ts.tz is None:
        # Default naive string to Europe/Kyiv, then convert to UTC
        ts_kyiv = ts.tz_localize(KYIV_TZ)
        return ts_kyiv.tz_convert(UTC_TZ)
    return ts.tz_convert(UTC_TZ)


def main():
    parser = argparse.ArgumentParser(
        description="Forecast probability of an air alert in Kyiv for window [T, T+1h) using strictly data <= T."
    )
    parser.add_argument(
        "--timestamp",
        "-t",
        type=str,
        default=None,
        help="Forecast moment T (e.g. '2026-10-12 18:00:00' or '2026-10-12T15:00:00Z'). Defaults to current time.",
    )
    parser.add_argument(
        "--model-path",
        type=str,
        default="reports/models/lightgbm_calibrated.joblib",
        help="Path to trained LightGBM model artifact.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force fetch fresh snapshot from Kyiv Digital.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output forecast as JSON.",
    )

    args = parser.parse_args()

    # 1. Determine Moment T
    if args.timestamp:
        t_utc = parse_input_timestamp(args.timestamp)
    else:
        # Current time floored to minute
        now_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        t_utc = pd.Timestamp(now_dt)

    t_kyiv = t_utc.astimezone(KYIV_TZ)
    t_next_utc = t_utc + pd.Timedelta(hours=1)
    t_next_kyiv = t_kyiv + pd.Timedelta(hours=1)

    # 2. Ingest Data <= T
    snapshot_path = get_latest_snapshot()
    if snapshot_path is None or args.force_refresh:
        snapshot_path = fetch_kyiv_digital_snapshot(force_fetch=args.force_refresh)

    alerts_df = parse_kyiv_digital_html(snapshot_path)

    neighbor_path = Path("data/raw/volunteer_data_uk.csv")
    if neighbor_path.exists():
        neighbor_df = parse_volunteer_neighbor_starts(neighbor_path)
    else:
        neighbor_df = pd.DataFrame(columns=["region", "started_at"])

    # 3. Extract Strictly Causal Features <= T
    feats_dict = extract_single_timestamp_features(t_utc, alerts_df, neighbor_df)
    X_single = pd.DataFrame([feats_dict])

    # 4. Load Models & Score
    model_path = Path(args.model_path)
    if not model_path.exists():
        sys.exit(f"Error: Model artifact not found at {model_path}. Run training first via src/train_and_evaluate.py.")

    lgbm = joblib.load(model_path)
    prob_lgbm = float(lgbm.predict_proba(X_single)[0])
    prob_raw = float(lgbm.predict_proba_raw(X_single)[0]) if hasattr(lgbm, "predict_proba_raw") else prob_lgbm

    # Baselines
    clim_path = Path("reports/models/climatology_baseline.joblib")
    pers_path = Path("reports/models/persistence_baseline.joblib")

    prob_clim = float(joblib.load(clim_path).predict_proba(X_single)[0]) if clim_path.exists() else 0.14
    prob_pers = float(joblib.load(pers_path).predict_proba(X_single)[0]) if pers_path.exists() else 0.50

    is_active = bool(feats_dict["kyiv_is_active_at_T"])
    active_dur = float(feats_dict["kyiv_active_duration_min_at_T"])
    time_since_end = float(feats_dict["kyiv_hours_since_last_end"])
    neigh_starts_1h = int(feats_dict["neighbor_starts_last_1h"])
    neigh_starts_3h = int(feats_dict["neighbor_starts_last_3h"])

    result_payload = {
        "moment_T_utc": t_utc.isoformat(),
        "moment_T_kyiv": t_kyiv.isoformat(),
        "forecast_window_utc": f"[{t_utc.strftime('%Y-%m-%d %H:%M')}, {t_next_utc.strftime('%Y-%m-%d %H:%M')})",
        "forecast_window_kyiv": f"[{t_kyiv.strftime('%Y-%m-%d %H:%M')}, {t_next_kyiv.strftime('%Y-%m-%d %H:%M')})",
        "kyiv_status_at_T": {
            "is_active": is_active,
            "active_duration_minutes": active_dur if is_active else 0.0,
            "hours_since_last_all_clear": time_since_end if not is_active else 0.0,
        },
        "forecast_probability_lgbm_calibrated": round(prob_lgbm, 4),
        "forecast_probability_lgbm_raw": round(prob_raw, 4),
        "baseline_persistence": round(prob_pers, 4),
        "baseline_climatology": round(prob_clim, 4),
        "neighbor_activity": {
            "starts_last_1h": neigh_starts_1h,
            "starts_last_3h": neigh_starts_3h,
        },
    }

    if args.json:
        print(json.dumps(result_payload, indent=2, ensure_ascii=False))
        return

    # Formatted terminal display
    status_icon = "🔴 ТРИВОГА ТРИВАЄ" if is_active else "🟢 ТРИВОГИ НЕМАЄ"
    status_detail = (
        f"триває вже {int(active_dur)} хв"
        if is_active
        else f"відбій {time_since_end:.1f} год тому"
    )

    print("=" * 70)
    print("🚨 KYIV AIR ALERT FORECAST [T, T+1h)")
    print("=" * 70)
    print(f" Момент прогнозу T:")
    print(f"   Київський час:  {t_kyiv.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"   UTC:            {t_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f" Вікно прогнозу:   [{t_kyiv.strftime('%H:%M')}, {t_next_kyiv.strftime('%H:%M')}) за Києвом")
    print(f" Поточний стан у Т: {status_icon} ({status_detail})")
    print("-" * 70)
    print(f" 🎯 ЙМОВІРНІСТЬ ТРИВОГИ В КИЄВІ (LightGBM):  {prob_lgbm * 100:.1f}%")
    print(f"    ├─ Калібрована (Platt / Sigmoid):         {prob_lgbm * 100:.1f}%")
    print(f"    └─ Сира оцінка моделі (Raw LightGBM):     {prob_raw * 100:.1f}%")
    print(f"    Інерційний бейзлайн (Persistence):        {prob_pers * 100:.1f}%")
    print(f"    Кліматологічний бейзлайн (Climatology):   {prob_clim * 100:.1f}%")
    print("-" * 70)
    print(f" 🛰️  Сусідні області (Київська, Чернігів, Суми, тощо):")
    print(f"    Тривог почалось за останню 1 год: {neigh_starts_1h}")
    print(f"    Тривог почалось за останні 3 год: {neigh_starts_3h}")
    print("=" * 70)


if __name__ == "__main__":
    main()
