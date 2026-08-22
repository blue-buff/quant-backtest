"""Execute one spec action and write the result into the ledger (MLflow).

train action (P5 executor contract):
  data ensure (fixed menu) -> executor subprocess -> contract check ->
  fixed tester (pipeline.metrics, the ONLY metric source) -> ledger import.
The pipeline does not inspect executor internals; the executor only reads
pipeline-provided feature parquet and writes pred.pkl.
"""
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path
from . import QLAB_ROOT, DATA_VERSION
from . import registry, spec as specmod

RUNS_DIR = QLAB_ROOT / "results" / "runs"


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


def _flatten_metrics(full, task="regression"):
    """Generic metric import: numeric leaves of metrics.json -> ledger keys,
    plus the stable legacy key names so board/AGENTS views stay unchanged."""
    out = {}
    legacy = {
        "rankic_mean": full.get("nonoverlap_mean_rank_ic"),
        "rankic_std": full.get("rank_ic_std"),
        "rankic_ir": full.get("nonoverlap_rank_icir"),
        "ic": full.get("mean_ic"),
        "icir": full.get("icir"),
        "p_le0": (full.get("bootstrap_rankic") or {}).get("p_le0"),
        "hit_rate": full.get("hit_rate"),
        "n_days": full.get("n_days"),
        "n_nonoverlap": full.get("n_nonoverlap"),
        "top_bottom": (full.get("deciles") or {}).get("top_minus_bottom"),
    }
    if task == "classification":
        legacy = {"auc_mean": full.get("mean_auc"),
                  "auc_ir": full.get("auc_ir"),
                  "auc_p_le05": (full.get("bootstrap_auc") or {}).get("p_le05"),
                  "n_days": full.get("n_days"),
                  "n_inst": full.get("n_inst")}
    for k, v in legacy.items():
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass

    def walk(obj, path, depth):
        if isinstance(obj, dict):
            if depth < 3:
                for k2, v2 in obj.items():
                    walk(v2, path + str(k2) + ".", depth + 1)
        elif isinstance(obj, list):
            # lists of per-period dicts (quarters/monthly_ic) keep their period key
            for item in obj:
                if isinstance(item, dict):
                    for kp in ("quarter", "month"):
                        if kp in item:
                            walk(item, path + str(item[kp]) + ".", depth + 1)
                            break
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            try:
                out[(path)[:-1]] = float(obj)
            except (TypeError, ValueError):
                pass

    walk(full, "", 0)
    return out


def _run_executor_and_tester(spec, run_dir, cfg, executor_name, d):
    """Shared compute phase (local queue path AND remote --compute-only path):
    executor subprocess -> contract check -> fixed tester -> artifacts + work.json.
    Returns (work, full, run_dir). Never touches the ledger."""
    import pandas as pd
    from . import executor as execmod, metrics as metricsmod
    cfg_path = run_dir / "executor_config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    rc, eout, eerr, esec = execmod.run_executor(executor_name, cfg_path,
                                                d["train_pq"], d["test_pq"], run_dir)
    (run_dir / "executor.log").write_text(
        (eout or "") + "\n=== STDERR ===\n" + (eerr or ""), encoding="utf-8")
    if rc != 0:
        sys.stderr.write("executor %s failed rc=%s\n%s\n%s" % (
            executor_name, rc, (eout or "")[-800:], (eerr or "")[-800:]))
        sys.exit(4)
    rep = execmod.check_pred(run_dir / "pred.pkl", d["test_pq"])
    (run_dir / "contract_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not rep["ok"]:
        sys.stderr.write("QLAB_CONTRACT_FAIL %s\n" % json.dumps(rep, ensure_ascii=False))
        sys.exit(5)
    pred_path = run_dir / "pred.pkl"
    label_path = run_dir / "label_matrix.pkl"
    test_df = pd.read_parquet(d["test_pq"])
    test_df["y"].to_pickle(label_path)
    del test_df
    task = cfg["task"]
    if task == "classification":
        full = metricsmod.compute_full_cls(pred_path, label_path)
    else:
        full = metricsmod.compute_full(pred_path, label_path, h=cfg["horizon"])
    (run_dir / "metrics.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    work = {"exp_id": str(spec.get("exp_id")), "executor": executor_name, "task": task,
            "data_key": d["train_key"], "test_key": d["test_key"],
            "cfg": cfg, "contract": rep, "executor_seconds": esec,
            "tester": "pipeline.metrics", "data_version": DATA_VERSION,
            "runner": str(spec.get("runner", "local")),
            "expectation": spec.get("expectation"),
            "git_commit": git_commit() or os.environ.get("QLAB_EXPECTED_COMMIT", "")}
    (run_dir / "work.json").write_text(
        json.dumps(work, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return work, full, run_dir


def _import_run(work, full, run_dir, spec_hash, batch_id=None, job_id=None):
    """Ledger import from compute artifacts (local queue path OR after remote
    rsync-back). Metrics come ONLY from the fixed tester (metrics.json)."""
    from . import metrics as metricsmod
    cfg = work["cfg"]
    task = work["task"]
    executor_name = work["executor"]
    exp = str(work["exp_id"])
    rep = work["contract"]
    tags = registry.std_tags(spec_hash, batch_id=batch_id, source="harness")
    tags["qlab.git"] = str(work.get("git_commit") or git_commit())
    tags["qlab.data_version"] = DATA_VERSION
    tags["qlab.run_name"] = exp + "_" + (spec_hash or "")[:6]
    tags["qlab.exp_id"] = exp
    tags["qlab.pool"] = str(cfg["pool"])
    tags["qlab.handler"] = str(cfg["handler_class"])
    tags["qlab.task"] = str(task)
    tags["qlab.executor"] = str(executor_name)
    tags["qlab.label_h"] = str(cfg["horizon"])
    tags["qlab.seeds"] = ",".join(str(s) for s in cfg.get("seeds", []))
    tags["qlab.data_key"] = str(work["data_key"])
    tags["qlab.train"] = "true"
    params = {
        "pool": str(cfg["pool"]),
        "handler": str(cfg["handler_class"]),
        "task": str(task),
        "executor": str(executor_name),
        "label": str(cfg["label_formula"]),
        "horizon": str(cfg["horizon"]),
        "seeds": json.dumps(cfg.get("seeds", [])),
        "model": json.dumps(cfg.get("model", {})),
        "train_window": json.dumps(cfg.get("train", [])),
        "valid_window": json.dumps(cfg.get("valid", [])),
        "test_window": json.dumps([cfg.get("test_start"), cfg.get("test_end")]),
        "data_key": str(work["data_key"]),
        "executor_seconds": str(work.get("executor_seconds", "")),
        "coverage": json.dumps({k: rep.get(k) for k in
                                ("n_dates", "date_frac", "n_inst", "inst_frac", "nan_frac")}),
    }
    metrics = _flatten_metrics(full, task=task)
    artifacts = {
        "pred_matrix.pkl": str(run_dir / "pred.pkl"),
        "label_matrix.pkl": str(run_dir / "label_matrix.pkl"),
        "metrics.json": str(run_dir / "metrics.json"),
        "work.json": str(run_dir / "work.json"),
        "executor_config.json": str(run_dir / "executor_config.json"),
        "executor.log": str(run_dir / "executor.log"),
        "contract_report.json": str(run_dir / "contract_report.json"),
    }
    run_id = registry.log_run(exp, params, metrics, tags, artifacts)
    core = metricsmod.core_metrics(full, exp, run_id, DATA_VERSION,
                                   work.get("expectation"), task=task)
    (run_dir / "core_metrics.json").write_text(
        json.dumps(core, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_runid_file(run_id)
    return run_id


def run(spec_path, job_id=None, batch_id=None, compute_only=False):
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
    elif kind == "train":
        from . import data as datamod, executor as execmod
        exp = str(spec.get("exp_id"))
        run_dir = RUNS_DIR / exp
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        act = spec.get("action") or {}
        # ---- data ensure (fixed menu, pipeline-owned) ----
        d = datamod.ensure(spec, eff)
        cfg = dict(d["cfg"])
        cfg.update({
            "model": eff.get("model", {}),
            "seeds": list(eff.get("seeds") or [42]),
            "ensemble": str(eff.get("ensemble") or "rank_mean(seeds)"),
            "rounds": int(act.get("rounds", datamod.DEFAULTS["rounds"])),
            "early_stopping": int(act.get("early_stopping", datamod.DEFAULTS["early_stopping"])),
            "num_threads": int(act.get("num_threads", datamod.DEFAULTS["num_threads"])),
            "save_models": bool(act.get("save_models", False)),
        })
        executor_name = str(act.get("executor") or execmod.DEFAULT_EXECUTOR)
        # ---- executor -> contract check -> fixed tester (shared with remote) ----
        work, full, run_dir = _run_executor_and_tester(spec, run_dir, cfg,
                                                        executor_name, d)
        if compute_only:
            # remote path: artifacts stay for rsync-back; ledger import happens later
            print("QLAB_COMPUTE_OK " + json.dumps(
                {"exp_id": exp, "run_dir": str(run_dir), "spec_hash": h,
                 "git": work["git_commit"]}))
            return None
        # ---- local path: ledger import right away ----
        run_id = _import_run(work, full, run_dir, h, batch_id, job_id)
        print("QLAB_RESULT " + json.dumps({"run_id": run_id, "exp_name": exp, "spec_hash": h}))
        return run_id
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


def import_run(run_dir, spec_hash="", batch_id=None, job_id=None):
    """Import a compute-only run dir into the ledger (remote path: after
    rsync-back). Reuses the exact same import as the local queue path."""
    dirty = git_dirty_code()
    if dirty:
        shown = ",".join(dirty[:10])
        if len(dirty) > 10:
            shown += ",...(+%d more)" % (len(dirty) - 10)
        print("QLAB_UNCOMMITTED_CODE " + shown, file=sys.stderr)
        print("uncommitted code changes detected: commit them as a NEW commit id first (qlab.git must reference the code that produced the result)", file=sys.stderr)
        sys.exit(3)
    run_dir = Path(run_dir)
    work = json.loads((run_dir / "work.json").read_text())
    full = json.loads((run_dir / "metrics.json").read_text())
    run_id = _import_run(work, full, run_dir, spec_hash, batch_id, job_id)
    print("QLAB_RESULT " + json.dumps({"run_id": run_id, "exp_name": work["exp_id"],
                                       "spec_hash": spec_hash}))
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
    p1.add_argument("--compute-only", action="store_true",
                    help="compute only (no ledger): remote protocol path")
    p2 = sub.add_parser("backfill")
    p2.add_argument("--meta-dir", default="docs/evidence/exps")
    p2.add_argument("--eval-dir", default="results/eval")
    p3 = sub.add_parser("import")
    p3.add_argument("run_dir")
    p3.add_argument("--spec-hash", default="")
    p3.add_argument("--job-id")
    p3.add_argument("--batch-id")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.spec_path, a.job_id, a.batch_id, a.compute_only)
    elif a.cmd == "import":
        import_run(a.run_dir, a.spec_hash, a.job_id, a.batch_id)
    elif a.cmd == "backfill":
        backfill(a.meta_dir, a.eval_dir)


if __name__ == "__main__":
    main()
