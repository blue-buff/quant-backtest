"""Execute one spec action and write the result into the ledger (MLflow)."""
import argparse, json, os, subprocess, sys
from pathlib import Path
from . import QLAB_ROOT, DATA_VERSION
from . import registry, spec as specmod

def _write_runid_file(run_id):
    """Let the queue link/close the ledger run even if this process dies later."""
    p = os.environ.get("QLAB_RUNID_FILE")
    if not p or not run_id:
        return
    try:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(str(run_id))
    except Exception:
        pass

def git_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(QLAB_ROOT),
                             capture_output=True, text=True)
        return out.stdout.strip()[:8]
    except Exception:
        return ""


CODE_EXTENSIONS = (".py", ".yaml", ".yml", ".mjs", ".js", ".ts", ".sh", ".ps1", ".toml")

def git_dirty_code():
    """Code changes not yet in any commit (tracked modifications or untracked
    code files). The ledger tag qlab.git must reference the commit that actually
    produced the logged result, so logging is refused while code changes sit
    uncommitted; the operator must create a NEW commit id first, then retry."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(QLAB_ROOT),
                             capture_output=True, text=True).stdout
    except Exception:
        return []
    dirty = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1].strip().strip('"')
        if path.lower().endswith(CODE_EXTENSIONS) and not path.startswith("results/"):
            dirty.append(path)
    return dirty

def _log_legacy(meta, tags, artifacts, fallback_name=""):
    name = meta.get("name") or fallback_name or "unnamed"
    exp = "legacy_" + str(name)
    params = {
        "label": str(meta.get("label", "")),
        "pool": str(meta.get("pool", "")),
        "yaml": str(meta.get("yaml", "")),
        "window": str(meta.get("window", "")),
        "segments": json.dumps(meta.get("segments", {})),
        "git": str(meta.get("git", "")),
        "seconds": str(meta.get("seconds", "")),
        "source_file": str(artifacts.get("meta.json", "")),
    }
    for k, v in (meta.get("model_kwargs") or {}).items():
        params["model." + str(k)] = str(v)
    metrics = {}
    for k in ("IC", "ICIR", "rank_IC", "rank_ICIR", "train_l2", "valid_l2"):
        if k in meta and meta[k] is not None:
            metrics[k] = float(meta[k])
    for k, v in (meta.get("metrics") or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            metrics[str(k)] = float(v)
    tags["qlab.legacy"] = "true"
    tags["qlab.pool"] = str(meta.get("pool", ""))
    tags["qlab.yaml"] = str(meta.get("yaml", ""))
    tags["qlab.source"] = "legacy_backfill"
    run_id = registry.log_run(exp, params, metrics, tags, artifacts)
    return run_id, exp, metrics

def _log_eval(ev, tags, artifacts):
    exp = "eval_" + ev.get("name", "unnamed")
    params = {
        "name": str(ev.get("name", "")),
        "pool": str(ev.get("pool", "")),
        "h": str(ev.get("h", "")),
        "source_file": str(artifacts.get("eval.json", "")),
    }
    metrics = {}
    for k, v in ev.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            metrics[str(k)] = float(v)
        elif k == "bootstrap" and isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, (int, float)) and not isinstance(vv, bool):
                    metrics["bootstrap." + str(kk)] = float(vv)
        elif k == "quarters" and isinstance(v, dict):
            for q, d in v.items():
                for kk, vv in d.items():
                    if isinstance(vv, (int, float)) and not isinstance(vv, bool):
                        metrics["quarters." + str(q) + "." + str(kk)] = float(vv)
    tags["qlab.pool"] = str(ev.get("pool", ""))
    tags["qlab.source"] = "eval_backfill"
    run_id = registry.log_run(exp, params, metrics, tags, artifacts)
    return run_id, exp, metrics

def find_existing(exp_name, source_file):
    """Return run_id if this experiment already has a run logged from the same source file.
    Matches on the source_file param (new runs) or the artifact basename (old runs)."""
    if not source_file:
        return None
    c = registry.client()
    e = c.get_experiment_by_name(exp_name)
    if e is None:
        return None
    target = Path(source_file).name
    for r in c.search_runs(e.experiment_id, max_results=5000):
        if r.data.params.get("source_file") == source_file:
            return r.info.run_id
        try:
            for a in c.list_artifacts(r.info.run_id):
                if a.path == target:
                    return r.info.run_id
        except Exception:
            pass
    return None

def run(spec_path, job_id=None, batch_id=None):
    spec = specmod.load_spec(spec_path)
    eff = specmod.resolve(spec)
    h = specmod.spec_hash(eff)
    expected = os.environ.get("QLAB_EXPECTED_HASH")
    if expected and expected != h:
        # The spec file changed after submit; running it would break reproducibility.
        sys.stderr.write("QLAB_SPEC_DRIFT expected=%s actual=%s\n" % (expected, h))
        sys.exit(2)
    dirty = git_dirty_code()
    if dirty:
        shown = ",".join(dirty[:10])
        if len(dirty) > 10:
            shown += ",...(+%d more)" % (len(dirty) - 10)
        print("QLAB_UNCOMMITTED_CODE " + shown, file=sys.stderr)
        print("uncommitted code changes detected: commit them as a NEW commit id first, then retry the job (qlab.git must reference the code that produced the result)", file=sys.stderr)
        sys.exit(3)
    action = spec.get("action") or {"kind": "smoke"}
    kind = action.get("kind", "smoke")
    tags = registry.std_tags(h, batch_id=batch_id, source="harness")
    tags["qlab.git"] = git_commit()
    tags["qlab.data_version"] = DATA_VERSION
    tags["qlab.run_name"] = str(spec.get("exp_id")) + "_" + h[:6]
    tags["qlab.exp_id"] = str(spec.get("exp_id"))
    if kind == "smoke":
        exp = spec.get("exp_id")
        tags["qlab.smoke"] = "true"
        params = {"exp_id": str(exp), "action": "smoke",
                  "note": "wiring check only, not a research result"}
        metrics = {"smoke_check": 1.0}
        run_id = registry.log_run(exp, params, metrics, tags)
    elif kind == "log_legacy":
        mf = Path(QLAB_ROOT / action["meta_file"])
        meta = json.loads(mf.read_text())
        exp = "legacy_" + str(meta.get("name") or mf.stem)
        existing = find_existing(exp, str(mf))
        if existing:
            run_id = existing
            _write_runid_file(run_id)
            print("QLAB_RESULT " + json.dumps({"run_id": run_id, "exp_name": exp,
                                               "spec_hash": h, "reused": True}))
            return run_id
        run_id, exp, metrics = _log_legacy(meta, tags, {"meta.json": str(mf)}, fallback_name=mf.stem)
    elif kind == "eval_existing":
        ef = Path(QLAB_ROOT / action["eval_file"])
        ev = json.loads(ef.read_text())
        exp = "eval_" + str(ev.get("name", "unnamed"))
        existing = find_existing(exp, str(ef))
        if existing:
            run_id = existing
            _write_runid_file(run_id)
            print("QLAB_RESULT " + json.dumps({"run_id": run_id, "exp_name": exp,
                                               "spec_hash": h, "reused": True}))
            return run_id
        run_id, exp, metrics = _log_eval(ev, tags, {"eval.json": str(ef)})
    elif kind == "sleep_ok":
        import time as _time
        _time.sleep(float(action.get("seconds", 10)))
        exp = spec.get("exp_id")
        tags["qlab.smoke"] = "true"
        params = {"exp_id": str(exp), "action": "sleep_ok",
                  "seconds": str(action.get("seconds", 10)),
                  "note": "timing/mechanics test only, not a research result"}
        metrics = {"smoke_check": 1.0}
        run_id = registry.log_run(exp, params, metrics, tags)
    elif kind == "hang":
        import time as _time
        _time.sleep(float(action.get("seconds", 300)))
        raise SystemExit("hang action ended without timeout (queue bug)")
    elif kind == "crash":
        raise ValueError("deterministic crash for failure-path testing")
    else:
        raise ValueError("unknown action kind: " + repr(kind))
    _write_runid_file(run_id)
    print("QLAB_RESULT " + json.dumps({"run_id": run_id, "exp_name": exp, "spec_hash": h}))
    return run_id

def backfill(meta_dir, eval_dir):
    dirty = git_dirty_code()
    if dirty:
        shown = ",".join(dirty[:10])
        if len(dirty) > 10:
            shown += ",...(+%d more)" % (len(dirty) - 10)
        print("QLAB_UNCOMMITTED_CODE " + shown, file=sys.stderr)
        print("uncommitted code changes detected: commit them as a NEW commit id first (qlab.git must reference the code that produced the result)", file=sys.stderr)
        sys.exit(3)
    c = registry.client()
    have = {e.name for e in c.search_experiments()}
    res = {"legacy_created": 0, "legacy_skipped": 0, "eval_created": 0, "eval_skipped": 0}
    for mf in sorted(Path(QLAB_ROOT / meta_dir).glob("*.json")):
        meta = json.loads(mf.read_text())
        exp = "legacy_" + str(meta.get("name") or mf.stem)
        if exp in have:
            res["legacy_skipped"] += 1
            continue
        tags = registry.std_tags("", source="legacy_backfill")
        tags["qlab.git"] = str(meta.get("git", ""))
        _log_legacy(meta, tags, {"meta.json": str(mf)}, fallback_name=mf.stem)
        have.add(exp)
        res["legacy_created"] += 1
    for ef in sorted(Path(QLAB_ROOT / eval_dir).glob("*.json")):
        ev = json.loads(ef.read_text())
        exp = "eval_" + ev.get("name", "unnamed")
        if exp in have:
            res["eval_skipped"] += 1
            continue
        tags = registry.std_tags("", source="eval_backfill")
        _log_eval(ev, tags, {"eval.json": str(ef)})
        have.add(exp)
        res["eval_created"] += 1
    print(json.dumps(res))
    return res

def main():
    ap = argparse.ArgumentParser(prog="pipeline.harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("run")
    p1.add_argument("spec_path")
    p1.add_argument("--job-id")
    p1.add_argument("--batch-id")
    p2 = sub.add_parser("backfill")
    p2.add_argument("--meta-dir", default="docs/evidence/exps")
    p2.add_argument("--eval-dir", default="results/eval")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.spec_path, a.job_id, a.batch_id)
    elif a.cmd == "backfill":
        backfill(a.meta_dir, a.eval_dir)

if __name__ == "__main__":
    main()
