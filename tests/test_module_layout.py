"""Guard the module boundaries before and during the worker package split."""
import ast
import builtins
from pathlib import Path
import symtable
import tempfile
import unittest


SCRIPTS = (Path(__file__).resolve().parents[1]
           / "plugins/codex-worker/skills/codex-worker/scripts")


class _Bindings(ast.NodeVisitor):
    """Collect module bindings without treating nested scopes as module code."""

    def __init__(self):
        self.names = {}
        self.nonimports = {}

    def bind(self, name, node, imported=False):
        self.names.setdefault(name, node.lineno)
        if not imported:
            self.nonimports.setdefault(name, node.lineno)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.bind(node.id, node)

    def visit_Import(self, node):
        for alias in node.names:
            self.bind(alias.asname or alias.name.split(".")[0], node, imported=True)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.bind(alias.asname or alias.name, node, imported=True)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bind(node.name, node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.bind(node.name, node)
        for value in _function_outer_values(node):
            self.visit(value)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.bind(node.name, node)
        for value in node.decorator_list + node.bases:
            self.visit(value)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node):
        for value in node.args.defaults + node.args.kw_defaults:
            if value is not None:
                self.visit(value)

    def visit_comprehension(self, node):
        # Comprehension targets are local; a walrus in an expression can bind
        # a name in the enclosing module, so keep visiting the expressions.
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)


def _function_outer_values(node):
    args = node.args
    values = list(node.decorator_list) + args.defaults + args.kw_defaults
    values += [arg.annotation for arg in args.posonlyargs + args.args + args.kwonlyargs]
    values += [args.vararg.annotation if args.vararg else None,
               args.kwarg.annotation if args.kwarg else None, node.returns]
    return [value for value in values if value is not None]


class _Reads(ast.NodeVisitor):
    """Locate reads in the scopes identified by Python's symbol table."""

    def __init__(self, table):
        self.table = table
        self.tables = []
        self.children = {}
        self.positions = {}
        self.used = set()
        self.annotations = {}
        self.class_name = None
        self.class_scopes = {}
        self.comprehension_locals = []
        self.class_comprehension_reads = {}
        self._index(table)

    def _index(self, table):
        self.tables.append(table)
        children = table.get_children()
        self.children[id(table)] = children
        self.positions[id(table)] = {}
        for child in children:
            self._index(child)

    def _child(self, kind, name, line):
        for child in self.children[id(self.table)]:
            if (id(child) not in self.used and child.get_type() == kind
                    and child.get_name() == name and child.get_lineno() == line):
                self.used.add(id(child))
                return child
        return None

    def _visit_in(self, table, nodes):
        previous = self.table
        self.table = table or previous
        for node in nodes:
            if node is not None:
                self.visit(node)
        self.table = previous

    def _annotation(self, line, nodes, shared=False):
        # Python 3.14 gives deferred annotations their own symbol tables;
        # earlier supported Pythons evaluate them in the enclosing scope.
        annotation = self.annotations.get(id(self.table)) if shared else None
        if annotation is None:
            annotation = self._child("annotation", "__annotate__", line)
            if shared and annotation is not None:
                self.annotations[id(self.table)] = annotation
        self._visit_in(annotation, nodes)

    def _name(self, name):
        if (self.class_name and name.startswith("__") and not name.endswith("__")
                and self.class_name.lstrip("_")):
            name = "_" + self.class_name.lstrip("_") + name
        return name

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            name = self._name(node.id)
            self.positions[id(self.table)].setdefault(name, node.lineno)
            if self.comprehension_locals and self.table is self.comprehension_locals[-1][0]:
                local = any(name in names for table, names in self.comprehension_locals
                            if table is self.table)
                if not local:
                    self.class_comprehension_reads.setdefault(id(self.table), {}).setdefault(name, node.lineno)

    def visit_FunctionDef(self, node):
        args = node.args
        for value in node.decorator_list + args.defaults + args.kw_defaults:
            if value is not None:
                self.visit(value)
        annotations = [arg.annotation for arg in args.posonlyargs + args.args + args.kwonlyargs]
        annotations += [args.vararg.annotation if args.vararg else None,
                        args.kwarg.annotation if args.kwarg else None, node.returns]
        self._annotation(node.lineno, annotations)
        self._visit_in(self._child("function", node.name, node.lineno), node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for value in node.decorator_list + node.bases:
            self.visit(value)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous_class = self.class_name
        self.class_name = node.name
        table = self._child("class", node.name, node.lineno)
        if table is not None:
            self.class_scopes[(node.lineno, node.col_offset)] = id(table)
        self._visit_in(table, node.body)
        self.class_name = previous_class

    def visit_AnnAssign(self, node):
        self.visit(node.target)
        self._annotation(node.lineno, [node.annotation], shared=True)
        if node.value is not None:
            self.visit(node.value)

    def visit_Lambda(self, node):
        for value in node.args.defaults + node.args.kw_defaults:
            if value is not None:
                self.visit(value)
        self._visit_in(self._child("function", "lambda", node.lineno), [node.body])

    def _comprehension(self, node, name, values):
        first, *rest = node.generators
        self.visit(first.iter)
        inner = [first.target] + first.ifs
        for generator in rest:
            inner += [generator.iter, generator.target] + generator.ifs
        table = self._child("function", name, node.lineno)
        inlined_class = table is None and self.table.get_type() == "class"
        if inlined_class:
            targets = {self._name(target.id) for generator in node.generators
                       for target in ast.walk(generator.target) if isinstance(target, ast.Name)}
            self.comprehension_locals.append((self.table, targets))
        self._visit_in(table, inner + values)
        if inlined_class:
            self.comprehension_locals.pop()

    def visit_ListComp(self, node):
        self._comprehension(node, "listcomp", [node.elt])

    def visit_SetComp(self, node):
        self._comprehension(node, "setcomp", [node.elt])

    def visit_DictComp(self, node):
        self._comprehension(node, "dictcomp", [node.key, node.value])

    def visit_GeneratorExp(self, node):
        self._comprehension(node, "genexpr", [node.elt])

    def class_comprehension_globals(self, source):
        # Python 3.12+ inlines list/set/dict comprehensions and merges their
        # symbols into the class table, even though their bodies skip class
        # bindings. Ask symtable about equivalent method reads to retain the
        # distinction between globals and captures from enclosing functions.
        if not self.class_comprehension_reads:
            return {}
        tree = ast.parse(source)
        used_names = {symbol.get_name() for table in self.tables for symbol in table.get_symbols()}
        probes = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            scope = self.class_scopes.get((node.lineno, node.col_offset))
            positions = self.class_comprehension_reads.get(scope)
            if not positions:
                continue
            name = "_layout_comprehension_probe"
            while name in used_names:
                name += "_"
            used_names.add(name)
            node.body.append(ast.parse("def %s():\n    return %s\n"
                                       % (name, ", ".join(positions))).body[0])
            probes[name] = (scope, positions)
        table = symtable.symtable(ast.unparse(tree), "<class-comprehensions>", "exec")
        globals_by_scope = {}

        def collect(table):
            if table.get_type() == "function" and table.get_name() in probes:
                scope, positions = probes[table.get_name()]
                globals_by_scope[scope] = {symbol.get_name(): positions[symbol.get_name()]
                                          for symbol in table.get_symbols()
                                          if symbol.is_global() and symbol.is_referenced()}
            for child in table.get_children():
                collect(child)

        collect(table)
        return globals_by_scope


def check_layout(scripts):
    """Return file:line diagnostics for the entrypoint and its own package."""
    paths = [scripts / "codex_task.py"] + sorted((scripts / "codex_worker").glob("*.py"))
    modules = {}
    for path in paths:
        module = ("codex_task" if path.parent == scripts else
                  "codex_worker" if path.stem == "__init__" else "codex_worker." + path.stem)
        modules[module] = path
    findings = []
    definitions = {}
    edges = {module: [] for module in modules}

    def report(path, line, rule, detail):
        findings.append("%s:%s: %s: %s" % (path, line, rule, detail))

    for module, path in modules.items():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        table = symtable.symtable(source, str(path), "exec")
        bindings = _Bindings()
        bindings.visit(tree)
        reads = _Reads(table)
        reads.visit(tree)
        syntax_reads = {node.id: node.lineno for node in ast.walk(tree)
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        # __file__ is injected by Python's module loader. Package modules have
        # a separate prohibition below; entrypoint path constants may use it.
        available = set(bindings.names) | set(dir(builtins)) | {"__file__"}
        class_globals = reads.class_comprehension_globals(source)
        for scope in reads.tables:
            for symbol in scope.get_symbols():
                class_line = class_globals.get(id(scope), {}).get(symbol.get_name())
                if ((symbol.is_global() and symbol.is_referenced() or class_line is not None)
                        and symbol.get_name() not in available):
                    line = class_line or reads.positions[id(scope)].get(symbol.get_name(), syntax_reads.get(symbol.get_name()))
                    # Some Python versions add referenced compiler symbols
                    # (e.g. __conditional_annotations__) with no source read.
                    if line is not None:
                        report(path, line, "undefined-global", symbol.get_name())
        for name, line in bindings.nonimports.items():
            if name in definitions:
                other_path, other_line = definitions[name]
                report(path, line, "duplicate-name", "%s already defined at %s:%s"
                       % (name, other_path, other_line))
            else:
                definitions[name] = (path, line)
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                report(path, node.lineno, "no-global", ", ".join(node.names))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"globals", "locals", "vars"}
                    and not node.args and not node.keywords):
                report(path, node.lineno, "no-dynamic-namespace", node.func.id + "()")
            if (module != "codex_task" and isinstance(node, ast.Name)
                    and node.id == "__file__"):
                report(path, node.lineno, "no-package-file", "__file__")
            imports = []
            if isinstance(node, ast.ImportFrom):
                internal = bool(node.level) or (node.module or "").split(".")[0] in {"codex_task", "codex_worker"}
                if internal:
                    if ((node.level == 1 and node.module is None)
                            or (not node.level and node.module == "codex_worker")):
                        imports = ["codex_worker." + alias.name for alias in node.names]
                    else:
                        report(path, node.lineno, "module-import-only", "import the module, not its definitions")
            elif isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names
                           if alias.name.split(".")[0] in {"codex_task", "codex_worker"}]
            for target in imports:
                if target == "codex_worker" and target not in modules:
                    continue  # A namespace package is also a module.
                if target not in modules:
                    report(path, node.lineno, "module-import-only", "unknown own module " + target)
                else:
                    edges[module].append((target, node.lineno))

    visiting = []
    visited = set()

    def visit(module):
        visiting.append(module)
        for target, line in edges[module]:
            if target in visiting:
                cycle = visiting[visiting.index(target):] + [target]
                report(modules[module], line, "acyclic-imports", " -> ".join(cycle))
            elif target not in visited:
                visit(target)
        visiting.pop()
        visited.add(module)

    for module in modules:
        if module not in visited:
            visit(module)
    return findings


class ModuleLayoutTests(unittest.TestCase):
    def check_tree(self, files):
        with tempfile.TemporaryDirectory(prefix="codex-module-layout-") as directory:
            root = Path(directory)
            (root / "codex_task.py").write_text("", encoding="utf-8")
            for relative, source in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            return check_layout(root)

    def test_production_module_layout(self):
        self.assertEqual(check_layout(SCRIPTS), [])

    def test_each_mutant_is_rejected(self):
        mutants = (
            ({"codex_worker/a.py": "def outer():\n    def inner():\n        return missing\n    return inner\n"},
             "a.py:3: undefined-global"),
            ({"codex_worker/a.py": "def function(value=missing):\n    return value\n"},
             "a.py:1: undefined-global"),
            ({"codex_worker/a.py": "value = 1\n", "codex_worker/b.py": "from .a import value\n"},
             "b.py:1: module-import-only"),
            ({"codex_worker/a.py": "def function():\n    global value\n"},
             "a.py:2: no-global"),
            ({"codex_worker/a.py": "value = globals()\n"},
             "a.py:1: no-dynamic-namespace"),
            ({"codex_worker/a.py": "value = 1\n", "codex_worker/b.py": "value = 2\n"},
             "b.py:1: duplicate-name"),
            ({"codex_worker/a.py": "from . import b\n", "codex_worker/b.py": "from . import a\n"},
             "b.py:1: acyclic-imports"),
            ({"codex_worker/a.py": "path = __file__\n"},
             "a.py:1: no-package-file"),
        )
        for files, expected in mutants:
            with self.subTest(rule=expected):
                findings = self.check_tree(files)
                self.assertEqual(len(findings), 1, findings)
                self.assertIn(expected, findings[0])

    def test_global_reads_in_other_scopes(self):
        for source, line in (
            ("class Example:\n    value = missing\n", 2),
            ("values = [missing for item in ()]\n", 1),
            ("function = lambda: missing\n", 1),
            ("@missing\ndef function():\n    pass\n", 1),
            ("def outer():\n    def inner(value=missing):\n        return value\n    return inner\n", 2),
            ("def function(value: missing):\n    return value\n", 1),
            ("first: int\nsecond: missing\n", 2),
            ("class Example:\n    first: int\n    second: missing\n", 3),
        ):
            with self.subTest(source=source):
                findings = self.check_tree({"codex_worker/a.py": source})
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("a.py:%s: undefined-global: missing" % line, findings[0])

    def test_class_comprehension_bodies_skip_class_bindings(self):
        for expression in (
            "[missing for item in (0,)]",
            "{missing for item in (0,)}",
            "{missing: item for item in (0,)}",
            "{item: missing for item in (0,)}",
            "[item for item in (0,) if missing]",
            "[item for first in (0,) for item in missing]",
            "[[missing for item in (0,)] for first in (0,)]",
        ):
            with self.subTest(expression=expression):
                source = "class Example:\n    missing = (1,)\n    values = %s\n" % expression
                findings = self.check_tree({"codex_worker/a.py": source})
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("a.py:3: undefined-global: missing", findings[0])

    def test_class_comprehension_locals_and_enclosing_captures_pass(self):
        for source in (
            "class Example:\n    items = (1,)\n    values = [item for item in items]\n",
            "class Example:\n    value = 1\n    values = [value for value in (2,)]\n",
            "class Example:\n    values = [[value for item in (0,)] for value in (1,)]\n",
            "class Example:\n    values = [item for value in ((1,),) for item in value if value]\n",
            "def outer():\n    value = 1\n    class Example:\n        value = 2\n        values = [value for item in (0,)]\n    return Example\nouter()\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(self.check_tree({"codex_worker/a.py": source}), [])

    def test_private_class_global_has_its_read_line(self):
        findings = self.check_tree({
            "codex_worker/a.py": "class Example:\n    def method(self):\n        return __missing\n",
        })
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("a.py:3: undefined-global: _Example__missing", findings[0])

    def test_entrypoint_imports_are_own_imports(self):
        findings = self.check_tree({
            "codex_task.py": "value = 1\n",
            "codex_worker/a.py": "from codex_task import value\n",
        })
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("a.py:1: module-import-only", findings[0])
        findings = self.check_tree({
            "codex_task.py": "import codex_worker.a as worker\n",
            "codex_worker/a.py": "import codex_task as entry\n",
        })
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("a.py:1: acyclic-imports", findings[0])

    def test_known_module_import_forms_pass(self):
        self.assertEqual(self.check_tree({
            "codex_task.py": "import codex_worker.a as worker\n",
            "codex_worker/a.py": "from codex_worker import b\n",
            "codex_worker/b.py": "import codex_worker.c as other\n",
            "codex_worker/c.py": "name = 1\n",
        }), [])
        findings = self.check_tree({"codex_worker/a.py": "from . import missing\n"})
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("a.py:1: module-import-only: unknown own module", findings[0])

    def test_control_flow_targets_are_module_bindings(self):
        self.assertEqual(self.check_tree({
            "codex_task.py": ("for item in ():\n    pass\n"
                              "with open('unused') as stream:\n    pass\n"
                              "try:\n    pass\nexcept Exception as error:\n    pass\n"
                              "def function():\n    return item, stream, error\n"),
        }), [])

    def test_clean_module_import_passes(self):
        findings = self.check_tree({
            "codex_worker/a.py": "from . import b\nvalue = b.name\n",
            "codex_worker/b.py": "name = 1\n",
        })
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
