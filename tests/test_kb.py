"""P7 T6: auto-claim + spec.claims 联动回写。"""
from pipeline import kb


def _seed(tmp_path, claim_id="c-0005", status="untested", linked=None):
    kb.save_claims([{"claim_id": claim_id, "text": "t", "source": "s",
                     "ctype": "empirical", "status": status,
                     "linked_exp_ids": linked or [], "tags": [],
                     "created_at": "2026-08-01"}], tmp_path)


def test_append_run_claim(tmp_path):
    row = kb.append_run_claim("exp1", kb_dir=tmp_path)
    assert row["status"] == "untested"
    assert row["tags"] == ["auto"]
    assert row["linked_exp_ids"] == ["exp1"]
    assert row["source"] == "auto:metrics.json"
    assert row["claim_id"] == "c-0001"
    rows = kb.load_claims(tmp_path)
    assert len(rows) == 1
    row2 = kb.append_run_claim("exp2", kb_dir=tmp_path)
    assert row2["claim_id"] == "c-0002"
    assert len(kb.load_claims(tmp_path)) == 2


def test_link_claims_met(tmp_path):
    _seed(tmp_path)
    kb.link_claims(["c-0005"], "exp9", "met", kb_dir=tmp_path)
    r = kb.load_claims(tmp_path)[0]
    assert r["status"] == "confirmed"
    assert r["linked_exp_ids"] == ["exp9"]
    assert "updated_at" in r


def test_link_claims_not_met(tmp_path):
    _seed(tmp_path)
    kb.link_claims(["c-0005"], "exp9", "not_met", kb_dir=tmp_path)
    r = kb.load_claims(tmp_path)[0]
    assert r["status"] == "falsified"
    assert r["linked_exp_ids"] == ["exp9"]


def test_link_claims_na_links_but_keeps_status(tmp_path):
    _seed(tmp_path)
    kb.link_claims(["c-0005"], "exp9", "n/a", kb_dir=tmp_path)
    r = kb.load_claims(tmp_path)[0]
    assert r["status"] == "untested"   # n/a 不动状态
    assert r["linked_exp_ids"] == ["exp9"]  # 但照样链接


def test_link_claims_missing_claim_untouched(tmp_path):
    _seed(tmp_path)
    touched = kb.link_claims(["c-9999"], "exp9", "met", kb_dir=tmp_path)
    assert touched == []
    r = kb.load_claims(tmp_path)[0]
    assert r["linked_exp_ids"] == []
    assert r["status"] == "untested"


def test_link_claims_second_call_no_dup_exp(tmp_path):
    _seed(tmp_path, linked=["exp1"])
    kb.link_claims(["c-0005"], "exp9", "met", kb_dir=tmp_path)
    kb.link_claims(["c-0005"], "exp9", "met", kb_dir=tmp_path)
    r = kb.load_claims(tmp_path)[0]
    assert r["linked_exp_ids"] == ["exp1", "exp9"]  # exp9 只追加一次


def test_load_claims_skips_corrupt_lines(tmp_path):
    p = tmp_path / "claims.jsonl"
    p.write_text("garbage line\n")
    kb.append_run_claim("exp1", kb_dir=tmp_path)
    rows = kb.load_claims(tmp_path)
    assert len(rows) == 1 and rows[0]["claim_id"] == "c-0001"


def test_concurrent_appends_no_loss_no_dup_ids(tmp_path):
    """回归（审计 #3）：并发 auto-claim 不丢行、不撞 id。"""
    import threading
    errs = []

    def worker(i):
        try:
            kb.append_run_claim("exp%d" % i, kb_dir=tmp_path)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []
    rows = kb.load_claims(tmp_path)
    assert len(rows) == 8
    ids = [r["claim_id"] for r in rows]
    assert len(set(ids)) == 8
    assert sorted(ids) == ["c-%04d" % i for i in range(1, 9)]
    assert sorted(r["linked_exp_ids"][0] for r in rows) == \
        ["exp%d" % i for i in range(8)]
