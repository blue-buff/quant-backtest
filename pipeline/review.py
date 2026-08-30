"""Advisory result checks (deterministic v1; LLM red-team lands in P2)."""
import argparse, json
from . import registry

def check_metrics(m, extra=None):
    checks = []
    def add(name, ok, note):
        checks.append({"check": name, "pass": ok, "note": note})
    add("p_value_reported", any("p_le0" in k or "p_value" in k for k in m),
        "指标里应带 p 值（p_le0 或 p_value），否则无法判断显著性")
    add("sample_size_reported", "n_days" in m, "应报告样本天数 n_days")
    add("window_reported", bool((extra or {}).get("window")), "结论应注明样本窗口")
    add("subsets_reported", any("subset" in k for k in m), "应含 hs300/zz500 子域指标（跨池稳健性）")
    add("quarters_reported", any(k.startswith("quarters.") for k in m), "应含分季度指标（跨 regime 稳健性）")
    add("multiplicity_reported", "n_variants" in m, "同一假说试过多个变体时应报告变体数并修正 p 值")
    return checks

def from_run(run_id):
    c = registry.client()
    r = c.get_run(run_id)
    m = {k: float(v) for k, v in r.data.metrics.items()}
    extra = {"window": "2025-01-01~2026-08-14 (legacy default)" if r.data.tags.get("qlab.legacy") else ""}
    return check_metrics(m, extra)

def from_file(path):
    """Check a metrics.json artifact (P7 T7): nested numeric leaves are
    flattened so p_le0/n_days/quarters are found; sample_window feeds the
    window_reported check for new harness runs."""
    m = json.loads(open(path).read())
    if isinstance(m, dict) and "metrics" in m:
        m = m["metrics"]
    flat = {}

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, prefix + str(k) + ".")
        elif isinstance(obj, list):
            # per-period dict lists (quarters/monthly_ic) keep their period key
            for item in obj:
                if isinstance(item, dict):
                    for kp in ("quarter", "month"):
                        if kp in item:
                            walk(item, prefix + str(item[kp]) + ".")
                            break
                    else:
                        walk(item, prefix)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            flat[prefix[:-1]] = obj
    walk(m)
    extra = {}
    if isinstance(m, dict) and m.get("sample_window"):
        extra["window"] = str(m["sample_window"])
    return check_metrics(flat, extra)

def main():
    ap = argparse.ArgumentParser(prog="pipeline.review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("run"); p1.add_argument("run_id")
    p2 = sub.add_parser("file"); p2.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "run":
        print(json.dumps(from_run(a.run_id), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(from_file(a.path), ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
