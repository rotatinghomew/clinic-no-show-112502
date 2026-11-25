"""命令行入口：输出综合报告 JSON。

用法：
    python -m clinic_no_show.cli --data data --as-of 2026-10-12T08:00
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .report import build_report
from .time_utils import parse_ts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="门诊爽约可解释分层与号源干预报告")
    parser.add_argument("--data", required=True, help="资料包目录")
    parser.add_argument("--as-of", default=None, help="决策时刻 ISO8601，默认取最后可见事实之后")
    parser.add_argument("--output", default=None, help="输出文件，默认打印到标准输出")
    args = parser.parse_args(argv)

    as_of = parse_ts(args.as_of) if args.as_of else None
    report = build_report(Path(args.data), as_of=as_of)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
