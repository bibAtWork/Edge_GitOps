"""Offline demo: fake agent output, real queue transitions and git diff."""
import unittest
from tests.test_pipeline import PipelineTests

result = unittest.TextTestRunner(verbosity=2).run(
    unittest.TestSuite([PipelineTests('test_full_pipeline_report_review_and_real_git_patch')]))
raise SystemExit(not result.wasSuccessful())
