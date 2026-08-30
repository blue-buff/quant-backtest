"""P8 T7: auto/spark 路由（spark 优先、auto 回退本地、spark 不可达 blocked）。"""
import json
import sqlite3
import sys
from pathlib import Path

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
    monkeypatch.setattr(queue, "QLAB_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    repo_root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", repo_root)
    (tmp_path / "spec.json").write_text(json.dumps({
        "exp_id": "t_route", "base": {}, "runner": "auto",
        "action": {"kind": "sleep_ok", "seconds": 1}}))
    queue.db().close()  # 建表 + 迁移（含 data_rev 列）
    return tmp_path


def _insert_row(qenv, runner):
    from pipeline import spec as specmod
    spec = specmod.load_spec(str(qenv / "spec.json"))
    h = specmod.spec_hash(specmod.resolve(spec))
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    cur = conn.execute(
        "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,timeout_min,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("b1", "t_route", "spec.json", h, runner, "running", 5, "2026-08-23"))
    conn.commit()
    jid = cur.lastrowid
    conn.close()
    row = {"job_id": jid, "batch_id": "b1", "exp_id": "t_route",
           "spec_path": "spec.json", "spec_hash": h, "runner": runner,
           "status": "running", "timeout_min": 5}
    return row


def _status(qenv, jid):
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    r = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone())
    conn.close()
    return r


def _events(qenv, jid):
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE job_id=? ORDER BY id", (jid,)).fetchall()]
    conn.close()
    return rows


def test_auto_uses_spark_when_ok(qenv, monkeypatch):
    from pipeline import remote as remotemod
    monkeypatch.setattr(remotemod, "dispatch",
                        lambda row: {"ok": True, "run_id": "rid9"})
    row = _insert_row(qenv, "auto")
    queue._execute(row)
    r = _status(qenv, row["job_id"])
    assert r["status"] == "done" and r["mlflow_run_id"] == "rid9"
    assert all(e["status"] != "spark_fallback" for e in _events(qenv, row["job_id"]))


def test_auto_falls_back_to_local(qenv, monkeypatch):
    # 子进程 harness 用临时根目录（不污染真实台账）
    monkeypatch.setenv("QLAB_ROOT", str(qenv))
    from pipeline import remote as remotemod
    monkeypatch.setattr(remotemod, "dispatch",
                        lambda row: {"ok": False, "blocked": False,
                                     "reason": "spark down (test)"})
    row = _insert_row(qenv, "auto")
    queue._execute(row)  # 回退本地执行 sleep_ok（真实 harness 子进程）
    r = _status(qenv, row["job_id"])
    assert r["status"] == "done" and r["mlflow_run_id"]
    evs = _events(qenv, row["job_id"])
    assert any(e["status"] == "spark_fallback" and "spark down" in (e["error"] or "")
               for e in evs)
    assert "fell back to local" in (r["note"] or "")


def test_spark_explicit_blocks_when_down(qenv, monkeypatch):
    from pipeline import remote as remotemod
    monkeypatch.setattr(remotemod, "dispatch",
                        lambda row: {"ok": False, "blocked": True,
                                     "reason": "QLAB_SPARK_SSH is blank"})
    row = _insert_row(qenv, "spark")
    queue._execute(row)
    r = _status(qenv, row["job_id"])
    assert r["status"] == "blocked"
    assert "QLAB_SPARK_SSH" in (r["note"] or "")


def test_spark_failure_blocks_with_reason(qenv, monkeypatch):
    from pipeline import remote as remotemod
    monkeypatch.setattr(remotemod, "dispatch",
                        lambda row: {"ok": False, "blocked": False,
                                     "reason": "remote compute failed rc=5"})
    row = _insert_row(qenv, "spark")
    queue._execute(row)
    r = _status(qenv, row["job_id"])
    assert r["status"] == "blocked"
    assert "remote compute failed" in (r["note"] or "")


def test_submit_defaults_to_auto(qenv, monkeypatch, capsys):
    (qenv / "batch.json").write_text(json.dumps(
        {"batch_id": "b2", "specs": ["spec.json"]}))
    # spec.json 无 runner 字段 -> 应落 auto
    (qenv / "spec.json").write_text(json.dumps(
        {"exp_id": "t_auto2", "base": {}, "action": {"kind": "sleep_ok"}}))
    queue.submit(str(qenv / "batch.json"))
    assert json.loads(capsys.readouterr().out)["new"] == 1
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    r = dict(conn.execute("SELECT * FROM jobs WHERE exp_id='t_auto2'").fetchone())
    conn.close()
    assert r["runner"] == "auto"


def test_retry_blocked_only_no_failed_drag(qenv, capsys):
    """回归（审计 #5b）：--blocked 只重排 blocked，不再连坐 failed 历史行。"""
    from pipeline import spec as specmod
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.execute(
        "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
        "timeout_min,created_at) VALUES('b1','e_fail','spec.json','hfail','local',"
        "'failed',5,'2026-08-23')")
    conn.execute(
        "INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
        "timeout_min,created_at) VALUES('b1','e_blk','spec.json','hblk','spark',"
        "'blocked',5,'2026-08-23')")
    conn.commit()
    conn.close()
    def _status_map():
        conn = sqlite3.connect(str(qenv / "jobs.db"))
        conn.row_factory = sqlite3.Row
        rows = {r["spec_hash"]: dict(r) for r in conn.execute(
            "SELECT * FROM jobs").fetchall()}
        conn.close()
        return rows

    queue.retry("b1", include_blocked=True)
    out = json.loads(capsys.readouterr().out)
    assert out["requeued"] == 1
    rows = _status_map()
    assert rows["hblk"]["status"] == "queued"      # blocked 重排
    assert rows["hfail"]["status"] == "failed"     # failed 未被连坐
    # --blocked --failed 两者都排
    queue.retry("b1", include_blocked=True, include_failed=True)
    out = json.loads(capsys.readouterr().out)
    assert out["requeued"] == 1  # failed 那行（hblk 已在队列，唯一索引/自排除挡掉）
    rows = _status_map()
    assert rows["hfail"]["status"] == "queued"


def test_retry_blocked_requeues_self_keeps_runner(qenv, capsys):
    """回归：retry --blocked 曾把自己所在的 blocked 行排除掉（同一 hash 自排除），
    导致 blocked 任务永远重排不了。"""
    from pipeline import spec as specmod
    spec = {"exp_id": "t_blk", "base": {}, "runner": "spark"}
    (qenv / "spec.json").write_text(json.dumps(spec))
    h = specmod.spec_hash(specmod.resolve(spec))
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.execute("INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
                 "timeout_min,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 ("b1", "t_blk", "spec.json", h, "spark", "blocked", 5, "2026-08-23"))
    conn.commit()
    conn.close()
    queue.retry(include_blocked=True)
    out = json.loads(capsys.readouterr().out)
    assert out["requeued"] == 1
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.row_factory = sqlite3.Row
    r = dict(conn.execute("SELECT * FROM jobs WHERE exp_id='t_blk'").fetchone())
    conn.close()
    assert r["status"] == "queued" and r["runner"] == "spark"


def test_submit_skips_done_only_at_same_revision(qenv, monkeypatch, capsys):
    from pipeline import spec as specmod
    from pipeline import data as datamod
    spec = {"exp_id": "t_rev", "base": {}}
    (qenv / "spec.json").write_text(json.dumps(spec))
    h = specmod.spec_hash(specmod.resolve(spec))
    (qenv / "batch.json").write_text(json.dumps(
        {"batch_id": "b3", "specs": ["spec.json"]}))
    conn = sqlite3.connect(str(qenv / "jobs.db"))
    conn.execute("INSERT INTO jobs(batch_id,exp_id,spec_path,spec_hash,runner,status,"
                 "timeout_min,created_at,data_rev) VALUES(?,?,?,?,?,?,?,?,?)",
                 ("b3", "t_rev", "spec.json", h, "local", "done", 5,
                  "2026-08-23", 0))
    conn.commit()
    conn.close()
    # 当前修订号 0：同 hash done 行 data_rev=0 -> 跳过
    queue.submit(str(qenv / "batch.json"))
    assert json.loads(capsys.readouterr().out)["skip_done"] == 1
    # 修订号推进后：同 spec 重新 submit 会正常重跑
    monkeypatch.setattr(datamod, "data_revision", lambda *a, **k: 3)
    queue.submit(str(qenv / "batch.json"))
    out2 = json.loads(capsys.readouterr().out)
    assert out2["new"] == 1 and out2["skip_done"] == 0
