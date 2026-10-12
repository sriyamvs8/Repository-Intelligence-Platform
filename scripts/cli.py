"""Command-line access without the UI.

  python scripts/cli.py analyze https://github.com/spring-projects/spring-petclinic
  python scripts/cli.py ask <graph_id> "Which tables are touched by POST /owners/new?"
  python scripts/cli.py list
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.graph.store import get_store  # noqa: E402
from backend.kg.pipeline import analyze_repository  # noqa: E402
from backend.nlp.engine import AskEngine  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("analyze", "ask", "list"):
        print(__doc__)
        return 1
    store = get_store()
    if argv[1] == "list":
        for m in store.list_graphs():
            print(m["graph_id"], m["repo_key"], (m.get("commit") or "")[:10], m.get("analyzed_at"))
        return 0
    if argv[1] == "analyze":
        res = analyze_repository(argv[2], argv[3] if len(argv) > 3 else None,
                                 progress=lambda m, p: print(f"[{int(p * 100):3d}%] {m}", file=sys.stderr))
        store.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics, res.rejected)
        print(json.dumps({"graph_id": res.graph_id, **{k: res.report[k] for k in (
            "total_entities", "total_relationships", "apis_total", "apis_with_handler", "elapsed_seconds")}}, indent=1))
        return 0
    r = AskEngine(store, argv[2]).ask(" ".join(argv[3:]))
    print(f"[{r['status']}] intent={r['query_plan']['intent']} planner={r['planner']}\n")
    print(r["answer"])
    if r.get("explanation"):
        print("\n" + r["explanation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
