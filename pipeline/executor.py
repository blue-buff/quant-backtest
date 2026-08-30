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
import argparse, hashlib, json, os, subprocess, sys, threading, time
from pathlib import Path

import numpy as np
import pandas as pd

from . import QLAB_ROOT

EXECUTORS_DIR = QLAB_ROOT / "executors"
# QLAB_VENV_DIR override: remote dispatches point this OUTSIDE the extracted
# repo (e.g. <workdir>/executor_venvs) so requirements venvs survive the
# per-dispatch repo wipe (P8 6.3).
VENV_DIR = Path(os.environ.get("QLAB_VENV_DIR", QLAB_ROOT / "results" / "venvs"))
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
    # stamp = sha256(requirements.txt): re-install whenever it changes (a venv
    # that predates a requirements edit must NOT keep running stale deps)
    stamp = venv / ".requirements.sha256"
    h = hashlib.sha256(req.read_bytes()).hexdigest()

    def _ready():
        return py.exists() and stamp.exists() and stamp.read_text().strip() == h

    if _ready():
        return str(py)
    venv.parent.mkdir(parents=True, exist_ok=True)
    # cold-build race guard (audit #2): concurrent dispatcher threads serialize
    # on one lock; losers re-check the stamp and reuse the winner's venv. A
    # crashed winner releases the lock via the kernel (no deadlock, no wait).
    try:
        import fcntl
    except ImportError:
        fcntl = None
    lk = None
    if fcntl is not None:
        try:
            lk = open(str(venv.parent / (".%s.build.lock" % name)), "w")
            fcntl.flock(lk, fcntl.LOCK_EX)
        except OSError:
            if lk is not None:
                lk.close()
            lk = None
    try:
        if _ready():
            return str(py)
        if not py.exists():
            r = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("venv create failed: %s" % r.stderr[-400:])
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("pip install failed: %s" % r.stderr[-600:])
        # stamp LAST and atomically: only a fully installed venv counts ready
        tmp = stamp.with_name(".requirements.sha256.tmp")
        tmp.write_text(h)
        os.replace(tmp, stamp)
    finally:
        if lk is not None:
            try:
                fcntl.flock(lk, fcntl.LOCK_UN)
            except OSError:
                pass
            lk.close()
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
    log_path = Path(out_dir) / "executor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    bufs = {"out": [], "err": []}
    lfh = open(str(log_path), "w")

    def _tee(pipe, key):
        # stream to <out_dir>/executor.log line-by-line (audit #3): a crash or
        # timeout leaves its partial log on disk instead of losing the scene
        for line in iter(pipe.readline, ""):
            try:
                lfh.write(line)
                lfh.flush()
            except (OSError, ValueError):
                pass
            bufs[key].append(line)

    proc = subprocess.Popen(cmd, cwd=str(exdir), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env,
                            bufsize=1)
    threads = [threading.Thread(target=_tee, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=_tee, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = -9
        lfh.write("\n=== EXECUTOR TIMEOUT (killed) ===\n")
        lfh.flush()
    for t in threads:
        t.join(timeout=5)
    try:
        lfh.close()
    except OSError:
        pass
    out = "".join(bufs["out"])
    err = "".join(bufs["err"])
    if timed_out:
        err = (err or "") + "\n=== EXECUTOR TIMEOUT ==="
    return rc, out, err, round(time.time() - t0, 1)


def declared_extra_features(run_dir):
    """Extra-feature declaration from <out>/run_info.json (P7 T3, data/extra/
    convention). Missing file = []; wrong type = warn + []. The pipeline only
    records the declaration, it never validates the referenced parquet."""
    p = Path(run_dir) / "run_info.json"
    if not p.exists():
        return []
    try:
        info = json.loads(p.read_text())
    except (ValueError, OSError):
        return []
    ef = info.get("extra_features", [])
    if not isinstance(ef, list):
        print("QLAB_EXTRA_WARN run_info.json extra_features not a list",
              file=sys.stderr)
        return []
    return [str(x) for x in ef]


def check_pred(pred_path, test_pq, run_dir=None, min_inst=20, min_date_frac=0.5):
    """Contract check: pred.pkl schema/index/coverage vs the standard test set.
    Lenient on universe (subset prediction is a legitimate research choice) but
    strict on schema. Coverage numbers are recorded, not only judged.
    Constant predictions (std=0 / <2 unique values) are rejected: there is
    nothing to rank, and such rows would pollute the board."""
    issues, warns = [], []
    pred = pd.read_pickle(pred_path)
    score = pred["score"] if hasattr(pred, "columns") else pred
    rep = {"ok": False, "n_pred_rows": int(len(score)), "issues": issues,
           "warns": warns, "extra_features": declared_extra_features(run_dir) if run_dir else []}
    if not isinstance(score.index, pd.MultiIndex):
        issues.append("pred index must be MultiIndex (datetime, instrument), got %s"
                      % type(score.index).__name__)
        return rep
    names = [str(n) for n in score.index.names]
    if "instrument" not in names:
        issues.append("pred index level 1 must be named 'instrument', got %r" % names)
        return rep
    dup = score.index.duplicated()
    if dup.any():
        issues.append("pred index has %d duplicate (datetime, instrument) rows"
                      % int(dup.sum()))
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
    finite = vals[np.isfinite(vals)]
    if finite.size and (np.std(finite) == 0 or np.unique(finite).size < 2):
        issues.append("constant prediction (std=0 or <2 unique values): nothing to rank")
    if run_dir and not (Path(run_dir) / "run_info.json").exists():
        warns.append("run_info.json missing (declared extra_features defaults to [])")
    rep["ok"] = not issues
    return rep


def check_portfolio(pf_path, test_pq, min_date_frac=0.5, min_inst=20):
    """Portfolio contract (P8 D1/D2): <out>/portfolio.pkl = daily target weights.
    MultiIndex (datetime, instrument), column 'weight', float; weights >= 0,
    row sums <= 1 (cash = 1 - sum); rows for unheld names may be omitted.
    Constant weights are rejected (degenerate, nothing to trade)."""
    issues, warns = [], []
    try:
        pf = pd.read_pickle(pf_path)
    except Exception as e:
        return {"ok": False, "n_weight_rows": 0,
                "issues": ["portfolio.pkl unreadable: %s" % e], "warns": []}
    w = pf["weight"] if hasattr(pf, "columns") else pf
    rep = {"ok": False, "n_weight_rows": int(len(w)), "issues": issues,
           "warns": warns}
    if not isinstance(w.index, pd.MultiIndex):
        issues.append("portfolio index must be MultiIndex (datetime, instrument), got %s"
                      % type(w.index).__name__)
        return rep
    names = [str(n) for n in w.index.names]
    if "instrument" not in names:
        issues.append("portfolio index level 1 must be named 'instrument', got %r" % names)
        return rep
    try:
        vals = w.to_numpy(dtype="float64", na_value=np.nan)
    except Exception as e:
        issues.append("weight not numeric: %s" % e)
        return rep
    finite = vals[np.isfinite(vals)]
    if finite.size == 0:
        issues.append("portfolio empty / all-NaN weights")
    elif np.nanmin(vals) < -1e-12:
        issues.append("negative weights not allowed (long-only + cash)")
    # 常数判定 = 每日权重向量（含 0）完全不变：等权 topK 轮换成员是合法的，
    # 只有"每天一模一样的持仓"才是退化组合
    if len(w):
        wide = w.unstack("instrument").fillna(0.0)
        change = wide.diff().abs().sum(axis=1)
        if len(change) > 1 and float(change.iloc[1:].sum()) == 0:
            issues.append("constant portfolio: the daily weight vector never changes (degenerate)")
    test_df = pd.read_parquet(test_pq, columns=["y"])
    test_dates = set(test_df.index.get_level_values(0).unique())
    test_insts = set(test_df.index.get_level_values(1).unique())
    pf_dates = set(w.index.get_level_values(0).unique())
    pf_insts = set(w.index.get_level_values(1).unique())
    extra_dates = pf_dates - test_dates
    if extra_dates:
        warns.append("%d portfolio dates outside the test window (dropped by tester)"
                     % len(extra_dates))
    rep.update({
        "n_dates": int(len(pf_dates & test_dates)),
        "date_frac": float(len(pf_dates & test_dates) / max(1, len(test_dates))),
        "n_inst": int(len(pf_insts & test_insts)),
        "inst_frac": float(len(pf_insts & test_insts) / max(1, len(test_insts))),
        "nan_frac": float(np.isnan(vals).mean()),
        "n_test_dates": int(len(test_dates)),
        "n_test_insts": int(len(test_insts)),
    })
    if rep["date_frac"] < min_date_frac:
        issues.append("portfolio date coverage %.1f%% < %.0f%%"
                      % (rep["date_frac"] * 100, min_date_frac * 100))
    if rep["n_inst"] < min_inst:
        issues.append("portfolio instrument overlap %d < %d" % (rep["n_inst"], min_inst))
    if len(w):
        s = w.groupby(level=0).sum()
        bad = s[s > 1 + 1e-6]
        if len(bad):
            issues.append("weight sums exceed 1 on %d dates (max %.4f)"
                          % (len(bad), float(bad.max())))
        rep["max_daily_sum"] = float(s.max())
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
