
"""provcat.backends_mlflow
MLflow helper layer to be reused by both CLI and inline Python.
"""
from __future__ import annotations
from typing import Dict, Any, Optional
import mlflow

def _client() -> mlflow.tracking.MlflowClient:
    return mlflow.tracking.MlflowClient()

def set_tracking_uri(uri: str | None) -> None:
    if uri:
        mlflow.set_tracking_uri(uri)

def ensure_experiment(name: str, artifact_location: Optional[str] = None, tags: Optional[Dict[str, str]] = None) -> str:
    client = _client()
    exp = mlflow.get_experiment_by_name(name)
    if exp is None:
        return client.create_experiment(name, artifact_location=artifact_location, tags=tags or {})
    else:
        if tags:
            for k, v in tags.items():
                client.set_experiment_tag(exp.experiment_id, k, v)
        return exp.experiment_id

def start_run(experiment_name: str, run_name: Optional[str] = None, parent_run_id: Optional[str] = None,
              tags: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> str:
    exp = mlflow.get_experiment_by_name(experiment_name)
    if exp is None:
        exp_id = ensure_experiment(experiment_name)
    else:
        exp_id = exp.experiment_id
    with mlflow.start_run(experiment_id=exp_id, run_name=run_name, nested=bool(parent_run_id)) as run:
        rid = run.info.run_id
        if parent_run_id:
            mlflow.set_tag("mlflow.parentRunId", parent_run_id)
        if tags:
            for k, v in tags.items():
                mlflow.set_tag(k, v)
        if params:
            for k, v in params.items():
                mlflow.log_param(k, v)
        return rid

def end_run(status: str = "FINISHED") -> None:
    mlflow.end_run(status=status)

def log_text(name: str, text: str, artifact_path: Optional[str] = None) -> None:
    mlflow.log_text(text, artifact_file=name, artifact_path=artifact_path)

def log_dict(obj: Dict[str, Any], name: str = "data.json", artifact_path: Optional[str] = None) -> None:
    mlflow.log_dict(obj, artifact_file=name, artifact_path=artifact_path)

def log_file(path: str, artifact_path: Optional[str] = None) -> None:
    mlflow.log_artifact(path, artifact_path=artifact_path)

def register_model_version(model_name: str, source_path: str, description: Optional[str] = None,
                           tags: Optional[Dict[str, str]] = None) -> str:
    client = _client()
    mv = client.create_model_version(name=model_name, source=source_path, run_id=None, description=description)
    if tags:
        for k, v in tags.items():
            client.set_model_version_tag(name=model_name, version=mv.version, key=k, value=v)
    return str(mv.version)

def set_model_stage(model_name: str, version: str | int, stage: str, archive_existing: bool = False) -> None:
    client = _client()
    client.transition_model_version_stage(name=model_name, version=str(version), stage=stage, archive_existing_versions=archive_existing)

def latest_version_for_stage(model_name: str, stage: str) -> Optional[str]:
    client = _client()
    vers = client.get_latest_versions(model_name, stages=[stage])
    if not vers:
        return None
    mv = sorted(vers, key=lambda v: (v.creation_timestamp or 0), reverse=True)[0]
    return str(mv.version)

def tag_run(run_id: str, tags: Dict[str, str]) -> None:
    client = _client()
    for k, v in tags.items():
        client.set_tag(run_id=run_id, key=k, value=v)

def tag_model_version(model_name: str, version: str | int, tags: Dict[str, str]) -> None:
    client = _client()
    for k, v in tags.items():
        client.set_model_version_tag(name=model_name, version=str(version), key=k, value=v)

def ensure_and_start_run(experiment_name: str, run_name: Optional[str] = None, parent_run_id: Optional[str] = None,
                         tags: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> str:
    ensure_experiment(experiment_name)
    return start_run(experiment_name, run_name=run_name, parent_run_id=parent_run_id, tags=tags, params=params)
