"""审计 #4/#3 回归：spark dispatch 每 job 独立目录 + 失败现场保回。"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from pipeline import remote


class _R:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _patch(monkeypatch, tmp_path, compute_rc=0):
    monkeypatch.setattr(remote, "QLAB_ROOT", tmp_path)
    cfg = {"ssh": "dev@h", "port": "2223", "jump": "j@j",
           "workdir": "/home/dev/quant", "python": "python"}
    monkeypatch.setattr(remote, "spark_config", lambda: cfg)
    monkeypatch.setattr(remote, "pack", lambda: (tmp_path / "pack.tar.gz", "abc123456789"))
    ssh_calls = []
    scp_calls = []

    def fake_ssh(cfg, cmd, timeout):
        ssh_calls.append(cmd)
        if "harness run" in cmd:
            return _R(rc=compute_rc, stdout="boom stdout", stderr="boom stderr")
        return _R(0)

    def fake_scp(cfg, src, dst):
        scp_calls.append((src, dst))
        return _R(0)

    monkeypatch.setattr(remote, "_ssh", fake_ssh)
    monkeypatch.setattr(remote, "_scp", fake_scp)

    def fake_run(cmd, **kw):
        if cmd[:2] == ["tar", "xzf"]:
            return _R(0)
        if "pipeline.harness" in cmd and "import" in cmd:
            return _R(0, stdout='QLAB_RESULT {"run_id": "r9"}\n')
        return _R(0)

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    spec_file = tmp_path / "experiments" / "specs" / "x.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps({"exp_id": "e_x", "base": {}}))
    return ssh_calls, scp_calls


def test_dispatch_per_job_dir_and_cleanup(monkeypatch, tmp_path):
    ssh_calls, scp_calls = _patch(monkeypatch, tmp_path)
    row = {"job_id": 7, "batch_id": "b1", "exp_id": "e_x",
           "spec_path": "experiments/specs/x.json", "spec_hash": "h1",
           "runner": "spark", "timeout_min": 5}
    out = remote.dispatch(row)
    assert out["ok"] is True and out["run_id"] == "r9"
    joined = "\n".join(ssh_calls)
    assert "jobs/job_7/repo" in joined                    # 每 job 独立目录
    assert "ln -s /home/dev/quant/cache" in joined        # 共享 cache 软链
    assert "QLAB_ROOT=/home/dev/quant/jobs/job_7/repo" in joined
    assert "rm -rf /home/dev/quant/jobs/job_7" in joined  # 成功即清理
    assert any("/results_7_e_x.tar.gz" in c[0] for c in scp_calls)


def test_dispatch_failure_pulls_scene_back(monkeypatch, tmp_path):
    ssh_calls, scp_calls = _patch(monkeypatch, tmp_path, compute_rc=1)
    row = {"job_id": 8, "batch_id": "b1", "exp_id": "e_y",
           "spec_path": "experiments/specs/x.json", "spec_hash": "h2",
           "runner": "spark", "timeout_min": 5}
    out = remote.dispatch(row)
    assert out["ok"] is False
    assert "remote compute failed rc=1" in out["reason"]
    assert "preserved at results/remote_fail/" in out["reason"]  # 现场保回
    assert "(remote job dir kept: /home/dev/quant/jobs/job_8)" in out["reason"]
    assert any("fail_e_y_" in c[0] for c in scp_calls)  # 失败现场 scp 回来


def test_ssh_key_applies_to_target_and_jump(monkeypatch):
    monkeypatch.setenv("QLAB_SPARK_SSH_KEY", "/tmp/test_key")
    monkeypatch.setenv("QLAB_SPARK_JUMP", "j@j")
    cfg = remote.spark_config()
    cmd = remote._ssh_cmd(cfg)
    assert "-J" in cmd
    assert cmd[cmd.index("-i") + 1] == "/tmp/test_key"
