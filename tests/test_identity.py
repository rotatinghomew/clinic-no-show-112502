"""身份合并：只在授权关联范围内，代理人关系不合并。"""

import unittest

from helpers import build_context
from clinic_no_show.models import LINK_AGENT


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.identities, _ = build_context()

    def test_authorized_family_links_merge(self):
        g1 = self.identities.group_for("p-1")
        g1b = self.identities.group_for("p-1b")
        self.assertIsNotNone(g1)
        self.assertEqual(g1, g1b)
        self.assertEqual(self.identities.members[g1], frozenset({"p-1", "p-1b"}))

    def test_agent_relation_does_not_merge(self):
        # p-3 的代理人关联只用于登记溯源，p-3b 保持独立身份。
        self.assertNotEqual(
            self.identities.group_for("p-3"),
            self.identities.group_for("p-3b"),
        )
        codes = {l.code for l in self.identities.limitations}
        self.assertIn("agent_booking_not_merged", codes)

    def test_unauthorized_link_ignored(self):
        self.assertNotEqual(
            self.identities.group_for("p-4"),
            self.identities.group_for("p-4x"),
        )
        codes = {l.code for l in self.identities.limitations}
        self.assertIn("unauthorized_link_ignored", codes)

    def test_unknown_key_returns_itself_and_none_for_missing(self):
        self.assertEqual(self.identities.group_for("p-999"), "p-999")
        self.assertIsNone(self.identities.group_for(None))

    def test_agent_link_constant(self):
        link = next(l for l in self.repo.identity_links
                    if l.patient_key == "p-3" and l.linked_key == "p-3b")
        self.assertEqual(link.relation, LINK_AGENT)


if __name__ == "__main__":
    unittest.main()
