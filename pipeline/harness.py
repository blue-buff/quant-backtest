"""Execute one spec action and write the result into the ledger (MLflow).

train action (P5 executor contract):
  data ensure (fixed menu) -> executor subprocess -> contract check ->
  fixed tester (pipeline.metrics, the ONLY metric source) -> ledger import.
The pipeline does not inspect executor internals; the executor only reads
pipeline-provided feature parquet and writes pred.pkl.
"""
import argparse, json, os, shutil, signal, subprocess, sys, time
from pathlib import Path
from . import QLAB_ROOT, DATA_VERSION
from . import registry, spec as specmod

RUNS_DIR = QLAB_ROOT / "results" / "runs"


def _write_sig_probe(signum):
    """Best-effort forensic note on signal receipt (audit #9 instrumentation):
    the next mysterious-SIGTERM records its sender (ppid cmdline) instead of
    remaining an unsolved cold case. Never raises, never blocks teardown."""
    try:
        probe_dir = Path(os.environ.get("QLAB_SIG_PROBE_DIR")
                         or (QLAB_ROOT / "results" / "queue"))
        probe_dir.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        ppid = os.getppid()
        ppid_cmd = ""
        try:
            ppid_cmd = Path("/proc/%d/cmdline" % ppid).read_text().replace("\x00", " ")
        except OSError:
            pass
        self_status = ""
        try:
            self_status = " ".join(Path("/proc/%d/status" % pid)
                                   .read_text().splitlines()[:8])
        except OSError:
            pass
        probe = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "signum": int(signum),
            "pid": pid, "ppid": ppid, "ppid_cmdline": ppid_cmd.strip(),
            "cwd": str(Path.cwd()),
            "self_status_head": self_status,
            "note": "sender provenance probe (audit #9); when the signal came "
                    "from a direct parent, ppid_cmdline names the sender",
        }
        p = probe_dir / ("sig_probe_%d.json" % pid)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(probe, ensure_ascii=False, indent=1))
        os.replace(tmp, p)
    except Exception:
        pass


_TERM_MAIN_PID = None


def _term_handler(signum, frame):
    """Kill our whole process group on SIGTERM, then exit. This aligns remote
    teardown ('timeout N env ... python -m pipeline.harness run' over ssh) with
    the local queue's killpg semantics: the executor child dies with us.

    Two guards (both observed in the wild on the DGX Spark remote):
    1. Forked-child guard: qlib data-ensure workers (joblib multiprocessing
       backend) inherit this handler. A ROUTINE pool shutdown SIGTERMs every
       worker, and a worker running killpg(0) here TERMs the whole process
       group INCLUDING the harness main -- the run kills itself mid-fetch
       (2026-08-24/25, 4+ reproductions; kernel trace: no external sender).
       Workers therefore revert to default SIGTERM death and never killpg.
    2. Re-entrancy guard: killpg(0, SIGTERM) delivers SIGTERM back to this
       very process, and without ignoring it the handler recurses until
       RecursionError (observed on the spark remote, 2026-08-24)."""
    if _TERM_MAIN_PID is not None and os.getpid() != _TERM_MAIN_PID:
        # forked child (joblib/loky/multiprocessing worker): die of SIGTERM
        # like a plain process, never killpg the group.
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            pass
        sys.exit(128 + int(signum))
    _write_sig_probe(signum)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    try:
        os.killpg(0, signal.SIGTERM)
    except OSError:
        pass
    sys.exit(128 + int(signum))


def _install_term_handler():
    """Only in non-interactive runs (queue / remote). In an interactive terminal
    the harness shares the shell's process group, and killpg(0) would kill the
    user's shell."""
    global _TERM_MAIN_PID
    import signal as _signal
    try:
        if not sys.stdout.isatty():
            _TERM_MAIN_PID = os.getpid()
            _signal.signal(_signal.SIGTERM, _term_handler)
    except (ValueError, OSError):
        pass


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


CODE_EXTENSIONS = (".py", ".yaml", ".yml", ".mjs", ".js", ".ts", ".sh", ".ps1",
                  ".toml", ".txt")  # .txt: executor requirements.txt is code


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


def _require_clean_code():
    """Dirty-code gate shared by run / import_run / backfill."""
    dirty = git_dirty_code()
    if not dirty:
        return
    shown = ",".join(dirty[:10])
    if len(dirty) > 10:
        shown += ",...(+%d more)" % (len(dirty) - 10)
    print("QLAB_UNCOMMITTED_CODE " + shown, file=sys.stderr)
    print("uncommitted code changes detected: commit them as a NEW commit id first (qlab.git must reference the code that produced the result)", file=sys.stderr)
    sys.exit(3)


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


def _run_executor_and_tester(spec, spec_path, run_dir, cfg, executor_name, d,
                            commit_pin=""):
    """Shared compute phase (local queue path AND remote --compute-only path):
    executor subprocess -> contract check -> fixed tester -> artifacts + work.json.
    Returns (work, full, run_dir). Never touches the ledger.
    commit_pin = HEAD captured BEFORE anything ran (the code that produced the
    result), so a commit landing mid-run cannot mislabel the ledger row."""
    import time
    import pandas as pd
    from . import executor as execmod, metrics as metricsmod
    # the exact spec is archived verbatim into the run dir (reproducibility)
    shutil.copyfile(spec_path, run_dir / "spec.json")
    families = list(spec.get("metrics") or []) or None
    trade_fams = [f for f in (families or []) if f in metricsmod.TRADE_FAMILIES]
    cfg["metric_families"] = families or ["prediction"]
    if trade_fams:
        from . import data as datamod
        cfg["price_pq"] = str(datamod.price_ensure(cfg))
    cfg_path = run_dir / "executor_config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    rc, eout, eerr, esec = execmod.run_executor(executor_name, cfg_path,
                                                d["train_pq"], d["test_pq"], run_dir)
    # executor.log already holds the streamed live output (audit #3); only fill
    # it in when the executor produced nothing at all (e.g. env failure)
    logfile = run_dir / "executor.log"
    if not logfile.exists() or logfile.stat().st_size == 0:
        logfile.write_text(
            (eout or "") + "\n=== STDERR ===\n" + (eerr or ""), encoding="utf-8")
    if rc != 0:
        sys.stderr.write("executor %s failed rc=%s\n%s\n%s" % (
            executor_name, rc, (eout or "")[-800:], (eerr or "")[-800:]))
        sys.exit(4)
    rep = execmod.check_pred(run_dir / "pred.pkl", d["test_pq"], run_dir)
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
    # ---- portfolio contract (P8 T2) ----
    pf_path = run_dir / "portfolio.pkl"
    pf_rep = None
    if trade_fams and pf_path.exists():
        pf_rep = execmod.check_portfolio(pf_path, d["test_pq"])
        (run_dir / "portfolio_contract.json").write_text(
            json.dumps(pf_rep, ensure_ascii=False, indent=2), encoding="utf-8")
        if not pf_rep["ok"]:
            sys.stderr.write("QLAB_CONTRACT_FAIL (portfolio) %s\n"
                             % json.dumps(pf_rep, ensure_ascii=False))
            sys.exit(5)
    t_test0 = time.time()
    if task == "classification":
        full = metricsmod.compute_full_cls(pred_path, label_path)
    else:
        full = metricsmod.compute_full(pred_path, label_path, h=cfg["horizon"])
    if trade_fams:
        if pf_path.exists():
            bench = {"hs300": "sh000300", "zz500": "sh000905"}.get(cfg["pool"])
            full = metricsmod.compute_full_trade(
                full, pred_path, label_path, pf_path, cfg.get("price_pq"),
                trade_fams, costs=(spec.get("action") or {}).get("costs"),
                task=task, h=cfg["horizon"], benchmark=bench)
        else:
            full["portfolio_missing"] = True
    full = metricsmod.filter_pred(full, families)
    (run_dir / "metrics.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    work = {"exp_id": str(spec.get("exp_id")), "executor": executor_name, "task": task,
            "base_ref": str(spec.get("base") or "inline"),
            "hypothesis": str(spec.get("hypothesis") or ""),
            "claims": list(spec.get("claims") or []),
            "data_key": d["train_key"], "test_key": d["test_key"],
            "data_revision": int(d.get("data_revision") or 0),
            "cfg": cfg, "contract": rep, "executor_seconds": esec,
            "tester": "pipeline.metrics", "data_version": DATA_VERSION,
            "runner": str(spec.get("runner", "local")),
            "expectation": spec.get("expectation"),
            "metric_families": cfg["metric_families"],
            "portfolio_contract": pf_rep,
            "changes": str(spec.get("changes") or ""),
            "tester_seed": int(metricsmod.TESTER_BOOTSTRAP_SEED),
            "tester_seconds": round(time.time() - t_test0, 2),
            "git_commit": commit_pin or git_commit()
            or os.environ.get("QLAB_EXPECTED_COMMIT", "")}
    (run_dir / "work.json").write_text(
        json.dumps(work, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return work, full, run_dir


def _collect_artifacts(run_dir):
    """EVERY file in the run dir goes into the ledger (models, run_info.json,
    portfolio.pkl, the verbatim spec.json copy...). Restoring a snapshot must
    not lose anything the executor produced."""
    artifacts = {
        "pred_matrix.pkl": str(run_dir / "pred.pkl"),
        "label_matrix.pkl": str(run_dir / "label_matrix.pkl"),
        "metrics.json": str(run_dir / "metrics.json"),
        "work.json": str(run_dir / "work.json"),
        "executor_config.json": str(run_dir / "executor_config.json"),
        "executor.log": str(run_dir / "executor.log"),
        "contract_report.json": str(run_dir / "contract_report.json"),
        "review.json": str(run_dir / "review.json"),
    }
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and str(p.relative_to(run_dir)) not in artifacts:
            artifacts[str(p.relative_to(run_dir))] = str(p)
    return artifacts


def _build_params(work, rep):
    """Ledger params: the full reproduction-relevant config (quant-grade
    completeness: seeds, ensemble, rounds, threads, fit window, instruments,
    price key, tester seed, durations, changes text, coverage)."""
    cfg = work["cfg"]
    price = str(cfg.get("price_pq") or "")
    return {
        "pool": str(cfg["pool"]),
        "handler": str(cfg["handler_class"]),
        "task": str(work["task"]),
        "executor": str(work["executor"]),
        "instruments": str(cfg.get("instruments")),
        "label": str(cfg["label_formula"]),
        "horizon": str(cfg["horizon"]),
        "fit_window": json.dumps([cfg.get("fit_start_time"), cfg.get("fit_end_time")]),
        "seeds": json.dumps(cfg.get("seeds", [])),
        "ensemble": str(cfg.get("ensemble", "")),
        "rounds": str(cfg.get("rounds", "")),
        "early_stopping": str(cfg.get("early_stopping", "")),
        "num_threads": str(cfg.get("num_threads", "")),
        "model": json.dumps(cfg.get("model", {})),
        "train_window": json.dumps(cfg.get("train", [])),
        "valid_window": json.dumps(cfg.get("valid", [])),
        "test_window": json.dumps([cfg.get("test_start"), cfg.get("test_end")]),
        "data_key": str(work["data_key"]),
        "price_key": Path(price).name if price else "",
        "tester_seed": str(work.get("tester_seed", "")),
        "tester_seconds": str(work.get("tester_seconds", "")),
        "executor_seconds": str(work.get("executor_seconds", "")),
        "changes": str(work.get("changes") or ""),
        "coverage": json.dumps({k: rep.get(k) for k in
                                ("n_dates", "date_frac", "n_inst", "inst_frac", "nan_frac")}),
    }


def _find_prior_run(work, spec_hash):
    """Idempotent import: if THIS exact compute (same spec hash + commit + data
    keys) is already in the ledger, reuse its row. Different commit = different
    code = a legitimately new run, never deduped."""
    want_git = str(work.get("git_commit") or "")
    want_dk = str(work.get("data_key") or "")
    want_tk = str(work.get("test_key") or "")
    c = registry.client()
    for e in c.search_experiments():
        for r in c.search_runs(e.experiment_id, max_results=50000):
            t = dict(r.data.tags)
            if (r.info.status == "FINISHED"
                    and t.get("qlab.spec_hash") == (spec_hash or "")
                    and t.get("qlab.git") == want_git
                    and t.get("qlab.data_key") == want_dk
                    and t.get("qlab.test_key") == want_tk):
                return r.info.run_id
    return None


def _import_run(work, full, run_dir, spec_hash, batch_id=None, job_id=None):
    """Ledger import from compute artifacts (local queue path OR after remote
    rsync-back). Metrics come ONLY from the fixed tester (metrics.json)."""
    from . import data as datamod
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
    br = str(work.get("base_ref") or "inline")
    tags["qlab.base_ref"] = br[4:] if br.startswith("ref:") else (br or "inline")
    if work.get("hypothesis"):
        tags["qlab.hypothesis"] = str(work["hypothesis"])
    tags["qlab.pool"] = str(cfg["pool"])
    tags["qlab.handler"] = str(cfg["handler_class"])
    tags["qlab.task"] = str(task)
    tags["qlab.executor"] = str(executor_name)
    tags["qlab.label_h"] = str(cfg["horizon"])
    tags["qlab.seeds"] = ",".join(str(s) for s in cfg.get("seeds", []))
    tags["qlab.data_key"] = str(work["data_key"])
    tags["qlab.test_key"] = str(work.get("test_key") or "")
    tags["qlab.runner"] = str(work.get("runner") or "local")
    # audit #7: the revision under which the feature cache was BUILT
    # (recorded in work.json at compute time), not the import-time manifest
    tags["qlab.data_rev"] = str(work.get("data_revision")
                                if work.get("data_revision") is not None
                                else datamod.data_revision())
    extra = work.get("contract", {}).get("extra_features") or []
    if extra:
        tags["qlab.extra_features"] = ",".join(str(x) for x in extra)
    fams = work.get("cfg", {}).get("metric_families") or ["prediction"]
    tags["qlab.metric_families"] = ",".join(str(f) for f in fams)
    if any(f in metricsmod.TRADE_FAMILIES for f in fams) and \
            not (run_dir / "portfolio.pkl").exists():
        tags["qlab.portfolio_missing"] = "true"
    # ---- auto advisory review (P7 T7): recorded, never blocks ----
    from . import review as reviewmod
    checks = reviewmod.from_file(str(run_dir / "metrics.json"))
    passed = sum(1 for c in checks if c.get("pass"))
    (run_dir / "review.json").write_text(
        json.dumps({"checks": checks, "passed": passed, "total": len(checks)},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    tags["qlab.review"] = "%d/%d" % (passed, len(checks))
    attrs = full.get("attribution") or {}
    if attrs:
        tags["qlab.attribution"] = ";".join(
            "%s=%s" % (k, str(v.get("verdict")))
            for k, v in attrs.items() if isinstance(v, dict))
    if "backtest" in full:
        tags["qlab.benchmark"] = str(full["backtest"].get("benchmark") or "")
    tags["qlab.train"] = "true"
    params = _build_params(work, rep)
    metrics = _flatten_metrics(full, task=task)
    artifacts = _collect_artifacts(run_dir)
    run_id = registry.log_run(exp, params, metrics, tags, artifacts)
    try:
        core = metricsmod.core_metrics(full, exp, run_id, DATA_VERSION,
                                       work.get("expectation"), task=task)
        (run_dir / "core_metrics.json").write_text(
            json.dumps(core, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        # ---- knowledge loop (P7 T6): auto-claim + spec-declared claim linkback ----
        from . import kb as kbmod
        check = core["conclusion"]["expectation_check"]
        kbmod.append_run_claim(exp)
        kbmod.link_claims(work.get("claims") or [], exp, check)
        # ---- completion notification line (P7 T4): bridge reads results/queue/done.log ----
        from . import queue as queuemod
        if task == "classification":
            sig = full.get("mean_auc")
            pval = (full.get("bootstrap_auc") or {}).get("p_le05")
        else:
            sig = full.get("nonoverlap_mean_rank_ic")
            pval = (full.get("bootstrap_rankic") or {}).get("p_le0")
        queuemod.append_done_log({
            "exp_id": exp, "run_id": run_id,
            "batch_id": str(batch_id or ""), "job_id": str(job_id or ""),
            "rankic": sig, "p": pval,
            "expectation_check": core["conclusion"]["expectation_check"],
        })
    except Exception:
        # log_run already committed a FINISHED row: remove it so a failed import
        # never leaves a half-recorded ledger row behind
        try:
            registry.client().delete_run(run_id)
        except Exception:
            pass
        raise
    _write_runid_file(run_id)
    return run_id


def _safe_run_dir(exp):
    """exp_id is validated as a single path segment upstream (spec.resolve);
    this assertion is the last line of defense before ANY rmtree (audit #4)."""
    run_dir = (RUNS_DIR / str(exp)).resolve()
    try:
        run_dir.relative_to(RUNS_DIR.resolve())
    except ValueError:
        raise ValueError("QLAB_SPEC_INVALID: exp_id %r escapes RUNS_DIR" % exp)
    return run_dir


def run(spec_path, job_id=None, batch_id=None, compute_only=False):
    _install_term_handler()
    spec = specmod.load_spec(spec_path)
    eff = specmod.resolve(spec)
    h = specmod.spec_hash(eff)
    expected = os.environ.get("QLAB_EXPECTED_HASH")
    if expected and expected != h:
        # The spec file changed after submit; running it would break reproducibility.
        sys.stderr.write("QLAB_SPEC_DRIFT expected=%s actual=%s\n" % (expected, h))
        sys.exit(2)
    _require_clean_code()
    # pin the commit BEFORE anything runs: qlab.git must reference the code
    # that actually produced this result, even if commits land mid-run
    commit_pin = git_commit() or os.environ.get("QLAB_EXPECTED_COMMIT", "")
    action = spec.get("action") or {"kind": "smoke"}
    kind = action.get("kind", "smoke")
    tags = registry.std_tags(h, batch_id=batch_id, source="harness")
    tags["qlab.git"] = commit_pin or git_commit()
    tags["qlab.data_version"] = DATA_VERSION
    tags["qlab.run_name"] = str(spec.get("exp_id")) + "_" + h[:6]
    tags["qlab.exp_id"] = str(spec.get("exp_id"))
    base = str(spec.get("base") or "")
    tags["qlab.base_ref"] = base[4:] if base.startswith("ref:") else "inline"
    if spec.get("hypothesis"):
        tags["qlab.hypothesis"] = str(spec["hypothesis"])
    if kind == "smoke":
        exp = spec.get("exp_id")
        tags["qlab.smoke"] = "true"
        if compute_only:
            # remote protocol: leave a minimal run dir; the LOCAL import logs it
            run_dir = _safe_run_dir(exp)
            if run_dir.exists():
                shutil.rmtree(run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            work = {"kind": "smoke", "exp_id": str(exp), "spec_hash": h,
                    "base_ref": str(spec.get("base") or "inline"),
                    "hypothesis": str(spec.get("hypothesis") or ""),
                    "runner": str(spec.get("runner", "auto")),
                    "git_commit": commit_pin}
            (run_dir / "work.json").write_text(
                json.dumps(work, ensure_ascii=False, indent=2))
            print("QLAB_COMPUTE_OK " + json.dumps(
                {"exp_id": exp, "run_dir": str(run_dir), "spec_hash": h,
                 "kind": "smoke"}))
            return None
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
        run_dir = _safe_run_dir(exp)
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
            "params": eff.get("_params") or {},
        })
        executor_name = str(act.get("executor") or execmod.DEFAULT_EXECUTOR)
        # ---- executor -> contract check -> fixed tester (shared with remote) ----
        work, full, run_dir = _run_executor_and_tester(spec, spec_path, run_dir, cfg,
                                                        executor_name, d, commit_pin)
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
    _require_clean_code()
    run_dir = Path(run_dir)
    work = json.loads((run_dir / "work.json").read_text())
    if work.get("kind") != "smoke":
        prior = _find_prior_run(work, spec_hash)
        if prior:
            print("QLAB_RESULT " + json.dumps({"run_id": prior,
                                               "exp_name": work["exp_id"],
                                               "spec_hash": spec_hash,
                                               "reused": True}))
            return prior
    if work.get("kind") == "smoke":
        # smoke over the remote chain: log the wiring-check row locally
        tags = registry.std_tags(work.get("spec_hash", spec_hash),
                                 batch_id=batch_id, source="harness")
        tags["qlab.smoke"] = "true"
        tags["qlab.git"] = str(work.get("git_commit") or git_commit())
        tags["qlab.run_name"] = str(work["exp_id"]) + "_" + str(work.get("spec_hash", ""))[:6]
        tags["qlab.exp_id"] = str(work["exp_id"])
        br = str(work.get("base_ref") or "inline")
        tags["qlab.base_ref"] = br[4:] if br.startswith("ref:") else (br or "inline")
        if work.get("hypothesis"):
            tags["qlab.hypothesis"] = str(work["hypothesis"])
        params = {"exp_id": str(work["exp_id"]), "action": "smoke",
                  "note": "wiring check via remote compute-only, not a research result"}
        run_id = registry.log_run(work["exp_id"], params, {"smoke_check": 1.0}, tags)
        print("QLAB_RESULT " + json.dumps({"run_id": run_id,
                                           "exp_name": work["exp_id"],
                                           "spec_hash": work.get("spec_hash", "")}))
        return run_id
    full = json.loads((run_dir / "metrics.json").read_text())
    run_id = _import_run(work, full, run_dir, spec_hash, batch_id, job_id)
    print("QLAB_RESULT " + json.dumps({"run_id": run_id, "exp_name": work["exp_id"],
                                       "spec_hash": spec_hash}))
    return run_id


def backfill(meta_dir, eval_dir):
    _require_clean_code()
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
        import_run(a.run_dir, a.spec_hash, job_id=a.job_id, batch_id=a.batch_id)
    elif a.cmd == "backfill":
        backfill(a.meta_dir, a.eval_dir)


if __name__ == "__main__":
    main()
