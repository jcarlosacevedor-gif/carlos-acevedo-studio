import ast
from pathlib import Path
import runpy
import unittest


PROJECT_ROOT = Path(__file__).parent.parent
GUNICORN_CONFIG = PROJECT_ROOT / "gunicorn.conf.py"


class GunicornAccessLogConfigTests(unittest.TestCase):
    def test_access_log_format_uses_path_without_query_or_identifying_atoms(self):
        config = runpy.run_path(str(GUNICORN_CONFIG))
        access_log_format = config["access_log_format"]

        self.assertEqual(
            access_log_format,
            '%(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)s',
        )
        self.assertIn("%(U)s", access_log_format)
        for forbidden_atom in ("%(r)s", "%(q)s", "%(h)s", "%(f)s", "%(a)s", "%({"):
            with self.subTest(atom=forbidden_atom):
                self.assertNotIn(forbidden_atom, access_log_format)

    def test_config_changes_only_access_log_format(self):
        tree = ast.parse(GUNICORN_CONFIG.read_text(encoding="utf-8"))
        assigned_names = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]

        self.assertEqual(assigned_names, ["access_log_format"])


if __name__ == "__main__":
    unittest.main()
