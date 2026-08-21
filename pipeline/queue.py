"""Job queue: sqlite task table + dispatcher + events/heartbeat/heal.
CLI: submit / status / run / retry / heal / show / events / notify."""
import argparse, json, os, signal, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from . import JOBS_DB, QUEUE_LOGS, QLAB_ROOT
from . import spec as specmod

EVENTS_LOG = QLAB_ROOT / "results" / "queue" / "events.log"
HEARTBEAT_FILE = QLAB_ROOT / "results" / "queue" / "heartbeat"
MARKER_FILE = QLAB_ROOT / "results" / "queue" / "notify_marker"
STALE_MINUTES = 5

SCHEMA = """CREATE TABLE IF NOT EXISTS jobs(
 job_id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id TEXT NOT NULL,
 exp_id TEXT NOT NULL,
 spec_path TEXT NOT NULL,
 spec_hash TEXT NOT NULL,
 runner TEXT DEFAULT 'local',
 status TEXT DEFAULT 'queued',
 attempts INTEGER DEFAULT 0,
 timeout_min INTEGER DEFAULT 60,
 created_at TEXT, started_at TEXT, finished_at TEXT,
 mlflow_run_id TEXT, error TEXT, note TEXT)"""

EVENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER, batch_id TEXT, exp_id TEXT, status TEXT, error TEXT, ts TEXT)"""

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOBS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.execute(EVENTS_SCHEMA)
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
    """Dispatcher heartbeat: touch heartbeat file with current timestamp."""
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(_now())

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
        timeout = int(spec.get("timeout_min", batch.get("timeout_min", 60)))
        row = conn.execute(
            "SELECT job_id,status FROM jobs WHERE exp_id=? AND spec_hash=? ORDER BY job_id DESC LIMIT 1",
            (exp_id, h)).fetchone()
        if row and row["status"] == "done":
            skip_done += 1
            continue
        if row and row["status"] in ("queued", "running", "blocked"):
            skip_dup += 1
            continue
        cur = conn.execute(
            "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,timeout_min,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (bid, exp_id, sp, h, runner, "queued", timeout, _now()))
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

def _execute(conn, row):
    jid, spath = row["job_id"], row["spec_path"]
    if row["runner"] != "local":
        conn.execute("UPDATE jobs SET status='blocked', note=? WHERE job_id=?",
                     ("remote runner not enabled (lab rules: remote machine untouched)", jid))
        _event(conn, jid, row["batch_id"], row["exp_id"], "blocked",
               "remote runner not enabled")
        conn.commit()
        return
    conn.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE job_id=?",
                 (_now(), jid))
    _event(conn, jid, row["batch_id"], row["exp_id"], "running")
    conn.commit()
    logfile = QUEUE_LOGS / ("job_%s.log" % jid)
    cmd = [sys.executable, "-m", "pipeline.harness", "run", str(QLAB_ROOT / spath),
           "--job-id", str(jid), "--batch-id", str(row["batch_id"])]
    env_timeout = int(os.environ.get("QLAB_QUEUE_TIMEOUT_SECONDS", 0) or 0)
    timeout = env_timeout or int(row["timeout_min"] or 60) * 60
    proc = subprocess.Popen(cmd, cwd=str(QLAB_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        with open(logfile, "w") as f:
            f.write(out or "")
            f.write("\n=== STDERR ===\n")
            f.write(err or "")
        result = None
        for line in (out or "").splitlines():
            if line.startswith("QLAB_RESULT "):
                result = json.loads(line[len("QLAB_RESULT "):])
        if proc.returncode == 0 and result and result.get("run_id"):
            conn.execute("UPDATE jobs SET status='done', finished_at=?, mlflow_run_id=?, error=NULL WHERE job_id=?",
                         (_now(), result["run_id"], jid))
            _event(conn, jid, row["batch_id"], row["exp_id"], "done")
        else:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                         (_now(), ((err or "")[-400:] or "no QLAB_RESULT in stdout"), jid))
            _event(conn, jid, row["batch_id"], row["exp_id"], "failed",
                   ((err or "")[-400:] or "no QLAB_RESULT in stdout"))
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
        conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                     (_now(), "timeout (killed process group)", jid))
        _event(conn, jid, row["batch_id"], row["exp_id"], "failed",
               "timeout (killed process group)")
    except Exception as e:
        conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                     (_now(), repr(e)[:400], jid))
        _event(conn, jid, row["batch_id"], row["exp_id"], "failed", repr(e)[:400])
    conn.commit()

def run(batch_id=None, once=True, concurrency=1):
    conn = db()
    _beat()
    while True:
        _beat()
        q = "SELECT * FROM jobs WHERE status='queued'"
        args = []
        if batch_id:
            q += " AND batch_id=?"
            args.append(batch_id)
        q += " ORDER BY job_id LIMIT ?"
        args.append(concurrency)
        rows = conn.execute(q, args).fetchall()
        if not rows:
            break
        for row in rows:
            _execute(conn, row)
        if not once:
            time.sleep(10)
    conn.close()
    print(json.dumps({"drained": True, "batch_id": batch_id}))

def retry(batch_id=None, only_failed=True):
    conn = db()
    q = "UPDATE jobs SET status='queued' WHERE status='failed' AND attempts<3"
    args = []
    if batch_id:
        q += " AND batch_id=?"
        args.append(batch_id)
    cur = conn.execute(q, args)
    for r in conn.execute("SELECT * FROM jobs WHERE status='queued' AND attempts>0").fetchall():
        _event(conn, r["job_id"], r["batch_id"], r["exp_id"], "queued", "requeued by retry")
    conn.commit()
    print(json.dumps({"requeued": cur.rowcount}))

def heal(stale_minutes=STALE_MINUTES):
    """Auto-heal: running jobs whose dispatcher heartbeat went stale -> failed."""
    conn = db()
    hb_age = None
    if HEARTBEAT_FILE.exists():
        try:
            hb_ts = datetime.strptime(HEARTBEAT_FILE.read_text().strip(), "%Y-%m-%d %H:%M:%S")
            hb_age = (datetime.now() - hb_ts).total_seconds()
        except ValueError:
            hb_age = None
    rows = conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
    dead = hb_age is None or hb_age > stale_minutes * 60
    healed = []
    for r in rows:
        if dead:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                         (_now(), "dispatcher died (heartbeat stale), auto-healed", r["job_id"]))
            _event(conn, r["job_id"], r["batch_id"], r["exp_id"], "failed",
                   "auto-healed: dispatcher heartbeat stale")
            healed.append(r["job_id"])
    conn.commit()
    print(json.dumps({"stale_running": len(rows),
                      "heartbeat_age_sec": (round(hb_age, 1) if hb_age is not None else None),
                      "auto_healed": healed}))

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
    A host-side bridge calls this periodically and forwards output to DSH/human."""
    conn = db()
    last = 0
    if MARKER_FILE.exists():
        try:
            last = int(MARKER_FILE.read_text().strip() or 0)
        except ValueError:
            last = 0
    rows = [dict(x) for x in conn.execute(
        "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 500", (last,)).fetchall()]
    MARKER_FILE.write_text(str(rows[-1]["id"] if rows else last))
    if rows:
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
    p3.add_argument("--concurrency", type=int, default=1)
    p4 = sub.add_parser("retry")
    p4.add_argument("--batch")
    sub.add_parser("heal")
    p5 = sub.add_parser("show")
    p5.add_argument("job_id", type=int)
    p6 = sub.add_parser("events")
    p6.add_argument("--since", type=int, default=None)
    p6.add_argument("--limit", type=int, default=100)
    sub.add_parser("notify")
    a = ap.parse_args()
    if a.cmd == "submit":
        submit(a.batch_path)
    elif a.cmd == "status":
        status(a.batch, a.json)
    elif a.cmd == "run":
        run(a.batch, a.once, a.concurrency)
    elif a.cmd == "retry":
        retry(a.batch)
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
