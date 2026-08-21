"""Export board.csv summary of the ledger (+ console / json view)."""
import argparse, csv, json
from . import BOARD_CSV, registry

FIELDS = ["exp", "run_id", "status", "source", "pool", "seed", "batch",
          "legacy", "smoke", "git", "rank_IC", "IC", "rankic_mean", "p_le0",
          "start_time"]

def rows():
    out = []
    c = registry.client()
    for e in c.search_experiments():
        for r in c.search_runs(e.experiment_id, max_results=5000):
            t = dict(r.data.tags)
            m = r.data.metrics
            out.append({
                "exp": e.name,
                "run_id": r.info.run_id,
                "status": r.info.status,
                "source": t.get("qlab.source", ""),
                "pool": t.get("qlab.pool", ""),
                "seed": t.get("qlab.seed", ""),
                "batch": t.get("qlab.batch_id", ""),
                "legacy": t.get("qlab.legacy", ""),
                "smoke": t.get("qlab.smoke", ""),
                "git": t.get("qlab.git", ""),
                "rank_IC": m.get("rank_IC"),
                "IC": m.get("IC"),
                "rankic_mean": m.get("rankic_mean"),
                "p_le0": m.get("bootstrap.p_le0"),
                "start_time": r.info.start_time,
            })
    out.sort(key=lambda x: (x["exp"], x["start_time"] or 0))
    return out

def export(csv_path=None, to_console=True):
    data = rows()
    out = csv_path or str(BOARD_CSV)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in data:
            w.writerow(r)
    if to_console:
        print(json.dumps({"runs": len(data), "csv": out}))
    return data

def main():
    ap = argparse.ArgumentParser(prog="pipeline.board")
    ap.add_argument("--csv", default=None, help="output path (default results/board.csv)")
    ap.add_argument("--json", action="store_true", help="print rows as json instead of csv summary")
    a = ap.parse_args()
    if a.json:
        print(json.dumps(rows(), ensure_ascii=False, indent=1, default=str))
    else:
        export(a.csv)

if __name__ == "__main__":
    main()
