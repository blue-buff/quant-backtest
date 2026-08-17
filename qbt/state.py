"""阶段状态追踪：results/status.json"""
import json
import os
from datetime import datetime
from pathlib import Path

from qbt.config import load_config, project_root


def state_file() -> Path:
    cfg = load_config()
    return project_root() / cfg["project"]["results_dir"] / "status.json"


def read_state() -> dict:
    f = state_file()
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def write_state(**updates) -> None:
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    st = read_state()
    for k, v in updates.items():
        if v is not None:
            st[k] = v
    st["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    f.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def log_dir() -> Path:
    d = project_root() / load_config()["project"]["results_dir"] / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
