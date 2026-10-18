#!/usr/bin/env python3
"""Repeat reset synchronization tests with concurrent suite or CPU load.

Run with /usr/bin/python3 -I tests/stress_rotation.py --repetitions 20 --jobs 4.
The selected methods are discovered by following calls to reset_after_ready
through test helpers, rather than maintained as a separate list. Both runners
use the current interpreter.
Use --selection timing to also select CODEX_TASK_MINUTE_SECONDS test methods.
Use --cpu-load --load-jobs 10 for ten CPU-bound processes instead of full suites.
"""
import argparse
import ast
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest


SCRIPT = Path(__file__).resolve()
TEST_SCRIPT = SCRIPT.with_name("test_rotation.py")
RUNNER_SCRIPT = SCRIPT.with_name("rotation_runner.py")
_suite_module = None
_suite_names = []
_suite_repetitions = 0


class _CallGraph:
    """Resolve calls within test sources without importing helper modules."""

    def __init__(self, sources):
        self.functions, self.classes, self.symbols = {}, {}, {}
        for module, tree in sources.items():
            self._index(tree, (module,))
        self.edges = {}
        self.reset, self.timing = set(), set()
        for key, function in self.functions.items():
            nodes = list(self._body_nodes(function))
            self.edges[key] = {target for node in nodes if isinstance(node, ast.Call)
                               for target in [self._resolve(node.func, key)]
                               if target in self.functions}
            if function.name == "reset_after_ready" or any(
                    isinstance(node, ast.Call) and
                    ((isinstance(node.func, ast.Attribute) and node.func.attr == "reset_after_ready")
                     or (isinstance(node.func, ast.Name) and node.func.id == "reset_after_ready"))
                    for node in nodes):
                self.reset.add(key)
            if any((isinstance(node, ast.Constant) and node.value == "CODEX_TASK_MINUTE_SECONDS")
                   or (isinstance(node, ast.keyword) and node.arg == "CODEX_TASK_MINUTE_SECONDS")
                   for node in nodes):
                self.timing.add(key)

    def _index(self, node, scope):
        symbols = self.symbols.setdefault(scope, {})
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                key = scope + (child.name,)
                symbols[child.name] = key
                if isinstance(child, ast.ClassDef):
                    self.classes[key] = child.bases
                else:
                    self.functions[key] = child
                self._index(child, key)
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    name = alias.name if alias.asname else alias.name.split(".")[0]
                    symbols[alias.asname or name] = (name,)
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name != "*":
                        module = child.module or ""
                        if child.level:
                            parent = scope[0].split(".")[:-child.level]
                            module = ".".join(parent + ([module] if module else []))
                        symbols[alias.asname or alias.name] = (module, alias.name)
            else:
                self._index(child, scope)

    def _body_nodes(self, node):
        # A nested helper participates only when it is called.
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield child
                yield from self._body_nodes(child)

    def _canonical(self, target):
        seen = set()
        while target is not None and target not in seen:
            seen.add(target)
            resolved = self.symbols.get(target[:-1], {}).get(target[-1])
            if resolved is None or resolved == target:
                break
            target = resolved
        return target

    def _member(self, owner, name, seen=None):
        seen = set() if seen is None else seen
        if owner in seen:
            return None
        seen.add(owner)
        member = self.symbols.get(owner, {}).get(name)
        if member is not None:
            return self._canonical(member)
        for base in self.classes.get(owner, ()):
            parent = self._resolve(base, owner[:-1])
            member = self._member(parent, name, seen)
            if member is not None:
                return member
        return None

    def _resolve(self, node, scope):
        if isinstance(node, ast.Name):
            if node.id in ("self", "cls"):
                return next((scope[:i] for i in range(len(scope), 0, -1)
                             if scope[:i] in self.classes), None)
            for i in range(len(scope), 0, -1):
                outer = scope[:i]
                if outer in self.classes:
                    continue
                target = self.symbols.get(outer, {}).get(node.id)
                if target is not None:
                    return self._canonical(target)
        elif isinstance(node, ast.Attribute):
            owner = self._resolve(node.value, scope)
            member = self._member(owner, node.attr)
            if member is not None:
                return member
            if owner is not None:
                return (".".join(owner), node.attr)
        elif isinstance(node, ast.Call):
            # A direct constructor such as Helper().reset() has a known class.
            target = self._resolve(node.func, scope)
            if target in self.classes:
                return target
        return None

    def reaches(self, start, seeds):
        pending, seen = [start], set()
        while pending:
            key = pending.pop()
            if key in seeds:
                return True
            if key not in seen:
                seen.add(key)
                pending.extend(self.edges.get(key, ()))
        return False


def select_tests(modules, directory, selection="reset"):
    """Find tests transitively using reset or minute-budget helpers in tests/."""
    directory = Path(directory)
    sources = {".".join(path.relative_to(directory).with_suffix("").parts):
               ast.parse(path.read_text()) for path in sorted(directory.rglob("*.py"))}
    graph = _CallGraph(sources)
    seeds = graph.reset | (graph.timing if selection == "timing" else set())
    names = []
    for module in modules:
        module_name = ".".join(Path(module.__file__).relative_to(directory).with_suffix("").parts)
        for cls in sources[module_name].body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for method in cls.body:
                if (isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and method.name.startswith("test_")
                        and graph.reaches((module_name, cls.name, method.name), seeds)):
                    names.append("RotationTests." + method.name)
    return names


def reset_tests(selection="reset"):
    modules = runpy.run_path(str(TEST_SCRIPT), run_name="rotation_tests")["TEST_MODULES"]
    return select_tests(modules, SCRIPT.parent, selection)


def load_tests(loader, tests, pattern):
    # The one-worker path also uses this hook through standard unittest.main.
    return unittest.TestSuite(loader.loadTestsFromName(name, _suite_module)
                              for _ in range(_suite_repetitions) for name in _suite_names)


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def run_stress(args, names):
    global _suite_module, _suite_names, _suite_repetitions
    _suite_module = types.SimpleNamespace(**runpy.run_path(str(TEST_SCRIPT), run_name="rotation_tests"))
    _suite_module.load_tests = load_tests
    _suite_names, _suite_repetitions = names, args.repetitions
    os.environ["CODEX_TEST_JOBS"] = str(args.jobs)
    sys.argv = [str(SCRIPT)]
    runpy.run_path(str(RUNNER_SCRIPT))["run"](_suite_module)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=positive, default=20)
    parser.add_argument("--jobs", type=positive, default=os.cpu_count() or 1)
    parser.add_argument("--selection", choices=("reset", "timing"), default="reset",
                        help="reset helper users, or their union with minute-budget tests")
    parser.add_argument("--load-jobs", type=positive, default=20,
                        help="full-suite workers, or CPU processes with --cpu-load")
    parser.add_argument("--cpu-load", action="store_true",
                        help="use CPU-bound processes instead of background full suites")
    parser.add_argument("--run-stress", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    names = reset_tests(args.selection)
    if not names:
        parser.error("no tests match --selection %s" % args.selection)
    if args.run_stress:
        run_stress(args, names)
        return

    print("Stress selection: %d tests x %d repetitions = %d runs; %d workers" %
          (len(names), args.repetitions, len(names) * args.repetitions, args.jobs), flush=True)
    for name in names:
        print("  " + name, flush=True)
    helper = runpy.run_path(str(RUNNER_SCRIPT))
    root = tempfile.mkdtemp(prefix="codex-rotation-stress-")
    watcher = stress = load = None
    burners = []
    load_log = None
    completed, failed = 0, 0
    started = time.monotonic()
    exitcode = 1

    def finish_load():
        nonlocal completed, failed, load_log
        completed += 1
        load_log.close()
        print("Full-suite load #%d: exit=%d" % (completed, load.returncode), flush=True)
        if load.returncode:
            failed += 1
            print(Path(load_log.name).read_text(errors="replace"), file=sys.stderr, end="", flush=True)
        load_log = None

    def start_load():
        nonlocal load, load_log
        directory = Path(root) / ("load-%d" % completed)
        directory.mkdir()
        load_log = (directory / "output.log").open("w")
        env = dict(os.environ, TMPDIR=str(directory), CODEX_TEST_JOBS=str(args.load_jobs))
        load = subprocess.Popen([sys.executable, "-I", str(TEST_SCRIPT)], env=env,
                                stdout=load_log, stderr=subprocess.STDOUT, start_new_session=True)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    def check_burners():
        for index, process in enumerate(burners):
            if process.poll() is not None:
                raise RuntimeError("CPU load process %d exited early (exit %s)" %
                                   (index, process.returncode))

    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        # Closing this pipe also cleans up detached descendants after SIGKILL.
        watcher = subprocess.Popen([sys.executable, "-I", str(RUNNER_SCRIPT), "--watch", root],
                                   stdin=subprocess.PIPE, start_new_session=True)
        if args.cpu_load:
            for index in range(args.load_jobs):
                directory = Path(root) / ("cpu-%d" % index)
                directory.mkdir()
                burners.append(subprocess.Popen(
                    [sys.executable, "-I", "-c", "while True:\n    pass\n"],
                    env=dict(os.environ, TMPDIR=str(directory)),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True))
            check_burners()
            print("CPU load: %d processes" % len(burners), flush=True)
        else:
            start_load()
        directory = Path(root) / "stress"
        directory.mkdir()
        stress = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "--run-stress", "--repetitions",
             str(args.repetitions), "--jobs", str(args.jobs), "--selection", args.selection],
            env=dict(os.environ, TMPDIR=str(directory)), start_new_session=True)
        while stress.poll() is None:
            if watcher.poll() is not None:
                raise RuntimeError("stress cleanup watchdog exited unexpectedly")
            if args.cpu_load:
                check_burners()
            elif load.poll() is not None:
                finish_load()
                start_load()
            time.sleep(0.05)
        if args.cpu_load:
            check_burners()
            load_summary = "CPU load: %d processes, 0 early exits" % len(burners)
        else:
            # Finish the last load suite so an already-failing background test is
            # never hidden by cancelling that suite as soon as stress completes.
            while load.poll() is None:
                if watcher.poll() is not None:
                    raise RuntimeError("stress cleanup watchdog exited unexpectedly")
                time.sleep(0.05)
            finish_load()
            load_summary = "full-suite load: %d completed, %d failed" % (completed, failed)
        exitcode = 0 if stress.returncode == 0 and failed == 0 else 1
        print("Stress: %d scheduled runs, exit=%d; %s; %.3fs" %
              (len(names) * args.repetitions, stress.returncode, load_summary,
               time.monotonic() - started), flush=True)
    except KeyboardInterrupt:
        print("Stress run interrupted", file=sys.stderr, flush=True)
        exitcode = 130
    finally:
        # The watchdog also handles interruption and failed controller startup.
        if watcher is not None:
            watcher.stdin.close()
            try:
                if watcher.wait(timeout=10) != 0:
                    helper["_cleanup"](root)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.wait()
                helper["_cleanup"](root)
        else:
            helper["_cleanup"](root)
        for process in [stress, load] + burners:
            if process is not None:
                process.wait()
        if load_log is not None:
            load_log.close()
        shutil.rmtree(root, ignore_errors=True)
        signal.signal(signal.SIGTERM, previous_term)
    sys.exit(exitcode)


if __name__ == "__main__":
    main()
