"""Guard the module boundaries before and during the worker package split."""
import ast
import builtins
from pathlib import Path
import symtable
import sys
import tempfile
import unittest


SCRIPTS = (Path(__file__).resolve().parents[1]
           / "plugins/codex-worker/skills/codex-worker/scripts")
PACKAGE_FILE_ALLOWLIST = {"codex_worker/config.py"}


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
        self.class_bound = {}
        self.class_fallbacks = {}
        self.comprehension_locals = []
        self.class_comprehension_reads = {}
        self._index(table)

    def _index(self, table):
        self.tables.append(table)
        children = table.get_children()
        self.children[id(table)] = children
        self.positions[id(table)] = {}
        if table.get_type() == "class":
            bound = self.class_bound[id(table)] = {"__module__", "__qualname__"}
            if sys.version_info >= (3, 13):
                bound.add("__firstlineno__")
            if (any(symbol.is_annotated() for symbol in table.get_symbols())
                    and not any(child.get_type() == "annotation" and child.get_name() == "__annotate__"
                                for child in children)):
                bound.add("__annotations__")
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
        name = self._name(node.id)
        if isinstance(node.ctx, ast.Load):
            self.positions[id(self.table)].setdefault(name, node.lineno)
            if self.comprehension_locals and self.table is self.comprehension_locals[-1][0]:
                local = any(name in names for table, names in self.comprehension_locals
                            if table is self.table)
                if not local:
                    self.class_comprehension_reads.setdefault(id(self.table), {}).setdefault(name, node.lineno)
            elif (self.table.get_type() == "class" and name in self.table.get_identifiers()
                  and self.table.lookup(name).is_local()
                  and name not in self.class_bound[id(self.table)]):
                # Unbound class locals use globals (LOAD_NAME).
                self.class_fallbacks.setdefault(id(self.table), {}).setdefault(name, node.lineno)
        else:
            self._class_bind(node.id, not isinstance(node.ctx, ast.Del))

    def _class_bind(self, name, bound=True):
        if (name and self.table.get_type() == "class"
                and not any(table is self.table for table, _ in self.comprehension_locals)):
            names = self.class_bound[id(self.table)]
            if bound:
                names.add(self._name(name))
            else:
                names.discard(self._name(name))

    def visit_Assign(self, node):
        self._visit_in(None, [node.value] + node.targets)

    def visit_NamedExpr(self, node):
        self._visit_in(None, [node.value, node.target])

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.visit(ast.copy_location(ast.Name(id=node.target.id, ctx=ast.Load()), node.target))
        else:
            self.visit(node.target)
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self.visit(node.target)

    def visit_For(self, node):
        self._visit_in(None, [node.iter, node.target] + node.body + node.orelse)

    visit_AsyncFor = visit_For

    def visit_Import(self, node):
        for alias in node.names:
            self._class_bind(_import_name(node, alias))

    visit_ImportFrom = visit_Import

    def visit_ExceptHandler(self, node):
        self._visit_in(None, [node.type])
        self._class_bind(node.name)
        self._visit_in(None, node.body)
        self._class_bind(node.name, False)

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
        self._class_bind(node.name)

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
        self._class_bind(node.name)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self.visit(node.value)
        if node.value is not None or not isinstance(node.target, ast.Name):
            self.visit(node.target)
        self._annotation(node.lineno, [node.annotation], shared=True)

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


def _imports(tree, nested=False):
    if isinstance(tree, (ast.Import, ast.ImportFrom)):
        yield tree, not nested
    nested = nested or isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    for child in ast.iter_child_nodes(tree):
        yield from _imports(child, nested)


def _import_name(node, alias):
    return alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)


def _file_bindings(tree):
    """Enumerate every explicit binding/deletion, regardless of its scope."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield node.id, node.lineno, node
        elif isinstance(node, ast.arg):
            yield node.arg, node.lineno, node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno, node
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name, node.lineno, node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                yield _import_name(node, alias), getattr(alias, "lineno", node.lineno), alias
        elif type(node).__name__ in {"MatchAs", "MatchStar", "MatchMapping"}:
            name = getattr(node, "name", None) or getattr(node, "rest", None)
            if name:
                yield name, node.lineno, node


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
    analyses = {}

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
        analyses[module] = (source, tree, bindings, reads)

    for module, path in modules.items():
        source, tree, bindings, reads = analyses[module]
        aliases = {}
        for node, top in _imports(tree):
            if isinstance(node, ast.ImportFrom):
                internal = bool(node.level) or (node.module or "").split(".")[0] in {"codex_task", "codex_worker"}
                imports = []
                if internal:
                    imports = ([(alias, "codex_worker." + alias.name) for alias in node.names]
                               if ((node.level == 1 and node.module is None)
                                   or (not node.level and node.module == "codex_worker")) else None)
            else:
                imports = [(alias, alias.name) for alias in node.names
                           if alias.name.split(".")[0] in {"codex_task", "codex_worker"}]
                internal = bool(imports)
            if internal and not top:
                report(path, node.lineno, "module-import-top", "import own modules at module level")
            if imports is None:
                report(path, node.lineno, "module-import-only", "import the module, not its definitions")
                continue
            for alias, target in imports:
                owner = target if alias.asname or isinstance(node, ast.ImportFrom) else target.split(".")[0]
                aliases.setdefault(_import_name(node, alias), (owner, alias))
                if target == "codex_worker" and target not in modules:
                    continue  # A namespace package is also a module.
                if target not in modules:
                    report(path, node.lineno, "module-import-only", "unknown own module " + target)
                else:
                    edges[module].append((target, node.lineno))
        for name, line, binding in _file_bindings(tree):
            if name in aliases and binding is not aliases[name][1]:
                report(path, line, "module-alias-shadowed", name)
        syntax_reads = {node.id: node.lineno for node in ast.walk(tree)
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        # __file__ is injected by Python's module loader. Package modules have
        # a separate prohibition below, apart from the path-owning config.
        available = set(bindings.names) | set(dir(builtins)) | {"__file__"}
        class_globals = reads.class_comprehension_globals(source)
        for scope, positions in reads.class_fallbacks.items():
            class_globals.setdefault(scope, {}).update(positions)
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
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                target = aliases[node.value.id][0]
                if target in analyses and (not isinstance(node.ctx, ast.Load)
                                           or node.attr not in analyses[target][2].names):
                    report(path, node.lineno, "module-attribute", node.value.id + "." + node.attr)
            if isinstance(node, ast.Global):
                report(path, node.lineno, "no-global", ", ".join(node.names))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"globals", "locals", "vars"}
                    and not node.args and not node.keywords):
                report(path, node.lineno, "no-dynamic-namespace", node.func.id + "()")
            if (module != "codex_task"
                    and path.relative_to(scripts).as_posix() not in PACKAGE_FILE_ALLOWLIST
                    and isinstance(node, ast.Name)
                    and node.id == "__file__"):
                report(path, node.lineno, "no-package-file", "__file__")

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
            ({"codex_worker/config.py": "config_path = __file__\n",
              "codex_worker/a.py": "path = __file__\n"},
             "a.py:1: no-package-file"),
            ({"codex_worker/a.py": "from . import b\nvalue = b.missing\n",
              "codex_worker/b.py": "name = 1\n"},
             "a.py:2: module-attribute: b.missing"),
            ({"codex_worker/a.py": "from . import b\nb.name = 1\n",
              "codex_worker/b.py": "name = 1\n"},
             "a.py:2: module-attribute: b.name"),
            ({"codex_worker/a.py": "from . import b\ndel b.name\n",
              "codex_worker/b.py": "name = 1\n"},
             "a.py:2: module-attribute: b.name"),
            ({"codex_worker/a.py": "from . import b\nb.name += 1\n",
              "codex_worker/b.py": "name = 1\n"},
             "a.py:2: module-attribute: b.name"),
            ({"codex_worker/a.py": "from . import b\nb = object()\n",
              "codex_worker/b.py": ""},
             "a.py:2: module-alias-shadowed: b"),
            ({"codex_worker/a.py": "from . import b\ndef f(b):\n    return b.x\n",
              "codex_worker/b.py": "x = 1\n"},
             "a.py:2: module-alias-shadowed: b"),
            ({"codex_worker/a.py": "from . import b\nvalues = [b for b in ()]\n",
              "codex_worker/b.py": ""},
             "a.py:2: module-alias-shadowed: b"),
            ({"codex_worker/a.py": "def f():\n    from . import b\n",
              "codex_worker/b.py": ""},
             "a.py:2: module-import-top"),
            ({"codex_worker/a.py": "from . import b\nclass Example:\n    value = b.missing\n",
              "codex_worker/b.py": ""},
             "a.py:3: module-attribute: b.missing"),
            ({"codex_worker/a.py": "from . import b\ndef f():\n    return b.missing\n",
              "codex_worker/b.py": ""},
             "a.py:3: module-attribute: b.missing"),
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

    def test_package_config_can_anchor_paths(self):
        self.assertEqual(self.check_tree({
            "codex_task.py": "entry_path = __file__\n",
            "codex_worker/config.py": "config_path = __file__\n",
        }), [])

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

    def test_class_local_reads_before_binding_fall_back_to_globals(self):
        for source, line, name in (
            ("class Example:\n    value = value\n", 2, "value"),
            ("class Example:\n    first = value\n    value = 1\n", 2, "value"),
            ("class Example:\n    value: int\n    first = value\n", 3, "value"),
            ("class Example:\n    value: int = value\n", 2, "value"),
            ("class Example:\n    value += 1\n", 2, "value"),
            ("class Example:\n    first = (value := value)\n", 2, "value"),
            ("class Example:\n    for value in value:\n        pass\n", 2, "value"),
            ("class Example:\n    value = 1\n    first = value\n    del value\n    second = value\n", 5, "value"),
            ("class Example:\n    values = [value for value in ()]\n    first = value\n    value = 1\n", 3, "value"),
            ("class Example:\n    __value = __value\n", 2, "_Example__value"),
            ("class Example:\n    __annotations__ = __annotations__\n", 2, "__annotations__"),
            ("def outer():\n    value = 1\n    class Example:\n        value = value\nouter()\n", 4, "value"),
        ):
            with self.subTest(source=source):
                with self.assertRaises(NameError):
                    exec(source, {})
                findings = self.check_tree({"codex_worker/a.py": source})
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("a.py:%s: undefined-global: %s" % (line, name), findings[0])

    def test_bound_class_names_and_enclosing_captures_pass(self):
        for source in (
            "class Example:\n    value = 1\n    first = value\n",
            "value = 1\nclass Example:\n    value = value\n",
            "class Example:\n    len = len\n",
            "class Example:\n    __module__ = __module__\n    __qualname__ = __qualname__\n",
            "from __future__ import annotations\nclass Example:\n    value: int\n    __annotations__ = __annotations__\n",
            "class Example:\n    value: int = 1\n    first = value\n",
            "class Example:\n    first = (value := 1)\n    second = value\n",
            "class Example:\n    value = 1\n    value += 1\n    first = value\n",
            "class Example:\n    def value():\n        pass\n    first = value\n",
            "class Example:\n    class value:\n        pass\n    first = value\n",
            "class Example:\n    import math as value\n    first = value\n",
            "class Example:\n    from math import pi as value\n    first = value\n",
            "class Example:\n    for value in (1,):\n        first = value\n",
            "class Example:\n    try:\n        raise ValueError()\n    except ValueError as value:\n        first = value\n",
            "def outer():\n    value = 1\n    class Example:\n        first = value\n    return Example\nouter()\n",
        ):
            with self.subTest(source=source):
                exec(source, {})
                self.assertEqual(self.check_tree({"codex_worker/a.py": source}), [])

    def test_version_specific_class_namespace_setup(self):
        source = "class Example:\n    value: int\n    __annotations__ = __annotations__\n"
        if sys.version_info < (3, 14):
            exec(source, {})
            self.assertEqual(self.check_tree({"codex_worker/a.py": source}), [])
        else:
            with self.assertRaises(NameError):
                exec(source, {})
            findings = self.check_tree({"codex_worker/a.py": source})
            self.assertEqual(len(findings), 1, findings)
            self.assertIn("a.py:3: undefined-global: __annotations__", findings[0])
        if sys.version_info >= (3, 13):
            source = "class Example:\n    __firstlineno__ = __firstlineno__\n"
            exec(source, {})
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
        self.assertEqual(self.check_tree({
            "codex_worker/a.py": "import codex_worker.b\nvalue = codex_worker.b.name\n",
            "codex_worker/__init__.py": "from . import b\n",
            "codex_worker/b.py": "name = 1\n",
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

    def test_module_attribute_import_forms_and_nested_reads(self):
        for statement, alias in (
            ("from . import b", "b"),
            ("from . import b as other", "other"),
            ("from codex_worker import b", "b"),
            ("from codex_worker import b as other", "other"),
            ("import codex_worker.b as other", "other"),
        ):
            with self.subTest(statement=statement):
                findings = self.check_tree({
                    "codex_worker/a.py": statement + "\ndef outer():\n    def inner():\n        return " + alias + ".missing\n    return inner\n",
                    "codex_worker/b.py": "name = 1\n",
                })
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("a.py:4: module-attribute: " + alias + ".missing", findings[0])

    def test_module_attributes_and_content_mutation_pass(self):
        self.assertEqual(self.check_tree({
            "codex_worker/a.py": ("from . import b\n"
                                  "value = b.name\n"
                                  "b.holder['key'] = 1\n"
                                  "del b.holder['key']\n"
                                  "copied_import = b.math\n"
                                  "def outer():\n"
                                  "    def inner():\n"
                                  "        return b.name\n"
                                  "    return inner\n"),
            "codex_worker/b.py": "import math\nname = 1\nholder = {}\n",
        }), [])

    def test_module_aliases_cannot_be_bound_in_any_scope(self):
        for statement, line in (
            ("b = object()\n", 2),
            ("b += 1\n", 2),
            ("b: object\n", 2),
            ("value = (b := object())\n", 2),
            ("b, *rest = ()\n", 2),
            ("for b in ():\n    pass\n", 2),
            ("with open('unused') as b:\n    pass\n", 2),
            ("try:\n    pass\nexcept Exception as b:\n    pass\n", 4),
            ("values = [b for b in ()]\n", 2),
            ("values = {b for b in ()}\n", 2),
            ("values = {b: 1 for b in ()}\n", 2),
            ("values = (b for b in ())\n", 2),
            ("def function(b):\n    pass\n", 2),
            ("def function(b, /):\n    pass\n", 2),
            ("def function(*, b):\n    pass\n", 2),
            ("def function(*b):\n    pass\n", 2),
            ("def function(**b):\n    pass\n", 2),
            ("function = lambda b: b\n", 2),
            ("function = lambda *b: b\n", 2),
            ("function = lambda **b: b\n", 2),
            ("def b():\n    pass\n", 2),
            ("async def b():\n    pass\n", 2),
            ("class b:\n    pass\n", 2),
            ("def function():\n    b = object()\n", 3),
            ("class Example:\n    b = object()\n", 3),
            ("async def function():\n    async for b in ():\n        pass\n", 3),
            ("async def function():\n    async with open('unused') as b:\n        pass\n", 3),
            ("import json as b\n", 2),
            ("from json import loads as b\n", 2),
            ("from . import b\n", 2),
            ("from codex_worker import b\n", 2),
            ("import codex_worker.b as b\n", 2),
            ("del b\n", 2),
        ):
            with self.subTest(statement=statement):
                findings = self.check_tree({
                    "codex_worker/a.py": "from . import b\n" + statement,
                    "codex_worker/b.py": "",
                })
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("a.py:%s: module-alias-shadowed: b" % line, findings[0])

    def test_own_imports_are_confined_to_module_scope(self):
        for source in (
            "def function():\n    from . import b\n",
            "async def function():\n    import codex_worker.b as b\n",
            "class Example:\n    from codex_worker import b\n",
            "def function():\n    if True:\n        from . import b\n",
        ):
            with self.subTest(source=source):
                findings = self.check_tree({
                    "codex_worker/a.py": source,
                    "codex_worker/b.py": "",
                })
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("module-import-top", findings[0])
        for source in (
            "if True:\n    from . import b\nvalue = b.name\n",
            "try:\n    from . import b\nexcept ImportError:\n    pass\nvalue = b.name\n",
            "from . import b\ndef function():\n    import math\n    return b.name\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(self.check_tree({
                    "codex_worker/a.py": source,
                    "codex_worker/b.py": "name = 1\n",
                }), [])

    def test_late_binding_cannot_hide_a_module_attribute(self):
        findings = self.check_tree({
            "codex_worker/a.py": "from . import b\ndef function():\n    return b.missing\nb = object()\n",
            "codex_worker/b.py": "",
        })
        self.assertEqual(len(findings), 2, findings)
        self.assertTrue(any("a.py:4: module-alias-shadowed: b" in item for item in findings), findings)
        self.assertTrue(any("a.py:3: module-attribute: b.missing" in item for item in findings), findings)

    @unittest.skipUnless(hasattr(ast, "Match"), "pattern matching requires Python 3.10")
    def test_pattern_captures_cannot_shadow_module_aliases(self):
        for pattern in ("b", "[*b]", "{'key': value, **b}"):
            with self.subTest(pattern=pattern):
                findings = self.check_tree({
                    "codex_worker/a.py": "from . import b\nmatch ():\n    case " + pattern + ":\n        pass\n",
                    "codex_worker/b.py": "",
                })
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("a.py:3: module-alias-shadowed: b", findings[0])


if __name__ == "__main__":
    unittest.main()
