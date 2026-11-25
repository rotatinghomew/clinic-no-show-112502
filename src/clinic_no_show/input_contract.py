"""读取合成资料并执行最小输入契约校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_FILES = ('source_info.json', 'appointments.json', 'attendance.json', 'reminders.json', 'slots.json', 'waitlist.json')
# 可选资料：缺失时由仓储层产出显式限制，而不是静默跳过。
OPTIONAL_FILES = ('identity_links.json', 'contact_preferences.json')


def load_bundle(root: str | Path) -> dict[str, Any]:
    """读取资料包，保留原始记录、缺失值和顺序。"""
    base = Path(root)
    bundle: dict[str, Any] = {}
    for name in (*REQUIRED_FILES, *OPTIONAL_FILES):
        path = base / name
        if not path.is_file():
            if name in REQUIRED_FILES:
                raise FileNotFoundError(path)
            continue  # 可选文件缺失：交由仓储层转成显式限制
        with path.open(encoding="utf-8") as handle:
            bundle[name] = json.load(handle)
    if bundle["source_info.json"].get("source_kind") != "synthetic":
        raise ValueError("资料包必须标明为 synthetic")
    return bundle
