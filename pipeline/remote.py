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
  3. ssh: extract to a per-job dir <workdir>/jobs/job_<id>/repo (cache/ symlinks
     to the shared <workdir>/cache) -- concurrent dispatches never interleave
  4. ssh: remote harness run <spec> --compute-only with QLAB_ROOT=<job repo>,
     QLAB_EXPECTED_HASH, QLAB_EXPECTED_COMMIT, QLAB_QLIB_DATA=<workdir>
     (data menu + executor + contract check + fixed tester on the remote; NO ledger)
  5. tar+scp <job repo>/results/runs/<exp_id>/ back to local results/runs/
     (on compute failure the same pull-back preserves the failure scene)
  6. local: harness import <run_dir> -> ledger row (sqlite single-writer stays local)

While QLAB_SPARK_SSH is blank, dispatch returns blocked=True (placeholder behavior).

Data sync paths (P8 6.4, validated 2026-08-23):
- feature caches: built ON the remote from qlib bins (QLAB_QLIB_DATA=<workdir>,
  must contain qlib_data/{cn_data,cn_data_zz500,cn_data_all}); repo/cache/ persists
  across dispatches (extract wipes everything else).
- price caches: built LOCALLY from qlib_data_src (canonical hfq source) and
  scp-staged to repo/cache before the remote run (trade specs only).
- executor requirements venvs: QLAB_VENV_DIR=<workdir>/executor_venvs, persistent;
  remote pip uses the tuna PyPI mirror (~/.config/pip/pip.conf).
"""
import hashlib, json, os, re, shlex, shutil, subprocess, sys, time
from pathlib import Path

from . import QLAB_ROOT

PACK_DIR = QLAB_ROOT / "results" / "remote_pack"

def _ssh_opts():
    """Common ssh options; optional key applies to both target and ProxyJump."""
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new"]
    key = os.environ.get("QLAB_SPARK_SSH_KEY", "").strip()
    if key:
        opts += ["-i", key]
    return opts

# Each job gets its OWN remote working dir (workdir/jobs/job_<id>/repo); the
# shared feature/price cache lives in workdir/cache and is symlinked in, so
# concurrent dispatches no longer tear each other's repo down (observed
# 2026-08-24 with one shared repo: "remote extract failed / Directory not
# empty" + "No module named pipeline"). Concurrent cache builds serialize on
# the per-file flock inside pipeline.data.


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
    cmd = ["ssh"] + _ssh_opts()
    if cfg["jump"]:
        # OpenSSH does not reliably propagate -i into the ProxyJump child.
        # Build an explicit ProxyCommand so the jump host uses the same key.
        key = os.environ.get("QLAB_SPARK_SSH_KEY", "").strip()
        auth = "-i " + shlex.quote(key) + " " if key else ""
        proxy = ("ssh " + auth + "-o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                 "-W %h:%p " + cfg["jump"])
        cmd += ["-o", "ProxyCommand=" + proxy]
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
    cmd = ["scp"] + _ssh_opts()
    if cfg["jump"]:
        cmd += ["-J", cfg["jump"]]
    cmd += ["-P", cfg["port"], src, dst]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def _pull_failure(cfg, exp_id, repo, workdir):
    """On remote compute failure, tar the run dir back BEFORE anything else
    touches the per-job dir (audit #3). Best effort: never masks the original
    failure."""
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        tar_remote = "%s/fail_%s_%s.tar.gz" % (workdir, exp_id, stamp)
        r = _ssh(cfg, "cd %s/results/runs && tar czf %s %s 2>/dev/null"
                      % (repo, tar_remote, exp_id), 300)
        if r.returncode != 0:
            return ""
        FAIL_DIR = QLAB_ROOT / "results" / "remote_fail"
        FAIL_DIR.mkdir(parents=True, exist_ok=True)
        local = FAIL_DIR / ("fail_%s_%s.tar.gz" % (exp_id, stamp))
        r2 = _scp(cfg, cfg["ssh"] + ":" + tar_remote, str(local))
        _ssh(cfg, "rm -f " + tar_remote, 60)
        if r2.returncode == 0:
            return " (failure run dir preserved at results/remote_fail/%s)" % local.name
        return ""
    except Exception:
        return ""


def dispatch(row):
    """Run one job's compute phase on the DGX Spark container, then import the
    results into the local ledger. Returns {"ok": True, "run_id": ...} on
    success; {"blocked": True, "reason": ...} while unconfigured; otherwise
    {"ok": False, "reason": ...}. Concurrent jobs are safe: per-job dirs +
    symlinked shared cache (audit #4)."""
    return _dispatch_locked(row)


def _dispatch_locked(row):
    cfg = spark_config()
    if not configured():
        return {"ok": False, "blocked": True,
                "reason": "spark runner not configured: QLAB_SPARK_SSH is blank"}
    exp_id = str(row.get("exp_id", ""))
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", exp_id):
        return {"ok": False, "blocked": False,
                "reason": "exp_id fails path-segment validation: %r" % exp_id}
    spec_path = str(row.get("spec_path", ""))
    spec_hash = str(row.get("spec_hash", ""))
    timeout_min = int(row.get("timeout_min") or 120)
    try:
        tarball, commit = pack()
        workdir = cfg["workdir"]
        jobdir = "%s/jobs/job_%s" % (workdir, str(row.get("job_id") or "0"))
        repo = jobdir + "/repo"
        r = _ssh(cfg, "mkdir -p %s/packs %s/cache %s/executor_venvs %s"
                      % (workdir, workdir, workdir, jobdir), 60)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote mkdir failed: %s" % r.stderr[-300:]}
        r = _scp(cfg, str(tarball), cfg["ssh"] + ":" + workdir + "/packs/")
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "scp pack failed: %s" % r.stderr[-300:]}
        # fresh per-job repo + symlinked shared cache (audit #4): no more
        # rm -rf tearing between concurrent dispatches
        r = _ssh(cfg, "mkdir -p %s && tar -xzf %s/packs/%s -C %s && rm -rf %s/cache && ln -s %s/cache %s/cache"
                      % (repo, workdir, tarball.name, repo, repo, workdir, repo), 300)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote extract failed: %s" % r.stderr[-300:]}
        # ---- pre-stage the price cache for trade specs (P8 T1): prices are
        # always built locally from qlib_data_src (canonical hfq source); the
        # remote only reads the staged parquet (no remote CSV source). ----
        try:
            from . import data as datamod, spec as specmod
            _sp = specmod.load_spec(str(QLAB_ROOT / spec_path))
            _eff = specmod.resolve(_sp)
            _fams = list(_sp.get("metrics") or []) or None
            if any(f in ("portfolio", "backtest", "attribution") for f in (_fams or [])):
                _cfg = datamod.resolve(_sp, _eff)
                _pq = datamod.price_ensure(_cfg)
                r = _scp(cfg, str(_pq), cfg["ssh"] + ":" + workdir + "/cache/")
                if r.returncode != 0:
                    return {"ok": False, "blocked": False,
                            "reason": "scp price cache failed: %s" % r.stderr[-300:]}
        except Exception as e:
            return {"ok": False, "blocked": False,
                    "reason": "price cache staging failed: %s" % e}
        # QLAB_QLIB_DATA = parent of qlib_data/ (pipeline.data appends /qlib_data/...)
        env = ("QLAB_ROOT=%s QLAB_EXPECTED_HASH=%s QLAB_EXPECTED_COMMIT=%s "
               "QLAB_QLIB_DATA=%s QLAB_VENV_DIR=%s/executor_venvs "
               "OMP_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8 "
               "QLAB_KERNELS=14"
               % (repo, spec_hash, commit, workdir, workdir))
        run_cmd = ("cd %s && timeout %d env %s %s -m pipeline.harness run %s --compute-only"
                   % (repo, timeout_min * 60, env, cfg["python"], spec_path))
        r = _ssh(cfg, run_cmd, timeout_min * 60 + 120)
        if r.returncode != 0:
            reason = "remote compute failed rc=%s: %s" % (
                r.returncode, (r.stdout + r.stderr)[-600:])
            reason += _pull_failure(cfg, exp_id, repo, workdir)
            reason += " (remote job dir kept: %s)" % jobdir
            return {"ok": False, "blocked": False, "reason": reason}
        # ---- pull results back: tar on remote + single-stream scp (no remote rsync) ----
        job_key = "%s_%s" % (str(row.get("job_id") or "0"), exp_id)
        tar_remote = workdir + "/results_%s.tar.gz" % job_key
        r = _ssh(cfg, "cd %s/results/runs && tar czf %s %s"
                      % (repo, tar_remote, exp_id), 300)
        if r.returncode != 0:
            return {"ok": False, "blocked": False,
                    "reason": "remote tar results failed: %s" % r.stderr[-300:]}
        local_tar = PACK_DIR / ("results_%s.tar.gz" % job_key)
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
        _ssh(cfg, "rm -rf " + jobdir, 60)  # success: per-job dir no longer needed
        return {"ok": True, "run_id": rid, "commit": commit[:8]}
    except Exception as e:
        return {"ok": False, "blocked": False, "reason": "spark dispatch error: %s" % e}


def main():
    print(json.dumps({"configured": configured(), "config": spark_config()}))


if __name__ == "__main__":
    main()
