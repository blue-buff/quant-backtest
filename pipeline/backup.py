"""Dual backup: local snapshot + git push. CLI: snap / push."""
import argparse, hashlib, json, shutil, sqlite3, subprocess, time, zipfile
from pathlib import Path
from . import QLAB_ROOT, JOBS_DB, REGISTRY_EXPORT, BOARD_CSV, registry
from . import board as boardmod

GIT_PATHS = ["pipeline", "experiments", "knowledge", "docs", "AGENTS.md",
             "scripts/mlflow_server.sh", ".gitignore",
             "scripts/eval_matrix.py", "scripts/remote_queue.py",
             "scripts/remote_run_allmarket.py", "scripts/train_weighted.py"]
# results/ is gitignored wholesale; these are force-added separately in push()
FORCE_PATHS = ["results/board.csv", "results/registry_export.json", "results/backup"]

def snap():
    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = QLAB_ROOT / "results" / "backup" / "snaps"
    outdir.mkdir(parents=True, exist_ok=True)
    stage = outdir / ("stage_" + ts)
    stage.mkdir()
    registry.export_json(str(REGISTRY_EXPORT))
    shutil.copy(str(REGISTRY_EXPORT), str(stage / "registry_export.json"))
    boardmod.export(str(stage / "board.csv"), to_console=False)
    if JOBS_DB.exists():
        conn = sqlite3.connect(str(JOBS_DB))
        with open(stage / "jobs_export.sql", "w") as f:
            for line in conn.iterdump():
                f.write(line + "\n")
        conn.close()
    for src in ("knowledge", "experiments"):
        s = QLAB_ROOT / src
        if s.exists():
            shutil.copytree(s, stage / src)
    zp = outdir / ("snap_" + ts + ".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage))
    shutil.rmtree(stage)
    h = hashlib.sha256(zp.read_bytes()).hexdigest()
    with open(outdir / "manifest.sha256", "a") as f:
        f.write("%s  %s\n" % (h, zp.name))
    print(json.dumps({"zip": str(zp), "sha256": h}))
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
    # results/ is gitignored wholesale; force-add the small ledger snapshots
    git("add", "-f", "--", *FORCE_PATHS)
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
    p2.add_argument("--message", default="backup: snapshot" )
    p2.add_argument("--token", default=None)
    a = ap.parse_args()
    if a.cmd == "snap":
        snap()
    elif a.cmd == "push":
        push(a.message, a.token)

if __name__ == "__main__":
    main()
