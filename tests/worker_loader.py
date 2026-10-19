"""Load fresh script modules without caching their environment or bytecode."""
import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path
from unittest import mock


SCRIPT = (Path(__file__).resolve().parents[1]
          / "plugins/codex-worker/skills/codex-worker/scripts/codex_task.py")
LOADER_PATH = Path(__file__).resolve()
# run_path works in isolated subprocesses, where tests is absent from sys.path.
WORKER_BOOTSTRAP = ("import runpy\nload_worker = runpy.run_path(" + repr(str(LOADER_PATH))
                    + ")[\"load_worker\"]\n")


@lru_cache(maxsize=None)
def script_code(script):
    # Reuse compilation, not module globals: every fixture needs independent
    # mutable queues and fresh constants derived from its own environment.
    return compile(script.read_bytes(), str(script), "exec", dont_inherit=True)


def load_worker(env=None, name="rotation_worker", *, clear=False, script=SCRIPT):
    """Execute a fresh module with the caller's existing environment semantics."""
    script = Path(script)
    with mock.patch.dict(os.environ, {} if env is None else env, clear=clear), \
            mock.patch.object(sys, "dont_write_bytecode", True), \
            mock.patch.object(sys, "path", [str(script.parent)] + sys.path):
        # Future package imports must see this fixture's environment as well.
        for module_name in tuple(sys.modules):
            if module_name == "codex_worker" or module_name.startswith("codex_worker."):
                del sys.modules[module_name]
        spec = importlib.util.spec_from_file_location(name, str(script))
        worker = importlib.util.module_from_spec(spec)
        exec(script_code(script), worker.__dict__)
    return worker
