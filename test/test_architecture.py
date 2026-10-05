import ast
import unittest
from pathlib import Path


class TestArchitectureAndOrdering(unittest.TestCase):
    """
    Enforces architectural invariants and strict alphabetical ordering
    across classes, methods, top-level functions, and constants.
    """

    # Mapping of module filenames to class names defined within each file
    FILES_AND_CLASSES = {
        'boot.py':           ['Reboot', 'RebootState'],
        'computer.py':       ['MacOS'],
        'homebrew.py':       ['HomeBrew'],
        'mas.py':            ['AppStore'],
        'plist.py':          ['LaunchAgent', 'PackageSync', 'SavePreferences', 'StartupRun'],
        'softwareupdate.py': ['SoftwareUpdate'],
        'util.py':           ['Logger', 'PlaySound', 'PrivilegedCMD', 'RunAppleScript', 'RunCMD', 'Runner', 'SudoKeepAlive', 'Version', 'Wave'],
        'xcode.py':          ['Xcode'],
    }


    @classmethod
    def setUpClass(cls):
        cls.pkg_dir = Path(__file__).resolve().parent.parent / 'src' / 'freshenmac'
        cls.trees = {}
        for py_file in sorted(cls.pkg_dir.glob('*.py')):
            with open(py_file, 'r', encoding='utf-8') as f:
                cls.trees[py_file.name] = ast.parse(f.read(), filename=str(py_file))

    def _get_class_node(self, filename: str, class_name: str) -> ast.ClassDef:
        tree = self.trees[filename]
        return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)

    def test_all_package_classes_registered(self):
        """Verifies every class in src/freshenmac is registered in FILES_AND_CLASSES."""
        discovered = {
            filename: [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
            for filename, tree in sorted(self.trees.items())
            if any(isinstance(n, ast.ClassDef) for n in tree.body)
        }
        self.assertEqual(discovered, self.FILES_AND_CLASSES)

    def test_class_definitions_alphabetical_order(self):
        """Verifies classes defined within each module match FILES_AND_CLASSES and are sorted."""
        for filename, expected_classes in self.FILES_AND_CLASSES.items():
            with self.subTest(file=filename):
                classes = [
                    node.name for node in self.trees[filename].body
                    if isinstance(node, ast.ClassDef)
                ]
                self.assertEqual(classes, expected_classes, f"Classes in {filename} do not match expected: {classes}")
                self.assertEqual(classes, sorted(classes), f"Classes in {filename} are not sorted: {classes}")

    def test_class_methods_alphabetical_order(self):
        """Verifies methods within each class across all files and classes are sorted alphabetically."""
        for filename, class_names in self.FILES_AND_CLASSES.items():
            for class_name in class_names:
                with self.subTest(file=filename, class_name=class_name):
                    cls_node = self._get_class_node(filename, class_name)
                    methods = [
                        n.name for n in cls_node.body
                        if isinstance(n, ast.FunctionDef)
                        and n.name != '__init__'
                        and (filename, class_name, n.name) != ('computer.py', 'MacOS', 'arm_installed')
                    ]
                    self.assertEqual(
                        methods,
                        sorted(methods),
                        f"Methods of {class_name} in {filename} are not sorted: {methods}",
                    )

    def test_constants_alphabetical_order(self):
        """Verifies module-level constants across all files are sorted alphabetically."""
        for filename, tree in sorted(self.trees.items()):
            with self.subTest(file=filename):
                constants = []
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id.isupper():
                                constants.append(target.id)
                self.assertEqual(
                    constants,
                    sorted(constants),
                    f"Constants in {filename} are not sorted: {constants}",
                )

    def test_keyword_only_parameters_alphabetical_order(self):
        """Verifies keyword-only arguments across all functions and methods are sorted alphabetically."""
        for filename, tree in sorted(self.trees.items()):
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kw_args = [arg.arg for arg in node.args.kwonlyargs]
                    if kw_args:
                        with self.subTest(file=filename, func=node.name):
                            self.assertEqual(
                                kw_args,
                                sorted(kw_args),
                                f"Keyword-only parameters of {node.name} in {filename} are not sorted: {kw_args}",
                            )

    def test_top_level_functions_alphabetical_order(self):
        """Verifies top-level functions across all files are sorted alphabetically."""
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
