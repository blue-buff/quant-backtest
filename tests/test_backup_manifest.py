"""备份分支设计（2026-08-30）：snap 只写本地 results/backup/，不进 git（main 保持纯代码）；
数据快照由 push 打到 backup 分支（_push_backups）。这里测两个不变量：
1) snap 的 manifest 不再自提交（_commit_manifest 已删除，results/ 忽略后树保持干净）；
2) _push_backups 用独立临时 index 构建树：只含 results/ 快照产物，不污染 main 的 index/HEAD。"""
import re
import subprocess

from pipeline import backup


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init",
                    "--allow-empty"], check=True)


def test_snap_manifest_no_longer_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "QLAB_ROOT", tmp_path)
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("results/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "gitignore"],
                   check=True)
    p = tmp_path / "results" / "backup" / "snaps" / "manifest.sha256"
    p.parent.mkdir(parents=True)
    p.write_text("aaa  snap1.zip\n")

    # 自提交 helper 已删除：snap 不再产生任何 git commit
    assert not hasattr(backup, "_commit_manifest")
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "snap manifest" not in log
    # results/ 被忽略：追加行不 dirty 树
    st = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                        capture_output=True, text=True)
    assert st.stdout.strip() == ""


def test_push_backups_uses_isolated_index(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "QLAB_ROOT", tmp_path)
    _init_repo(tmp_path)
    head_before = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    snaps = tmp_path / "results" / "backup" / "snaps"
    snaps.mkdir(parents=True)
    (snaps / "snap_x.zip").write_bytes(b"zipdata")
    (snaps / "manifest.sha256").write_text("h  snap_x.zip\n")
    (tmp_path / "results" / "board.csv").write_text("exp,ic\n")
    (tmp_path / "results" / "registry_export.json").write_text("{}\n")

    calls = []
    def fake_git(*args):
        calls.append(args)
        return ""
    backup._push_backups("https://fake", fake_git)

    # 最终 push 调用：refspec = <commit sha>:refs/heads/backup + force
    assert len(calls) == 1
    _, refspec, flag = calls[0][5], calls[0][6], calls[0][7]
    assert calls[0][:5] == ("-c", "http.proxy=", "-c", "https.proxy=", "push")
    assert flag == "--force"
    sha, branch = refspec.split(":")
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert branch == "refs/heads/backup"
    # main 的 HEAD/index 未被触碰
    head_after = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    staged = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached",
                             "--name-only"], capture_output=True, text=True).stdout
    assert staged.strip() == ""
    # 构建出的 commit 树只含 results/ 快照产物
    tree_files = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-tree", "-r", "--name-only", sha],
        capture_output=True, text=True).stdout.splitlines()
    assert tree_files == ["results/backup/snaps/manifest.sha256",
                          "results/backup/snaps/snap_x.zip",
                          "results/board.csv",
                          "results/registry_export.json"]
