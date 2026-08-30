"""Dual backup: local snapshot (incl. online-consistent DB copies + run artifacts)
+ git push split by branch:
- main branch   = 只推干净代码（GIT_PATHS，不含任何 results/ 产物）
- backup 分支   = 数据快照（snaps/*.zip + manifest + board.csv + registry_export.json），
  每次 push 打一个全量 commit 并 force-push 到 refs/heads/backup（main 保持干净）。
CLI: snap / push."""
import argparse, hashlib, json, shutil, sqlite3, subprocess, time, zipfile
from pathlib import Path
from . import (QLAB_ROOT, JOBS_DB, REGISTRY_EXPORT, BOARD_CSV, MLFLOW_DB,
               ARTIFACT_DIR, registry)
from . import board as boardmod

GIT_PATHS = ["pipeline", "executors", "experiments", "knowledge", "docs",
             "tests", "data", "AGENTS.md", "notify_bridge.js",
             "scripts/mlflow_server.sh", "scripts/probe_node.ps1", ".gitignore"]
TOKEN_FILE = QLAB_ROOT / ".qlab_github_token"


def _token(env):
    """GitHub token: QLAB_GITHUB_TOKEN env first, then /root/.qlab_github_token
    file (P8 T0: unattended push without exporting the token everywhere)."""
    t = (env or "").strip()
    if not t:
        try:
            t = TOKEN_FILE.read_text().strip()
        except OSError:
            t = ""
    return t
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
    # manifest.sha256 不再进 git（results/ 整体 gitignored）：main 保持纯代码，
    # 数据快照只在本机 + backup 分支（push 时由 _push_backups 打 commit）。
    print(json.dumps({"zip": str(zp), "sha256": h, "db_copies_bytes": db_copies,
                      "excluded_artifacts": excluded}))
    return str(zp), h


def push(message, token=None):
    token = _token(token if token is not None
                   else __import__("os").environ.get("QLAB_GITHUB_TOKEN", ""))

    def _redact(s):
        # never leak the token into error strings / events
        return s.replace(token, "<REDACTED>") if token else s

    def git(*args):
        r = subprocess.run(["git", "-C", str(QLAB_ROOT)] + list(args),
                           capture_output=True, text=True)
        if r.returncode != 0:
            # redact the WHOLE message: args include the token URL (oauth2:gho_***@),
            # and queue.py persists str(e) into events/events.log verbatim.
            raise RuntimeError(_redact("git %s failed: %s%s" % (
                " ".join(args), r.stdout[-300:], r.stderr[-300:])))
        return r.stdout.strip()
    git("add", "--", *GIT_PATHS)
    try:
        git("commit", "-m", message)
    except RuntimeError as e:
        if "nothing to commit" not in str(e):
            raise
    if not token:
        print(json.dumps({"pushed": False, "note": "no QLAB_GITHUB_TOKEN, committed only"}))
        return
    url = "https://oauth2:" + token + "@github.com/blue-buff/quant-backtest.git"
    # 1) main = 纯代码（results/ 产物已全部 untrack，不进 main）
    git("-c", "http.proxy=", "-c", "https.proxy=", "push", url, "HEAD:main")
    # 2) backup 分支 = 数据快照（全量单 commit，force-push；main 不受影响）
    _push_backups(url, git)
    print(json.dumps({"pushed": True}))


def _push_backups(url, git):
    """把当前数据快照打成单个 commit，force-push 到 refs/heads/backup。

    用独立临时 index（GIT_INDEX_FILE）暂存 results/ 快照产物，绝不碰 main 的
    index/工作树；commit 无父节点（每次 push 都是"当前全量快照"），force-push
    覆盖 backup 分支。无快照时跳过（不报错）。"""
    import os, tempfile
    snaps = QLAB_ROOT / "results" / "backup" / "snaps"
    if not snaps.exists():
        return
    # 唯一路径但必须不存在：git 会自行创建 index（空文件会被拒：index file smaller than expected）
    fd, tmp_index = tempfile.mkstemp(prefix="qlab_backup_idx_")
    os.close(fd)
    os.unlink(tmp_index)
    env = dict(os.environ)
    env.update({"GIT_DIR": str(QLAB_ROOT / ".git"),
                "GIT_WORK_TREE": str(QLAB_ROOT),
                "GIT_INDEX_FILE": tmp_index})
    try:
        def g(*a):
            r = subprocess.run(["git"] + list(a), capture_output=True,
                               text=True, env=env, cwd=str(QLAB_ROOT))
            if r.returncode != 0:
                raise RuntimeError("backup-tree git %s failed: %s%s" % (
                    " ".join(a), r.stdout[-300:], r.stderr[-300:]))
            return r.stdout.strip()
        g("add", "-f", "--", "results/board.csv", "results/registry_export.json",
          "results/backup")
        tree = g("write-tree")
        commit = g("commit-tree", tree, "-m", "backup: snapshot (data, not on main)")
        git("-c", "http.proxy=", "-c", "https.proxy=", "push", url,
            commit + ":refs/heads/backup", "--force")
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass

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
