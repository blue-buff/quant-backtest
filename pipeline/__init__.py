"""QLab pipeline: MLflow ledger + sqlite job queue for the quant lab."""
import os
from pathlib import Path


def _default_root():
    """QLAB_ROOT 默认值：env 优先；容器环境（/root/quant 存在）行为不变；
    其余（本机无容器，如 macOS 开发机）自动探测仓库根（pipeline 包上一级）。"""
    env = os.environ.get("QLAB_ROOT")
    if env:
        return env
    if Path("/root/quant").exists():
        return "/root/quant"
    return str(Path(__file__).resolve().parent.parent)


QLAB_ROOT = Path(_default_root())
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
