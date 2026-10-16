import ast
import re
import unittest
from pathlib import Path


class TestArchitectureAndOrdering(unittest.TestCase):
    """
    Enforces architectural invariants and strict alphabetical ordering
    across classes, methods, top-level functions, and constants.

    Maintaining strict alphabetical ordering prevents code bloat, avoids merge
    conflicts, eliminates arbitrary placement of methods, and ensures rapid navigation.
    """

    INITS = ('__init__', '__post_init__')
    # Authoritative registry: mapping of module filenames to class names defined within each file
    FILES_AND_CLASSES = {
        'boot.py':           ['ActionState', 'DeferState', 'Reboot', 'RebootState'],
        'computer.py':       ['MacOS'],
        'homebrew.py':       ['HomeBrew'],
        'mas.py':            ['AppStore'],
        'plist.py':          ['CronSchedule', 'LaunchAgent', 'PackageSync', 'SavePreferences', 'StartupRun'],
        'softwareupdate.py': ['SoftwareUpdate'],
        'util.py':           [
            'Logger', 'PlaySound', 'PrivilegedCMD', 'RunAppleScript', 'RunCMD', 'Runner', 'SudoKeepAlive', 'Version',
            'Wave',
        ],
        'xcode.py':          ['Xcode'],
    }
    PATTERN: dict[str, re.Pattern[str]] = {
        'noqa WPS338': re.compile(r'#\s*noqa:\s*[^#\n]*\bWPS338\b'),
    }

    @classmethod
    def setUpClass(cls):
        """Parse all Python source files in src/freshenmac into AST trees once for all tests."""
        cls.pkg_dir = Path(__file__).resolve().parent.parent / 'src' / 'freshenmac'
        cls.sources = {}
        cls.trees = {}
        for py_file in sorted(cls.pkg_dir.glob('*.py')):
            content = py_file.read_text(encoding='utf-8')
            cls.sources[py_file.name] = content.splitlines()
            cls.trees[py_file.name] = ast.parse(content, filename=str(py_file))

    def _clean_names(
        self,
        func_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef],
        a_filename: str,
    ) -> list[str]:
        """Cleans and filters method nodes into a list of method names for alphabetical validation."""
        cleaned = []
        for node in func_nodes:
            if self._has_noqa_wps338(a_filename, node):
                continue

            cleaned.append(node.name)
        return cleaned

    def _get_class_node(self, filename: str, class_name: str) -> ast.ClassDef:
        """Helper to retrieve an ast.ClassDef node by name from a parsed module tree."""
        tree = self.trees[filename]
        return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)

    def _has_noqa_wps338(self, filename: str, func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Checks if a function or method signature contains a '# noqa: WPS338' comment directive."""
        lines = self.sources[filename]
        start = (func_node.decorator_list[0].lineno if func_node.decorator_list else func_node.lineno) - 1
        end = func_node.body[0].lineno - 1 if func_node.body else func_node.lineno
        for idx in range(start, max(start + 1, end)):
            if idx < len(lines) and self.PATTERN['noqa WPS338'].search(lines[idx]):
                return True
        return False

    def test_all_package_classes_registered(self):
        """
        Verifies that every ClassDef defined in src/freshenmac is registered in FILES_AND_CLASSES.

        Ensures no newly created classes are omitted from architectural checks.
        """
        # Discover all class definitions in each module
        discovered = {
            filename: [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
            for filename, tree in sorted(self.trees.items())
            if any(isinstance(node, ast.ClassDef) for node in tree.body)
        }
        self.assertEqual(discovered, self.FILES_AND_CLASSES)

    def test_class_definitions_alphabetical_order(self):
        """
        Verifies that classes defined in each module match FILES_AND_CLASSES and are sorted alphabetically.
        """
        for filename, expected_classes in self.FILES_AND_CLASSES.items():
            with self.subTest(file=filename):
                classes = [
                    node.name for node in self.trees[filename].body
                    if isinstance(node, ast.ClassDef)
                ]
                self.assertEqual(classes, expected_classes, f"Classes in {filename} do not match expected: {classes}")
                self.assertEqual(classes, sorted(classes), f"Classes in {filename} are not sorted: {classes}")

    def test_class_methods_alphabetical_order(self):
        """
        Verifies that methods within each class across all files are sorted alphabetically.

        Initializers (__init__, then __post_init__) form their own group at the top of the class.
        All subsequent methods must be sorted alphabetically, unless exempted by '# noqa: WPS338'.
        """
        for filename, class_names in self.FILES_AND_CLASSES.items():
            for a_name in class_names:
                with self.subTest(file=filename, class_name=a_name):
                    cls_node = self._get_class_node(filename, a_name)
                    func_nodes = [
                        n for n in cls_node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]
                    all_methods = [n.name for n in func_nodes]
                    all_inits = [m for m in all_methods if m in self.INITS]
                    expected_inits = [m for m in self.INITS if m in all_inits]
                    self.assertEqual(
                        all_inits,
                        expected_inits,
                        f"Initializers in {a_name} ({filename}) must be ordered __init__ then __post_init__: {all_inits}",
                    )
                    self.assertEqual(
                        all_methods[:len(all_inits)],
                        expected_inits,
                        f"Initializers in {a_name} ({filename}) must be at the top of the class: {all_methods[:len(all_inits)]} != {expected_inits}",
                    )
                    remaining = self._clean_names(func_nodes[len(all_inits):], filename)
                    self.assertEqual(
                        remaining,
                        sorted(remaining),
                        f"Methods of {a_name} in {filename} after initializers are not sorted: {remaining}",
                    )

    def test_constants_alphabetical_order(self):
        """
        Verifies that module-level UPPERCASE constants across all files are sorted alphabetically.
        """
        for filename, tree in sorted(self.trees.items()):
            with self.subTest(file=filename):
                constants = []
                for node in tree.body:
                    if not isinstance(node, ast.Assign):
                        continue

                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            constants.append(target.id)
                self.assertEqual(
                    constants,
                    sorted(constants),
                    f"Constants in {filename} are not sorted: {constants}",
                )

    def test_keyword_only_parameters_alphabetical_order(self):
        """
        Verifies that keyword-only arguments across all functions and methods are sorted alphabetically.

        Enforcing alphabetical order on keyword-only arguments makes call sites predictable
        and avoids arbitrary signature churn.
        """
        for filename, tree in sorted(self.trees.items()):
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                kw_args = [arg.arg for arg in node.args.kwonlyargs]
                if kw_args:
                    with self.subTest(file=filename, func=node.name):
                        self.assertEqual(
                            kw_args,
                            sorted(kw_args),
                            f"Keyword-only parameters of {node.name} in {filename} are not sorted: {kw_args}",
                        )

    def test_noqa_wps338_directive_detection(self):
        """Verifies _has_noqa_wps338 and _clean_names recognize '# noqa: WPS338' directives."""
        code = (
            "class Sample:\n"
            "    @cached_property\n"
            "    def custom_order(self):  # noqa: WPS338\n"
            "        pass\n"
            "    def normal_order(self):\n"
            "        pass\n"
        )
        tree = ast.parse(code)
        cls_node = tree.body[0]
        self.sources['<test>'] = code.splitlines()
        self.assertTrue(self._has_noqa_wps338('<test>', cls_node.body[0]))
        self.assertFalse(self._has_noqa_wps338('<test>', cls_node.body[1]))
        cleaned = self._clean_names(cls_node.body, '<test>')
        self.assertEqual(cleaned, ['normal_order'])

    def test_shutdown_tests_call_guard(self):
        """
        Verifies that any test method testing shutdown operations (e.g. escalate_shutdown,
        turn_off, or asserting shutdown commands) calls ensure_no_active_shutdown()
        to guarantee no uncanceled system shutdown timer remains on the host.

        Also verifies that the enclosing test class defines tearDown() calling the guard
        as a defense-in-depth safety net if a test fails before completion.
        """
        test_dir = Path(__file__).resolve().parent
        for py_file in sorted(test_dir.glob('test_*.py')):
            if py_file.name == 'test_architecture.py':
                continue

            with open(py_file, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=str(py_file))

            for cls_node in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                shutdown_tests = []
                for func_node in [
                    n for n in cls_node.body
                    if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')
                ]:
                    # Exclude the shutdown guard's own unit tests
                    if func_node.name.startswith(('test_ensure_no_active_shutdown', 'test_get_active_shutdown_pids')):
                        continue

                    # Scan function AST for shutdown string literals, attributes, or names
                    is_shutdown_test = False
                    for node in ast.walk(func_node):
                        if isinstance(node, ast.Constant) and isinstance(node.value, str):
                            if 'shutdown' in node.value:
                                is_shutdown_test = True
                                break

                        elif isinstance(node, ast.Attribute) and 'shutdown' in node.attr:
                            is_shutdown_test = True
                            break

                        elif isinstance(node, ast.Name) and all(
                            [
                                node.id not in ('ensure_no_active_shutdown', 'get_active_shutdown_pids'),
                                'shutdown' in node.id,
                            ],
                        ):
                            is_shutdown_test = True
                            break

                    if is_shutdown_test:
                        shutdown_tests.append(func_node.name)
                        # Extract all function/method call names within the test method
                        calls = [
                            n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                            for n in ast.walk(func_node)
                            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
                        ]
                        with self.subTest(file=py_file.name, cls=cls_node.name, test=func_node.name):
                            self.assertIn(
                                'ensure_no_active_shutdown',
                                calls,
                                f"Test {cls_node.name}.{func_node.name} in {py_file.name} "
                                "tests shutdown but does not call ensure_no_active_shutdown()",
                            )

                # If this class contains shutdown tests, ensure it defines tearDown() as a safety net
                if shutdown_tests:
                    tear_down = next(
                        (n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == 'tearDown'),
                        None,
                    )
                    with self.subTest(file=py_file.name, cls=cls_node.name, check='tearDown'):
                        self.assertIsNotNone(
                            tear_down,
                            f"Class {cls_node.name} in {py_file.name} tests shutdown ({shutdown_tests}) "
                            "but does not define tearDown()",
                        )
                        td_calls = [
                            n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                            for n in ast.walk(tear_down)
                            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
                        ]
                        self.assertIn(
                            'ensure_no_active_shutdown',
                            td_calls,
                            f"tearDown() in {cls_node.name} ({py_file.name}) does not call ensure_no_active_shutdown()",
                        )

    def test_top_level_functions_alphabetical_order(self):
        """
        Verifies that top-level functions across all files are sorted alphabetically.
        """
        for filename, tree in sorted(self.trees.items()):
            with self.subTest(file=filename):
                functions = [
                    node.name for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                ]
                self.assertEqual(
                    functions,
                    sorted(functions),
                    f"Top-level functions in {filename} are not sorted: {functions}",
                )


if __name__ == '__main__':
    unittest.main()
