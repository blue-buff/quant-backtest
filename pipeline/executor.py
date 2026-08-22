"""Executor contract (P5): the pipeline does NOT know what happens inside an
executor. Contract:

  executors/<name>/main.py --config <json> --train <pq> --test <pq> --out <dir>
  outputs: <out>/pred.pkl -- pickled DataFrame with column "score" (or a Series),
           MultiIndex (datetime, instrument). Everything else in <out>/ is archived
           as artifacts. Anything the executor prints is captured to executor.log.

Env: if executors/<name>/requirements.txt exists, the executor runs in its own
venv (results/venvs/<name>), created on first use; otherwise system python.
Contract checks run BEFORE the fixed tester (pipeline.metrics), which is the
only component allowed to compute ledger metrics.

CLI: python -m pipeline.executor check --pred <pkl> --test <pq> --out <json>
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

from . import QLAB_ROOT

EXECUTORS_DIR = QLAB_ROOT / "executors"
VENV_DIR = QLAB_ROOT / "results" / "venvs"
DEFAULT_EXECUTOR = "executors/_example_lgb"


def resolve_executor(name):
    name = str(name).replace("\\", "/").strip("/")
    if name.startswith("executors/"):
        name = name[len("executors/"):]
    if not name or ".." in name.split("/"):
        raise ValueError("QLAB_SPEC_INVALID: bad executor name %r" % name)
    d = EXECUTORS_DIR / name
    if not (d / "main.py").exists():
        raise ValueError("QLAB_SPEC_INVALID: executor not found: %s (needs main.py)" % d)
    return name, d


def _venv_python(name, exdir):
    req = exdir / "requirements.txt"
    if not req.exists():
        return sys.executable
    venv = VENV_DIR / name
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("venv create failed: %s" % r.stderr[-400:])
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("pip install failed: %s" % r.stderr[-600:])
    return str(py)


def run_executor(name, config_path, train_pq, test_pq, out_dir, timeout=None):
    """Run the executor subprocess. Returns (returncode, stdout, stderr, seconds)."""
    name, exdir = resolve_executor(name)
    py = _venv_python(name, exdir)
    cmd = [py, "main.py", "--config", str(config_path), "--train", str(train_pq),
           "--test", str(test_pq), "--out", str(out_dir)]
    t0 = time.time()
    env = os.environ.copy()
    cap = env.get("QLAB_EXECUTOR_THREADS", "8")
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        env[k] = cap
    proc = subprocess.Popen(cmd, cwd=str(exdir), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        err = (err or "") + "\n=== EXECUTOR TIMEOUT ==="
    return proc.returncode, out, err, round(time.time() - t0, 1)


def check_pred(pred_path, test_pq, min_inst=20, min_date_frac=0.5):
    """Contract check: pred.pkl schema/index/coverage vs the standard test set.
    Lenient on universe (subset prediction is a legitimate research choice) but
    strict on schema. Coverage numbers are recorded, not only judged."""
    issues, warns = [], []
    pred = pd.read_pickle(pred_path)
    score = pred["score"] if hasattr(pred, "columns") else pred
    rep = {"ok": False, "n_pred_rows": int(len(score)), "issues": issues,
           "warns": warns}
    if not isinstance(score.index, pd.MultiIndex):
        issues.append("pred index must be MultiIndex (datetime, instrument), got %s"
                      % type(score.index).__name__)
        return rep
    names = [str(n) for n in score.index.names]
    if "instrument" not in names:
        issues.append("pred index level 1 must be named 'instrument', got %r" % names)
        return rep
    try:
        vals = score.to_numpy(dtype="float64", na_value=np.nan)
    except Exception as e:
        issues.append("score not numeric: %s" % e)
        return rep
    test_df = pd.read_parquet(test_pq, columns=["y"])
    test_dates = set(test_df.index.get_level_values(0).unique())
    test_insts = set(test_df.index.get_level_values(1).unique())
    pred_dates = set(score.index.get_level_values(0).unique())
    pred_insts = set(score.index.get_level_values(1).unique())
    extra_dates = pred_dates - test_dates
    if extra_dates:
        warns.append("%d prediction dates outside the test window (dropped by tester)"
                     % len(extra_dates))
    rep.update({
        "n_dates": int(len(pred_dates & test_dates)),
        "date_frac": float(len(pred_dates & test_dates) / max(1, len(test_dates))),
        "n_inst": int(len(pred_insts & test_insts)),
        "inst_frac": float(len(pred_insts & test_insts) / max(1, len(test_insts))),
        "nan_frac": float(np.isnan(vals).mean()),
        "n_test_dates": int(len(test_dates)),
        "n_test_insts": int(len(test_insts)),
    })
    if rep["date_frac"] < min_date_frac:
        issues.append("date coverage %.1f%% < %.0f%%" % (rep["date_frac"] * 100,
                                                         min_date_frac * 100))
    if rep["n_inst"] < min_inst:
        issues.append("instrument overlap %d < %d" % (rep["n_inst"], min_inst))
    if len(vals) == 0 or np.all(np.isnan(vals)):
        issues.append("prediction empty / all-NaN")
    rep["ok"] = not issues
    return rep


def main():
    ap = argparse.ArgumentParser(prog="pipeline.executor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("check")
    p1.add_argument("--pred", required=True)
    p1.add_argument("--test", required=True)
    p1.add_argument("--out", default=None)
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "check":
        rep = check_pred(a.pred, a.test)
        text = json.dumps(rep, ensure_ascii=False, indent=1)
        print(text)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
        if not rep["ok"]:
            sys.exit(1)
    elif a.cmd == "list":
        if EXECUTORS_DIR.exists():
            for d in sorted(EXECUTORS_DIR.iterdir()):
                if (d / "main.py").exists():
                    print(d.name)
        else:
            print("(no executors dir)")


if __name__ == "__main__":
    main()
