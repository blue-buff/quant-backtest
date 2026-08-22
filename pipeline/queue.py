"""Job queue: sqlite task table + dispatcher + events/heartbeat/heal.
CLI: submit / status / run / retry / unblock / heal / show / events / notify.

Concurrency is real (thread pool + atomic claim + WAL). Heartbeat is written by a
background thread every 5s and carries the dispatcher PID; heal fires only after
verifying that PID is dead, so long-running jobs cannot be mis-killed by the bridge.
Every task runs with an expected spec hash in the environment; the harness refuses
to run if the spec drifted after submit."""
import argparse, json, os, re, signal, sqlite3, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from . import JOBS_DB, QUEUE_LOGS, QLAB_ROOT
from . import registry
from . import spec as specmod

EVENTS_LOG = QLAB_ROOT / "results" / "queue" / "events.log"
HEARTBEAT_FILE = QLAB_ROOT / "results" / "queue" / "heartbeat"
MARKER_FILE = QLAB_ROOT / "results" / "queue" / "notify_marker"
RUNID_DIR = QLAB_ROOT / "results" / "queue" / "runids"
STALE_MINUTES = 5
HEARTBEAT_SECS = 5
DEFAULT_TIMEOUT_MIN = 120
DRIFT_RE = re.compile(r"QLAB_SPEC_DRIFT expected=(\S+) actual=(\S+)")

SCHEMA = """CREATE TABLE IF NOT EXISTS jobs(
 job_id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id TEXT NOT NULL,
 exp_id TEXT NOT NULL,
 spec_path TEXT NOT NULL,
 spec_hash TEXT NOT NULL,
 runner TEXT DEFAULT 'local',
 status TEXT DEFAULT 'queued',
 attempts INTEGER DEFAULT 0,
 timeout_min INTEGER DEFAULT 120,
 created_at TEXT, started_at TEXT, finished_at TEXT,
 mlflow_run_id TEXT, error TEXT, note TEXT)"""

EVENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER, batch_id TEXT, exp_id TEXT, status TEXT, error TEXT, ts TEXT)"""

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    RUNID_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOBS_DB), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(SCHEMA)
    conn.execute(EVENTS_SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "pgid" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN pgid INTEGER")
    # one active row per spec hash: submit is idempotent even under double-submit
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_spec ON jobs(spec_hash)"
                 " WHERE status IN ('queued','running','blocked')")
    conn.commit()
    return conn

def _event(conn, job_id, batch_id, exp_id, status, error=None):
    """Record a status transition into events table + events.log."""
    ts = _now()
    conn.execute("INSERT INTO events(job_id,batch_id,exp_id,status,error,ts) VALUES(?,?,?,?,?,?)",
                 (job_id, batch_id, exp_id, status, error, ts))
    line = json.dumps({"ts": ts, "job_id": job_id, "exp_id": exp_id,
                       "status": status, "error": error}, ensure_ascii=False)
    with open(EVENTS_LOG, "a") as f:
        f.write(line + "\n")
    return line

def _beat():
    """Dispatcher heartbeat: '<epoch> <pid>', written atomically (timezone-proof)."""
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_FILE.with_suffix(".tmp")
    tmp.write_text("%d %d" % (int(time.time()), os.getpid()))
    os.replace(tmp, HEARTBEAT_FILE)

def _hb_loop(stop):
    while not stop.wait(HEARTBEAT_SECS):
        try:
            _beat()
        except Exception:
            pass

def _pid_alive(pid):
    # zombie check first: a killed-but-unreaped process still answers kill(pid, 0)
    try:
        st = Path("/proc/%d/stat" % pid).read_text()
        state = st.split(") ", 1)[1].split(" ", 1)[0] if ") " in st else ""
        if state == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

def submit(batch_path):
    batch = json.loads(Path(batch_path).read_text())
    bid = batch["batch_id"]
    conn = db()
    new, skip_done, skip_dup = 0, 0, 0
    for sp in batch["specs"]:
        spec = specmod.load_spec(str(QLAB_ROOT / sp))
        eff = specmod.resolve(spec)
        h = specmod.spec_hash(eff)
        exp_id = spec.get("exp_id")
        runner = spec.get("runner", "local")
        timeout = int(spec.get("timeout_min", batch.get("timeout_min", DEFAULT_TIMEOUT_MIN)))
        row = conn.execute(
            "SELECT job_id,status FROM jobs WHERE exp_id=? AND spec_hash=? ORDER BY job_id DESC LIMIT 1",
            (exp_id, h)).fetchone()
        if row and row["status"] == "done":
            skip_done += 1
            continue
        if row and row["status"] in ("queued", "running", "blocked"):
            skip_dup += 1
            continue
        try:
            cur = conn.execute(
                "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,timeout_min,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (bid, exp_id, sp, h, runner, "queued", timeout, _now()))
        except sqlite3.IntegrityError:
            skip_dup += 1
            continue
        _event(conn, cur.lastrowid, bid, exp_id, "queued")
        new += 1
    conn.commit()
    print(json.dumps({"batch_id": bid, "new": new, "skip_done": skip_done, "skip_dup": skip_dup}))

def status(batch_id=None, as_json=False):
    conn = db()
    q = "SELECT * FROM jobs" + (" WHERE batch_id=?" if batch_id else "") + " ORDER BY job_id"
    rows = [dict(r) for r in conn.execute(q, ((batch_id,) if batch_id else ())).fetchall()]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print("%-6s %-8s %-28s %-10s %-3s %s" % ("JOB", "STATUS", "EXP", "RUNNER", "ATT", "MLFLOW/ERROR"))
        for r in rows:
            tail = r["mlflow_run_id"] or (r["error"] or "")[:40]
            print("%-6s %-8s %-28s %-10s %-3s %s" % (
                r["job_id"], r["status"], r["exp_id"][:28], r["runner"], r["attempts"], tail))
    return rows

def _mark_ledger_failed(job_id, row, reason):
    """Close the ledger run (if any and still RUNNING) as FAILED; best effort."""
    run_id = row.get("mlflow_run_id")
    if not run_id:
        f = RUNID_DIR / ("job_%s.runid" % job_id)
        if f.exists():
            run_id = f.read_text().strip() or None
    if not run_id:
        return
    try:
        registry.mark_failed(run_id, reason)
    except Exception:
        pass

def _execute(row):
    conn = db()
    jid, spath = row["job_id"], row["spec_path"]
    try:
        if row["runner"] != "local":
            reason = "remote runner not enabled (lab rules: remote machine untouched)"
            if row["runner"] == "spark":
                # P6: DGX Spark docker dispatch (v1 code-complete in
                # pipeline/remote.py; blocked while QLAB_SPARK_SSH is blank).
                try:
                    from . import remote
                    res = remote.dispatch(dict(row))
                except Exception as e:
                    res = {"ok": False, "blocked": False, "reason": "spark dispatch error: %s" % e}
                if res.get("ok"):
                    conn.execute("UPDATE jobs SET status='done', finished_at=?, mlflow_run_id=?, error=NULL"
                                 " WHERE job_id=? AND status='running'",
                                 (_now(), res.get("run_id"), jid))
                    _event(conn, jid, row["batch_id"], row["exp_id"], "done")
                    conn.commit()
                    return
                reason = res.get("reason", "spark dispatch failed")
            conn.execute("UPDATE jobs SET status='blocked', note=? WHERE job_id=?",
                         (reason, jid))
            _event(conn, jid, row["batch_id"], row["exp_id"], "blocked", reason)
            conn.commit()
            return
        logfile = QUEUE_LOGS / ("job_%s.log" % jid)
        cmd = [sys.executable, "-m", "pipeline.harness", "run", str(QLAB_ROOT / spath),
               "--job-id", str(jid), "--batch-id", str(row["batch_id"])]
        env = os.environ.copy()
        # cgroup pids.max=256: cap BLAS/OpenMP threads in the harness process
        # (the executor child gets its own cap in pipeline.executor)
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            env.setdefault(k, "4")
        env["QLAB_JOB_ID"] = str(jid)
        env["QLAB_BATCH_ID"] = str(row["batch_id"])
        env["QLAB_EXPECTED_HASH"] = str(row["spec_hash"])
        env["QLAB_RUNID_FILE"] = str(RUNID_DIR / ("job_%s.runid" % jid))
        env_timeout = int(os.environ.get("QLAB_QUEUE_TIMEOUT_SECONDS", 0) or 0)
        timeout = env_timeout or int(row["timeout_min"] or DEFAULT_TIMEOUT_MIN) * 60
        proc = subprocess.Popen(cmd, cwd=str(QLAB_ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True,
                                env=env)
        conn.execute("UPDATE jobs SET pgid=? WHERE job_id=?", (proc.pid, jid))
        conn.commit()
        try:
            out, err = proc.communicate(timeout=timeout)
            with open(logfile, "w") as f:
                f.write(out or "")
                f.write("\n=== STDERR ===\n")
                f.write(err or "")
            result = None
            for line in (out or "").splitlines():
                if line.startswith("QLAB_RESULT "):
                    try:
                        result = json.loads(line[len("QLAB_RESULT "):])
                    except ValueError:
                        result = None
            if proc.returncode == 0 and result and result.get("run_id"):
                cur = conn.execute("UPDATE jobs SET status='done', finished_at=?, mlflow_run_id=?, error=NULL"
                                   " WHERE job_id=? AND status='running'",
                                   (_now(), result["run_id"], jid))
                if cur.rowcount > 0:
                    _event(conn, jid, row["batch_id"], row["exp_id"], "done")
                else:
                    # heal raced us: keep the healed state but preserve the ledger link
                    conn.execute("UPDATE jobs SET mlflow_run_id=?, note=COALESCE(note,'')||? "
                                 "WHERE job_id=? AND mlflow_run_id IS NULL",
                                 (result["run_id"],
                                  " [completed after auto-heal; run_id linked, status kept]", jid))
                    _event(conn, jid, row["batch_id"], row["exp_id"], "done",
                           "completed but job had been auto-healed concurrently; run_id linked")
            else:
                errmsg = ((err or "")[-400:] or "no QLAB_RESULT in stdout")
                drift = DRIFT_RE.search((out or "") + (err or ""))
                if drift:
                    errmsg = ("spec changed after submit: expected hash %s, file now %s "
                              "(re-submit to record the new spec)" % (drift.group(1), drift.group(2)))
                conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                             (_now(), errmsg, jid))
                _event(conn, jid, row["batch_id"], row["exp_id"], "failed", errmsg)
                _mark_ledger_failed(jid, row, errmsg)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                out, err = proc.communicate()
            with open(logfile, "w") as f:
                f.write(out or "")
                f.write("\n=== STDERR ===\n")
                f.write(err or "")
                f.write("\n=== TIMEOUT: killed process group pgid=%s ===\n" % proc.pid)
            errmsg = "timeout (killed process group)"
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                         (_now(), errmsg, jid))
            _event(conn, jid, row["batch_id"], row["exp_id"], "failed", errmsg)
            _mark_ledger_failed(jid, row, errmsg)
    except Exception as e:
        try:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                         (_now(), repr(e)[:400], jid))
            _event(conn, jid, row["batch_id"], row["exp_id"], "failed", repr(e)[:400])
        except Exception:
            pass
    finally:
        try:
            conn.commit()
        except Exception:
            pass
        conn.close()

def run(batch_id=None, once=True, concurrency=2):
    """Drain queued jobs. concurrency is real: N jobs execute in parallel processes."""
    concurrency = max(1, int(concurrency))
    conn = db()
    _beat()
    stop = threading.Event()
    hb = threading.Thread(target=_hb_loop, args=(stop,), daemon=True)
    hb.start()
    executor = ThreadPoolExecutor(max_workers=concurrency)
    try:
        while True:
            q = "SELECT * FROM jobs WHERE status='queued'"
            args = []
            if batch_id:
                q += " AND batch_id=?"
                args.append(batch_id)
            q += " ORDER BY job_id LIMIT ?"
            args.append(concurrency)
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
            claimed = []
            for row in rows:
                # atomic claim: only the dispatcher that wins the UPDATE runs the job
                cur = conn.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 "
                                   "WHERE job_id=? AND status='queued'",
                                   (_now(), row["job_id"]))
                if cur.rowcount == 1:
                    _event(conn, row["job_id"], row["batch_id"], row["exp_id"], "running")
                    claimed.append(row)
            conn.commit()
            if not claimed:
                if once:
                    break
                time.sleep(10)
                continue
            futures = [executor.submit(_execute, row) for row in claimed]
            for f in futures:
                f.result()
    finally:
        stop.set()
        hb.join(timeout=2)
        executor.shutdown(wait=True)
        conn.close()
    print(json.dumps({"drained": True, "batch_id": batch_id, "concurrency": concurrency}))

def _requeue(rows, conn, reason, force_local=False):
    for r in rows:
        q = "UPDATE jobs SET status='queued', error=NULL, finished_at=NULL, started_at=NULL"
        args = []
        if force_local:
            q += ", runner='local'"
        q += ", note=? WHERE job_id=?"
        args += [reason, r["job_id"]]
        conn.execute(q, args)
        _event(conn, r["job_id"], r["batch_id"], r["exp_id"], "queued", reason)

def retry(batch_id=None, include_blocked=False, job_ids=None):
    conn = db()
    flt = "status IN ('failed','blocked')" if include_blocked else "status='failed'"
    q = ("SELECT * FROM jobs WHERE " + flt + " AND attempts<3"
         " AND spec_hash NOT IN (SELECT spec_hash FROM jobs WHERE status IN ('queued','running','blocked'))")
    args = []
    if batch_id:
        q += " AND batch_id=?"
        args.append(batch_id)
    if job_ids:
        q += " AND job_id IN (%s)" % ",".join("?" * len(job_ids))
        args += [int(j) for j in job_ids]
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    # one active row per spec_hash: only the latest submission of each spec is requeued
    seen, picked = set(), []
    for r in sorted(rows, key=lambda x: x["job_id"], reverse=True):
        if r["spec_hash"] in seen:
            continue
        seen.add(r["spec_hash"])
        picked.append(r)
    for r in picked:
        # blocked -> local (old semantics), EXCEPT spark jobs: they keep their
        # runner so a fixed transport can be retried as spark.
        force_local = r["status"] == "blocked" and r["runner"] != "spark"
        if r["status"] == "blocked":
            reason = ("requeued by retry (blocked, runner kept)"
                      if r["runner"] == "spark" else "requeued by retry (blocked->local)")
        else:
            reason = "requeued by retry"
        try:
            _requeue([r], conn, reason, force_local=force_local)
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    print(json.dumps({"requeued": len(picked), "job_ids": [r["job_id"] for r in picked]}))

def unblock(job_ids=None):
    """Blocked jobs have no path back; force them queued on the local runner."""
    conn = db()
    if job_ids:
        q = "SELECT * FROM jobs WHERE status='blocked' AND job_id IN (%s)" % ",".join("?" * len(job_ids))
        rows = [dict(r) for r in conn.execute(q, job_ids).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status='blocked'").fetchall()]
    _requeue(rows, conn, "unblocked by operator (runner forced local)", force_local=True)
    conn.commit()
    print(json.dumps({"unblocked": [r["job_id"] for r in rows]}))

def heal(stale_minutes=STALE_MINUTES):
    """Auto-heal. Only fires when the heartbeat is stale AND the dispatcher PID is
    verifiably dead; a missing heartbeat file is 'unknown' (no mutation, manual check).
    Prints a JSON state object for the host bridge."""
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall()]
    out = {"state": "ok", "running": len(rows), "heartbeat": None, "auto_healed": []}
    if not rows:
        print(json.dumps(out))
        return
    hb = None
    if HEARTBEAT_FILE.exists():
        try:
            parts = HEARTBEAT_FILE.read_text().strip().split()
            hb = {"epoch": int(parts[0]) if parts else None,
                  "pid": int(parts[1]) if len(parts) > 1 else None}
        except (ValueError, OSError):
            hb = {"epoch": None, "pid": None}
    out["heartbeat"] = hb
    if hb is None or hb["epoch"] is None:
        out["state"] = "unknown"
        out["reason"] = "heartbeat file missing/unreadable; refusing to auto-heal, manual check required"
        print(json.dumps(out))
        return
    age = time.time() - hb["epoch"]
    out["heartbeat_age_sec"] = round(age, 1)
    if age <= stale_minutes * 60:
        print(json.dumps(out))
        return
    if hb["pid"] is not None and _pid_alive(hb["pid"]):
        out["state"] = "alive_but_stale"
        out["reason"] = ("dispatcher pid %s is alive; heartbeat lag (clock drift?), no action" % hb["pid"])
        print(json.dumps(out))
        return
    reason = ("dispatcher died (heartbeat stale, pid %s verified dead), auto-healed" % hb["pid"]
              if hb["pid"] is not None
              else "dispatcher died (heartbeat stale, no pid recorded), auto-healed")
    for r in rows:
        if r.get("pgid"):
            try:
                os.killpg(r["pgid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                     (_now(), reason, r["job_id"]))
        _event(conn, r["job_id"], r["batch_id"], r["exp_id"], "failed", reason)
        _mark_ledger_failed(r["job_id"], r, reason)
        out["auto_healed"].append(r["job_id"])
    conn.commit()
    out["state"] = "healed"
    print(json.dumps(out))

def show(job_id):
    """One-shot view for failure triage: job row + its events + log tail."""
    conn = db()
    r = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not r:
        print(json.dumps({"error": "no such job: %s" % job_id}))
        return
    evs = [dict(x) for x in conn.execute(
        "SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()]
    logfile = QUEUE_LOGS / ("job_%s.log" % job_id)
    tail = ""
    if logfile.exists():
        tail = "".join(logfile.read_text().splitlines(keepends=True)[-30:])
    print(json.dumps({"job": dict(r), "events": evs, "log_tail": tail},
                      ensure_ascii=False, indent=1))

def events(since=None, limit=100):
    conn = db()
    if since:
        rows = conn.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?",
                            (since, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        rows = list(reversed(rows))
    print(json.dumps([dict(x) for x in rows], ensure_ascii=False, indent=1))

def notify():
    """Bridge endpoint: print events newer than the marker, then advance marker.
    Drains completely (no cap), so burst events are never silently dropped."""
    conn = db()
    last = 0
    if MARKER_FILE.exists():
        try:
            last = int(MARKER_FILE.read_text().strip() or 0)
        except ValueError:
            last = 0
    rows = []
    while True:
        batch = [dict(x) for x in conn.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 500", (last,)).fetchall()]
        if not batch:
            break
        rows += batch
        last = batch[-1]["id"]
        if len(batch) < 500:
            break
    if rows:
        tmp = MARKER_FILE.with_suffix(".tmp")
        tmp.write_text(str(rows[-1]["id"]))
        os.replace(tmp, MARKER_FILE)
        print(json.dumps(rows, ensure_ascii=False))
    return rows

def main():
    ap = argparse.ArgumentParser(prog="pipeline.queue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("submit").add_argument("batch_path")
    p2 = sub.add_parser("status")
    p2.add_argument("--batch")
    p2.add_argument("--json", action="store_true")
    p3 = sub.add_parser("run")
    p3.add_argument("--batch")
    p3.add_argument("--once", action="store_true", default=True)
    p3.add_argument("--watch", dest="once", action="store_false")
    p3.add_argument("--concurrency", type=int, default=2)
    p4 = sub.add_parser("retry")
    p4.add_argument("--batch")
    p4.add_argument("--blocked", action="store_true", help="also requeue blocked jobs (forced to local runner)")
    p4.add_argument("job_ids", nargs="*", type=int, help="only these job ids; empty = all eligible")
    p5 = sub.add_parser("unblock")
    p5.add_argument("job_ids", nargs="*", type=int, help="job ids; empty = all blocked")
    sub.add_parser("heal")
    p6 = sub.add_parser("show")
    p6.add_argument("job_id", type=int)
    p7 = sub.add_parser("events")
    p7.add_argument("--since", type=int, default=None)
    p7.add_argument("--limit", type=int, default=100)
    sub.add_parser("notify")
    a = ap.parse_args()
    if a.cmd == "submit":
        submit(a.batch_path)
    elif a.cmd == "status":
        status(a.batch, a.json)
    elif a.cmd == "run":
        run(a.batch, a.once, a.concurrency)
    elif a.cmd == "retry":
        retry(a.batch, a.blocked, a.job_ids or None)
    elif a.cmd == "unblock":
        unblock(a.job_ids or None)
    elif a.cmd == "heal":
        heal()
    elif a.cmd == "show":
        show(a.job_id)
    elif a.cmd == "events":
        events(a.since, a.limit)
    elif a.cmd == "notify":
        notify()

if __name__ == "__main__":
    main()
