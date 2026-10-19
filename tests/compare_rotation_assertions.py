#!/usr/bin/env python3
"""Compare rotation test names and assertion ASTs with a local Git revision.

Run with /usr/bin/python3 -I tests/compare_rotation_assertions.py. The default
base revision is origin/main. No tests are imported or executed.
Assertion comparisons cover changed tests and existing helpers, retain every
argument and ignore AST locations. --strip-module-hops also ignores package
module hops at the known loaded-worker expressions during a module split.
"""

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"rotation_runner.py", "stress_rotation.py"}
# Loaded-script expressions in rotation_*.py: local fixtures use worker,
# PromptTests stores self.worker, and RotationTestCase.worker() returns a module.
# Keep this explicit so fixture methods such as self.state(slug) stay untouched.
WORKER_EXPRESSIONS = ("worker", "self.worker", "self.worker()")
WORKER_ASTS = frozenset(
    ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)
    for expression in WORKER_EXPRESSIONS
)
SCRIPTS = ROOT / "plugins/codex-worker/skills/codex-worker/scripts"


class StripModuleHops(ast.NodeTransformer):
    def __init__(self, modules):
        self.modules = modules

    def visit_Attribute(self, node):
        worker_hop = (node.attr in self.modules
                      and ast.dump(node.value, include_attributes=False) in WORKER_ASTS)
        self.generic_visit(node)
        if worker_hop:
            replacement = node.value
            if isinstance(replacement, (ast.Name, ast.Attribute)):
                replacement.ctx = node.ctx
            return ast.copy_location(replacement, node)
        return node


def git_show(spec):
    return subprocess.run(
        ["git", "show", "--no-ext-diff", spec],
        cwd=str(ROOT), check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
    ).stdout


def is_test_file(name):
    return (name.startswith("rotation_") and name.endswith(".py")
            and name not in EXCLUDED)


def sources_at_revision(revision):
    # Reading the tree as well as its files catches deleted or renamed modules.
    listing = git_show(revision + ":tests")
    return {
        "tests/" + name: git_show(revision + ":tests/" + name)
        for name in listing.splitlines() if is_test_file(name)
    }


def working_sources():
    return {
        "tests/" + path.name: path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "tests").glob("rotation_*.py"))
        if is_test_file(path.name)
    }


def assertion_calls(node):
    calls = Counter()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        name = (function.attr if isinstance(function, ast.Attribute)
                else function.id if isinstance(function, ast.Name) else "")
        if name.startswith("assert"):
            calls[ast.dump(child, include_attributes=False)] += 1
    return calls


def collect_definitions(sources, modules=()):
    tests = {}
    helpers = {}
    for path, source in sorted(sources.items()):
        class Definitions(ast.NodeVisitor):
            def __init__(self):
                self.scope = []

            def visit_ClassDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_FunctionDef(self, node):
                body = ast.Module(body=node.body, type_ignores=[])
                definition = {
                    "source": "{}:{}".format(path, node.lineno),
                    "body": ast.dump(body, include_attributes=False),
                    "assertions": assertion_calls(node),
                }
                if node.name.startswith("test_"):
                    if node.name in tests:
                        raise ValueError("duplicate test name: " + node.name)
                    tests[node.name] = definition
                else:
                    qualified_name = "{}:{}".format(
                        path, ".".join(self.scope + [node.name]))
                    if qualified_name in helpers:
                        raise ValueError("duplicate helper name: " + qualified_name)
                    helpers[qualified_name] = definition
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

        tree = ast.parse(source, filename=path)
        if modules:
            tree = StripModuleHops(modules).visit(tree)
        Definitions().visit(tree)
    return tests, helpers


def counter_items(counter):
    return [{"ast": value, "count": count}
            for value, count in sorted(counter.items())]


def changed_definitions(before, after, require_assertions=False):
    changed = []
    for name in sorted(before.keys() & after.keys()):
        original, current = before[name], after[name]
        if original["body"] == current["body"]:
            continue
        if (require_assertions
                and not original["assertions"] and not current["assertions"]):
            continue
        removed = original["assertions"] - current["assertions"]
        added = current["assertions"] - original["assertions"]
        changed.append({
            "name": name,
            "before_source": original["source"],
            "after_source": current["source"],
            "before_assertions": sum(original["assertions"].values()),
            "after_assertions": sum(current["assertions"].values()),
            "removed_assertions": counter_items(removed),
            "added_assertions": counter_items(added),
        })
    return changed


def compare(revision, strip_module_hops=False):
    modules = ({path.stem for path in (SCRIPTS / "codex_worker").glob("*.py")}
               if strip_module_hops else set())
    before, before_helpers = collect_definitions(sources_at_revision(revision), modules)
    after, after_helpers = collect_definitions(working_sources(), modules)
    changed = changed_definitions(before, after)
    changed_helpers = changed_definitions(
        before_helpers, after_helpers, require_assertions=True)
    result = {
        "baseline": revision,
        "before_test_count": len(before),
        "after_test_count": len(after),
        "removed_tests": sorted(before.keys() - after.keys()),
        "added_tests": sorted(after.keys() - before.keys()),
        "changed_tests": changed,
        "changed_helpers": changed_helpers,
    }
    result["passed"] = not (
        result["removed_tests"] or result["added_tests"]
        or any(test["removed_assertions"] or test["added_assertions"]
               for test in changed + changed_helpers)
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", metavar="REV", default="origin/main",
                        help="local Git revision to compare (default: %(default)s)")
    parser.add_argument("--json", action="store_true",
                        help="emit the full comparison as JSON")
    parser.add_argument("--strip-module-hops", action="store_true",
                        help="ignore worker.<package module> hops in test bodies")
    args = parser.parse_args()
    try:
        result = compare(args.base, strip_module_hops=args.strip_module_hops)
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as error:
        parser.exit(2, "comparison failed: {}\n".format(error))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("{}: {} -> {} tests; {} changed test bodies; {} changed helpers".format(
            "PASS" if result["passed"] else "FAIL",
            result["before_test_count"], result["after_test_count"],
            len(result["changed_tests"]), len(result["changed_helpers"])))
        for name in result["removed_tests"]:
            print("Removed test: " + name)
        for name in result["added_tests"]:
            print("Added test: " + name)
        for test in result["changed_tests"] + result["changed_helpers"]:
            print("{}: assert* calls {} -> {}".format(
                test["name"], test["before_assertions"], test["after_assertions"]))
            for action in ("removed", "added"):
                for difference in test[action + "_assertions"]:
                    print("  {} {} x {}".format(
                        action, difference["count"], difference["ast"]))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
