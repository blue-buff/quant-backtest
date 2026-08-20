"""生成预测力实验 yaml 矩阵（第一轮：P0-1 数据前移 + P0-2 官方预处理 + P0-3 标签周期 + P0-5 种子）。

每个实验一个 yaml，写入 qlib_examples/experiments/。
- e00_robust_1d: 当前 RobustZScoreNorm 配置 + 2022 起数据（隔离"数据前移"这一变量）
- e01_off_1d: qlib 官方 benchmark 默认处理器（learn: DropnaLabel+CSZScoreNorm(label)，infer: ProcessInf+ZScoreNorm+Fillna）
- e02_off_5d / e03_off_10d / e04_off_20d: 官方处理器 + 多日标签（h 日未来收益，Ref($close,-(h+1))/Ref($close,-1)-1）
- e05_off_1d_dart: 官方处理器 + DART
- e06_off_1d_small: 官方处理器 + 浅树/小叶子（针对短样本窗口）
- e07_off_1d_seed{a,b,c}: 官方处理器 + 不同种子（多种子集成用）

用法: python scripts/gen_exps.py
"""
import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "qlib_examples" / "experiments"
OUT.mkdir(parents=True, exist_ok=True)

POOLS = {
    "hs300": {"market": "csi300", "benchmark": "SH000300", "uri": "~/.qlib/qlib_data/cn_data"},
    "zz500": {"market": "csi500", "benchmark": "SH000905", "uri": "~/.qlib/qlib_data/cn_data_zz500"},
}

OFFICIAL_MODEL = {
    "class": "LGBModel",
    "module_path": "qlib.contrib.model.gbdt",
    "kwargs": {
        "loss": "mse",
        "colsample_bytree": 0.8879,
        "learning_rate": 0.2,
        "subsample": 0.8789,
        "lambda_l1": 205.6999,
        "lambda_l2": 580.9768,
        "max_depth": 8,
        "num_leaves": 210,
        "num_threads": 20,
    },
}

LABELS = {
    "1d": None,
    "5d": ["Ref($close, -6)/Ref($close, -1) - 1"],
    "10d": ["Ref($close, -11)/Ref($close, -1) - 1"],
    "20d": ["Ref($close, -21)/Ref($close, -1) - 1"],
}


def make_yaml(pool, label_key, model=None, processors="off", name=None):
    p = POOLS[pool]
    handler_kwargs = {
        "start_time": "2022-01-01",
        "end_time": "2026-08-14",
        "fit_start_time": "2022-01-01",
        "fit_end_time": "2024-06-30",
        "instruments": p["market"],
    }
    if label_key != "1d":
        handler_kwargs["label"] = LABELS[label_key]
    d = {
        "qlib_init": {"provider_uri": p["uri"], "region": "cn"},
        "market": p["market"],
        "benchmark": p["benchmark"],
        "data_handler_config": dict(handler_kwargs),
        "task": {
            "model": model if model is not None else copy.deepcopy(OFFICIAL_MODEL),
            "dataset": {
                "class": "DatasetH",
                "module_path": "qlib.data.dataset",
                "kwargs": {
                    "handler": {
                        "class": "Alpha158",
                        "module_path": "qlib.contrib.data.handler",
                        "kwargs": dict(handler_kwargs),
                    },
                    "segments": {
                        "train": ["2022-01-01", "2024-06-30"],
                        "valid": ["2024-07-01", "2024-12-31"],
                        "test": ["2025-01-01", "2026-08-14"],
                    },
                },
            },
            "record": [
                {"class": "SignalRecord", "module_path": "qlib.workflow.record_temp",
                 "kwargs": {"model": "<MODEL>", "dataset": "<DATASET>"}},
                {"class": "SigAnaRecord", "module_path": "qlib.workflow.record_temp",
                 "kwargs": {"ana_long_short": False, "ann_scaler": 252}},
            ],
        },
    }
    if processors == "off":
        # 官方默认：不写 processors（handler 默认 learn=[DropnaLabel, CSZScoreNorm(label)]，
        # infer=[ProcessInf, ZScoreNorm, Fillna]）
        pass
    elif processors == "robust":
        d["data_handler_config"].update({
            "learn_processors": [
                {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
                {"class": "RobustZScoreNorm", "module_path": "qlib.data.dataset.processor"},
                {"class": "DropnaProcessor", "module_path": "qlib.data.dataset.processor"},
            ],
            "infer_processors": [
                {"class": "RobustZScoreNorm", "module_path": "qlib.data.dataset.processor"},
                {"class": "DropnaProcessor", "module_path": "qlib.data.dataset.processor"},
            ],
        })
    # processors 放进 handler.kwargs（qlib 从 handler kwargs 读取）
    for k in ("learn_processors", "infer_processors"):
        if k in d["data_handler_config"]:
            d["task"]["dataset"]["kwargs"]["handler"]["kwargs"][k] = d["data_handler_config"][k]
            del d["data_handler_config"][k]
    out = OUT / f"{name}_{pool}.yaml"
    out.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return out


if __name__ == "__main__":
    written = []
    for pool in POOLS:
        written.append(make_yaml(pool, "1d", processors="robust", name="e00_robust_1d"))
        written.append(make_yaml(pool, "1d", processors="off", name="e01_off_1d"))
        written.append(make_yaml(pool, "5d", processors="off", name="e02_off_5d"))
        written.append(make_yaml(pool, "10d", processors="off", name="e03_off_10d"))
        written.append(make_yaml(pool, "20d", processors="off", name="e04_off_20d"))
        dart = copy.deepcopy(OFFICIAL_MODEL)
        dart["kwargs"].update({"boosting_type": "dart", "drop_rate": 0.1, "skip_drop": 0.5, "max_drop": 50})
        written.append(make_yaml(pool, "1d", model=dart, processors="off", name="e05_off_1d_dart"))
        small = copy.deepcopy(OFFICIAL_MODEL)
        small["kwargs"].update({"learning_rate": 0.1, "num_leaves": 128, "min_child_samples": 100,
                                "lambda_l1": 1.0, "lambda_l2": 10.0})
        written.append(make_yaml(pool, "1d", model=small, processors="off", name="e06_off_1d_small"))
        for i, seed in enumerate([100, 200, 300], start=1):
            seeded = copy.deepcopy(OFFICIAL_MODEL)
            seeded["kwargs"].update({"seed": seed, "bagging_seed": seed + 1, "feature_fraction_seed": seed + 2})
            written.append(make_yaml(pool, "1d", model=seeded, processors="off", name=f"e07{i}_off_1d_seed{seed}"))
    for p in written:
        print(p)
    print(f"共 {len(written)} 个 yaml")
