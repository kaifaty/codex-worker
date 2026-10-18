"""Fast checks for AST discovery of stress tests and their helper calls."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_rotation


class StressSelectionTests(unittest.TestCase):
    def selected(self, sources, selection="reset"):
        with tempfile.TemporaryDirectory(prefix="codex-stress-selection-") as directory:
            root = Path(directory)
            for name, source in sources.items():
                (root / name).write_text(textwrap.dedent(source))
            module = SimpleNamespace(__file__=str(root / "suite.py"))
            return stress_rotation.select_tests([module], root, selection)

    def test_real_reset_selection_includes_indirect_review_and_is_nonempty(self):
        names = stress_rotation.reset_tests()
        self.assertTrue(names)
        self.assertIn("RotationTests.test_review_pause_and_wake_use_its_executor_account", names)
        self.assertIn("RotationTests.test_second_review_angle_pause_and_wake_use_its_executor_account", names)
        self.assertTrue(set(names) <= set(stress_rotation.reset_tests("timing")))

    def test_selection_follows_imported_functions_and_terminates_cycles(self):
        sources = {
            "helpers.py": """
                def begin(case):
                    recurse(case)
                def recurse(case):
                    begin(case)
                    case.reset_after_ready()
                def minutes():
                    configure(CODEX_TASK_MINUTE_SECONDS="1")
                def cycle_a():
                    cycle_b()
                def cycle_b():
                    cycle_a()
            """,
            "forwarders.py": """
                from helpers import begin as forwarded_begin
            """,
            "unrelated.py": """
                def recurse(case):
                    return None
            """,
            "suite.py": """
                import helpers as helpers_alias
                from helpers import minutes
                from forwarders import forwarded_begin as imported_begin
                from unrelated import recurse
                class Cases:
                    def test_imported(self):
                        imported_begin(self)
                    def test_module(self):
                        helpers_alias.begin(self)
                    def test_timing(self):
                        minutes()
                    def test_other_module(self):
                        recurse(self)
                    def test_cycle_without_seed(self):
                        helpers_alias.cycle_a()
                    def test_unused_nested_helper(self):
                        def unused():
                            self.reset_after_ready()
            """,
        }
        expected = ["RotationTests.test_imported", "RotationTests.test_module"]
        self.assertEqual(self.selected(sources), expected)
        self.assertEqual(self.selected(sources, "timing"), expected + ["RotationTests.test_timing"])

    def test_selection_follows_inherited_methods_nested_helpers_and_constructors(self):
        sources = {
            "helpers.py": """
                def reset_route(case):
                    case.reset_after_ready()
                class Base:
                    def route(self):
                        reset_route(self)
                class Helper:
                    def route(self, case):
                        reset_route(case)
            """,
            "suite.py": """
                from helpers import Base, Helper
                class Cases(Base):
                    def test_inherited(self):
                        self.route()
                    def test_nested(self):
                        def first():
                            second()
                        def second():
                            self.route()
                        first()
                    def test_constructed(self):
                        Helper().route(self)
                class Other:
                    def route(self):
                        return None
                    def test_unrelated_method(self):
                        self.route()
            """,
        }
        self.assertEqual(self.selected(sources), [
            "RotationTests.test_inherited", "RotationTests.test_nested", "RotationTests.test_constructed"])

    def test_timing_selection_follows_literal_budget_through_helpers(self):
        sources = {
            "suite.py": """
                def configure_budget(case):
                    case.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
                class Cases:
                    def budget(self):
                        configure_budget(self)
                    def test_indirect_budget(self):
                        self.budget()
                    def test_direct_reset(self):
                        self.reset_after_ready()
                    def test_unrelated(self):
                        pass
            """,
        }
        self.assertEqual(self.selected(sources), ["RotationTests.test_direct_reset"])
        self.assertEqual(self.selected(sources, "timing"), [
            "RotationTests.test_indirect_budget", "RotationTests.test_direct_reset"])


if __name__ == "__main__":
    unittest.main()
