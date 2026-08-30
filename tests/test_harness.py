"""P8 T7: harness SIGTERM -> killpg 处理器（远端超时杀组语义对齐）。"""
import signal as _signal

import pytest

from pipeline import harness


def test_term_handler_kills_group_and_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("QLAB_SIG_PROBE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: calls.append((pgid, sig)))
    with pytest.raises(SystemExit) as ei:
        harness._term_handler(15, None)
    assert ei.value.code == 143
    assert calls == [(0, _signal.SIGTERM)]


def test_term_handler_oserror_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.setenv("QLAB_SIG_PROBE_DIR", str(tmp_path))
    def boom(pgid, sig):
        raise OSError("no such process")
    monkeypatch.setattr("os.killpg", boom)
    with pytest.raises(SystemExit):
        harness._term_handler(9, None)


def test_term_handler_writes_sig_probe(monkeypatch, tmp_path):
    """回归（审计 #9 探测）：收到信号时落盘发送者溯源探针。"""
    import json as _json
    monkeypatch.setenv("QLAB_SIG_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)
    with pytest.raises(SystemExit):
        harness._term_handler(15, None)
    probes = list(tmp_path.glob("sig_probe_*.json"))
    assert len(probes) == 1
    p = _json.loads(probes[0].read_text())
    assert p["signum"] == 15
    assert p["ppid"] > 0
    assert "pid" in p and "ppid_cmdline" in p


def test_term_handler_forked_child_never_killpg(monkeypatch, tmp_path):
    """回归（spark SIGTERM 自伤级联）：fork 出的取数 worker 继承本 handler，
    池关闭的例行 SIGTERM 绝不能让它 killpg(0) 把整个进程组（含主进程）带走。
    2026-08-24/25 远端 4+ 次复现，内核 trace 证明无外部发送者、纯自伤。"""
    import os as _os
    monkeypatch.setenv("QLAB_SIG_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(harness, "_TERM_MAIN_PID", 999999)  # 主进程是"别人"
    group_kills = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: group_kills.append((pgid, sig)))
    self_kills = []
    monkeypatch.setattr("os.kill", lambda pid, sig: self_kills.append((pid, sig)))
    monkeypatch.setattr("signal.signal", lambda s, h: None)
    with pytest.raises(SystemExit) as ei:
        harness._term_handler(15, None)
    assert ei.value.code == 143
    assert group_kills == []  # 绝不杀组
    assert (_os.getpid(), _signal.SIGTERM) in self_kills  # 只按默认方式杀自己
    assert not list(tmp_path.glob("sig_probe_*.json"))  # 子进程不写探针


def test_safe_run_dir_rejects_escape(tmp_path, monkeypatch):
    """回归（审计 #4）：exp_id 越出 RUNS_DIR 时，rmtree 前最后一道防线必须拦住。"""
    monkeypatch.setattr(harness, "RUNS_DIR", tmp_path / "runs")
    ok = harness._safe_run_dir("ok_1")
    assert ok == (tmp_path / "runs" / "ok_1").resolve()
    with pytest.raises(ValueError, match="escapes"):
        harness._safe_run_dir("..")
    with pytest.raises(ValueError, match="escapes"):
        harness._safe_run_dir("../../tmp/evil")
