"""P8: 执行器 venv 的 requirements 印章（改动后重装，防止旧依赖继续跑）。"""
from pipeline import executor as ex


def test_venv_requirements_stamp(tmp_path, monkeypatch):
    exdir = tmp_path / "ex"
    exdir.mkdir()
    req = exdir / "requirements.txt"
    req.write_text("six\n")
    venv_root = tmp_path / "venvs"
    monkeypatch.setattr(ex, "VENV_DIR", venv_root)
    venv = venv_root / "name"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    calls = []

    class FakeR:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return FakeR()
    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    py = ex._venv_python("name", exdir)
    assert py == str(venv / "bin" / "python")
    assert len(calls) == 1  # 首次 pip install
    assert (venv / ".requirements.sha256").exists()
    # 同样 requirements：不重装
    ex._venv_python("name", exdir)
    assert len(calls) == 1
    # requirements 变了：必须重装
    req.write_text("six\nidna\n")
    ex._venv_python("name", exdir)
    assert len(calls) == 2
    # 再跑一次又不装
    ex._venv_python("name", exdir)
    assert len(calls) == 2


def test_run_executor_streams_log_on_crash(tmp_path, monkeypatch):
    """回归（审计 #3）：执行器崩在中途，executor.log 已含部分输出（现场不丢）。"""
    exdir = tmp_path / "ex"
    exdir.mkdir()
    (exdir / "main.py").write_text(
        "import sys\n"
        "print('line-one', flush=True)\n"
        "print('line-two', flush=True)\n"
        "sys.exit(3)\n")
    monkeypatch.setattr(ex, "EXECUTORS_DIR", tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    rc, out, err, _secs = ex.run_executor(
        "ex", str(tmp_path / "cfg.json"), str(tmp_path / "t.pq"),
        str(tmp_path / "s.pq"), out_dir)
    assert rc == 3
    log = (out_dir / "executor.log").read_text()
    assert "line-one" in log and "line-two" in log
    assert "line-one" in out


def test_venv_concurrent_cold_build(tmp_path, monkeypatch):
    """回归（审计 #2）：两个线程同时冷建同一 venv：pip 只跑一次，双方拿到同一
    可用 python，印章正确。"""
    import hashlib
    import threading
    import time as _time

    exdir = tmp_path / "ex"
    exdir.mkdir()
    req = exdir / "requirements.txt"
    req.write_text("six\n")
    monkeypatch.setattr(ex, "VENV_DIR", tmp_path / "venvs")
    venv = ex.VENV_DIR / "name"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    calls = []

    class FakeR:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        _time.sleep(0.2)  # 放大竞态窗口
        return FakeR()

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    out = {}
    errs = []

    def worker(i):
        try:
            out[i] = ex._venv_python("name", exdir)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []
    assert len(calls) == 1  # 只有赢家跑一次 pip
    assert set(out.values()) == {str(venv / "bin" / "python")}
    assert (venv / ".requirements.sha256").read_text().strip() == \
        hashlib.sha256(b"six\n").hexdigest()
