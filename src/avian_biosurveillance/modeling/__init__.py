"""Modeling utilities for avian anomaly scoring."""

from .anomaly_model import BehaviorAnomalyModelConfig, score_behavior_anomalies
from .count_baseline import CountBaselineConfig, score_weekly_count_anomalies
from .embedding_drift import (
    EmbeddingDriftConfig,
    score_embedding_clusters,
    score_embedding_drift,
)
from .environmental_context import add_environmental_context
from .evaluation import build_validation_report
from .feature_set import build_species_week_feature_set
from .pipeline import run_model_anomaly_scoring
from .schemas import ModelingRunStats
from .settings import SETTINGS, ModelingSettings, load_modeling_settings


__all__ = [
    "SETTINGS",
    "BehaviorAnomalyModelConfig",
    "CountBaselineConfig",
    "EmbeddingDriftConfig",
    "ModelingRunStats",
    "ModelingSettings",
    "add_environmental_context",
    "build_species_week_feature_set",
    "build_validation_report",
    "load_modeling_settings",
    "run_model_anomaly_scoring",
    "score_behavior_anomalies",
    "score_embedding_clusters",
    "score_embedding_drift",
    "score_weekly_count_anomalies",
]
