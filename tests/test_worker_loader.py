"""Keep worker fixture imports independent of environment and process state."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_loader import load_worker


class WorkerLoaderTests(unittest.TestCase):
    def test_load_worker_refreshes_package_and_restores_process_state(self):
        with tempfile.TemporaryDirectory(prefix="teamster-loader-") as directory:
            root = Path(directory)
            package = root / "teamster"
            package.mkdir()
            (package / "__init__.py").write_text(
                "import os\n"
                "VALUE = os.environ['WORKER_LOADER_VALUE']\n", encoding="utf-8")
            script = root / "teamster.py"
            script.write_text(
                "import os\n"
                "import teamster\n"
                "VALUE = teamster.VALUE\n"
                "ENVIRONMENT = dict(os.environ)\n", encoding="utf-8")
            environment = dict(os.environ)
            path = list(sys.path)
            path_object = sys.path
            stale = types.ModuleType("teamster")
            stale.VALUE = "stale"
            # Restore any caller-owned worker modules after both fresh loads.
            with mock.patch.dict(sys.modules, {"teamster": stale}):
                first = load_worker({"WORKER_LOADER_VALUE": "first"},
                                    name="first_worker", clear=True, script=script)
                self.assertEqual(first.VALUE, "first")
                self.assertEqual(first.ENVIRONMENT, {"WORKER_LOADER_VALUE": "first"})
                self.assertIsNot(first.teamster, stale)
                self.assertEqual(dict(os.environ), environment)
                self.assertEqual(sys.path, path)
                self.assertIs(sys.path, path_object)

                second = load_worker({"WORKER_LOADER_VALUE": "second"},
                                     name="second_worker", script=script)
                self.assertEqual(second.VALUE, "second")
                self.assertEqual(second.ENVIRONMENT,
                                 dict(environment, WORKER_LOADER_VALUE="second"))
                self.assertIsNot(second.teamster, first.teamster)
                self.assertIs(sys.modules["teamster"], second.teamster)
                self.assertEqual(first.VALUE, "first")
                self.assertEqual(first.teamster.VALUE, "first")
                self.assertEqual(dict(os.environ), environment)
                self.assertEqual(sys.path, path)
                self.assertIs(sys.path, path_object)
            self.assertEqual(list(root.rglob("*.pyc")), [])


if __name__ == "__main__":
    unittest.main()
