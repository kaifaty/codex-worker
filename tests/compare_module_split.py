#!/usr/bin/env python3
"""Compare worker definitions across a module-only split without executing code.

Read the base entrypoint and package from Git, and compare them with the working
tree. Only references through imported worker modules are normalized; the named
attribute must be defined by that module in the same tree. Imports and the main
guard are reported separately as glue. Module docstrings are not binding units;
docstrings inside definitions remain part of the comparison.
"""

import argparse
import ast
from collections import defaultdict
import copy
from dataclasses import dataclass
import difflib
from pathlib import Path, PurePosixPath
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = "plugins/codex-worker/skills/codex-worker/scripts"
ENTRYPOINT = SCRIPTS + "/codex_task.py"
PACKAGE = SCRIPTS + "/codex_worker"
CATEGORIES = ("moved", "same", "changed", "missing", "duplicate", "new")


def _git(root, *args):
    return subprocess.run(
        ["git"] + list(args), cwd=str(root), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _is_worker_path(path):
    return (path == ENTRYPOINT
            or (str(PurePosixPath(path).parent) == PACKAGE
                and path.endswith(".py")))


def sources_at_revision(root, revision):
    # A fetch may move a symbolic ref between reads; pin the whole base tree.
    revision = _git(
        root, "rev-parse", "--verify", revision + "^{commit}",
    ).decode("ascii").strip()
    listing = _git(root, "ls-tree", "-r", "--name-only", "-z", revision,
                   "--", ENTRYPOINT, PACKAGE)
    paths = sorted(path.decode("utf-8") for path in listing.split(b"\0")
                   if path and _is_worker_path(path.decode("utf-8")))
    return {path: _git(root, "show", "--no-ext-diff", revision + ":" + path)
            for path in paths}


def working_sources(root):
    paths = [root / ENTRYPOINT] + sorted((root / PACKAGE).glob("*.py"))
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in paths if path.is_file()}


def _binding_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (node.name,)
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return tuple(sorted({child.id for target in targets
                             for child in ast.walk(target)
                             if isinstance(child, ast.Name)
                             and isinstance(child.ctx, ast.Store)}))
    return ()


def _is_main_guard(node):
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq) and len(test.comparators) == 1
            and ((isinstance(test.left, ast.Name) and test.left.id == "__name__"
                  and isinstance(test.comparators[0], ast.Constant)
                  and test.comparators[0].value == "__main__")
                 or (isinstance(test.left, ast.Constant)
                     and test.left.value == "__main__"
                     and isinstance(test.comparators[0], ast.Name)
                     and test.comparators[0].id == "__name__")))


def _module_name(path):
    if path == ENTRYPOINT:
        return "codex_task"
    stem = PurePosixPath(path).stem
    return "codex_worker" if stem == "__init__" else "codex_worker." + stem


def _module_imports(tree):
    """Yield imports at module scope, including those inside conditionals."""
    if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    if isinstance(tree, (ast.Import, ast.ImportFrom)):
        yield tree
    for child in ast.iter_child_nodes(tree):
        yield from _module_imports(child)


def _aliases(tree, modules):
    candidates = defaultdict(list)
    for node in _module_imports(tree):
        if isinstance(node, ast.ImportFrom):
            if not ((node.level == 1 and node.module is None)
                    or (node.level == 0 and node.module == "codex_worker")):
                continue
            for alias in node.names:
                target = "codex_worker." + alias.name
                if target in modules:
                    candidates[alias.asname or alias.name].append((target, alias))
        else:
            for alias in node.names:
                if (alias.asname and alias.name.startswith("codex_worker.")
                        and alias.name in modules):
                    candidates[alias.asname].append((alias.name, alias))

    # The layout checker forbids aliases being rebound in any scope. Be
    # conservative here too: a local argument or reassignment must not make a
    # behavioral change disappear through prefix normalization.
    bindings = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings[node.id].append(node)
        elif isinstance(node, ast.arg):
            bindings[node.arg].append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name].append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bindings[node.name].append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or (alias.name.split(".")[0]
                                        if isinstance(node, ast.Import) else alias.name)
                bindings[name].append(alias)
        elif type(node).__name__ in {"MatchAs", "MatchStar", "MatchMapping"}:
            name = getattr(node, "name", None) or getattr(node, "rest", None)
            if name:
                bindings[name].append(node)
    return {name: items[0][0] for name, items in candidates.items()
            if len(items) == 1 and bindings[name] == [items[0][1]]}


class _NormalizeModules(ast.NodeTransformer):
    def __init__(self, aliases, definitions):
        self.aliases = aliases
        self.definitions = definitions

    def visit_Attribute(self, node):
        # Inspect the original value so stripping one hop cannot accidentally
        # turn a longer attribute chain into another module reference.
        if isinstance(node.value, ast.Name):
            module = self.aliases.get(node.value.id)
            if module is not None and node.attr in self.definitions[module]:
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return self.generic_visit(node)


@dataclass
class _Definition:
    names: tuple
    path: str
    line: int
    node: ast.AST

    @property
    def location(self):
        return "%s:%s" % (self.path, self.line)


def collect_definitions(sources):
    trees = {path: ast.parse(source, filename=path)
             for path, source in sorted(sources.items())}
    modules = {_module_name(path): path for path in trees}
    module_definitions = {
        module: {name for node in trees[path].body for name in _binding_names(node)}
        for module, path in modules.items()
    }
    definitions = defaultdict(list)
    glue = []
    for path, tree in trees.items():
        normalizer = _NormalizeModules(_aliases(tree, modules), module_definitions)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)) or _is_main_guard(node):
                glue.append("%s:%s: %s" % (
                    path, node.lineno, ast.unparse(node).replace("\n", " ")))
                continue
            names = _binding_names(node)
            if names:
                normalized = normalizer.visit(copy.deepcopy(node))
                definitions[names].append(_Definition(names, path, node.lineno, normalized))
    return definitions, glue


def _entry(names, before, after):
    return {"names": list(names),
            "before": [definition.location for definition in before],
            "after": [definition.location for definition in after]}


def compare(root, revision, allow_changed=()):
    before, before_glue = collect_definitions(sources_at_revision(root, revision))
    after, after_glue = collect_definitions(working_sources(root))
    categories = {category: [] for category in CATEGORIES}
    before_names, after_names = defaultdict(list), defaultdict(list)
    for definitions, names in ((before, before_names), (after, after_names)):
        for units in definitions.values():
            for definition in units:
                for name in definition.names:
                    names[name].append(definition)
    duplicate_names = {name for name in before_names.keys() | after_names.keys()
                       if len(before_names[name]) > 1 or len(after_names[name]) > 1}
    for name in sorted(duplicate_names):
        categories["duplicate"].append(_entry(
            (name,), before_names[name], after_names[name]))

    for names in sorted(before.keys() | after.keys()):
        if duplicate_names.intersection(names):
            continue
        original, current = before.get(names, []), after.get(names, [])
        entry = _entry(names, original, current)
        if not current:
            category = "missing"
        elif not original:
            category = "new"
        elif ast.dump(original[0].node) == ast.dump(current[0].node):
            category = "same" if original[0].path == current[0].path else "moved"
        else:
            category = "changed"
            entry["diff"] = "".join(difflib.unified_diff(
                (ast.unparse(original[0].node) + "\n").splitlines(keepends=True),
                (ast.unparse(current[0].node) + "\n").splitlines(keepends=True),
                fromfile=original[0].location, tofile=current[0].location,
            ))
        categories[category].append(entry)
    allowed = set(allow_changed)
    return {
        "base": revision,
        "categories": categories,
        "glue": {"base": before_glue, "working": after_glue},
        "passed": not (categories["missing"] or categories["duplicate"]
                       or any(not set(entry["names"]).issubset(allowed)
                              for entry in categories["changed"])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", metavar="REV", required=True,
                        help="local Git revision to compare")
    parser.add_argument("--allow-changed", metavar="NAME", nargs="+", default=[],
                        help="allow changed definitions with these bound names")
    args = parser.parse_args()
    try:
        result = compare(ROOT, args.base, args.allow_changed)
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as error:
        parser.exit(2, "comparison failed: %s\n" % error)
    print("%s: %s" % ("PASS" if result["passed"] else "FAIL", "; ".join(
        "%s=%s" % (category, len(result["categories"][category]))
        for category in CATEGORIES)))
    print("category | name | from | to")
    for category in CATEGORIES:
        for entry in result["categories"][category]:
            print("%s | %s | %s | %s" % (
                category, ", ".join(entry["names"]),
                ", ".join(entry["before"]) or "-",
                ", ".join(entry["after"]) or "-"))
            if "diff" in entry:
                print(entry["diff"], end="")
    for tree, statements in result["glue"].items():
        print("Glue (%s):" % tree)
        for statement in statements:
            print("  " + statement)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
