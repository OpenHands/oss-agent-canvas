import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = [
    ROOT / "plugins/weekly-automation-health-review/scripts",
    ROOT / "automations/weekly-automation-conversation-health-review/tarball",
]


def load(path):
    spec = importlib.util.spec_from_file_location("safe_process_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProcessAuditTests(unittest.TestCase):
    def setUp(self):
        self.module = load(BUNDLES[0] / "safe_process_audit.py")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)

    def process(self, pid, *args):
        path = self.proc / str(pid)
        path.mkdir()
        (path / "cmdline").write_bytes(b"\0".join(args) + b"\0")

    def test_projects_flags_without_values_or_executable_names(self):
        self.process(23, b"/private/secret-service", b"--connection-token", b"fixture-private-value")
        self.process(27, b"secret-executable", b"--api_key=another-private-value")
        result = self.module.scan_processes(self.proc)
        self.assertEqual(result, {
            "status": "ok", "processes_scanned": 2, "processes_unreadable": 0,
            "credential_arguments": [
                {"pid": 23, "flags": ["connection-token"]},
                {"pid": 27, "flags": ["api-key"]},
            ],
        })
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret-executable", json.dumps(result))

    def test_noncredential_arguments_and_empty_processes_remain_safe(self):
        self.process(1, b"python3", b"main.py", b"--port", b"8000")
        self.process(2)
        result = self.module.scan_processes(self.proc)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["processes_scanned"], 2)
        self.assertEqual(result["credential_arguments"], [])

    def test_flag_presence_is_not_a_claim_about_credential_validity(self):
        self.process(1, b"server", b"--token", b"--password=", b"--token=duplicate")
        result = self.module.scan_processes(self.proc)
        self.assertEqual(result["credential_arguments"], [
            {"pid": 1, "flags": ["password", "token"]},
        ])

    def test_unreadable_or_disappeared_process_is_visible_without_exception_text(self):
        (self.proc / "123").mkdir()
        result = self.module.scan_processes(self.proc)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["processes_unreadable"], 1)
        self.assertEqual(result["credential_arguments"], [])
        self.assertNotIn(str(self.proc), json.dumps(result))

    def test_empty_procfs_is_unavailable_not_clean(self):
        self.assertEqual(self.module.scan_processes(self.proc)["status"], "unavailable")

    def test_missing_procfs_is_unavailable_not_clean(self):
        self.assertEqual(self.module.scan_processes(self.proc / "missing")["status"], "unavailable")

    def test_oversized_process_is_partial_without_echoing_arguments(self):
        self.process(1, b"private-value" * self.module.MAX_CMDLINE_BYTES)
        result = self.module.scan_processes(self.proc)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["processes_unreadable"], 1)
        self.assertEqual(result["credential_arguments"], [])

    def test_invalid_bytes_and_unknown_flags_never_reach_output(self):
        self.process(1, b"\xff-private", b"--unknown=private-value", b"--client-secret=\xff-private")
        result = self.module.scan_processes(self.proc)
        self.assertEqual(result["credential_arguments"], [{"pid": 1, "flags": ["client-secret"]}])
        self.assertNotIn("private", json.dumps(result))

    def test_packaged_entrypoints_emit_only_the_safe_schema(self):
        for bundle in BUNDLES:
            with self.subTest(bundle=bundle):
                result = subprocess.run([sys.executable, str(bundle / "safe_process_audit.py")], capture_output=True, text=True, check=True)
                self.assertEqual(result.stderr, "")
                self.assertTrue(self.module.valid_inventory(json.loads(result.stdout)))

    def test_remote_command_runs_self_contained_source(self):
        command = self.module.audit_command()
        result = subprocess.run(command, shell=True, capture_output=True, text=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertTrue(self.module.valid_inventory(json.loads(result.stdout)))

    def test_invalid_inventory_cannot_be_forwarded_to_the_prompt(self):
        result = self.module.scan_processes(self.proc)
        result["unexpected"] = "private-value"
        self.assertFalse(self.module.valid_inventory(result))
        del result["unexpected"]
        result["credential_arguments"] = [{"pid": 1, "flags": ["private-value"]}]
        self.assertFalse(self.module.valid_inventory(result))

    def test_plugin_and_legacy_copies_stay_aligned(self):
        for name in ["safe_process_audit.py", "main.py", "prompt.txt"]:
            self.assertEqual((BUNDLES[0] / name).read_bytes(), (BUNDLES[1] / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
