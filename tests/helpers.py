"""测试公用辅助。"""

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinic_no_show.identity import resolve_identities  # noqa: E402
from clinic_no_show.repository import build_repository  # noqa: E402
from clinic_no_show.timeline import build_timeline  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "scenario_a"
DATA = ROOT / "data"


def build_context(root=FIXTURE):
    repo = build_repository(str(root))
    identities = resolve_identities(repo)
    timeline = build_timeline(repo)
    return repo, identities, timeline


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)
