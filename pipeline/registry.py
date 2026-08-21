"""Thin wrapper over MLflow: the experiment ledger."""
import json, urllib.request
import mlflow
from mlflow.tracking import MlflowClient
from . import SQLITE_URI, ARTIFACT_DIR

SERVER_URL = "http://127.0.0.1:5000"

def _server_healthy():
    try:
        with urllib.request.urlopen(SERVER_URL + "/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False

def pick_uri():
    return SERVER_URL if _server_healthy() else SQLITE_URI

def client():
    uri = pick_uri()
    mlflow.set_tracking_uri(uri)
    return MlflowClient(uri)

def ensure_experiment(name):
    c = client()
    e = c.get_experiment_by_name(name)
    if e is None:
        c.create_experiment(name, artifact_location="file://" + str(ARTIFACT_DIR / name))
        e = c.get_experiment_by_name(name)
    return e.experiment_id

def log_run(exp_name, params=None, metrics=None, tags=None, artifacts=None):
    """Write one run into the ledger. Returns run_id."""
    exp_id = ensure_experiment(exp_name)
    run_name = (tags or {}).get("qlab.run_name", "run")[:200]
    with mlflow.start_run(experiment_id=exp_id, run_name=run_name) as run:
        for k, v in (params or {}).items():
            mlflow.log_param(str(k), str(v)[:490])
        for k, v in (metrics or {}).items():
            try:
                mlflow.log_metric(str(k), float(v))
            except (TypeError, ValueError):
                pass
        for k, v in (tags or {}).items():
            mlflow.set_tag(str(k), str(v)[:490])
        for _name, path in (artifacts or {}).items():
            mlflow.log_artifact(str(path))
        return run.info.run_id

def std_tags(spec_hash, batch_id=None, seed=None, source="harness", extra=None):
    t = {"qlab.spec_hash": spec_hash or "", "qlab.source": source}
    if batch_id:
        t["qlab.batch_id"] = str(batch_id)
    if seed is not None:
        t["qlab.seed"] = str(seed)
    if extra:
        for k, v in extra.items():
            t[str(k)] = str(v)
    return t

def all_runs():
    c = client()
    out = []
    for e in c.search_experiments():
        runs = []
        for r in c.search_runs(e.experiment_id, max_results=5000):
            runs.append({
                "run_id": r.info.run_id,
                "status": r.info.status,
                "start_time": r.info.start_time,
                "params": dict(r.data.params),
                "metrics": dict(r.data.metrics),
                "tags": dict(r.data.tags),
            })
        out.append({"experiment": e.name, "experiment_id": e.experiment_id, "runs": runs})
    return out

def export_json(path):
    data = all_runs()
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    return data
