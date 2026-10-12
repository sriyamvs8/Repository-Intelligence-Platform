"""Draw a reproducible random sample of relationships from a repository for manual precision review.
Usage: python scripts/sample_edges.py <repo path or URL> <per_type> <out.json>"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALLOW_LOCAL_PATHS", "true")
from backend.kg.pipeline import analyze_repository  # noqa: E402

TYPES = ["HANDLED_BY", "CALLS", "EXECUTES", "READS", "WRITES", "TESTS", "MAPS_TO", "FOREIGN_KEY", "OVERRIDES", "INSTANTIATES"]


def main(repo: str, per_type: int, out: str) -> None:
    res = analyze_repository(repo)
    ents = {e["id"]: e for e in res.entities}
    rng = random.Random(7)
    sample = []
    for t in TYPES:
        rels = sorted([r for r in res.relationships if r["type"] == t], key=lambda r: r["id"])
        for r in rng.sample(rels, min(per_type, len(rels))):
            s, d = ents[r["source"]], ents[r["target"]]
            sample.append({"id": r["id"], "type": t, "status": r["status"], "kind": r["source_kind"],
                           "source": s.get("qualified_name") or s["name"], "target": d.get("qualified_name") or d["name"],
                           "file": r.get("file"), "line": r.get("line"), "snippet": r.get("snippet") or r.get("evidence"),
                           "label": None, "note": ""})
    Path(out).write_text(json.dumps({"repo": res.meta, "sample": sample}, indent=1))
    print(f"{len(sample)} edges sampled -> {out}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
