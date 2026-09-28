from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trivy_capacity import check_capacity, quantity  # noqa: E402


def resources(request, limit, cpu="100m"):
    return {"requests": {"cpu": cpu, "memory": request}, "limits": {"memory": limit}}


def documents(concurrency=1):
    return [
        {"kind": "HelmRelease", "metadata": {"name": "trivy-operator"},
         "spec": {"values": {"operator": {"scanJobsConcurrentLimit": concurrency},
                            "resources": resources("512Mi", "1Gi"),
                            "trivy": {"resources": resources("256M", "1Gi")}}}},
        {"kind": "Deployment", "metadata": {"name": "trivy-server"},
         "spec": {"template": {"spec": {"containers": [
             {"resources": resources("256Mi", "512Mi")}
         ]}}}},
        {"kind": "ResourceQuota", "metadata": {"name": "default-quota", "namespace": "trivy-system"},
         "spec": {"hard": {"requests.cpu": "1" if concurrency == 1 else "2",
                           "requests.memory": "3Gi" if concurrency == 1 else "5Gi",
                           "limits.memory": "10Gi" if concurrency == 1 else "16Gi"}}},
    ]


class TrivyCapacity(unittest.TestCase):
    def test_profiles_have_enough_capacity(self):
        for concurrency in (1, 2):
            self.assertEqual(check_capacity(documents(concurrency), concurrency), [])

    def test_old_quota_blocks_requests_and_limits(self):
        docs = documents()
        docs[-1]["spec"]["hard"].update({"requests.memory": "2Gi", "limits.memory": "6Gi"})
        errors = check_capacity(docs, 1)
        self.assertTrue(any("requests.memory" in e for e in errors))
        self.assertTrue(any("limits.memory" in e for e in errors))

    def test_two_jobs_cannot_share_single_job_budget(self):
        docs = documents()
        docs[0]["spec"]["values"]["operator"]["scanJobsConcurrentLimit"] = 2
        self.assertTrue(check_capacity(docs, 2))

    def test_misspelled_concurrency_does_not_pass(self):
        docs = documents(2)
        docs[0]["spec"]["values"]["operator"] = {"scanJobConcurrentLimit": 2}
        self.assertTrue(any("misspelled" in e for e in check_capacity(docs, 2)))

    def test_init_images_count_towards_scan_budget(self):
        docs = documents()
        docs.append({"kind": "DaemonSet", "spec": {"template": {"spec": {
            "containers": [{}], "initContainers": [{}] * 9
        }}}})
        self.assertTrue(any("10-image" in e for e in check_capacity(docs, 1)))

    def test_larger_scanner_limit_requires_larger_quota(self):
        docs = deepcopy(documents())
        docs[0]["spec"]["values"]["trivy"]["resources"]["limits"]["memory"] = "2Gi"
        self.assertTrue(any("limits.memory" in e for e in check_capacity(docs, 1)))

    def test_decimal_memory_is_not_binary_memory(self):
        self.assertLess(quantity("1536M"), quantity("1536Mi"))
        self.assertEqual(quantity("1Gi"), 1024**3)
