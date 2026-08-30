"""Export board.csv summary of the ledger (+ console / json view).

P7 T5 additions:
- qlab.base_ref grouping with Bonferroni multiplicity correction (n_variants,
  p_bonf, multiplicity_risk); rows without base_ref (legacy) are never corrected.
- --source harness|legacy|eval filter (AGENTS rule: compare only within one source).
- summary: one-glance status for the agent's turn start.
"""
import argparse, csv, json
from collections import Counter
from . import BOARD_CSV, registry

FIELDS = ["exp", "run_id", "status", "source", "pool", "seed", "batch",
          "legacy", "smoke", "git", "executor", "handler", "task", "rank_IC",
          "IC", "rankic_mean", "p_le0", "base_ref", "n_variants", "p_bonf",
          "multiplicity_risk", "families", "sharpe", "ann_ret", "excess_ann",
          "turnover_mean", "cost_drag_ann", "attribution", "start_time"]

SOURCE_ALIASES = {"legacy": "legacy_backfill", "eval": "eval_backfill"}


def rows(formal=False, source=None):
    """formal=True: only FINISHED non-smoke research rows (no wiring checks,
    no failures). source filters on the qlab.source tag."""
    src = SOURCE_ALIASES.get(source, source) if source else None
    out = []
    c = registry.client()
    for e in c.search_experiments():
        for r in c.search_runs(e.experiment_id, max_results=50000):
            t = dict(r.data.tags)
            m = r.data.metrics
            if formal and (r.info.status != "FINISHED" or t.get("qlab.smoke") == "true"):
                continue
            if src and t.get("qlab.source", "") != src:
                continue
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
                "executor": t.get("qlab.executor", ""),
                "handler": t.get("qlab.handler", ""),
                "task": t.get("qlab.task", ""),
                "rank_IC": m.get("rank_IC"),
                "IC": m.get("IC"),
                "rankic_mean": m.get("rankic_mean"),
                # harness rows carry p_le0; legacy eval backfills carry bootstrap.p_le0
                "p_le0": m.get("p_le0", m.get("bootstrap.p_le0")),
                "base_ref": t.get("qlab.base_ref", ""),
                "families": t.get("qlab.metric_families", ""),
                "sharpe": m.get("backtest.sharpe"),
                "ann_ret": m.get("backtest.ann_ret"),
                "excess_ann": m.get("backtest.excess_ann"),
                "turnover_mean": m.get("portfolio.turnover_mean"),
                "cost_drag_ann": m.get("backtest.cost_drag_ann"),
                "attribution": t.get("qlab.attribution", ""),
                "start_time": r.info.start_time,
            })
    out.sort(key=lambda x: (x["exp"], x["start_time"] or 0))
    return _multiplicity(out)


def _multiplicity(data):
    """Bonferroni across variants sharing the same base config. Groups FINISHED
    rows by base_ref; rows without base_ref (legacy) are never corrected.
    p_bonf = min(1, p_le0 * n_variants); multiplicity_risk = p_bonf > 0.05.
    Rows with missing p_le0 keep n_variants but get no corrected p.
    Audit #9: smoke rows (wiring checks) never count, and repeated FINISHED
    rows of the SAME experiment within one base_ref count once (the latest):
    a rerun of the same spec is not a new variant."""
    groups = {}
    for r in data:
        if (r.get("status") != "FINISHED" or not r.get("base_ref")
                or str(r.get("smoke") or "") == "true"):
            continue
        groups.setdefault(r["base_ref"], []).append(r)
    for base, rs in groups.items():
        latest = {}
        for r in sorted(rs, key=lambda x: x.get("start_time") or 0):
            latest[r["exp"]] = r
        rs = list(latest.values())
        n = len(rs)
        for r in rs:
            r["n_variants"] = n
            p = r.get("p_le0")
            if p is None:
                r["p_bonf"] = None
                r["multiplicity_risk"] = ""
                continue
            pb = min(1.0, float(p) * n)
            r["p_bonf"] = pb
            r["multiplicity_risk"] = pb > 0.05
    return data


def export(csv_path=None, to_console=True, source=None):
    data = rows(source=source)
    out = csv_path or str(BOARD_CSV)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in data:
            w.writerow(r)
    if to_console:
        by_status = {}
        for r in data:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        print(json.dumps({"runs": len(data), "by_status": by_status, "csv": out}))
    return data


def summary():
    """One-glance turn-start status: ledger runs, queue states, untested claims,
    pending backups, last 5 completions."""
    from . import queue as queuemod
    from . import kb as kbmod
    runs = rows()
    conn = queuemod.db()
    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
    pend = conn.execute(
        "SELECT COUNT(*) FROM events WHERE status='backup_pending'").fetchone()[0]
    conn.close()
    qc = Counter(j["status"] for j in jobs)
    claims = kbmod.load_claims()
    untested = len([c for c in claims if c.get("status") == "untested"])
    recent = []
    if queuemod.DONE_LOG.exists():
        for line in queuemod.DONE_LOG.read_text().splitlines()[-5:]:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            recent.append({"exp_id": r.get("exp_id"), "rankic": r.get("rankic"),
                           "p": r.get("p")})
    out = {"runs": len(runs), "queue": dict(qc),
           "claims_untested": untested, "backup_pending_events": int(pend),
           "recent_done": recent}
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return out


def main():
    ap = argparse.ArgumentParser(prog="pipeline.board")
    ap.add_argument("--csv", default=None, help="output path (default results/board.csv)")
    ap.add_argument("--json", action="store_true", help="print rows as json instead of csv summary")
    ap.add_argument("--formal", action="store_true",
                    help="formal view: only FINISHED non-smoke research rows")
    ap.add_argument("--source", choices=["harness", "legacy", "eval"], default=None,
                    help="filter by metric source (compare only within one source)")
    sub = ap.add_subparsers(dest="cmd", required=False)
    sub.add_parser("summary", help="one-glance status for turn start")
    a = ap.parse_args()
    if a.cmd == "summary":
        summary()
    elif a.json:
        print(json.dumps(rows(a.formal, a.source), ensure_ascii=False, indent=1, default=str))
    else:
        export(a.csv, source=a.source)


if __name__ == "__main__":
    main()
