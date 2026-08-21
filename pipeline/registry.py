"""Thin wrapper over MLflow: the experiment ledger.
Agent path is direct sqlite (single source of truth, no 2s probe); the UI server is
only started for humans and only used when QLAB_USE_SERVER=1."""
import argparse, json, math, os, time, urllib.request
from contextlib import contextmanager
import mlflow
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient
from . import SQLITE_URI, ARTIFACT_DIR, DATA_VERSION, QLAB_ROOT, REGISTRY_EXPORT

try:
    import fcntl
except ImportError:  # non-Linux (host mirror) fallback: no lock
    fcntl = None

SERVER_URL = "http://127.0.0.1:5000"
LOCK_FILE = QLAB_ROOT / "results" / "registry.lock"

def _server_healthy():
    try:
        with urllib.request.urlopen(SERVER_URL + "/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False

def pick_uri():
    if os.environ.get("QLAB_USE_SERVER") == "1":
        return SERVER_URL if _server_healthy() else SQLITE_URI
    return SQLITE_URI

def client():
    uri = pick_uri()
    mlflow.set_tracking_uri(uri)
    return MlflowClient(uri)

@contextmanager
def _locked():
    """Cross-process lock around get-or-create (MLflow allows duplicate experiment names)."""
    if fcntl is None:
        yield
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_FILE, "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()

def ensure_experiment(name):
    with _locked():
        c = client()
        e = c.get_experiment_by_name(name)
        if e is None:
            c.create_experiment(name, artifact_location="file://" + str(ARTIFACT_DIR / name))
            e = c.get_experiment_by_name(name)
        return e.experiment_id

def _clip(v):
    s = str(v)
    if len(s) <= 490:
        return s, False
    return s[:490], True

def log_run(exp_name, params=None, metrics=None, tags=None, artifacts=None):
    """Write one run into the ledger. Returns run_id.
    Data integrity: non-finite metrics are dropped (with a tag), invalid keys are
    caught (with a tag) instead of crashing the job at the finish line, and any
    truncated param/tag is marked qlab.truncated."""
    exp_id = ensure_experiment(exp_name)
    run_name = str((tags or {}).get("qlab.run_name", "run"))[:200]
    dropped, bad_keys, trunc = [], [], []
    with mlflow.start_run(experiment_id=exp_id, run_name=run_name) as run:
        for k, v in (params or {}).items():
            sv, cut = _clip(v)
            if cut:
                trunc.append("param:" + str(k))
            try:
                mlflow.log_param(str(k), sv)
            except MlflowException:
                bad_keys.append("param:" + str(k))
        for k, v in (metrics or {}).items():
            try:
                fv = float(v)
                if not math.isfinite(fv):
                    dropped.append(str(k))
                    continue
                mlflow.log_metric(str(k), fv)
            except (TypeError, ValueError):
                dropped.append(str(k))
            except MlflowException:
                bad_keys.append("metric:" + str(k))
        for k, v in (tags or {}).items():
            sv, cut = _clip(v)
            if cut:
                trunc.append("tag:" + str(k))
            try:
                mlflow.set_tag(str(k), sv)
            except MlflowException:
                bad_keys.append("tag:" + str(k))
        for name, path in (artifacts or {}).items():
            try:
                mlflow.log_artifact(str(path))
            except MlflowException:
                bad_keys.append("artifact:" + str(name))
        if dropped:
            mlflow.set_tag("qlab.dropped_metrics", ",".join(dropped)[:490])
        if bad_keys:
            mlflow.set_tag("qlab.invalid_keys", ",".join(bad_keys)[:490])
        if trunc:
            mlflow.set_tag("qlab.truncated", ",".join(trunc)[:490])
        return run.info.run_id

def std_tags(spec_hash, batch_id=None, seed=None, source="harness", extra=None):
    t = {"qlab.spec_hash": spec_hash or "", "qlab.source": source,
         "qlab.data_version": DATA_VERSION}
    if batch_id:
        t["qlab.batch_id"] = str(batch_id)
    if seed is not None:
        t["qlab.seed"] = str(seed)
    if extra:
        for k, v in extra.items():
            t[str(k)] = str(v)
    return t

def mark_failed(run_id, reason):
    """Terminate a still-RUNNING ledger run as FAILED with a reason tag.
    Returns True if it changed anything. Never raises."""
    try:
        c = client()
        r = c.get_run(run_id)
        if r.info.status != "RUNNING":
            return False
        c.set_terminated(run_id, "FAILED")
        try:
            c.set_tag(run_id, "qlab.failed_reason", str(reason)[:490])
        except Exception:
            pass
        return True
    except Exception:
        return False

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
    p = str(path)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, p)
    return data

def heal_zombies(older_hours=24, dry_run=False):
    """Close RUNNING runs that are older than N hours (crashed recorder leftovers)."""
    cutoff = time.time() - float(older_hours) * 3600
    c = client()
    fixed, skipped = [], 0
    for e in c.search_experiments():
        for r in c.search_runs(e.experiment_id, max_results=5000):
            if r.info.status == "RUNNING" and r.info.start_time and r.info.start_time / 1000 < cutoff:
                if dry_run:
                    fixed.append(r.info.run_id)
                    continue
                try:
                    c.set_terminated(r.info.run_id, "FAILED")
                    c.set_tag(r.info.run_id, "qlab.failed_reason",
                              "zombie: RUNNING > %sh, auto-closed by registry.heal_zombies" % older_hours)
                    fixed.append(r.info.run_id)
                except Exception:
                    skipped += 1
    print(json.dumps({"closed": len(fixed), "skipped": skipped, "run_ids": fixed,
                      "dry_run": bool(dry_run)}))
    return fixed

def main():
    ap = argparse.ArgumentParser(prog="pipeline.registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("export")
    p1.add_argument("--path", default=None)
    p2 = sub.add_parser("heal-zombies")
    p2.add_argument("--older-hours", type=float, default=24)
    p2.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "export":
        export_json(a.path or str(REGISTRY_EXPORT))
    elif a.cmd == "heal-zombies":
        heal_zombies(a.older_hours, a.dry_run)

if __name__ == "__main__":
    main()
