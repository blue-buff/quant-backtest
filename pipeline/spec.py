"""Spec handling: load, resolve base+overrides, hash. CLI: resolve / hash."""
import argparse, hashlib, json, re, sys
from pathlib import Path
from . import REFS_DIR

def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def load_ref(name):
    p = REFS_DIR / (name + ".json")
    if not p.exists():
        raise ValueError("ref not found: " + name + " (looked at " + str(p) + ")")
    return json.loads(p.read_text())

def load_spec(path):
    return json.loads(Path(path).read_text())

META_KEYS = ("exp_id", "changes", "hypothesis", "pre_registration", "idea",
             "runner", "timeout_min", "action", "params", "claims",
             "metrics", "expectation")

KNOWN_TOP_KEYS = {"exp_id", "base", "overrides", "changes", "hypothesis",
                  "pre_registration", "idea", "expectation", "runner",
                  "timeout_min", "action", "params", "metrics", "claims"}

EXP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_warn(spec):
    """Warn (stderr) on unknown top-level keys; error only on missing exp_id/base.
    Unknown keys are NOT rejected: executor/research freedom comes first."""
    exp_id = str(spec.get("exp_id") or "")
    if not EXP_ID_RE.match(exp_id):
        raise ValueError(
            "QLAB_SPEC_INVALID: exp_id %r must be a single path segment matching "
            "^[A-Za-z0-9][A-Za-z0-9_.-]*$ (no '/', no '..'; it becomes a run dir "
            "name)" % exp_id)
    if "base" not in spec:
        raise ValueError("QLAB_SPEC_INVALID: base is required (use ref:name or a dict)")
    for k in sorted(set(spec) - KNOWN_TOP_KEYS):
        sys.stderr.write("QLAB_SPEC_WARN unknown key: %s\n" % k)
    fams = spec.get("metrics")
    if fams:
        from . import metrics as metricsmod
        for f in fams:
            if f not in metricsmod.KNOWN_METRICS:
                sys.stderr.write("QLAB_SPEC_WARN unknown metric family: %s\n" % f)
    return sorted(set(spec) - KNOWN_TOP_KEYS)

def resolve(spec):
    """Resolve base+overrides into an effective config (meta keys get _ prefix)."""
    validate_warn(spec)
    base = spec.get("base")
    if isinstance(base, str) and base.startswith("ref:"):
        b = load_ref(base[4:])
    elif isinstance(base, dict):
        b = base
    else:
        raise ValueError("base must be 'ref:name' or a dict, got: " + repr(base))
    eff = deep_merge(b, spec.get("overrides"))
    eff["_exp_id"] = spec.get("exp_id")
    for k in META_KEYS:
        if k in spec and k != "exp_id":
            if k in ("metrics", "expectation"):
                # scoring-gate keys: an explicit top-level value OVERRIDES the
                # base's and enters the spec hash under its real name (a changed
                # gate must never reuse an old ledger row)
                eff[k] = spec[k]
            else:
                eff["_" + k] = spec[k]
    return eff

def spec_hash(eff):
    canon_eff = dict(eff)
    m = canon_eff.get("metrics")
    if isinstance(m, list):
        # canonical order: metrics=["rankic","hit"] == metrics=["hit","rankic"]
        # (same scoring gate, same hash; different order must not redo work)
        canon_eff["metrics"] = sorted({str(x) for x in m})
    canon = json.dumps(canon_eff, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]

def main():
    ap = argparse.ArgumentParser(prog="pipeline.spec")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resolve").add_argument("spec_path")
    sub.add_parser("hash").add_argument("spec_path")
    a = ap.parse_args()
    spec = load_spec(a.spec_path)
    eff = resolve(spec)
    if a.cmd == "resolve":
        print(json.dumps(eff, indent=2, ensure_ascii=False))
    else:
        print(spec_hash(eff))

if __name__ == "__main__":
    main()
