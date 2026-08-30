#!/usr/bin/env python
"""Official qlib benchmark reproduction executor (batch quant-test task).

Runs qlib's official benchmark models VERBATIM on the pipeline-provided
feature parquet (fixed data menu owned by pipeline.data). The official
workflow_config_*.yaml model configs are copied into specs with only the time
windows swapped (train/valid/test -> 2021-06..2024-06 / 2024-07..2024-12 /
2025-01..2026-08-20), per the task's split discipline.

How it works:
- Feature parquet (train+test) is served to qlib through a DataHandlerLP
  compatible shim, so the official DatasetH / TSDatasetH / MTSDatasetH classes
  and every qlib.contrib.model model (fit/predict) run completely unmodified.
- The pipeline's requirements venv provides torch/xgboost/catboost/pytorch-
  tabnet; qlib itself is imported from the ambient interpreter's site-packages
  (pyqlib has no wheel for this platform), injected at the top of this file.
- portfolio.pkl reproduces qlib TopkDropoutStrategy semantics (topk=50,
  n_drop=5, equal weight 1/topk) from the test scores; the pipeline fixed
  tester computes all backtest metrics.

Contract: main.py --config <json> --train <pq> --test <pq> --out <dir>
Outputs: pred.pkl (MultiIndex datetime/instrument, column "score"),
         portfolio.pkl (MultiIndex datetime/instrument, column "weight"),
         run_info.json.
"""
import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path


def _ambient_site():
    """Path of the ambient interpreter's site-packages (qlib lives there)."""
    try:
        p = (Path(sys.base_prefix) / "lib"
             / ("python%d.%d" % sys.version_info[:2]) / "site-packages")
        if p.is_dir():
            return str(p)
    except Exception:
        pass
    return ""


_AMBIENT = _ambient_site()
if _AMBIENT and _AMBIENT not in sys.path:
    sys.path.insert(0, _AMBIENT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qlib.data.dataset import DatasetH, TSDatasetH  # noqa: E402
from qlib.data.dataset.handler import DataHandlerLP  # noqa: E402


class ParquetHandler(DataHandlerLP):
    """Serve pipeline feature parquet through the DataHandlerLP interface.

    Feature construction stays in pipeline.data (fixed menu); this shim only
    re-serves those features/labels in the exact shape qlib models expect
    (MultiIndex (datetime, instrument); col_set -> multi-level columns).
    Also exposes the `data_loader.fields` / `_learn` attributes that
    MTSDatasetH (TRA) reads directly.
    """

    def __init__(self, df):
        self._frame = df
        self._feat_cols = [c for c in df.columns if c != "LABEL0"]
        self.data_loader = type("_StaticLoader", (), {
            "fields": {"feature": [self._feat_cols], "label": [["LABEL0"]]}})()
        self._data = df
        self._learn = pd.concat({"feature": df[self._feat_cols],
                                 "label": df[["LABEL0"]]}, axis=1)

    def setup_data(self, *args, **kwargs):
        pass

    def fetch(self, selector=slice(None, None), level="datetime",
              col_set=DataHandlerLP.CS_ALL, data_key=DataHandlerLP.DK_I,
              squeeze=False, proc_func=None):
        df = self._frame
        dt = df.index.get_level_values(0)
        if isinstance(selector, slice):
            lo = selector.start if selector.start is not None else dt.min()
            hi = (selector.stop if selector.stop is not None
                  else dt.max() + pd.Timedelta(days=1))
            df = df[(dt >= lo) & (dt < hi)]
        elif isinstance(selector, (list, tuple)) and len(selector) == 2:
            df = df[(dt >= selector[0]) & (dt < selector[1])]
        elif isinstance(selector, pd.Index):
            df = df[dt.isin(selector)]
        single = isinstance(col_set, str)
        groups = [col_set] if single else list(col_set)
        parts = []
        for g in groups:
            if g == "feature":
                parts.append(df[self._feat_cols])
            elif g == "label":
                parts.append(df[["LABEL0"]])
            else:  # "raw" and any other group: serve everything we have
                parts.append(df)
        if single:
            return parts[0]
        return pd.concat({g: p for g, p in zip(groups, parts)}, axis=1)


def _torch_compat():
    """qlib 0.9.7 compat on torch 2.10:
    1. ReduceLROnPlateau no longer accepts verbose= (removed in torch 2.2);
       qlib models using the scheduler would fail construction.
    2. torch.load defaults to weights_only=True since 2.6; the official
       pretrained LSTM checkpoints (saved by old torch) fail to deserialize.
       These pkls are vendored/trusted, so default weights_only=False."""
    try:
        import torch
        from torch.optim import lr_scheduler as _ls
        import inspect as _inspect
        _orig = _ls.ReduceLROnPlateau
        if "verbose" not in _inspect.signature(_orig.__init__).parameters:
            class _Compat(_orig):
                def __init__(self, *a, **k):
                    k.pop("verbose", None)
                    super().__init__(*a, **k)
            _ls.ReduceLROnPlateau = _Compat
        _orig_load = torch.load
        def _load(*a, **k):
            k.setdefault("weights_only", False)
            return _orig_load(*a, **k)
        torch.load = _load
    except Exception:
        pass


def _load_model(cfg):
    m = cfg.get("model") or {}
    mod_path = str(m.get("module_path", "qlib.contrib.model.gbdt"))
    cls_name = str(m.get("class", "LGBModel"))
    kwargs = dict(m.get("kwargs") or {})
    # official ts models pass n_jobs=20 (DataLoader workers). On the spark
    # remote the workers' torch collate allocates /dev/shm shared memory and
    # the container's tiny shm (64MB) exhausts -> worker death or hang
    # (observed 2026-08-24, gru_a158). Force single-process loading: purely a
    # data-loading knob -- same batches, same order, same training math.
    if "n_jobs" in kwargs:
        kwargs["n_jobs"] = 0
    # module_path may be relative to the executor dir (e.g. vendored modules)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    mod = importlib.import_module(mod_path)
    _torch_compat()
    return getattr(mod, cls_name)(**kwargs)


def _build_dataset(cfg, frame):
    p = cfg.get("params") or {}
    T = pd.Timestamp
    seg = {
        "train": slice(T(cfg["train"][0]), T(cfg["train"][1])),
        "valid": slice(T(cfg["valid"][0]), T(cfg["valid"][1])),
        "test": slice(T(cfg["test_start"]), T(cfg["test_end"])),
        "pretrain": slice(T(cfg["train"][0]), T(cfg["train"][1])),
        "pretrain_validation": slice(T(cfg["valid"][0]), T(cfg["valid"][1])),
    }
    handler = ParquetHandler(frame)
    ds_class = str(p.get("dataset_class", "DatasetH"))
    if ds_class == "TSDatasetH":
        ds = TSDatasetH(handler=handler, segments=seg,
                        step_len=int(p.get("step_len", 20)))
    elif ds_class == "MTSDatasetH":
        from qlib.contrib.data.dataset import MTSDatasetH
        ds = MTSDatasetH(handler=handler, segments=seg,
                         seq_len=int(p.get("seq_len", 60)),
                         horizon=int(p.get("horizon", 0)),
                         num_states=int(p.get("num_states", 3)),
                         memory_mode=str(p.get("memory_mode", "sample")),
                         batch_size=int(p.get("batch_size", 1024)),
                         n_samples=p.get("n_samples"),
                         shuffle=bool(p.get("shuffle", True)),
                         drop_last=bool(p.get("drop_last", True)),
                         input_size=p.get("input_size"))
    else:
        ds = DatasetH(handler=handler, segments=seg)
    try:
        ds.setup_data()
    except Exception as e:  # DatasetH path doesn't need it; TSDatasetH does
        print("setup_data skipped: %s" % e, flush=True)
    return ds, seg


def _portfolio(score, topk=50, n_drop=5):
    """qlib TopkDropoutStrategy semantics: hold top-k by score, sell at most
    n_drop of the stale names per day, equal weight 1/topk."""
    w = score.unstack("instrument")
    holdings = set()
    rows = []
    for dt in w.index:
        s = w.loc[dt].dropna().sort_values(ascending=False)
        top = set(s.index[:topk])
        drop_cand = sorted([i for i in holdings if i not in top],
                           key=lambda i: s.get(i, -np.inf))
        sell = set(drop_cand[:n_drop])
        hold_now = (holdings - sell) & set(s.index)
        need = max(0, topk - len(hold_now))
        buys = [i for i in s.index if i not in hold_now][:need]
        holdings = set(hold_now) | set(buys)
        row = pd.Series(0.0, index=s.index)
        row[list(holdings)] = 1.0 / topk
        rows.append(row)
    pf = pd.concat(rows, axis=1).T
    pf.index = w.index
    pf = pf.stack().rename("weight")
    pf = pf[pf > 0]
    pf.index = pf.index.set_names(["datetime", "instrument"])
    return pf


def _rank_portfolio(score, topk=None):
    """Cross-sectional rank-weighted long-only portfolio: weight = rank_pct
    normalized to sum 1 (full universe) or restricted to the top `topk` names.
    Lower turnover than equal-weight top-k for hump-shaped signals."""
    w = score.unstack("instrument")
    rows = []
    for dt in w.index:
        s = w.loc[dt].dropna()
        r = s.rank(pct=True)
        if topk is not None:
            r = r[r >= r.quantile(1 - topk / len(s))]
        weight = r / r.sum()
        rows.append(weight)
    pf = pd.concat(rows, axis=1).T
    pf.index = w.index
    pf = pf.stack().rename("weight")
    pf = pf[pf > 0]
    pf.index = pf.index.set_names(["datetime", "instrument"])
    return pf


def main():
    ap = argparse.ArgumentParser(prog="executors/qlib_bench")
    ap.add_argument("--config", required=True)
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # qlib's Recorder (used by gbdt fit) requires qlib.init(); qlib defaults its
    # exp_manager to a FILESTORE uri (os.getcwd()/mlruns) which mlflow 3.x
    # refuses -- override with a sqlite backend inside the run dir (artifacts).
    os.environ["MLFLOW_TRACKING_URI"] = "sqlite:///" + str(out / "qlib_runs.db")
    import qlib
    qlib.init(provider_uri=str(cfg.get("provider_uri") or ""), region="cn",
              exp_manager={"class": "MLflowExpManager",
                           "module_path": "qlib.workflow.expm",
                           "kwargs": {"uri": "sqlite:///" + str(out / "qlib_runs.db"),
                                      "default_exp_name": "qlib_bench"}})
    # non-ts models end fit() with R.get_recorder() -> R.get_exp(create=False),
    # which raises unless the default experiment already exists in the store.
    from qlib.workflow import R
    try:
        R.get_exp(experiment_name="qlib_bench", create=True)
    except Exception as e:
        print("exp ensure: %s" % e, flush=True)

    tr = pd.read_parquet(a.train).rename(columns={"y": "LABEL0"})
    te = pd.read_parquet(a.test).rename(columns={"y": "LABEL0"})
    frame = pd.concat([tr, te])
    del tr, te
    n_feat = len([c for c in frame.columns if c != "LABEL0"])
    print("data: rows=%d feats=%d pool=%s handler=%s" % (
        len(frame), n_feat, cfg.get("pool"), cfg.get("handler_class")), flush=True)

    dataset, seg = _build_dataset(cfg, frame)
    model = _load_model(cfg)

    fit_t0 = time.time()
    # official orchestration: models' fit() ends with R.get_recorder()
    # (non-ts pytorch models) / R.log_metrics (gbdt) -- a recorder must be
    # active, exactly like qrun does with `with R.start(...)`. NOTE: R.start
    # is a contextmanager; calling it bare does nothing (observed bug).
    from qlib.workflow import R
    with R.start(experiment_name="qlib_bench", recorder_name="qb_run"):
        model.fit(dataset)
    fit_sec = round(time.time() - fit_t0, 1)
    pred = model.predict(dataset)
    if hasattr(pred, "columns"):
        score = pred["score"]
    else:
        score = pred
    score = pd.Series(pd.to_numeric(score, errors="coerce"),
                      index=score.index, name="score")
    score.index = score.index.set_names(["datetime", "instrument"])
    score = score[~score.index.duplicated(keep="last")]
    score.to_frame().to_pickle(out / "pred.pkl")
    print("pred: %d rows, fit %.0fs" % (len(score), fit_sec), flush=True)

    p = cfg.get("params") or {}
    mode = str(p.get("mode", "topk"))
    if mode == "rank":
        pf = _rank_portfolio(score, topk=p.get("topk"))
    else:
        pf = _portfolio(score, topk=int(p.get("topk", 50)),
                        n_drop=int(p.get("n_drop", 5)))
    pf.to_frame().to_pickle(out / "portfolio.pkl")

    info = {
        "executor": "qlib_bench",
        "model_class": str((cfg.get("model") or {}).get("class", "")),
        "model_module": str((cfg.get("model") or {}).get("module_path", "")),
        "model_kwargs": (cfg.get("model") or {}).get("kwargs", {}),
        "dataset_class": str(p.get("dataset_class", "DatasetH")),
        "segments": {k: [str(v.start)[:10], str(v.stop)[:10]]
                     for k, v in seg.items()},
        "n_features": int(n_feat),
        "seconds": round(time.time() - t0, 1),
        "fit_seconds": fit_sec,
        "topk": p.get("topk"),
        "n_drop": p.get("n_drop"),
        "portfolio_mode": mode,
        "portfolio_note": (("cross-sectional rank-weighted long-only"
                            if mode == "rank" else
                            "qlib TopkDropoutStrategy semantics replicated: "
                            "daily top-k equal weight, max n_drop stale sells")),
    }
    (out / "run_info.json").write_text(json.dumps(info, ensure_ascii=False,
                                                  indent=2, default=str))
    print(json.dumps({"done": True, "seconds": info["seconds"],
                      "fit_seconds": fit_sec, "n_pred": len(score)}), flush=True)


if __name__ == "__main__":
    main()
