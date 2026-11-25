"""门诊爽约的可解释分层与号源干预。

模块分工：
- input_contract/repository：原始资料读取与结构化，保留缺失语义；
- identity：授权范围内的身份合并；
- timeline：事实事件时间与可见时间，支撑 as-of 决策；
- attendance：取消/改期/离线/补录/无签到的显式出勤判定；
- risk：可解释因子的风险分层，证据不足输出限制；
- intervention/scheduling：干预动作、号源保护、候补调度与重排；
- segments/strategy：人群时段画像与提醒策略周期对比；
- report/cli：负责人视角综合报告与命令行。
"""

from .input_contract import load_bundle
from .repository import build_repository
from .report import build_report

__all__ = ["load_bundle", "build_repository", "build_report"]
