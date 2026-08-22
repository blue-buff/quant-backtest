"""Data layer (P5 executor contract): fixed-menu feature matrices.

The pipeline owns the data menu: pool x handler (Alpha158/Alpha360) x label x
windows. Features are built once into cached float32 parquet via a sliced,
memory-friendly fetch; executors only READ these parquet files -- they never
build features themselves. That fixed menu is what keeps every experiment
comparable on the board.

CLI: python -m pipeline.data ensure <spec_path>   (build/verify caches)
"""
import argparse, hashlib, json, time
from pathlib import Path

import numpy as np
import pandas as pd

from . import QLAB_ROOT, DATA_VERSION

CACHE_DIR = QLAB_ROOT / "cache"

POOL_MAP = {
    "all": ("/root/.qlib/qlib_data/cn_data_all", "all"),
    "hs300": ("/root/.qlib/qlib_data/cn_data", "csi300"),
    "zz500": ("/root/.qlib/qlib_data/cn_data_zz500", "csi500"),
}

HANDLER_CLASSES = ("Alpha158", "Alpha360")
TASKS = ("regression", "classification")

DEFAULTS = {
    "fit_start_time": "2021-06-01",
    "fit_end_time": "2024-06-30",
    "train": ["2021-06-01", "2024-06-30"],
    "valid": ["2024-07-01", "2024-12-31"],
    "test_start": "2025-01-01",
    "test_end": "2026-08-20",
    "rounds": 1000,
    "early_stopping": 50,
    # container cgroup pids.max=256: keep the default thread budget small
    "num_threads": 8,
}

DEFAULT_LEARN_PROCESSORS = [
    {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
    {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
     "kwargs": {"fields_group": "label"}},
]


def resolve(spec, eff):
    """Effective data config from resolved spec. Bad config raises ValueError
    with QLAB_SPEC_INVALID (fail loud, never silently fall back)."""
    h = eff.get("dataset", {}).get("handler", {})
    lab = eff.get("label", {})
    act = spec.get("action", {})
    pool = str(eff.get("universe") or "all")
    if pool not in POOL_MAP:
        raise ValueError("QLAB_SPEC_INVALID: unknown pool %r (allowed: %s)"
                         % (pool, sorted(POOL_MAP)))
    handler_class = str(h.get("class") or "Alpha158")
    if handler_class not in HANDLER_CLASSES:
        raise ValueError("QLAB_SPEC_INVALID: unknown handler %r (allowed: %s)"
                         % (handler_class, list(HANDLER_CLASSES)))
    task = str(act.get("task") or "regression")
    if task not in TASKS:
        raise ValueError("QLAB_SPEC_INVALID: unknown task %r (allowed: %s)"
                         % (task, list(TASKS)))
    horizon = int(lab.get("horizon", 10))
    label_formula = lab.get("formula")
    if not label_formula:
        label_formula = "Ref($close,-%d)/Ref($close,-1)-1" % (horizon + 1)
    provider, inst_default = POOL_MAP[pool]
    learn = eff.get("processors", {}).get("learn")
    cfg = {
        "pool": pool,
        "provider_uri": provider,
        "instruments": h.get("instruments") or inst_default,
        "handler_class": handler_class,
        "fit_start_time": h.get("fit_start_time", DEFAULTS["fit_start_time"]),
        "fit_end_time": h.get("fit_end_time", DEFAULTS["fit_end_time"]),
        "train": list(DEFAULTS["train"]),
        "valid": list(DEFAULTS["valid"]),
        "test_start": act.get("test_start", DEFAULTS["test_start"]),
        "test_end": act.get("test_end", DEFAULTS["test_end"]),
        "label_formula": label_formula,
        "horizon": horizon,
        "learn_processors": learn if learn else DEFAULT_LEARN_PROCESSORS,
        "task": task,
    }
    return cfg


def _key(cfg, test=False):
    canon = {
        "pool": cfg["pool"],
        "instruments": cfg["instruments"],
        "handler_class": cfg["handler_class"],
        "fit": [cfg["fit_start_time"], cfg["fit_end_time"]],
        "label": cfg["label_formula"],
        "learn_processors": cfg["learn_processors"],
    }
    # train cache covers fit_start..valid[1]; test cache covers test_start..test_end
    if test:
        canon["window"] = [cfg["test_start"], cfg["test_end"]]
    else:
        canon["window"] = [cfg["fit_start_time"], cfg["valid"][1]]
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()[:16]


def _fetch(cfg, start, end, cache_path):
    """Sliced fetch -> cached float32 parquet. Memory-friendly port of the proven
    trainer path: one handler per ~180-trading-day slice, infer_processors=[] to
    skip fit-window feature materialization, chunked float32 + pyarrow append."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        return cache_path
    from qlib.contrib.data.handler import Alpha158, Alpha360
    from qlib.data.dataset.handler import DataHandlerLP
    import pyarrow as pa
    import pyarrow.parquet as pq
    handler_cls = {"Alpha158": Alpha158, "Alpha360": Alpha360}[cfg["handler_class"]]
    days = pd.bdate_range(start, end)
    if len(days) == 0:
        raise ValueError("QLAB_SPEC_INVALID: empty fetch window %s..%s" % (start, end))
    SLICE_DAYS = 180
    writer = None
    for i in range(0, len(days), SLICE_DAYS):
        s0 = days[i]
        s1 = days[min(i + SLICE_DAYS, len(days)) - 1]
        h = handler_cls(instruments=cfg["instruments"], start_time=s0, end_time=s1,
                        fit_start_time=cfg["fit_start_time"],
                        fit_end_time=cfg["fit_end_time"],
                        learn_processors=cfg["learn_processors"], infer_processors=[],
                        label=[cfg["label_formula"]])
        data = h.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
        feat64 = data["feature"]
        lab = data["label"].iloc[:, 0]
        del data, h
        CHUNK = 400000
        for s in range(0, len(feat64), CHUNK):
            sub = feat64.iloc[s:s + CHUNK].astype(np.float32)
            chunk = sub.join(lab.iloc[s:s + CHUNK].rename("y"))
            table = pa.Table.from_pandas(chunk)
            if writer is None:
                writer = pq.ParquetWriter(cache_path, table.schema)
            writer.write_table(table)
            del sub, chunk, table
        del feat64, lab
        print("cache slice %s..%s done" % (s0.date(), s1.date()), flush=True)
    if writer is not None:
        writer.close()
    return cache_path


def ensure(spec, eff):
    """Ensure cached feature matrices exist for the spec's data config.
    Returns {"train_pq": Path, "test_pq": Path, "train_key": str,
             "test_key": str, "cfg": dict}."""
    import qlib
    cfg = resolve(spec, eff)
    qlib.init(provider_uri=cfg["provider_uri"], region="cn")
    train_key = _key(cfg, test=False)
    test_key = _key(cfg, test=True)
    train_pq = CACHE_DIR / ("train_%s.parquet" % train_key)
    test_pq = CACHE_DIR / ("test_%s.parquet" % test_key)
    t0 = time.time()
    _fetch(cfg, cfg["fit_start_time"], cfg["valid"][1], train_pq)
    _fetch(cfg, cfg["test_start"], cfg["test_end"], test_pq)
    print("data ready %.0fs | %s %s | %s %s" % (
        time.time() - t0, cfg["pool"], cfg["handler_class"],
        train_pq.name, test_pq.name), flush=True)
    return {"train_pq": train_pq, "test_pq": test_pq,
            "train_key": train_key, "test_key": test_key, "cfg": cfg}


def main():
    from . import spec as specmod
    ap = argparse.ArgumentParser(prog="pipeline.data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure").add_argument("spec_path")
    a = ap.parse_args()
    spec = specmod.load_spec(a.spec_path)
    eff = specmod.resolve(spec)
    d = ensure(spec, eff)
    print(json.dumps({k: str(v) for k, v in d.items() if k != "cfg"},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
