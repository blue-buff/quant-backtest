"""Knowledge base: claims.jsonl with status loop. CLI: search / claims / add / init."""
import argparse, json, time
from . import KNOWLEDGE_DIR

CLAIMS = KNOWLEDGE_DIR / "claims.jsonl"

def load_claims():
    if not CLAIMS.exists():
        return []
    out = []
    for line in CLAIMS.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out

def save_claims(rows):
    CLAIMS.parent.mkdir(parents=True, exist_ok=True)
    CLAIMS.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

def add_claim(text, source, ctype, tags, status="untested"):
    rows = load_claims()
    cid = "c-%04d" % (len(rows) + 1)
    row = {"claim_id": cid, "text": text, "source": source, "ctype": ctype,
           "status": status, "linked_exp_ids": [], "tags": tags.split(",") if tags else [],
           "created_at": time.strftime("%Y-%m-%d")}
    rows.append(row)
    save_claims(rows)
    print(json.dumps(row, ensure_ascii=False))

def search(q):
    hits = [r for r in load_claims() if q.lower() in json.dumps(r, ensure_ascii=False).lower()]
    print(json.dumps(hits, ensure_ascii=False, indent=1))
    return hits

def list_claims(status=None):
    rows = load_claims()
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

def init_seed():
    rows = load_claims()
    if rows:
        print(json.dumps({"note": "claims.jsonl already exists, skipped", "n": len(rows)}))
        return
    for text, source, ctype, status, tags in SEED_CLAIMS:
        rows.append({"claim_id": "c-%04d" % (len(rows) + 1), "text": text, "source": source,
                     "ctype": ctype, "status": status, "linked_exp_ids": [],
                     "tags": tags.split(","), "created_at": time.strftime("%Y-%m-%d")})
    save_claims(rows)
    print(json.dumps({"seeded": len(rows)}))

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
