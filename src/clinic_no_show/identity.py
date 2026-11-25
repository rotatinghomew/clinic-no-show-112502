"""身份解析：只在授权关联范围内合并患者键。

- ``family_authorized`` 且 ``authorized=true`` 的关系参与连通分量合并；
- ``agent``（代理人代约）只说明登记来源，**不**合并身份；
- 没有关联资料时不做任何跨键合并，并保留数据集级限制。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import LINK_AGENT, IdentityLink, Limitation
from .repository import Repository


@dataclass
class IdentityResolution:
    group_of: dict[str, str]
    members: dict[str, frozenset[str]]
    limitations: list[Limitation] = field(default_factory=list)

    def group_for(self, patient_key: str | None) -> str | None:
        if patient_key is None:
            return None
        return self.group_of.get(patient_key, patient_key)


def resolve_identities(repo: Repository) -> IdentityResolution:
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    limitations: list[Limitation] = []
    agent_links: list[IdentityLink] = []

    for link in repo.identity_links:
        if not link.authorized:
            limitations.append(Limitation(
                code="unauthorized_link_ignored",
                message="未授权的身份关联已忽略，不能据此合并患者身份",
                refs=(link.patient_key, link.linked_key),
                severity="warning",
            ))
            continue
        if link.relation == LINK_AGENT:
            # 代理人登记不是身份关系：保留事实但不合并。
            agent_links.append(link)
            continue
        union(link.patient_key, link.linked_key)

    # 资料中出现、但没有任何关联的患者键也要进入并查集。
    for appt in repo.appointments:
        if appt.patient_key is not None:
            find(appt.patient_key)
    for link in repo.identity_links:
        find(link.patient_key)
        find(link.linked_key)

    groups: dict[str, set[str]] = {}
    for key in list(parent):
        groups.setdefault(find(key), set()).add(key)

    group_of: dict[str, str] = {}
    members: dict[str, frozenset[str]] = {}
    for index, (root, keys) in enumerate(sorted(groups.items()), start=1):
        group_id = f"ig-{index:03d}"
        members[group_id] = frozenset(sorted(keys))
        for key in keys:
            group_of[key] = group_id

    if agent_links:
        limitations.append(Limitation(
            code="agent_booking_not_merged",
            message="代理人/家属代约关系仅用于登记溯源，未参与身份合并",
            refs=tuple(sorted({lk.patient_key for lk in agent_links})),
            severity="info",
        ))
    if not repo.identity_links:
        limitations.append(Limitation(
            code="identity_merge_unavailable",
            message="无授权关联资料，所有患者键按独立身份处理",
            severity="warning",
        ))

    return IdentityResolution(group_of=group_of, members=members, limitations=limitations)
