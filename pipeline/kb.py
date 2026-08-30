"""Knowledge base: claims.jsonl with status loop. CLI: search / claims / add / init.

P7 T6: pipeline-written hooks --
- append_run_claim(exp_id): every train run appends one auto-claim (untested,
  tags=["auto"], linked to the exp, source=auto:metrics.json).
- link_claims(claim_ids, exp_id, expectation_check): spec-declared claims get
  the exp linked; met -> confirmed, not_met -> falsified, n/a -> status kept.
All functions take kb_dir for testability.
"""
import argparse, json, os, re, time
from pathlib import Path
from . import KNOWLEDGE_DIR

CLAIMS = KNOWLEDGE_DIR / "claims.jsonl"


def _claims_path(kb_dir):
    return Path(kb_dir or KNOWLEDGE_DIR) / "claims.jsonl"


def load_claims(kb_dir=KNOWLEDGE_DIR):
    p = _claims_path(kb_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def save_claims(rows, kb_dir=KNOWLEDGE_DIR):
    p = _claims_path(kb_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("claims.jsonl.tmp")
    tmp.write_text(chr(10).join(json.dumps(r, ensure_ascii=False) for r in rows) + chr(10))
    os.replace(tmp, p)  # atomic: concurrent readers never see a torn file


def _lock(kb_dir):
    """flock guard for read-modify-write (audit #3): concurrent imports must
    not lose claims or mint duplicate ids. Reader-only loads stay lock-free
    (the atomic replace above keeps them consistent)."""
    p = Path(kb_dir)
    p.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        f = open(str(p / ".claims.lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f
    except (ImportError, OSError):
        return None


def _unlock(f):
    if f is None:
        return
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()


def _next_claim_id(rows):
    best = 0
    for r in rows:
        m = re.match(r"c-(\d+)", str(r.get("claim_id") or ""))
        if m:
            best = max(best, int(m.group(1)))
    return "c-%04d" % (best + 1)


def append_run_claim(exp_id, kb_dir=KNOWLEDGE_DIR):
    """Auto-claim per train run (P7 T6): one untested claim line pointing at the
    run's metrics, linked to the exp. Pipeline-written, agent-reviewable."""
    lk = _lock(kb_dir)
    try:
        rows = load_claims(kb_dir)
        cid = _next_claim_id(rows)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = {"claim_id": cid,
               "text": "auto-claim: train run %s 的结果待人工审读（metrics.json 已入台账）" % exp_id,
               "source": "auto:metrics.json", "ctype": "empirical",
               "status": "untested", "linked_exp_ids": [str(exp_id)],
               "tags": ["auto"], "created_at": now[:10], "updated_at": now}
        rows.append(row)
        save_claims(rows, kb_dir)
        return row
    finally:
        _unlock(lk)


def link_claims(claim_ids, exp_id, expectation_check, kb_dir=KNOWLEDGE_DIR):
    """Wire spec-declared claims to this run (P7 T6): append exp_id to
    linked_exp_ids (dedup); expectation met -> confirmed, not_met -> falsified,
    n/a or unknown claim -> status untouched."""
    if not claim_ids:
        return []
    lk = _lock(kb_dir)
    try:
        rows = load_claims(kb_dir)
        by_id = {}
        for r in rows:
            by_id.setdefault(str(r.get("claim_id")), []).append(r)
        touched = []
        for cid in claim_ids:
            for r in by_id.get(str(cid), []):
                ids = list(r.get("linked_exp_ids") or [])
                if str(exp_id) not in ids:
                    ids.append(str(exp_id))
                    touched.append(str(cid))
                if expectation_check == "met":
                    r["status"] = "confirmed"
                elif expectation_check == "not_met":
                    r["status"] = "falsified"
                r["linked_exp_ids"] = ids
                r["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_claims(rows, kb_dir)
        return touched
    finally:
        _unlock(lk)


def add_claim(text, source, ctype, tags, status="untested", kb_dir=KNOWLEDGE_DIR):
    lk = _lock(kb_dir)
    try:
        rows = load_claims(kb_dir)
        cid = _next_claim_id(rows)
        row = {"claim_id": cid, "text": text, "source": source, "ctype": ctype,
               "status": status, "linked_exp_ids": [], "tags": tags.split(",") if tags else [],
               "created_at": time.strftime("%Y-%m-%d")}
        rows.append(row)
        save_claims(rows, kb_dir)
    finally:
        _unlock(lk)
    print(json.dumps(row, ensure_ascii=False))


def search(q, kb_dir=KNOWLEDGE_DIR):
    hits = [r for r in load_claims(kb_dir) if q.lower() in json.dumps(r, ensure_ascii=False).lower()]
    print(json.dumps(hits, ensure_ascii=False, indent=1))
    return hits


def list_claims(status=None, kb_dir=KNOWLEDGE_DIR):
    rows = load_claims(kb_dir)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return rows


SEED_CLAIMS = [
    ["全市场 Alpha158 + 10d 标签 + LightGBM(官方参数) 3种子集成，样本外(2025-01~2026-08) RankIC 0.0859，bootstrap p<=0 为 0.0，强显著",
     "docs/PREDICTION_BOOST_REPORT.md", "empirical", "confirmed", "ens,all-market,10d"],
    ["同模型映射到 hs300 子集 RankIC 0.0361 (p=0.050)，zz500 子集 0.0408 (p=0.032)：跨池泛化显著但明显弱于全市场",
     "docs/PREDICTION_BOOST_REPORT.md", "empirical", "confirmed", "ens,subset"],
    ["池内各自最佳标签周期：hs300 -> 15d，zz500 -> 10d",
     "docs/PREDICTION_BOOST_REPORT.md", "empirical", "confirmed", "pool,label"],
    ["单票方向预测 hit rate 数学期望约为 0.5 + IC/pi，RankIC 0.086 对应约 0.527；0.49-0.50 的 hit 是正常现象不是 bug",
     "docs/PREDICTION_BOOST_REPORT.md", "theoretical", "confirmed", "hit-rate"],
    ["横截面中性化(行业+市值)预期提升 RankIC 约 10-20%（数据源受限，尚未验证）",
     "knowledge/notes/neutralization-idea.md", "empirical", "untested", "neutralization"],
    ["LightGBM DART 模式在池内实验中未优于 gbdt 基准",
     "docs/EXPERIMENTS.md", "empirical", "falsified", "dart"],
    ["RobustZScoreNorm 处理器未带来提升",
     "docs/EXPERIMENTS.md", "empirical", "falsified", "processor"],
    ["LambdaRank 排序损失在池内实验为负，弃用",
     "docs/EXPERIMENTS.md", "empirical", "falsified", "lambdarank"],
    ["60d 长标签在池内差于 10d/15d",
     "docs/EXPERIMENTS.md", "empirical", "falsified", "label"],
    ["时间衰减样本权重未带来提升",
     "docs/EXPERIMENTS.md", "empirical", "falsified", "weight"],
]


def init_seed(kb_dir=KNOWLEDGE_DIR):
    lk = _lock(kb_dir)
    try:
        rows = load_claims(kb_dir)
        if rows:
            print(json.dumps({"note": "claims.jsonl already exists, skipped", "n": len(rows)}))
            return
        for text, source, ctype, status, tags in SEED_CLAIMS:
            rows.append({"claim_id": _next_claim_id(rows), "text": text, "source": source,
                         "ctype": ctype, "status": status, "linked_exp_ids": [],
                         "tags": tags.split(","), "created_at": time.strftime("%Y-%m-%d")})
        save_claims(rows, kb_dir)
        print(json.dumps({"seeded": len(rows)}))
    finally:
        _unlock(lk)


def main():
    ap = argparse.ArgumentParser(prog="pipeline.kb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("search"); p1.add_argument("query")
    p2 = sub.add_parser("claims"); p2.add_argument("--status")
    p3 = sub.add_parser("add")
    p3.add_argument("--text", required=True)
    p3.add_argument("--source", required=True)
    p3.add_argument("--ctype", default="empirical")
    p3.add_argument("--tags", default="")
    p3.add_argument("--status", default="untested")
    sub.add_parser("init")
    a = ap.parse_args()
    if a.cmd == "search": search(a.query)
    elif a.cmd == "claims": list_claims(a.status)
    elif a.cmd == "add": add_claim(a.text, a.source, a.ctype, a.tags, a.status)
    elif a.cmd == "init": init_seed()


if __name__ == "__main__":
    main()
