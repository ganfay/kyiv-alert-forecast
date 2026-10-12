"""Train and evaluate models on chronological splits with strict leakage defense.

Evaluates 4 models on the Test split (last 28 days: 2026-09-14 to 2026-10-12):
1. Climatology Baseline
2. Persistence Baseline
3. Logistic Regression
4. Calibrated LightGBM (Tuned & calibrated on Validation split)

Generates:
- Comprehensive metrics table (Brier Score, BSS, ROC-AUC, PR-AUC, LogLoss)
- Stratified metrics: new alert onsets vs ongoing alert continuations
- High-resolution figures in reports/figures/
"""

import json
import logging
from pathlib import Path
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)

from src.data.fetcher import get_latest_snapshot
from src.data.grid import (
    TEST_END,
    TEST_START,
    TRAIN_START,
    VAL_START,
    compute_target,
    make_hourly_grid,
    split_time_series,
)
from src.data.parser import parse_kyiv_digital_html, parse_volunteer_neighbor_starts
from src.features.builder import build_feature_matrix
from src.models import (
    CalibratedLightGBM,
    ClimatologyBaseline,
    LogisticRegressionModel,
    PersistenceBaseline,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def compute_metrics(y_true: np.ndarray, y_prob: np.ndarray, climatology_brier: float = None) -> dict:
    """Compute standard classification and calibration metrics."""
    brier = float(brier_score_loss(y_true, y_prob, pos_label=1))
    bss = float(1.0 - (brier / climatology_brier)) if (climatology_brier and climatology_brier > 0) else 0.0

    # ROC-AUC and PR-AUC require at least one positive and one negative sample
    n_pos = int(np.sum(y_true == 1))
    n_neg = int(np.sum(y_true == 0))

    if n_pos > 0 and n_neg > 0:
        roc_auc = float(roc_auc_score(y_true, y_prob))
        pr_auc = float(average_precision_score(y_true, y_prob))
    else:
        roc_auc = float("nan")
        pr_auc = float("nan")

    ll = float(log_loss(y_true, np.clip(y_prob, 1e-6, 1.0 - 1e-6), labels=[0, 1]))

    return {
        "brier_score": round(brier, 5),
        "brier_skill_score": round(bss, 4),
        "roc_auc": round(roc_auc, 4) if not np.isnan(roc_auc) else "N/A",
        "pr_auc": round(pr_auc, 4) if not np.isnan(pr_auc) else "N/A",
        "log_loss": round(ll, 4),
        "n_samples": len(y_true),
        "positive_rate": round(float(np.mean(y_true)), 4),
    }


def main():
    figures_dir = Path("reports/figures")
    figures_dir.mkdir(parents=True, exist_ok=True)
    models_dir = Path("reports/models")
    models_dir.mkdir(parents=True, exist_ok=True)

    # 1. Ingest Data
    logger.info("Loading latest datasets...")
    snapshot = get_latest_snapshot()
    if snapshot is None:
        raise FileNotFoundError("No Kyiv Digital snapshot found in data/snapshots.")

    alerts_df = parse_kyiv_digital_html(snapshot)
    neighbor_csv = Path("data/raw/volunteer_data_uk.csv")
    neighbor_df = parse_volunteer_neighbor_starts(neighbor_csv) if neighbor_csv.exists() else pd.DataFrame()

    logger.info("Parsed %d Kyiv alerts and %d neighbor starts.", len(alerts_df), len(neighbor_df))

    # 2. Build Grid & Target
    logger.info("Building hourly grid [%s to %s]...", TRAIN_START, TEST_END)
    grid = make_hourly_grid(TRAIN_START, TEST_END)
    y_all = compute_target(grid, alerts_df)

    # 3. Build Feature Matrix
    logger.info("Extracting causal features for %d hourly steps...", len(grid))
    X_all = build_feature_matrix(grid, alerts_df, neighbor_df)

    # Combine into unified frame for splitting
    df_all = X_all.copy()
    df_all["target"] = y_all

    # 4. Strict Chronological Split
    logger.info("Splitting dataset into Train, Val, Test...")
    train_df, val_df, test_df = split_time_series(df_all)

    X_train, y_train = train_df.drop(columns=["target"]), train_df["target"]
    X_val, y_val = val_df.drop(columns=["target"]), val_df["target"]
    X_test, y_test = test_df.drop(columns=["target"]), test_df["target"]

    logger.info(
        "Train size: %d hours (pos: %.1f%%) | Val: %d hours (pos: %.1f%%) | Test: %d hours (pos: %.1f%%)",
        len(X_train),
        y_train.mean() * 100,
        len(X_val),
        y_val.mean() * 100,
        len(X_test),
        y_test.mean() * 100,
    )

    # 5. Fit Models
    models = {}

    # Baseline 1: Climatology
    logger.info("Fitting Climatology baseline...")
    clim = ClimatologyBaseline().fit(X_train, y_train)
    models["Climatology"] = clim

    # Baseline 2: Persistence
    logger.info("Fitting Persistence baseline...")
    pers = PersistenceBaseline().fit(X_train, y_train)
    models["Persistence"] = pers

    # Baseline 3: Logistic Regression
    logger.info("Fitting Logistic Regression baseline...")
    lr = LogisticRegressionModel().fit(X_train, y_train)
    models["LogisticRegression"] = lr

    # Primary Model: LightGBM (tuned & calibrated on validation split)
    logger.info("Fitting, tuning, and calibrating LightGBM...")
    lgbm = CalibratedLightGBM()
    tune_res = lgbm.fit_and_tune(X_train, y_train, X_val, y_val)
    logger.info("LightGBM best validation result: %s", tune_res)
    models["LightGBM"] = lgbm

    # Save model artifacts
    joblib.dump(lgbm, models_dir / "lightgbm_calibrated.joblib")
    joblib.dump(clim, models_dir / "climatology_baseline.joblib")
    joblib.dump(pers, models_dir / "persistence_baseline.joblib")
    joblib.dump(lr, models_dir / "logistic_regression.joblib")

    # 6. Evaluate on Test Split
    logger.info("Evaluating all models on Test split (last 28 days: %s to %s)...", TEST_START, TEST_END)

    test_preds = {}
    for name, m in models.items():
        test_preds[name] = m.predict_proba(X_test)

    clim_test_brier = brier_score_loss(y_test, test_preds["Climatology"], pos_label=1)

    # Masks for stratified evaluation
    mask_inactive = (X_test["kyiv_is_active_at_T"] == 0).values
    mask_active = (X_test["kyiv_is_active_at_T"] == 1).values

    clim_inactive_brier = brier_score_loss(y_test[mask_inactive], test_preds["Climatology"][mask_inactive], pos_label=1)
    clim_active_brier = brier_score_loss(y_test[mask_active], test_preds["Climatology"][mask_active], pos_label=1)

    results_overall = {}
    results_inactive = {}
    results_active = {}

    for name, p in test_preds.items():
        results_overall[name] = compute_metrics(y_test.values, p, climatology_brier=clim_test_brier)
        results_inactive[name] = compute_metrics(y_test.values[mask_inactive], p[mask_inactive], climatology_brier=clim_inactive_brier)
        results_active[name] = compute_metrics(y_test.values[mask_active], p[mask_active], climatology_brier=clim_active_brier)

    # 7. Print Terminal Summary Tables
    print("\n" + "=" * 95)
    print("TEST EVALUATION REPORT (LAST 28 DAYS: 2026-09-14 14:00 to 2026-10-12 14:00 UTC, 672 HOURS)")
    print("=" * 95)

    print("\n--- 1. OVERALL TEST METRICS (ALL 672 HOURS) ---")
    df_res_overall = pd.DataFrame(results_overall).T
    print(df_res_overall[["brier_score", "brier_skill_score", "roc_auc", "pr_auc", "log_loss", "n_samples"]].to_string())

    print(f"\n--- 2. SUBSET A: NO ALERT AT T (NEW ALERT ONSET) [N = {mask_inactive.sum()} hours] ---")
    df_res_inactive = pd.DataFrame(results_inactive).T
    print(df_res_inactive[["brier_score", "brier_skill_score", "roc_auc", "pr_auc", "log_loss", "positive_rate"]].to_string())

    print(f"\n--- 3. SUBSET B: ALERT ONGOING AT T (CONTINUATION) [N = {mask_active.sum()} hours] ---")
    df_res_active = pd.DataFrame(results_active).T
    print(df_res_active[["brier_score", "brier_skill_score", "roc_auc", "pr_auc", "log_loss", "positive_rate"]].to_string())

    # Save metrics JSON
    metrics_summary = {
        "overall": results_overall,
        "new_alert_onset": results_inactive,
        "alert_continuation": results_active,
        "tuning": tune_res,
    }
    with open("reports/metrics_test.json", "w", encoding="utf-8") as f:
        json.dump(metrics_summary, f, indent=2)

    # 8. Generate & Save Figures
    logger.info("Generating figures in %s...", figures_dir)
    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")

    # Figure 1: ROC & PR Curves
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    colors = {"Climatology": "gray", "Persistence": "orange", "LogisticRegression": "blue", "LightGBM": "green"}

    # ROC
    for name, p in test_preds.items():
        fpr, tpr, _ = roc_curve(y_test, p)
        auc_val = roc_auc_score(y_test, p)
        axes[0].plot(fpr, tpr, label=f"{name} (AUC = {auc_val:.3f})", color=colors[name], lw=2)
    axes[0].plot([0, 1], [0, 1], "k--", alpha=0.5)
    axes[0].set_title("ROC Curve — Out-of-Sample Test (28 Days)", fontsize=13, fontweight="bold")
    axes[0].set_xlabel("False Positive Rate", fontsize=11)
    axes[0].set_ylabel("True Positive Rate", fontsize=11)
    axes[0].legend(loc="lower right")

    # PR
    for name, p in test_preds.items():
        prec, rec, _ = precision_recall_curve(y_test, p)
        pr_val = average_precision_score(y_test, p)
        axes[1].plot(rec, prec, label=f"{name} (PR-AUC = {pr_val:.3f})", color=colors[name], lw=2)
    axes[1].axhline(y_test.mean(), color="k", linestyle="--", alpha=0.5, label=f"Base Rate ({y_test.mean():.2f})")
    axes[1].set_title("Precision-Recall Curve — Out-of-Sample Test", fontsize=13, fontweight="bold")
    axes[1].set_xlabel("Recall", fontsize=11)
    axes[1].set_ylabel("Precision", fontsize=11)
    axes[1].legend(loc="upper right")
    plt.tight_layout()
    fig1_path = figures_dir / "roc_pr_curves.png"
    plt.savefig(fig1_path, dpi=200)
    plt.close()

    # Figure 2: Calibration Curves (Reliability Diagram)
    fig, ax = plt.subplots(figsize=(8, 7))
    for name, p in test_preds.items():
        prob_true, prob_pred = calibration_curve(y_test, p, n_bins=10, strategy="uniform")
        ax.plot(prob_pred, prob_true, marker="o", label=f"{name}", color=colors[name], lw=2)
    ax.plot([0, 1], [0, 1], "k--", label="Perfectly Calibrated", alpha=0.7)
    ax.set_title("Calibration Curve (Reliability Diagram) — Test Split", fontsize=13, fontweight="bold")
    ax.set_xlabel("Mean Predicted Probability", fontsize=11)
    ax.set_ylabel("Fraction of Positives (Empirical)", fontsize=11)
    ax.legend(loc="upper left")
    plt.tight_layout()
    fig2_path = figures_dir / "calibration_curve.png"
    plt.savefig(fig2_path, dpi=200)
    plt.close()

    # Figure 3: Feature Importance (Gain)
    fi_df = lgbm.feature_importances_
    top_fi = fi_df.head(12).sort_values("importance_gain", ascending=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.barh(top_fi["feature"], top_fi["importance_gain"], color="#2b5c8f")
    ax.set_title("LightGBM Feature Importance (Gain)", fontsize=13, fontweight="bold")
    ax.set_xlabel("Total Gain", fontsize=11)
    plt.tight_layout()
    fig3_path = figures_dir / "feature_importance.png"
    plt.savefig(fig3_path, dpi=200)
    plt.close()

    # Figure 4: Timeline (Last 7 Days of Test: Actual vs Forecast)
    last_7d_start = TEST_END - pd.Timedelta(days=7)
    t_mask = (y_test.index >= last_7d_start) & (y_test.index < TEST_END)

    t_idx = y_test.index[t_mask]
    y_sub = y_test[t_mask]
    p_lgbm_sub = test_preds["LightGBM"][t_mask]
    p_pers_sub = test_preds["Persistence"][t_mask]

    fig, ax = plt.subplots(figsize=(15, 6))
    # Plot actual alert intervals as shaded areas
    ax.fill_between(t_idx, 0, y_sub.values, color="red", alpha=0.25, step="post", label="Actual Alert Active in [T, T+1h)")
    ax.plot(t_idx, p_lgbm_sub, label="LightGBM Predicted Prob", color="darkgreen", lw=2)
    ax.plot(t_idx, p_pers_sub, label="Persistence Baseline", color="orange", lw=1.5, linestyle="--")

    ax.set_title("Forecast Timeline vs Ground Truth (Last 7 Days: Oct 5 to Oct 12, 2026)", fontsize=13, fontweight="bold")
    ax.set_xlabel("Time (UTC)", fontsize=11)
    ax.set_ylabel("Probability / Active State", fontsize=11)
    ax.set_ylim(-0.05, 1.05)
    ax.legend(loc="upper left")
    plt.tight_layout()
    fig4_path = figures_dir / "timeline_actual_vs_pred.png"
    plt.savefig(fig4_path, dpi=200)
    plt.close()

    print("\n" + "=" * 95)
    print("SAVED ARTIFACTS AND FIGURES:")
    print("=" * 95)
    print(f"  - Metrics: reports/metrics_test.json")
    print(f"  - Model:   reports/models/lightgbm_calibrated.joblib")
    print(f"  - Figure 1: {fig1_path}")
    print(f"  - Figure 2: {fig2_path}")
    print(f"  - Figure 3: {fig3_path}")
    print(f"  - Figure 4: {fig4_path}")


if __name__ == "__main__":
    main()
