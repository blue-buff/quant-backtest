"""Job queue: sqlite task table + dispatcher. CLI: submit / status / run / retry."""
import argparse, json, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from . import JOBS_DB, QUEUE_LOGS, QLAB_ROOT
from . import spec as specmod

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

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOBS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.commit()
    return conn

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
        conn.execute(
            "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,timeout_min,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (bid, exp_id, sp, h, runner, "queued", timeout, _now()))
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
        conn.commit()
        return
    conn.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE job_id=?",
                 (_now(), jid))
    conn.commit()
    logfile = QUEUE_LOGS / ("job_%s.log" % jid)
    cmd = [sys.executable, "-m", "pipeline.harness", "run", str(QLAB_ROOT / spath),
           "--job-id", str(jid), "--batch-id", str(row["batch_id"])]
    try:
        proc = subprocess.run(cmd, cwd=str(QLAB_ROOT), capture_output=True, text=True,
                              timeout=int(row["timeout_min"] or 60) * 60)
        with open(logfile, "w") as f:
            f.write(proc.stdout or "")
            f.write("\n=== STDERR ===\n")
            f.write(proc.stderr or "")
        result = None
        for line in (proc.stdout or "").splitlines():
            if line.startswith("QLAB_RESULT "):
                result = json.loads(line[len("QLAB_RESULT "):])
        if proc.returncode == 0 and result and result.get("run_id"):
            conn.execute("UPDATE jobs SET status='done', finished_at=?, mlflow_run_id=?, error=NULL WHERE job_id=?",
                         (_now(), result["run_id"], jid))
        else:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                         (_now(), ((proc.stderr or "")[-400:] or "no QLAB_RESULT in stdout"), jid))
    except subprocess.TimeoutExpired:
        conn.execute("UPDATE jobs SET status='failed', finished_at=?, error='timeout' WHERE job_id=?",
                     (_now(), jid))
    except Exception as e:
        conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                     (_now(), repr(e)[:400], jid))
    conn.commit()

def run(batch_id=None, once=True, concurrency=1):
    conn = db()
    while True:
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
    conn.commit()
    print(json.dumps({"requeued": cur.rowcount}))

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
    a = ap.parse_args()
    if a.cmd == "submit":
        submit(a.batch_path)
    elif a.cmd == "status":
        status(a.batch, a.json)
    elif a.cmd == "run":
        run(a.batch, a.once, a.concurrency)
    elif a.cmd == "retry":
        retry(a.batch)

if __name__ == "__main__":
    main()
