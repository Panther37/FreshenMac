"""
Mostly my own sorting.  Uses logic from:
- https://pypi.org/project/flake8-alphabetize
- https://pypi.org/project/wemake-python-styleguide/
"""

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

    CLASS_TOP = ('__init_subclass__', '__new__', '__init__', '__post_init__', '__call__', '__await__')  # for WPS338
    FORMATTING_RULES: dict[str, dict[str, str | tuple[str, ...]]] = {
        'AZ':  {  # flake8-alphabetize
            '100': 'Import statements are in the wrong order',
            '200': 'The names in the import from are in the wrong order',
            '300': 'Two import from statements must be combined',
            '400': 'The names in the __all__ are in the wrong order',
            '500': 'The names in the exception handler list are in the wrong order',
        },
        'P37': {  # Developer
            'SORT_CHRONO': ('Second', 'Minute', 'Hour', 'Day', 'Weekday', 'Month', 'Year'),
            'SORT_CRON':   ('Minute', 'Hour', 'Day', 'Month', 'Weekday'),
            'SORT_OTHER':  'Exempt from sorting',
        },
        'WPS': {  # wemake-python-styleguide
            '338': 'Wrong method order in class',
        },
    }
    PATTERN: dict[str, re.Pattern[str]] = {
        'noqa':        re.compile(r"#\s*noqa:\s*(?P<rule>[\w-]+)", re.IGNORECASE),
        'guide, code': re.compile(r"(?P<guide>[A-Z]+)(?P<code>[0-9]+)"),
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

    def _check_constant_dicts(self, filename: str, const_name: str, value_node: ast.AST | None) -> None:
        """Verifies dictionary keys inside a constant are sorted alphabetically or match their noqa rule."""
        if not value_node:
            return

        for sub in ast.walk(value_node):
            if not isinstance(sub, ast.Dict):
                continue

            keys = [
                k.value for k in sub.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            if len(keys) <= 1 or keys == sorted(keys, key=str.lower):
                continue

            if noqa_rule := self._get_noqa_rule(filename, sub):
                rule_def = self.FORMATTING_RULES[noqa_rule[0]][noqa_rule[1]]
                if isinstance(rule_def, tuple):
                    expected = [k for k in rule_def if k in keys]
                    self.assertEqual(
                        keys,
                        expected,
                        f"Dict keys in {const_name} ({filename}:{sub.lineno}) do not match "
                        f"{noqa_rule[0]}-{noqa_rule[1]} order: {keys} != {expected}",
                    )
                continue

            self.assertEqual(
                keys,
                sorted(keys, key=str.lower),
                f"Dict keys in {const_name} ({filename}:{sub.lineno}) are not sorted: {keys}",
            )

    def _check_exception_handlers(self, filename: str, tree: ast.Module) -> None:
        """Verifies AZ500 (exception handler tuple names are sorted alphabetically)."""
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple)):
                continue

            exc_names = [ast.unparse(elt) for elt in node.type.elts]
            if exc_names != sorted(exc_names) and self._get_noqa_rule(filename, node) != ('AZ', '500'):
                self.assertEqual(
                    exc_names,
                    sorted(exc_names),
                    f"Exception handler tuple in {filename}:{node.lineno} is not sorted (AZ500): {exc_names}",
                )

    def _check_shutdown_class(self, filename: str, cls_node: ast.ClassDef) -> None:
        """Verifies shutdown test methods and tearDown() in a test class call ensure_no_active_shutdown()."""
        shutdown_tests = []
        for func_node in [
            n for n in cls_node.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')
        ]:
            # Exclude the shutdown guard's own unit tests
            if func_node.name.startswith(('test_ensure_no_active_shutdown', 'test_get_active_shutdown_pids')):
                continue

            if self._is_shutdown_test(func_node):
                shutdown_tests.append(func_node.name)
                calls = [
                    n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                    for n in ast.walk(func_node)
                    if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
                ]
                with self.subTest(file=filename, cls=cls_node.name, test=func_node.name):
                    self.assertIn(
                        'ensure_no_active_shutdown',
                        calls,
                        f"Test {cls_node.name}.{func_node.name} in {filename} "
                        "tests shutdown but does not call ensure_no_active_shutdown()",
                    )

        if shutdown_tests:
            self._check_shutdown_teardown(filename, cls_node, shutdown_tests)

    def _check_shutdown_teardown(self, filename: str, cls_node: ast.ClassDef, shutdown_tests: list[str]) -> None:
        """Verifies a test class with shutdown tests defines tearDown() calling ensure_no_active_shutdown()."""
        tear_down = next(
            (n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == 'tearDown'),
            None,
        )
        with self.subTest(file=filename, cls=cls_node.name, check='tearDown'):
            self.assertIsNotNone(
                tear_down,
                f"Class {cls_node.name} in {filename} tests shutdown ({shutdown_tests}) "
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
                f"tearDown() in {cls_node.name} ({filename}) does not call ensure_no_active_shutdown()",
            )

    def _check_top_level_imports_and_all(self, filename: str, tree: ast.Module) -> None:
        """
        Verifies AZ100 (import order), AZ200 (from-import names), AZ300 (combined from-imports), and AZ400 (__all__).
        """
        seen_from_modules: set[tuple[int, str | None]] = set()
        import_keys: list[tuple[int, int, str]] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    group = 2 if alias.name.startswith('freshenmac') else 1
                    if self._get_noqa_rule(filename, node) != ('AZ', '100'):
                        import_keys.append((group, 0, alias.name))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ''
                group = 0 if mod == '__future__' else (2 if mod.startswith('freshenmac') or node.level > 0 else 1)
                if self._get_noqa_rule(filename, node) != ('AZ', '100'):
                    import_keys.append((group, 1, mod))

                key = (node.level, node.module)
                if key in seen_from_modules and self._get_noqa_rule(filename, node) != ('AZ', '300'):
                    self.fail(f"Duplicate 'from {node.module} import ...' in {filename}:{node.lineno} (AZ300)")
                seen_from_modules.add(key)

                names = [a.name for a in node.names]
                if names != sorted(names) and self._get_noqa_rule(filename, node) != ('AZ', '200'):
                    self.assertEqual(
                        names,
                        sorted(names),
                        f"Imported names in 'from {node.module} import ...' ({filename}:{node.lineno}) "
                        f"are not sorted (AZ200): {names}",
                    )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if isinstance(target, ast.Name) and target.id == '__all__' and isinstance(
                    node.value, (ast.List, ast.Tuple),
                ):
                    all_names = [
                        elt.value for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                    if all_names != sorted(all_names) and self._get_noqa_rule(filename, node) != ('AZ', '400'):
                        self.assertEqual(
                            all_names,
                            sorted(all_names),
                            f"__all__ names in {filename}:{node.lineno} are not sorted (AZ400): {all_names}",
                        )

        self.assertEqual(
            import_keys,
            sorted(import_keys),
            f"Import statements in {filename} are not sorted (AZ100): {import_keys}",
        )

    def _clean_names(
        self,
        func_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef],
        a_filename: str,
    ) -> list[str]:
        """Cleans and filters method nodes into a list of method names for alphabetical validation."""
        cleaned = []
        for node in func_nodes:
            if self._get_noqa_rule(a_filename, node) == ('WPS', '338'):
                continue

            cleaned.append(node.name)
        return cleaned

    def _get_noqa_rule(self, filename: str, node: ast.AST) -> tuple[str, str] | None:
        """
        Extracts and validates a '# noqa: ...' directive on the given AST node's header/opening lines.
        Returns (guide, code) if present and registered in FORMATTING_RULES, otherwise None.
        """
        lines = self.sources[filename]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            end = node.body[0].lineno - 1 if node.body else node.lineno
        else:
            start = node.lineno - 1
            end = start + 1

        for idx in range(start, max(start + 1, end)):
            if idx < len(lines) and (match := self.PATTERN['noqa'].search(lines[idx])):
                rule = match.group('rule').upper()
                if '-' in rule:
                    guide, code = rule.split('-', 1)
                elif code_match := self.PATTERN['guide, code'].search(rule):
                    guide = code_match.group('guide')
                    code = code_match.group('code')
                else:
                    continue

                if code in self.FORMATTING_RULES.get(guide, {}):
                    return guide, code

        return None

    @staticmethod
    def _is_shutdown_test(func_node: ast.FunctionDef) -> bool:
        """Scans a test function AST for shutdown string literals, attributes, or names."""
        for node in ast.walk(func_node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 'shutdown' in node.value:
                return True
            if isinstance(node, ast.Attribute) and 'shutdown' in node.attr:
                return True
            if isinstance(node, ast.Name) and all(
                [
                    node.id not in ('ensure_no_active_shutdown', 'get_active_shutdown_pids'),
                    'shutdown' in node.id,
                ],
            ):
                return True

        return False

    def test_class_definitions_alphabetical_order(self):
        """
        Verifies that classes defined in each module are sorted alphabetically.
        """
        for filename, tree in sorted(self.trees.items()):
            with self.subTest(file=filename):
                classes = [
                    node.name for node in tree.body
                    if isinstance(node, ast.ClassDef)
                ]
                self.assertEqual(classes, sorted(classes), f"Classes in {filename} are not sorted: {classes}")

    def test_class_methods_alphabetical_order(self):
        """
        Verifies that methods within each class across all files are sorted alphabetically.

        Lifecycle methods in CLASS_TOP form their own group at the top of the class.
        All subsequent methods must be sorted alphabetically, unless exempted by '# noqa: WPS338'.
        """
        for filename, tree in sorted(self.trees.items()):
            for cls_node in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                a_name = cls_node.name
                with self.subTest(file=filename, class_name=a_name):
                    func_nodes = [
                        n for n in cls_node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]
                    all_methods = [n.name for n in func_nodes]
                    top_methods = [m for m in self.CLASS_TOP if m in all_methods]
                    self.assertEqual(
                        all_methods[:len(top_methods)],
                        top_methods,
                        f"CLASS_TOP methods in {a_name} ({filename}) must be at the top of the class "
                        f"in order {self.CLASS_TOP}: {all_methods[:len(top_methods)]} != {top_methods}",
                    )
                    remaining = [n.name for n in func_nodes[len(top_methods):]]
                    if remaining != sorted(remaining):
                        remaining = self._clean_names(func_nodes[len(top_methods):], filename)
                    self.assertEqual(
                        remaining,
                        sorted(remaining),
                        f"Methods of {a_name} in {filename} after CLASS_TOP are not sorted: {remaining}",
                    )

    def test_constants_alphabetical_order(self):
        """
        Verifies that module-level UPPERCASE constants and their dictionary keys
        across all files are sorted alphabetically, unless exempted by FORMATTING_RULES.
        """
        for filename, tree in sorted(self.trees.items()):
            with self.subTest(file=filename):
                constants = []
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id.isupper()]
                    elif isinstance(node, ast.AnnAssign) and isinstance(
                        node.target,
                        ast.Name,
                    ) and node.target.id.isupper():
                        targets = [node.target]
                    else:
                        continue

                    for target in targets:
                        constants.append(target.id)
                        self._check_constant_dicts(filename, target.id, node.value)

                self.assertEqual(
                    constants,
                    sorted(constants),
                    f"Constants in {filename} are not sorted: {constants}",
                )

    def test_exception_handlers_and_imports_alphabetical_order(self):
        """
        Verifies AZ100 (import statement order), AZ200 (from-import names),
        AZ300 (combined from-imports), AZ400 (__all__), and AZ500 (exception handler tuple names)
        across all package files.
        flake8-alphabetize
        """
        for filename, tree in sorted(self.trees.items()):
            with self.subTest(file=filename):
                self._check_top_level_imports_and_all(filename, tree)
                self._check_exception_handlers(filename, tree)

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

    def test_noqa_directive_detection(self):
        """Verifies _get_noqa_rule and _clean_names recognize '# noqa:' directives in FORMATTING_RULES."""
        code = (
            "class Sample:\n"
            "    @cached_property\n"
            "    def custom_order(self):  # noqa: WPS338\n"
            "        pass\n"
            "    def normal_order(self):\n"
            "        pass\n"
            "SCHEDULE = {  # noqa: P37-SORT_CHRONO\n"
            "    'Minute': 0,\n"
            "    'Hour': 2,\n"
            "    'Weekday': 0,\n"
            "}\n"
        )
        tree = ast.parse(code)
        cls_node = tree.body[0]
        dict_node = tree.body[1].value
        self.sources['<test>'] = code.splitlines()
        self.assertEqual(self._get_noqa_rule('<test>', cls_node.body[0]), ('WPS', '338'))
        self.assertIsNone(self._get_noqa_rule('<test>', cls_node.body[1]))
        self.assertEqual(self._get_noqa_rule('<test>', dict_node), ('P37', 'SORT_CHRONO'))
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
                self._check_shutdown_class(py_file.name, cls_node)

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
