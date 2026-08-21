"""Spec handling: load, resolve base+overrides, hash. CLI: resolve / hash."""
import argparse, hashlib, json
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
             "runner", "timeout_min", "action", "params")

def resolve(spec):
    """Resolve base+overrides into an effective config (meta keys get _ prefix)."""
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
            eff["_" + k] = spec[k]
    return eff

def spec_hash(eff):
    canon = json.dumps(eff, sort_keys=True, ensure_ascii=False)
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
