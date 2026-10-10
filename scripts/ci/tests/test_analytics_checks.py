import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('analytics_checks', Path(__file__).resolve().parents[1] / 'analytics_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class PermissionHarness(unittest.TestCase):
    def test_success_is_not_a_denial(self):
        with self.assertRaises(AssertionError):
            checks.assert_permission({'data': [[1]]}, True)

    def test_syntax_error_is_not_a_denial(self):
        with self.assertRaises(AssertionError):
            checks.assert_permission({'error': {'errorName': 'SYNTAX_ERROR'}}, True)

    def test_permission_denial_is_recognized(self):
        checks.assert_permission({'error': {'errorName': 'PERMISSION_DENIED'}}, True)

    def test_unexpected_denial_fails_allow_check(self):
        with self.assertRaises(AssertionError):
            checks.assert_permission({'error': {'errorName': 'PERMISSION_DENIED'}}, False)


if __name__ == '__main__':
    unittest.main()
