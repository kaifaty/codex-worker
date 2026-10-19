"""Keep isolated test bootstraps ready for neighboring worker modules."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from rotation_telemetry import PARTIAL_WRITE_BOOTSTRAP


class WorkerLoaderTests(unittest.TestCase):
    def test_partial_write_bootstrap_loads_fresh_neighbor_package_without_bytecode(self):
        with tempfile.TemporaryDirectory(prefix="codex-worker-loader-") as directory:
            root = Path(directory)
            package = root / "codex_worker"
            package.mkdir()
            (package / "__init__.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "TASKS = Path(os.environ['CODEX_TASK_HOME']) / 'tasks'\n"
                "class PhaseTelemetry:\n"
                "    def __init__(self, state, phase):\n"
                "        self.path = TASKS / state['slug'] / 'finished'\n"
                "    def finish(self, **kwargs):\n"
                "        self.path.touch()\n", encoding="utf-8")
            script = root / "codex_task.py"
            script.write_text(
                "import codex_worker\n"
                "TASKS = codex_worker.TASKS\n"
                "PhaseTelemetry = codex_worker.PhaseTelemetry\n", encoding="utf-8")
            # A stale package must be evicted before importing the neighbor.
            bootstrap = ("import sys, types\n"
                         "sys.modules['codex_worker'] = types.ModuleType('codex_worker')\n"
                         + PARTIAL_WRITE_BOOTSTRAP)
            result = subprocess.run(
                [sys.executable, "-I", "-c", bootstrap, str(script),
                 json.dumps({"slug": "partial"})],
                cwd=root, env=dict(os.environ, HOME=directory, CODEX_TASK_HOME=directory),
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "tasks/partial/finished").exists())
            self.assertEqual(list(root.rglob("*.pyc")), [])


if __name__ == "__main__":
    unittest.main()
