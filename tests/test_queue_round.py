"""审计回归：--once 一轮语义 + round_end 通知行 + heal 守卫 + auto backup 退避。"""
import json
import sqlite3
import time

import pytest

from pipeline import queue


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "JOBS_DB", tmp_path / "jobs.db")
    monkeypatch.setattr(queue, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(queue, "RUNID_DIR", tmp_path / "runids")
    monkeypatch.setattr(queue, "MARKER_FILE", tmp_path / "marker")
    monkeypatch.setattr(queue, "HEARTBEAT_FILE", tmp_path / "hb")
    monkeypatch.setattr(queue, "QUEUE_LOGS", tmp_path / "logs")
    monkeypatch.setattr(queue, "DONE_LOG", tmp_path / "done.log")
    monkeypatch.setattr(queue, "DONE_MARKER", tmp_path / "done_marker")
    monkeypatch.setattr(queue, "PUSH_BACKOFF_FILE", tmp_path / ".last_push_attempt")
    monkeypatch.setattr(queue, "PUSH_BACKOFF_SECS", 3600)
    queue.db().close()  # 建表 + 迁移
    return tmp_path


def _insert(qenv, n, batch="b1"):
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    for i in range(n):
        conn.execute(
            "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
            "timeout_min,created_at,data_rev) VALUES(?,?,?,?,?,?,?,?,?)",
            (batch, "e%d" % (i + 1), "spec.json", "h%d" % (i + 1), "local",
             "queued", 5, "2026-08-23", 1))
    conn.commit()
    conn.close()


def _rows(qenv):
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs ORDER BY job_id").fetchall()]
    conn.close()
    return rows


def _events(qenv, where="1=1"):
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE " + where).fetchall()]
    conn.close()
    return rows


def _fake_execute(qenv, executed):
    def fake(row):
        executed.append(row["job_id"])
        conn = sqlite3.connect(str(qenv / "jobs.db"))
        conn.execute("UPDATE jobs SET status='done', finished_at='2026-08-23' "
                     "WHERE job_id=?", (row["job_id"],))
        conn.commit()
        conn.close()
    return fake


def test_run_once_claims_one_wave_and_round_end(qenv, monkeypatch):
    """回归（审计 #14）：--once 只领一轮；轮结束写 round_end 行；排空才触发备份。"""
    _insert(qenv, 3)
    executed = []
    monkeypatch.setattr(queue, "_execute", _fake_execute(qenv, executed))
    backups = []
    monkeypatch.setattr(queue, "_auto_backup", lambda: backups.append(1))
    rounds = []
    monkeypatch.setattr(queue, "append_done_log", lambda e: rounds.append(e))

    queue.run(batch_id="b1", once=True, concurrency=2)
    assert sorted(executed) == [1, 2]          # 一轮只领 2 个
    assert len(rounds) == 1
    r = rounds[0]
    assert r["kind"] == "round_end"
    assert [(c["job_id"], c["status"]) for c in r["claimed"]] == [(1, "done"), (2, "done")]
    assert r["remaining_queued"] == 1
    assert backups == []                       # 队列没排空：不触发备份

    queue.run(batch_id="b1", once=True, concurrency=2)
    assert sorted(executed) == [1, 2, 3]       # 第二次一轮把剩余领走
    assert len(rounds) == 2
    assert rounds[-1]["remaining_queued"] == 0
    assert backups == [1]                      # 排空后自动备份挂钩


def test_run_watch_drains_all_then_waits(qenv, monkeypatch):
    """--watch 排空整队；空队列后进入等待（测试用 sleep 抛错退出）。"""
    _insert(qenv, 3)
    executed = []
    monkeypatch.setattr(queue, "_execute", _fake_execute(qenv, executed))
    backups = []
    monkeypatch.setattr(queue, "_auto_backup", lambda: backups.append(1))

    def fake_sleep(s):
        raise RuntimeError("stop watching")

    monkeypatch.setattr(queue.time, "sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop watching"):
        queue.run(batch_id="b1", once=False, concurrency=2)
    assert sorted(executed) == [1, 2, 3]
    assert backups == [1]


def test_heal_skips_row_finished_midflight(qenv, monkeypatch, capsys):
    """回归（审计 #15）：heal 的 UPDATE 带 status='running' 守卫；SELECT 与 UPDATE
    之间被完成的行保持 done，不被覆盖成 failed。"""
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.execute(
        "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
        "timeout_min,created_at) VALUES('b1','e1','spec.json','h1','local',"
        "'running',5,'2026-08-23')")
    conn.commit()
    conn.close()
    (qenv / "hb").write_text("%d %d" % (time.time() - 3600, 12345))
    monkeypatch.setattr(queue, "_pid_alive", lambda pid: False)

    class _Cursor:
        rowcount = 0

    real_db = queue.db

    class _RaceConn:
        def __init__(self, real):
            self.real = real

        def execute(self, sql, params=()):
            if "status='running'" in sql and "status='failed'" in sql:
                # 完成写回在 heal 的 UPDATE 之前落盘
                self.real.execute(
                    "UPDATE jobs SET status='done' WHERE status='running'")
                return _Cursor()
            return self.real.execute(sql, params)

        def commit(self):
            self.real.commit()

        def close(self):
            self.real.close()

    monkeypatch.setattr(queue, "db", lambda: _RaceConn(real_db()))
    queue.heal()
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "healed"
    assert out["auto_healed"] == []
    assert _rows(qenv)[0]["status"] == "done"   # 真实状态保留


def test_auto_backup_no_token_backoff(qenv, monkeypatch):
    """回归（审计 #13）：无 token 分支同样 1h 退避，不刷 backup_pending。"""
    from pipeline import backup as backupmod
    monkeypatch.setattr(backupmod, "snap", lambda: None)
    monkeypatch.setattr(backupmod, "_token", lambda env: "")
    pushed = []
    monkeypatch.setattr(backupmod, "push", lambda *a, **k: pushed.append(a))
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    queue._auto_backup()
    queue._auto_backup()  # 1h 内第二次：静默跳过
    assert pushed == []
    assert len(_events(qenv, "status='backup_pending'")) == 1


def test_auto_backup_success_resets_backoff(qenv, monkeypatch):
    """回归（审计 #13）：成功 push 后时钟重置，下次排空照样推。"""
    from pipeline import backup as backupmod
    monkeypatch.setattr(backupmod, "snap", lambda: None)
    monkeypatch.setattr(backupmod, "_token", lambda env: "t")
    pushed = []
    monkeypatch.setattr(backupmod, "push", lambda *a, **k: pushed.append(a))
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    queue._auto_backup()
    queue._auto_backup()
    assert len(pushed) == 2
    assert not (qenv / ".last_push_attempt").exists()


def test_cancel_queued_and_running_and_done(qenv, monkeypatch, capsys):
    """回归（审计 #5a）：cancel 终态 cancelled；running 先杀组；done 跳过。"""
    killed = []
    monkeypatch.setattr(queue.os, "killpg", lambda pgid, sig: killed.append(pgid))
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    for st in ("queued", "running", "done"):
        conn.execute(
            "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
            "pgid,timeout_min,created_at) VALUES('b1',?, 'spec.json',?, 'local', ?,"
            "111, 5, '2026-08-23')", (st + "_e", st + "_h", st))
    conn.commit()
    conn.close()
    queue.cancel([1, 2, 3])
    out = json.loads(capsys.readouterr().out)
    assert out["cancelled"] == [1, 2]
    assert out["skipped"] == [3]
    assert killed == [111]  # 只有 running 被杀了进程组
    rows = _rows(qenv)
    assert [r["status"] for r in rows] == ["cancelled", "cancelled", "done"]
    evs = [r for r in _events(qenv) if r["status"] == "cancelled"]
    assert [e["job_id"] for e in evs] == [1, 2]


def test_heal_quick_when_dead_pid_past_3_heartbeats(qenv, monkeypatch, capsys):
    """回归（审计 #6）：心跳过期 ~100s 且 pid 已死 → 立即 heal（不等 5 分钟窗）。"""
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.execute(
        "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
        "timeout_min,created_at) VALUES('b1','e1','spec.json','h1','local',"
        "'running',5,'2026-08-23')")
    conn.commit()
    conn.close()
    (qenv / "hb").write_text("%d 99999" % (time.time() - 100))
    monkeypatch.setattr(queue, "_pid_alive", lambda pid: False)
    queue.heal()
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "healed"
    assert out["auto_healed"] == [1]
    assert _rows(qenv)[0]["status"] == "failed"


def test_heal_alive_but_stale_no_action(qenv, monkeypatch, capsys):
    # 超过 stale 窗口且 pid 活着：只报 alive_but_stale，不动任何行
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.execute(
        "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
        "timeout_min,created_at) VALUES('b1','e1','spec.json','h1','local',"
        "'running',5,'2026-08-23')")
    conn.commit()
    conn.close()
    (qenv / "hb").write_text("%d 99999" % (time.time() - 400))
    monkeypatch.setattr(queue, "_pid_alive", lambda pid: True)
    queue.heal()
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "alive_but_stale"
    assert _rows(qenv)[0]["status"] == "running"  # 没动任何行


def test_auto_backup_failure_keeps_backoff(qenv, monkeypatch):
    from pipeline import backup as backupmod
    monkeypatch.setattr(backupmod, "snap", lambda: None)
    monkeypatch.setattr(backupmod, "_token", lambda env: "t")

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(backupmod, "push", boom)
    monkeypatch.delenv("QLAB_AUTO_BACKUP", raising=False)
    queue._auto_backup()
    queue._auto_backup()  # 失败写下的 backoff 生效：第二次被跳过
    assert len(_events(qenv, "status='backup_pending'")) == 1
    assert (qenv / ".last_push_attempt").exists()
