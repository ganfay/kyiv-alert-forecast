"""Baseline models for Kyiv air alert probability forecasting."""

from typing import Dict, Optional
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


class ClimatologyBaseline:
    """Predicts alert probability based on historical hourly base rate by hour-of-day in Kyiv time."""

    def __init__(self):
        self.hourly_rates_: Dict[int, float] = {}
        self.global_rate_: float = 0.14

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "ClimatologyBaseline":
        self.global_rate_ = float(y.mean())
        df = pd.DataFrame({"hour": X["hour_of_day_kyiv"].astype(int), "y": y})
        grouped = df.groupby("hour")["y"].mean().to_dict()
        self.hourly_rates_ = {h: grouped.get(h, self.global_rate_) for h in range(24)}
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        hours = X["hour_of_day_kyiv"].astype(int).values
        probs = np.array([self.hourly_rates_.get(h, self.global_rate_) for h in hours], dtype=float)
        return np.clip(probs, 1e-6, 1.0 - 1e-6)


class PersistenceBaseline:
    """Predicts alert probability conditioned strictly on whether an alert is already active at moment T."""

    def __init__(self):
        self.p_active_: float = 0.95
        self.p_inactive_: float = 0.05

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "PersistenceBaseline":
        is_active = X["kyiv_is_active_at_T"].astype(int)
        mask_active = is_active == 1
        mask_inactive = is_active == 0

        self.p_active_ = float(y[mask_active].mean()) if mask_active.sum() > 0 else 0.9
        self.p_inactive_ = float(y[mask_inactive].mean()) if mask_inactive.sum() > 0 else 0.05
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        is_active = X["kyiv_is_active_at_T"].astype(int).values
        probs = np.where(is_active == 1, self.p_active_, self.p_inactive_).astype(float)
        return np.clip(probs, 1e-6, 1.0 - 1e-6)


class LogisticRegressionModel:
    """Standardized L2-regularized logistic regression baseline."""

    def __init__(self, C: float = 1.0, random_state: int = 42):
        self.C = C
        self.random_state = random_state
        self.pipeline_ = Pipeline(
            [
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(C=C, max_iter=1000, random_state=random_state)),
            ]
        )

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "LogisticRegressionModel":
        self.pipeline_.fit(X, y)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        probs = self.pipeline_.predict_proba(X)[:, 1]
        return np.clip(probs, 1e-6, 1.0 - 1e-6)
