"""LightGBM classifier with hyperparameter tuning and parametric Platt (sigmoid) calibration."""

import logging
from typing import Any, Dict, List, Optional
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

logger = logging.getLogger(__name__)


class PlattCalibrator:
    """Parametric sigmoid probability calibration (Platt scaling).

    Fits a smooth logistic model P(y=1 | p) = sigma(A * logit(p) + B)
    on uncalibrated probabilities, avoiding the step-function overfitting
    and edge-clipping artifacts of non-parametric isotonic regression.
    """

    def __init__(self, C: float = 1.0):
        self.C = C
        self.lr_ = LogisticRegression(C=C, solver="lbfgs", random_state=42)

    def fit(self, probs: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        p = np.clip(probs, 1e-4, 1.0 - 1e-4)
        logits = np.log(p / (1.0 - p)).reshape(-1, 1)
        self.lr_.fit(logits, y)
        return self

    def predict(self, probs: np.ndarray) -> np.ndarray:
        p = np.clip(probs, 1e-4, 1.0 - 1e-4)
        logits = np.log(p / (1.0 - p)).reshape(-1, 1)
        return self.lr_.predict_proba(logits)[:, 1]


class CalibratedLightGBM:
    """LightGBM binary classifier with smooth post-hoc Platt (sigmoid) calibration."""

    def __init__(
        self,
        params: Optional[Dict[str, Any]] = None,
        use_calibration: bool = True,
        random_state: int = 42,
    ):
        self.params = params or {
            "n_estimators": 150,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 6,
            "min_child_samples": 50,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
        }
        self.use_calibration = use_calibration
        self.random_state = random_state

        self.model_: Optional[lgb.LGBMClassifier] = None
        self.calibrator_: Optional[PlattCalibrator] = None
        self.feature_names_: List[str] = []

    def fit_and_tune(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: pd.DataFrame,
        y_val: pd.Series,
        param_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Train LightGBM on train split, tune hyperparameters on validation split,

        and fit smooth Platt probability calibrator on validation split.
        """
        self.feature_names_ = list(X_train.columns)

        if param_candidates is None:
            param_candidates = [
                {"n_estimators": 100, "learning_rate": 0.03, "num_leaves": 15, "max_depth": 4, "min_child_samples": 50},
                {"n_estimators": 150, "learning_rate": 0.05, "num_leaves": 31, "max_depth": 6, "min_child_samples": 50},
                {"n_estimators": 200, "learning_rate": 0.03, "num_leaves": 31, "max_depth": 6, "min_child_samples": 100},
                {"n_estimators": 100, "learning_rate": 0.05, "num_leaves": 63, "max_depth": 8, "min_child_samples": 30},
                {"n_estimators": 250, "learning_rate": 0.02, "num_leaves": 31, "max_depth": 5, "min_child_samples": 50},
            ]

        best_score = float("inf")
        best_params = param_candidates[0]
        best_model = None

        logger.info("Tuning LightGBM across %d candidate configurations...", len(param_candidates))
        for p in param_candidates:
            clf = lgb.LGBMClassifier(
                **p,
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=self.random_state,
                verbose=-1,
                n_jobs=-1,
            )
            clf.fit(X_train, y_train)
            val_probs = clf.predict_proba(X_val)[:, 1]
            score = brier_score_loss(y_val, val_probs)
            logger.info("Params %s -> Validation Brier Score: %.5f", p, score)
            if score < best_score:
                best_score = score
                best_params = p
                best_model = clf

        self.params = best_params
        self.model_ = best_model

        # Fit smooth Platt calibrator on validation predictions
        raw_val_probs = self.model_.predict_proba(X_val)[:, 1]
        raw_val_brier = brier_score_loss(y_val, raw_val_probs)

        if self.use_calibration:
            cal = PlattCalibrator(C=1.0)
            cal.fit(raw_val_probs, y_val.values)
            cal_val_probs = cal.predict(raw_val_probs)
            cal_val_brier = brier_score_loss(y_val, cal_val_probs)

            # Retain calibrator only if it improves or maintains validation Brier score
            if cal_val_brier <= raw_val_brier:
                self.calibrator_ = cal
                logger.info(
                    "Platt calibrator retained: Validation Brier improved from %.5f to %.5f",
                    raw_val_brier,
                    cal_val_brier,
                )
            else:
                self.calibrator_ = None
                logger.info("Platt calibrator omitted (raw validation score was better).")
        else:
            self.calibrator_ = None

        return {
            "best_params": self.params,
            "val_brier_raw": raw_val_brier,
            "val_brier_calibrated": cal_val_brier if self.calibrator_ else raw_val_brier,
        }

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if self.model_ is None:
            raise ValueError("Model has not been fitted.")

        raw_probs = self.model_.predict_proba(X[self.feature_names_])[:, 1]
        if self.calibrator_ is not None:
            probs = self.calibrator_.predict(raw_probs)
        else:
            probs = raw_probs

        return np.clip(probs, 1e-6, 1.0 - 1e-6)

    def predict_proba_raw(self, X: pd.DataFrame) -> np.ndarray:
        if self.model_ is None:
            raise ValueError("Model has not been fitted.")
        raw_probs = self.model_.predict_proba(X[self.feature_names_])[:, 1]
        return np.clip(raw_probs, 1e-6, 1.0 - 1e-6)

    @property
    def feature_importances_(self) -> pd.DataFrame:
        if self.model_ is None:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "feature": self.feature_names_,
                "importance_split": self.model_.feature_importances_,
                "importance_gain": self.model_.booster_.feature_importance(importance_type="gain"),
            }
        ).sort_values("importance_gain", ascending=False).reset_index(drop=True)
