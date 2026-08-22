"""Remote runner (DGX Spark docker) -- v1 code-complete, SSH config intentionally
blank (user-approved: leave it empty until the machine details arrive).

Config (env): QLAB_SPARK_SSH=user@host / QLAB_SPARK_WORKDIR (default
/root/quant-spark) / QLAB_SPARK_IMAGE (default qlab:latest). The remote image
must mount the qlib data bins at /root/.qlib (same layout as the local pool).

Flow (dispatch, called by queue._execute for runner="spark"):
  1. pack: git archive of HEAD -> results/remote_pack/<commit>.tar.gz
  2. scp tarball to <workdir>/packs/
  3. ssh: extract to <workdir>/repo; docker exec <image> with QLAB_ROOT=<workdir>/repo,
     QLAB_EXPECTED_HASH, QLAB_EXPECTED_COMMIT -> harness run <spec> --compute-only
     (executor contract + contract check + fixed tester on the remote; NO ledger)
  4. rsync <workdir>/repo/results/runs/<exp_id>/ back to the local results/runs/
  5. local: harness import <run_dir> -> ledger row (sqlite single-writer kept local)

While QLAB_SPARK_SSH is blank, dispatch returns blocked=True and the queue
records the job as blocked (placeholder behavior, not a failure).
"""
import hashlib, json, os, subprocess, sys
from pathlib import Path

from . import QLAB_ROOT

PACK_DIR = QLAB_ROOT / "results" / "remote_pack"


def spark_config():
    return {"ssh": os.environ.get("QLAB_SPARK_SSH", "").strip(),
            "workdir": os.environ.get("QLAB_SPARK_WORKDIR", "/root/quant-spark").strip(),
            "image": os.environ.get("QLAB_SPARK_IMAGE", "qlab:latest").strip()}


def configured():
    return bool(spark_config()["ssh"])


def pack():
    """git archive of HEAD -> tarball. Returns (tarball_path, full commit)."""
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "-C", str(QLAB_ROOT), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    commit = r.stdout.strip()
    if r.returncode != 0 or not commit:
        raise RuntimeError("git rev-parse failed: %s" % r.stderr[-200:])
    out = PACK_DIR / ("repo_%s.tar.gz" % commit[:12])
    if not out.exists():
        r = subprocess.run(["git", "-C", str(QLAB_ROOT), "archive",
                            "--format=tar.gz", "-o", str(out), "HEAD"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("git archive failed: %s" % r.stderr[-200:])
        with open(PACK_DIR / "manifest.sha256", "a") as f:
            f.write("%s  %s\n" % (hashlib.sha256(out.read_bytes()).hexdigest(),
                                  out.name))
    return out, commit


def _ssh(cfg, cmd, timeout):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                           cfg["ssh"], cmd],
                          capture_output=True, text=True, timeout=timeout)


def dispatch(row):
    """Run one job's compute phase on the DGX Spark docker container, then import
    the results into the local ledger. Returns {"ok": True, "run_id": ...} on
    success; {"blocked": True, "reason": ...} while unconfigured; otherwise
    {"ok": False, "reason": ...}."""
    cfg = spark_config()
    if not configured():
        return {"ok": False, "blocked": True,
                "reason": "spark runner not configured: QLAB_SPARK_SSH is blank (v1 placeholder)"}
    exp_id = str(row.get("exp_id", ""))
    spec_path = str(row.get("spec_path", ""))
    spec_hash = str(row.get("spec_hash", ""))
    timeout_min = int(row.get("timeout_min") or 120)
    try:
        tarball, commit = pack()
        workdir = cfg["workdir"]
        _ssh(cfg, "mkdir -p %s/packs %s/repo" % (workdir, workdir), 60)
        r = subprocess.run(["scp", "-o", "BatchMode=yes", str(tarball),
                            cfg["ssh"] + ":" + workdir + "/packs/"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "scp pack failed: %s" % r.stderr[-300:]}
        r = _ssh(cfg, "tar -xzf %s/packs/%s -C %s/repo"
                      % (workdir, tarball.name, workdir), 300)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote extract failed: %s" % r.stderr[-300:]}
        run_cmd = ("docker exec -e QLAB_ROOT=%s/repo -e QLAB_EXPECTED_HASH=%s "
                   "-e QLAB_EXPECTED_COMMIT=%s -e OMP_NUM_THREADS=8 "
                   "-e OPENBLAS_NUM_THREADS=8 -e MKL_NUM_THREADS=8 %s "
                   "python -m pipeline.harness run %s --compute-only"
                   % (workdir, spec_hash, commit, cfg["image"], spec_path))
        r = _ssh(cfg, "cd %s/repo && timeout %d %s"
                      % (workdir, timeout_min * 60, run_cmd),
                 timeout_min * 60 + 120)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote compute failed rc=%s: %s" % (
                        r.returncode, (r.stdout + r.stderr)[-600:])}
        local_run = QLAB_ROOT / "results" / "runs" / exp_id
        local_run.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["rsync", "-a", "--delete",
                            cfg["ssh"] + ":" + workdir + "/repo/results/runs/"
                            + exp_id + "/",
                            str(local_run) + "/"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "rsync back failed: %s" % r.stderr[-300:]}
        imp = subprocess.run([sys.executable, "-m", "pipeline.harness", "import",
                              str(local_run), "--spec-hash", spec_hash,
                              "--job-id", str(row.get("job_id") or ""),
                              "--batch-id", str(row.get("batch_id") or "")],
                             cwd=str(QLAB_ROOT), capture_output=True, text=True)
        rid = ""
        for line in (imp.stdout or "").splitlines():
            if line.startswith("QLAB_RESULT "):
                try:
                    rid = json.loads(line[len("QLAB_RESULT "):]).get("run_id", "")
                except ValueError:
                    pass
        if imp.returncode != 0 or not rid:
            return {"ok": False, "blocked": False,
                    "reason": "remote compute ok but local import failed: %s"
                              % ((imp.stdout or "") + (imp.stderr or ""))[-600:]}
        return {"ok": True, "run_id": rid, "commit": commit[:8]}
    except Exception as e:
        return {"ok": False, "blocked": False, "reason": "spark dispatch error: %s" % e}


def main():
    print(json.dumps({"configured": configured(), "config": spark_config()}))


if __name__ == "__main__":
    main()
