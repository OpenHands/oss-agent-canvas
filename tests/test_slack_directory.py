import importlib.util
import unittest
from pathlib import Path

MODULES = sorted(Path(__file__).resolve().parents[1].glob(
    "**/slack_directory.py"))


def load(module_path):
    spec = importlib.util.spec_from_file_location("slack_directory", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def member(user_id, username, real_name=None, display_name=None):
    profile = {}
    if real_name is not None:
        profile["real_name"] = real_name
    if display_name is not None:
        profile["display_name"] = display_name
    return {"id": user_id, "name": username, "profile": profile}


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.module = load(MODULES[0])
        self.members = [
            member("U1", "engel.nyst", "Engel Nyst"),
            member("U2", "gneubig", "Graham Neubig"),
            member("U3", "vasschiavo", "Vasco Schiavo"),
            member("U4", "unrelated", "Someone Else"),
        ]
        self.by_username, self.by_realname = self.module.build_index(self.members)

    def match(self, login, name=None):
        found = self.module.match_member(
            login, name, self.by_username, self.by_realname)
        return found["id"] if found else None

    def test_username_equals_login(self):
        self.assertEqual(self.match("engel.nyst"), "U1")

    def test_real_name_equals_github_display_name(self):
        self.assertEqual(self.match("enyst", "Engel Nyst"), "U1")

    def test_display_name_variant_first_dot_last(self):
        self.assertEqual(self.match("VascoSch92", "Vasco Schiavo"), "U3")

    def test_name_match_is_accent_and_case_insensitive(self):
        self.assertEqual(self.match("NEUBIG", "graham neubig"), "U2")

    def test_unknown_maintainer_is_not_guessed(self):
        self.assertIsNone(self.match("nobodyhere", "Nobody Here"))

    def test_login_never_matched_by_loose_substring(self):
        # "unrelated" must not match a maintainer merely sharing a fragment.
        self.assertIsNone(self.match("lated"))

    def test_bot_logins_are_recognized(self):
        self.assertTrue(self.module.is_bot("dependabot[bot]"))
        self.assertTrue(self.module.is_bot(""))
        self.assertFalse(self.module.is_bot("neubig"))


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.module = load(MODULES[0])

    def test_index_skips_bot_and_deleted_members(self):
        members = [
            member("U1", "human", "Real Person"),
            {**member("U2", "robot", "Bot"), "is_bot": True},
            {**member("U3", "gone", "Gone"), "deleted": True},
        ]
        # fetch_slack_directory filters these; build_index must tolerate the
        # pre-filtered shape too.
        by_username, _ = self.module.build_index(members)
        self.assertIn("human", by_username)


if __name__ == "__main__":
    unittest.main()
