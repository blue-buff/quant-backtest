"""配置加载：qbt.yaml + 路径管理（环境变量优先，默认项目根推导）"""
import os
from pathlib import Path

import yaml

DEFAULT_CONFIG = {
    "project": {
        "name": "quant-backtest",
        "results_dir": "results",
    },
    "data": {
        "start": "2023-01-01",
        "end": "2026-08-15",
        "adjust": "2",
        "hs300_out": "qlib_data_src",
        "zz500_out": "qlib_data_src_zz500",
    },
    "train": {
        "yaml": "qlib_examples/lightgbm_alpha158_full.yaml",
        "qlib_dir": "~/.qlib/qlib_data/cn_data",
        "universe": "csi300",
        "benchmark": "SH000300",
    },
    "plan": {"topk": 50, "freq": "ME", "out": "qlib_examples/rebalance_plan.csv"},
    "backtest": {
        "capital": 1000000,
        "start": "2025-01-01",
        "end": "2026-08-14",
        "strategy": "qlib_examples/rq_strategy_qlib.py",
    },
}


def project_root() -> Path:
    """项目根目录（qbt 包所在目录的上一级）"""
    return Path(__file__).resolve().parent.parent


def find_config() -> Path | None:
    """从当前目录向上找 qbt.yaml"""
    d = Path.cwd()
    for _ in range(4):
        p = d / "qbt.yaml"
        if p.exists():
            return p
        d = d.parent
    return None


def load_config() -> dict:
    """加载 qbt.yaml；未找到则用默认值（可 init 生成）"""
    cfg = DEFAULT_CONFIG
    p = find_config()
    if p:
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = _deep_merge(cfg, user)
    # 环境变量覆盖
    if os.environ.get("RQALPHA_BUNDLE"):
        cfg.setdefault("backtest", {})["bundle_path"] = os.environ["RQALPHA_BUNDLE"]
    return cfg


def _deep_merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def resolve(p: str) -> str:
    """展开 ~ 并转为绝对路径"""
    return str(Path(os.path.expanduser(p)).resolve())
