"""Dual backup: local snapshot (incl. online-consistent DB copies + run artifacts)
+ git push (only the newest snap zip is force-added, older stay on disk).
CLI: snap / push."""
import argparse, hashlib, json, shutil, sqlite3, subprocess, time, zipfile
from pathlib import Path
from . import (QLAB_ROOT, JOBS_DB, REGISTRY_EXPORT, BOARD_CSV, MLFLOW_DB,
               ARTIFACT_DIR, registry)
from . import board as boardmod

GIT_PATHS = ["pipeline", "experiments", "knowledge", "docs", "AGENTS.md",
             "notify_bridge.js", "scripts/mlflow_server.sh", "scripts/probe_node.ps1",
             ".gitignore",
             "scripts/eval_matrix.py", "scripts/remote_queue.py",
             "scripts/remote_run_allmarket.py", "scripts/train_weighted.py"]
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

def _sqlite_copy(src, dst):
    """Online-consistent copy (sqlite backup API: WAL-safe, no lock conflicts)."""
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()

def _copy_artifacts(src, dst):
    excluded = []
    dst.mkdir(parents=True)
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_file() and p.stat().st_size > MAX_ARTIFACT_BYTES:
            excluded.append(str(rel))
            continue
        t = dst / rel
        if p.is_dir():
            t.mkdir(parents=True, exist_ok=True)
        else:
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)
    return excluded

def snap():
    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = QLAB_ROOT / "results" / "backup" / "snaps"
    outdir.mkdir(parents=True, exist_ok=True)
    stage = outdir / ("stage_" + ts)
    stage.mkdir()
    registry.export_json(str(REGISTRY_EXPORT))
    shutil.copy(str(REGISTRY_EXPORT), str(stage / "registry_export.json"))
    boardmod.export(str(stage / "board.csv"), to_console=False)
    db_copies = {}
    for name, src in (("jobs.db", JOBS_DB), ("mlflow.db", MLFLOW_DB)):
        if src.exists():
            _sqlite_copy(src, stage / name)
            db_copies[name] = (stage / name).stat().st_size
    for src in ("knowledge", "experiments"):
        s = QLAB_ROOT / src
        if s.exists():
            shutil.copytree(s, stage / src)
    excluded = []
    if ARTIFACT_DIR.exists():
        excluded = _copy_artifacts(ARTIFACT_DIR, stage / "artifacts")
    manifest = {"ts": ts,
                "db_copies_bytes": db_copies,
                "excluded_artifacts": excluded,
                "note": "jobs.db/mlflow.db are online-consistent copies (sqlite backup API); "
                        "artifacts >%dMB excluded" % (MAX_ARTIFACT_BYTES // (1024 * 1024))}
    (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    zp = outdir / ("snap_" + ts + ".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage))
    shutil.rmtree(stage)
    h = hashlib.sha256(zp.read_bytes()).hexdigest()
    with open(outdir / "manifest.sha256", "a") as f:
        f.write("%s  %s\n" % (h, zp.name))
    print(json.dumps({"zip": str(zp), "sha256": h, "db_copies_bytes": db_copies,
                      "excluded_artifacts": excluded}))
    return str(zp), h

def push(message, token=None):
    token = token or __import__("os").environ.get("QLAB_GITHUB_TOKEN", "")
    def git(*args):
        r = subprocess.run(["git", "-C", str(QLAB_ROOT)] + list(args),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("git %s failed: %s%s" % (" ".join(args), r.stdout[-300:], r.stderr[-300:]))
        return r.stdout.strip()
    git("add", "--", *GIT_PATHS)
    # results/ is gitignored wholesale; force-add the small ledger snapshots.
    # Only the NEWEST snap zip is tracked per commit (older stay on disk, already in git).
    snaps = sorted((QLAB_ROOT / "results" / "backup" / "snaps").glob("snap_*.zip"))
    force = ["results/board.csv", "results/registry_export.json",
             "results/backup/snaps/manifest.sha256"]
    if snaps:
        force.append("results/backup/snaps/" + snaps[-1].name)
    git("add", "-f", "--", *force)
    try:
        git("commit", "-m", message)
    except RuntimeError as e:
        if "nothing to commit" not in str(e):
            raise
    if token:
        url = "https://oauth2:" + token + "@github.com/blue-buff/quant-backtest.git"
        git("-c", "http.proxy=", "-c", "https.proxy=", "push", url, "HEAD:main")
        print(json.dumps({"pushed": True}))
    else:
        print(json.dumps({"pushed": False, "note": "no QLAB_GITHUB_TOKEN, committed only"}))

def main():
    ap = argparse.ArgumentParser(prog="pipeline.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snap")
    p2 = sub.add_parser("push")
    p2.add_argument("--message", default="backup: snapshot")
    p2.add_argument("--token", default=None)
    a = ap.parse_args()
    if a.cmd == "snap":
        snap()
    elif a.cmd == "push":
        push(a.message, a.token)

if __name__ == "__main__":
    main()
