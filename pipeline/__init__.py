"""QLab pipeline: MLflow ledger + sqlite job queue for the quant lab."""
import os
from pathlib import Path

QLAB_ROOT = Path(os.environ.get("QLAB_ROOT", "/root/quant"))
MLFLOW_DIR = QLAB_ROOT / "mlflow-server"
MLFLOW_DB = MLFLOW_DIR / "mlflow.db"
SQLITE_URI = "sqlite:///" + str(MLFLOW_DB)
ARTIFACT_DIR = MLFLOW_DIR / "artifacts"
REFS_DIR = QLAB_ROOT / "experiments" / "refs"
SPECS_DIR = QLAB_ROOT / "experiments" / "specs"
BATCHES_DIR = QLAB_ROOT / "experiments" / "batches"
JOBS_DB = QLAB_ROOT / "results" / "queue" / "jobs.db"
QUEUE_LOGS = QLAB_ROOT / "results" / "queue" / "logs"
KNOWLEDGE_DIR = QLAB_ROOT / "knowledge"
BOARD_CSV = QLAB_ROOT / "results" / "board.csv"
REGISTRY_EXPORT = QLAB_ROOT / "results" / "registry_export.json"
DATA_VERSION = "v3"

for _d in (MLFLOW_DIR, ARTIFACT_DIR, REFS_DIR, SPECS_DIR, BATCHES_DIR,
           QUEUE_LOGS, KNOWLEDGE_DIR, QLAB_ROOT / "results"):
    _d.mkdir(parents=True, exist_ok=True)
