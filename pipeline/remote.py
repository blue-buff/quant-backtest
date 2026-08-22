"""Remote runner (DGX Spark container) -- P6 v2 (measured link).

Measured 2026-08-22: ssh -J song@10.110.12.99 -p 2223 dev@10.0.0.5 lands
DIRECTLY inside the compute container (hostname is a docker id), so there is no
docker exec step. Upload ~2.0 MB/s, download ~6.1 MB/s.

Config (env):
  QLAB_SPARK_SSH      dev@10.0.0.5        (blank = not configured)
  QLAB_SPARK_SSH_PORT 2223                (default 22)
  QLAB_SPARK_JUMP     song@10.110.12.99   (ProxyJump; blank = direct)
  QLAB_SPARK_WORKDIR  /home/dev/quant     (remote workdir, must be writable)
  QLAB_SPARK_PYTHON   /home/dev/quant-venv/bin/python   (remote interpreter)

Flow (dispatch, called by queue._execute for runner="spark"):
  1. pack: git archive of HEAD -> results/remote_pack/<commit>.tar.gz
  2. scp tarball to <workdir>/packs/
  3. ssh: extract to <workdir>/repo
  4. ssh: remote harness run <spec> --compute-only with QLAB_ROOT=<workdir>/repo,
     QLAB_EXPECTED_HASH, QLAB_EXPECTED_COMMIT, QLAB_QLIB_DATA=<workdir>/qlib-data
     (data menu + executor + contract check + fixed tester on the remote; NO ledger)
  5. rsync <workdir>/repo/results/runs/<exp_id>/ back to local results/runs/
  6. local: harness import <run_dir> -> ledger row (sqlite single-writer stays local)

While QLAB_SPARK_SSH is blank, dispatch returns blocked=True (placeholder behavior).
"""
import hashlib, json, os, shutil, subprocess, sys
from pathlib import Path

from . import QLAB_ROOT

PACK_DIR = QLAB_ROOT / "results" / "remote_pack"

_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             "-o", "StrictHostKeyChecking=accept-new"]


def spark_config():
    return {
        "ssh": os.environ.get("QLAB_SPARK_SSH", "").strip(),
        "port": os.environ.get("QLAB_SPARK_SSH_PORT", "22").strip(),
        "jump": os.environ.get("QLAB_SPARK_JUMP", "").strip(),
        "workdir": os.environ.get("QLAB_SPARK_WORKDIR", "/home/dev/quant").strip(),
        "python": os.environ.get("QLAB_SPARK_PYTHON", "python3").strip(),
    }


def configured():
    return bool(spark_config()["ssh"])


def _ssh_cmd(cfg):
    cmd = ["ssh"] + _SSH_OPTS
    if cfg["jump"]:
        cmd += ["-J", cfg["jump"]]
    cmd += ["-p", cfg["port"], cfg["ssh"]]
    return cmd


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
    return subprocess.run(_ssh_cmd(cfg) + [cmd],
                          capture_output=True, text=True, timeout=timeout)


def _scp(cfg, src, dst):
    cmd = ["scp"] + _SSH_OPTS
    if cfg["jump"]:
        cmd += ["-J", cfg["jump"]]
    cmd += ["-P", cfg["port"], src, dst]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def dispatch(row):
    """Run one job's compute phase on the DGX Spark container, then import the
    results into the local ledger. Returns {"ok": True, "run_id": ...} on
    success; {"blocked": True, "reason": ...} while unconfigured; otherwise
    {"ok": False, "reason": ...}."""
    cfg = spark_config()
    if not configured():
        return {"ok": False, "blocked": True,
                "reason": "spark runner not configured: QLAB_SPARK_SSH is blank"}
    exp_id = str(row.get("exp_id", ""))
    spec_path = str(row.get("spec_path", ""))
    spec_hash = str(row.get("spec_hash", ""))
    timeout_min = int(row.get("timeout_min") or 120)
    try:
        tarball, commit = pack()
        workdir = cfg["workdir"]
        r = _ssh(cfg, "mkdir -p %s/packs %s/repo/cache %s/repo/results"
                      % (workdir, workdir, workdir), 60)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote mkdir failed: %s" % r.stderr[-300:]}
        r = _scp(cfg, str(tarball), cfg["ssh"] + ":" + workdir + "/packs/")
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "scp pack failed: %s" % r.stderr[-300:]}
        r = _ssh(cfg, "find %s/repo -mindepth 1 -maxdepth 1 ! -name cache -exec rm -rf {} + && tar -xzf %s/packs/%s -C %s/repo"
                      % (workdir, workdir, tarball.name, workdir), 300)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote extract failed: %s" % r.stderr[-300:]}
        # QLAB_QLIB_DATA = parent of qlib_data/ (pipeline.data appends /qlib_data/...)
        env = ("QLAB_ROOT=%s/repo QLAB_EXPECTED_HASH=%s QLAB_EXPECTED_COMMIT=%s "
               "QLAB_QLIB_DATA=%s OMP_NUM_THREADS=8 "
               "OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8"
               % (workdir, spec_hash, commit, workdir))
        run_cmd = ("cd %s/repo && timeout %d env %s %s -m pipeline.harness run %s --compute-only"
                   % (workdir, timeout_min * 60, env, cfg["python"], spec_path))
        r = _ssh(cfg, run_cmd, timeout_min * 60 + 120)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote compute failed rc=%s: %s" % (
                        r.returncode, (r.stdout + r.stderr)[-600:])}
        # ---- pull results back: tar on remote + single-stream scp (no remote rsync) ----
        tar_remote = workdir + "/results_%s.tar.gz" % exp_id
        r = _ssh(cfg, "cd %s/repo/results/runs && tar czf %s %s"
                      % (workdir, tar_remote, exp_id), 300)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote tar results failed: %s" % r.stderr[-300:]}
        local_tar = PACK_DIR / ("results_%s.tar.gz" % exp_id)
        r = _scp(cfg, cfg["ssh"] + ":" + tar_remote, str(local_tar))
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "scp results back failed: %s" % r.stderr[-300:]}
        _ssh(cfg, "rm -f " + tar_remote, 60)
        local_run = QLAB_ROOT / "results" / "runs" / exp_id
        if local_run.exists():
            shutil.rmtree(local_run)
        local_run.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["tar", "xzf", str(local_tar), "-C", str(local_run.parent)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "local extract results failed: %s" % r.stderr[-300:]}
        local_tar.unlink(missing_ok=True)
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
