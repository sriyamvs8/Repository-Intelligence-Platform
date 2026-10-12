import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ALLOW_LOCAL_PATHS"] = "true"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rkg-test-")
os.environ.setdefault("GRAPH_BACKEND", "in_memory")
os.environ["GEMINI_API_KEY"] = os.environ.get("RKG_TEST_GEMINI_KEY", "")   # tests are deterministic by default
os.environ["RATE_LIMIT_ASK_PER_MIN"] = "1000"
os.environ["RATE_LIMIT_ANALYZE_PER_MIN"] = "1000"
FIXTURE = str(ROOT / "tests" / "fixtures" / "shop")
