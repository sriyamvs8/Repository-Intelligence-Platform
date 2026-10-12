"""Precision / recall / F1 per relationship type against a labelled fixture.

Usage:  python scripts/evaluate.py            (uses tests/fixtures/shop)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ALLOW_LOCAL_PATHS", "true")

from backend.kg.pipeline import analyze_repository  # noqa: E402
from tests.fixtures import shop_expected as X  # noqa: E402


def predicted_sets(res):
    ents = {e["id"]: e for e in res.entities}
    rels = res.relationships
    by = lambda t: {(r["source"], r["target"]) for r in rels if r["type"] == t}
    access = set()
    sql_ops = {}
    for r in rels:
        if r["type"] in ("READS", "WRITES") and ents.get(r["source"], {}).get("type") == "SqlQuery":
            sql_ops.setdefault(r["source"], set()).add((r["type"], r["target"]))
    for r in rels:
        if r["type"] == "EXECUTES":
            for op, t in sql_ops.get(r["target"], ()):
                access.add((r["source"], op, t))
        elif r["type"] in ("READS", "WRITES") and ents.get(r["source"], {}).get("type") != "SqlQuery":
            access.add((r["source"], r["type"], r["target"]))
    return {
        "HANDLED_BY": by("HANDLED_BY"), "CALLS": by("CALLS"), "OVERRIDES": by("OVERRIDES"),
        "IMPLEMENTS": by("IMPLEMENTS"), "DATA_ACCESS": access, "HAS_COLUMN": by("HAS_COLUMN"),
        "FOREIGN_KEY": by("FOREIGN_KEY"), "MAPS_TO": by("MAPS_TO"), "CROSS_LANGUAGE_MAP": by("CROSS_LANGUAGE_MAP"),
        "TESTS": by("TESTS"),
    }, ents, rels


def evaluate(path: str | None = None, verbose: bool = True) -> dict:
    res = analyze_repository(path or str(ROOT / "tests" / "fixtures" / "shop"))
    pred, ents, rels = predicted_sets(res)
    out = {}
    tp_all = fp_all = fn_all = 0
    for name, pset in pred.items():
        exp = getattr(X, name)
        tp, fp, fn = len(pset & exp), len(pset - exp), len(exp - pset)
        tp_all, fp_all, fn_all = tp_all + tp, fp_all + fp, fn_all + fn
        p = tp / (tp + fp) if tp + fp else 1.0
        r = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        out[name] = {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn,
                     "false_positives": sorted(map(str, pset - exp))[:10], "missed": sorted(map(str, exp - pset))[:10]}
    P = tp_all / (tp_all + fp_all) if tp_all + fp_all else 1.0
    Rc = tp_all / (tp_all + fn_all) if tp_all + fn_all else 1.0
    out["_overall"] = {"precision": round(P, 3), "recall": round(Rc, 3), "f1": round(2 * P * Rc / (P + Rc), 3) if P + Rc else 0}
    apis = {e["id"] for e in ents.values() if e["type"] == "RestApi"}
    out["_apis"] = {"expected": len(X.APIS), "found": len(apis & X.APIS), "extra": sorted(apis - X.APIS), "missing": sorted(X.APIS - apis)}
    viol = [f for f in X.FORBIDDEN if any(r["type"] == f[0] and r["source"] == f[1] and (f[2] is None or r["target"] == f[2]) for r in rels)]
    out["_forbidden_violations"] = viol
    if verbose:
        print(f"{'relationship':<20}{'P':>7}{'R':>7}{'F1':>7}{'TP':>5}{'FP':>5}{'FN':>5}")
        for k, v in out.items():
            if not k.startswith("_"):
                print(f"{k:<20}{v['precision']:>7}{v['recall']:>7}{v['f1']:>7}{v['tp']:>5}{v['fp']:>5}{v['fn']:>5}")
                for x in v["false_positives"]:
                    print("    FP", x)
                for x in v["missed"]:
                    print("    FN", x)
        print("overall", out["_overall"])
        print("apis", out["_apis"])
        print("forbidden violations", out["_forbidden_violations"])
    return out


if __name__ == "__main__":
    r = evaluate(sys.argv[1] if len(sys.argv) > 1 else None)
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "evaluation_results.json").write_text(json.dumps(r, indent=2))
