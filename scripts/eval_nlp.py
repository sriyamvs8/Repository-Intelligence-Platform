"""NLP accuracy: intent accuracy + answer correctness on the labelled fixture questions.

Usage: python scripts/eval_nlp.py [dev|heldout|all]
An answer is correct when status == ANSWERED, the intent is one of the accepted intents and the expected
entity/text appears in the natural-language or developer answer."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ALLOW_LOCAL_PATHS", "true")
os.environ["GEMINI_API_KEY"] = ""

from backend.graph.store import InMemoryStore  # noqa: E402
from backend.kg.pipeline import analyze_repository  # noqa: E402
from backend.nlp.engine import AskEngine  # noqa: E402


def run(which: str = "all", verbose: bool = True, repo: str | None = None) -> dict:
    st = InMemoryStore(Path(tempfile.mkdtemp()))
    res = analyze_repository(repo or str(ROOT / "tests" / "fixtures" / "shop"))
    st.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics, res.rejected)
    eng = AskEngine(st, res.graph_id)
    out = {}
    for name in (["dev", "heldout"] if which == "all" else [which]):
        qs = json.loads((ROOT / "tests" / "nlp" / f"{name}_questions.json").read_text())
        ok_int = ok_ans = 0
        fails = []
        for item in qs:
            q, intents, expect = item[:3]
            want_status = item[3] if len(item) > 3 else "ANSWERED"
            r = eng.ask(q)
            text = " ".join([r.get("answer") or "", r.get("answer_nl") or "", r.get("answer_dev") or ""])
            it_ok = r["query_plan"]["intent"] in intents
            ans_ok = it_ok and r["status"] == want_status and (expect.lower() in text.lower() or
                                                                 any(expect.lower() in (c.get("name") or "").lower() for c in r.get("candidates", [])))
            ok_int += it_ok
            ok_ans += ans_ok
            if not ans_ok:
                fails.append({"q": q, "got_intent": r["query_plan"]["intent"], "want": intents, "status": r["status"],
                              "expect": expect, "answer": (r.get("answer") or "")[:160]})
        out[name] = {"questions": len(qs), "intent_accuracy": round(ok_int / len(qs), 3),
                     "answer_accuracy": round(ok_ans / len(qs), 3), "failures": fails}
        if verbose:
            print(f"{name}: intent {ok_int}/{len(qs)}  answer {ok_ans}/{len(qs)}")
            for f in fails:
                print("   FAIL", f)
    return out


if __name__ == "__main__":
    # python scripts/eval_nlp.py petclinic_blind <path-or-url-of-spring-petclinic>
    r = run(sys.argv[1] if len(sys.argv) > 1 else "all", repo=sys.argv[2] if len(sys.argv) > 2 else None)
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "nlp_results.json").write_text(json.dumps(r, indent=2))
