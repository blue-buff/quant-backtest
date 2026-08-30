"""P7 T4: 两阶段 notify / notify-done + done.log 追加 + auto backup 事件。"""
import sqlite3
import time

import pytest

from pipeline import queue


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    """Point queue globals at a temp dir: never touches the real queue."""
    monkeypatch.setattr(queue, "JOBS_DB", tmp_path / "jobs.db")
    monkeypatch.setattr(queue, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(queue, "RUNID_DIR", tmp_path / "runids")
    monkeypatch.setattr(queue, "MARKER_FILE", tmp_path / "marker")
    monkeypatch.setattr(queue, "HEARTBEAT_FILE", tmp_path / "hb")
    monkeypatch.setattr(queue, "QUEUE_LOGS", tmp_path / "logs")
    monkeypatch.setattr(queue, "PUSH_BACKOFF_FILE", tmp_path / ".last_push_attempt")
    monkeypatch.setattr(queue, "PUSH_BACKOFF_SECS", 3600)
    conn = sqlite3.connect(str(tmp_path / "jobs.db"))
    conn.execute(queue.SCHEMA)
    conn.execute(queue.EVENTS_SCHEMA)
    conn.commit()
    conn.close()
    return tmp_path


def _conn(qenv):
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _events(qenv, where="1=1"):
    conn = _conn(qenv)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE " + where).fetchall()]
    conn.close()
    return rows


def _insert_events(qenv, n):
    conn = _conn(qenv)
    for i in range(n):
        conn.execute(
            "INSERT INTO events(job_id,batch_id,exp_id,status,error,ts) VALUES(?,?,?,?,?,?)",
            (i + 1, "b1", "e%d" % (i + 1), "failed" if i % 2 == 0 else "done",
             "err%d" % i, "2026-08-23"))
    conn.commit()
    conn.close()


def test_notify_two_phase(qenv):
    _insert_events(qenv, 3)
    marker = qenv / "marker"
    rows = queue.notify(peek=True, marker_file=marker)
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert not marker.exists()          # peek 不推进 marker
    rows2 = queue.notify(peek=True, marker_file=marker)
    assert len(rows2) == 3              # 未 ack 前反复可见
    queue.notify(ack=3, marker_file=marker)
    assert marker.read_text().strip() == "3"
    assert queue.notify(peek=True, marker_file=marker) == []
    # ack 只前进不回退
    queue.notify(ack=2, marker_file=marker)
    assert marker.read_text().strip() == "3"
    assert queue.notify(peek=True, marker_file=marker) == []


def test_append_done_log_ids_and_two_phase(qenv):
    dl = qenv / "done.log"
    m = qenv / "done_marker"
    assert queue.append_done_log({"exp_id": "a", "run_id": "r1", "batch_id": "b1",
                                  "job_id": 1, "rankic": 0.05, "p": 0.01,
                                  "expectation_check": "met"}, done_log=dl) == 1
    assert queue.append_done_log({"exp_id": "b", "run_id": "r2", "batch_id": "b1",
                                  "job_id": 2, "rankic": None, "p": None,
                                  "expectation_check": "n/a"}, done_log=dl) == 2
    rows = queue.notify_done(peek=True, done_log=dl, done_marker=m)
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[0]["exp_id"] == "a" and rows[0]["rankic"] == 0.05
    assert not m.exists()
    queue.notify_done(ack=2, done_log=dl, done_marker=m)
    assert queue.notify_done(peek=True, done_log=dl, done_marker=m) == []
    # 追加新行后只出新的
    queue.append_done_log({"exp_id": "c", "run_id": "r3", "batch_id": "b1",
                           "job_id": 3, "rankic": 0.1, "p": 0.2,
                           "expectation_check": "met"}, done_log=dl)
    rows2 = queue.notify_done(peek=True, done_log=dl, done_marker=m)
    assert [r["id"] for r in rows2] == [3]
    # ack 只前进不回退：ack=1 不会把 marker 从 2 拉回 1
    queue.notify_done(ack=1, done_log=dl, done_marker=m)
    assert [r["id"] for r in queue.notify_done(peek=True, done_log=dl, done_marker=m)] == [3]
    queue.notify_done(ack=3, done_log=dl, done_marker=m)
    assert queue.notify_done(peek=True, done_log=dl, done_marker=m) == []


def test_notify_done_skips_corrupt_lines(qenv):
    dl = qenv / "done.log"
    dl.write_text("garbage line\nnot json at all\n")
    queue.append_done_log({"exp_id": "x", "run_id": "r", "batch_id": "b",
                           "job_id": 1, "rankic": 0, "p": 1,
                           "expectation_check": "met"}, done_log=dl)
    rows = queue.notify_done(peek=True, done_log=dl, done_marker=qenv / "m")
    assert len(rows) == 1 and rows[0]["exp_id"] == "x"


def test_auto_backup_pending_event(qenv, monkeypatch):
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    from pipeline import backup as backupmod
    monkeypatch.setattr(backupmod, "snap", lambda: None)
    monkeypatch.setattr(backupmod, "_token", lambda env: "")
    queue._auto_backup()
    rows = _events(qenv, "status='backup_pending'")
    assert len(rows) == 1
    assert "QLAB_GITHUB_TOKEN" in rows[0]["error"]


def test_auto_backup_with_token_pushes(qenv, monkeypatch):
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    monkeypatch.setattr(queue, "PUSH_BACKOFF_FILE", qenv / "backoff")
    from pipeline import backup as backupmod
    pushed = []
    monkeypatch.setattr(backupmod, "snap", lambda: None)
    monkeypatch.setattr(backupmod, "_token", lambda env: "t")
    monkeypatch.setattr(backupmod, "push", lambda *a, **k: pushed.append((a, k)))
    queue._auto_backup()
    assert pushed == [(("auto backup after queue drain",), {"token": "t"})]
    assert _events(qenv) == []


def test_auto_backup_disabled(qenv, monkeypatch):
    monkeypatch.setattr(queue, "PUSH_BACKOFF_FILE", qenv / "backoff")
    monkeypatch.setenv("QLAB_AUTO_BACKUP", "0")
    from pipeline import backup as backupmod
    calls = []
    monkeypatch.setattr(backupmod, "snap", lambda: calls.append(1))
    queue._auto_backup()
    assert calls == []
    assert _events(qenv) == []


def test_auto_backup_push_backoff(qenv, monkeypatch):
    """push 失败后 1 小时内不再尝试也不再记事件（网络不通时防通知轰炸）。"""
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    from pipeline import backup as backupmod
    pushes = []
    monkeypatch.setattr(backupmod, "snap", lambda: None)
    monkeypatch.setattr(backupmod, "_token", lambda env: "t")

    def boom(*a, **k):
        pushes.append(a)
        raise RuntimeError("network blocked")
    monkeypatch.setattr(backupmod, "push", boom)
    monkeypatch.setattr(queue, "PUSH_BACKOFF_FILE", qenv / "backoff")
    queue._auto_backup()  # 第一次：尝试并记事件
    assert len(pushes) == 1
    assert len(_events(qenv, "status='backup_pending'")) == 1
    queue._auto_backup()  # 1 小时内：静默跳过
    assert len(pushes) == 1
    assert len(_events(qenv, "status='backup_pending'")) == 1
    (qenv / "backoff").write_text(str(time.time() - 7200))  # 拨回 2 小时
    queue._auto_backup()  # 再试并记事件
    assert len(pushes) == 2
    assert len(_events(qenv, "status='backup_pending'")) == 2


def test_auto_backup_failure_becomes_event(qenv, monkeypatch):
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    monkeypatch.setattr(queue, "PUSH_BACKOFF_FILE", qenv / "backoff")
    from pipeline import backup as backupmod

    def boom():
        raise RuntimeError("snap exploded")
    monkeypatch.setattr(backupmod, "snap", boom)
    queue._auto_backup()
    rows = _events(qenv, "status='backup_pending'")
    assert len(rows) == 1 and "snap exploded" in rows[0]["error"]
