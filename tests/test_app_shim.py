import ast
import pathlib
import unittest


class TestAppPyIsShim(unittest.TestCase):
    def test_app_py_has_no_active_routes_or_db(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        app_py = root / "app.py"
        src = app_py.read_text(encoding="utf-8")

        tree = ast.parse(src)

        banned_call_names = {"route", "connect", "get_db_connection", "init_db"}
        banned_modules = {"sqlite3"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in banned_modules:
                        self.fail("app.py must not import sqlite3 (DB authority must live in time_tracker_pro/db.py)")

            if isinstance(node, ast.ImportFrom):
                if node.module in banned_modules:
                    self.fail("app.py must not import from sqlite3")

            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in banned_call_names:
                    self.fail(f"app.py must not call '{fn.attr}' at module scope; keep it as a shim")

        self.assertIn("create_app", src)


if __name__ == "__main__":
    unittest.main()
