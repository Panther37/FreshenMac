import ast
import unittest
from pathlib import Path


class TestArchitectureAndOrdering(unittest.TestCase):
    """
    Enforces architectural invariants and strict alphabetical ordering
    across classes, methods, top-level functions, and constants.

    Maintaining strict alphabetical ordering prevents code bloat, avoids merge
    conflicts, eliminates arbitrary placement of methods, and ensures rapid navigation.
    """

    # Authoritative registry: mapping of module filenames to class names defined within each file
    FILES_AND_CLASSES = {
        'boot.py':           ['Reboot', 'RebootState'],
        'computer.py':       ['MacOS'],
        'homebrew.py':       ['HomeBrew'],
        'mas.py':            ['AppStore'],
        'plist.py':          ['LaunchAgent', 'PackageSync', 'SavePreferences', 'StartupRun'],
        'softwareupdate.py': ['SoftwareUpdate'],
        'util.py':           [
            'Logger', 'PlaySound', 'PrivilegedCMD', 'RunAppleScript', 'RunCMD', 'Runner', 'SudoKeepAlive', 'Version',
            'Wave',
        ],
        'xcode.py':          ['Xcode'],
    }

    @classmethod
    def setUpClass(cls):
        """Parse all Python source files in src/freshenmac into AST trees once for all tests."""
        cls.pkg_dir = Path(__file__).resolve().parent.parent / 'src' / 'freshenmac'
        cls.trees = {}
        for py_file in sorted(cls.pkg_dir.glob('*.py')):
            with open(py_file, 'r', encoding='utf-8') as f:
                cls.trees[py_file.name] = ast.parse(f.read(), filename=str(py_file))

    def _get_class_node(self, filename: str, class_name: str) -> ast.ClassDef:
        """Helper to retrieve an ast.ClassDef node by name from a parsed module tree."""
        tree = self.trees[filename]
        return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)

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

        Note: __init__ is excluded as the standard constructor, and arm_installed
        in MacOS is excluded due to property dependency ordering.
        """
        for filename, class_names in self.FILES_AND_CLASSES.items():
            for class_name in class_names:
                with self.subTest(file=filename, class_name=class_name):
                    cls_node = self._get_class_node(filename, class_name)
                    methods = [
                        n.name for n in cls_node.body
                        if isinstance(n, ast.FunctionDef) and all(
                            [
                                n.name != '__init__',
                                (filename, class_name, n.name) != ('computer.py', 'MacOS', 'arm_installed'),
                            ],
                        )
                    ]
                    self.assertEqual(
                        methods,
                        sorted(methods),
                        f"Methods of {class_name} in {filename} are not sorted: {methods}",
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
