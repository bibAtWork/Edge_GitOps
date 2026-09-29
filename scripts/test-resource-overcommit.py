#!/usr/bin/env python3
"""Tests for resource-overcommit.py. Run: python3 scripts/test-resource-overcommit.py

Each case is either arithmetic this script has to get right (unit parsing, the
scheduler's effective-request formula for sidecars and init containers) or the
specific discrepancy that prompted it: two backlog entries recording the same
"memory limits vs allocatable" quantity five weeks apart, 121% and 164%, that
turned out to differ in how they summed rather than in what the cluster was
doing -- kube_pod_container_resource_limits keeps counting pods that have
finished, and completed workflow/CronJob pods sit in the API until they are
garbage collected.
"""
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "scripts")
import importlib
overcommit = importlib.import_module("resource-overcommit")


def pod(phase="Running", node="n1", containers=None, init_containers=None, overhead=None):
    return {
        "metadata": {"namespace": "ns", "name": "p"},
        "status": {"phase": phase},
        "spec": {
            "nodeName": node,
            "containers": containers or [],
            "initContainers": init_containers or [],
            **({"overhead": overhead} if overhead else {}),
        },
    }


def container(name="c", requests=None, limits=None):
    return {"name": name, "resources": {"requests": requests or {}, "limits": limits or {}}}


class Quantity(unittest.TestCase):
    def test_binary_suffixes(self):
        self.assertEqual(overcommit.quantity("1Gi"), 1024**3)
        self.assertEqual(overcommit.quantity("512Mi"), 512 * 1024**2)
        self.assertEqual(overcommit.quantity("1Ki"), 1024)

    def test_decimal_suffixes_are_not_binary(self):
        self.assertEqual(overcommit.quantity("1G"), 1_000_000_000)
        self.assertEqual(overcommit.quantity("1M"), 1_000_000)

    def test_millicpu(self):
        self.assertEqual(overcommit.quantity("500m"), 0.5)
        self.assertEqual(overcommit.quantity("1500m"), 1.5)

    def test_bare_cpu_cores(self):
        self.assertEqual(overcommit.quantity("2"), 2.0)

    def test_absent_is_zero(self):
        self.assertEqual(overcommit.quantity(None), 0.0)


class EffectiveResources(unittest.TestCase):
    def test_two_app_containers_sum(self):
        p = pod(containers=[container(limits={"memory": "1Gi"}), container("c2", limits={"memory": "512Mi"})])
        self.assertEqual(overcommit.effective(p, "limits", "memory"), 1.5 * 1024**3)

    def test_a_non_restarting_init_container_does_not_add_to_the_app_total(self):
        # A regular init container runs to completion before any app container
        # starts, so its own request never overlaps with the app containers'.
        p = pod(containers=[container(requests={"memory": "512Mi"})],
                init_containers=[{"name": "init", "resources": {"requests": {"memory": "2Gi"}}}])
        # The init container alone (2Gi) exceeds the app total (512Mi), so the
        # pod's effective request is the init peak, not their sum.
        self.assertEqual(overcommit.effective(p, "requests", "memory"), 2 * 1024**3)

    def test_a_small_init_container_does_not_lower_the_apps_total(self):
        p = pod(containers=[container(requests={"memory": "512Mi"})],
                init_containers=[{"name": "init", "resources": {"requests": {"memory": "64Mi"}}}])
        self.assertEqual(overcommit.effective(p, "requests", "memory"), 512 * 1024**2)

    def test_a_restartable_sidecar_init_container_keeps_running_and_adds_to_the_total(self):
        # restartPolicy: Always on an init container (a "native sidecar") keeps
        # running alongside the app containers, so it is not just a peak.
        p = pod(containers=[container(requests={"memory": "512Mi"})],
                init_containers=[{"name": "sidecar", "restartPolicy": "Always",
                                  "resources": {"requests": {"memory": "128Mi"}}}])
        self.assertEqual(overcommit.effective(p, "requests", "memory"), 640 * 1024**2)

    def test_pod_overhead_is_added(self):
        p = pod(containers=[container(requests={"memory": "512Mi"})], overhead={"memory": "50Mi"})
        self.assertEqual(overcommit.effective(p, "requests", "memory"), 562 * 1024**2)

    def test_no_containers_is_zero(self):
        self.assertEqual(overcommit.effective(pod(), "limits", "memory"), 0.0)


class CountsAgainstTheNode(unittest.TestCase):
    def test_a_running_placed_pod_counts(self):
        self.assertTrue(overcommit.counts_against_the_node(pod(phase="Running", node="n1")))

    def test_a_finished_pod_does_not_hold_anything(self):
        # The exact discrepancy: kube_pod_container_resource_limits keeps
        # counting a pod after it finishes; the scheduler does not.
        self.assertFalse(overcommit.counts_against_the_node(pod(phase="Succeeded", node="n1")))
        self.assertFalse(overcommit.counts_against_the_node(pod(phase="Failed", node="n1")))

    def test_an_unplaced_pod_does_not_hold_anything_yet(self):
        self.assertFalse(overcommit.counts_against_the_node(pod(phase="Pending", node=None)))

    def test_a_placed_pending_pod_counts(self):
        # Scheduled but not yet Running still reserves its request.
        self.assertTrue(overcommit.counts_against_the_node(pod(phase="Pending", node="n1")))


class Totals(unittest.TestCase):
    def test_finished_pods_are_excluded_from_the_sum(self):
        # This is the bug this script exists to not have: a naive sum over all
        # pods, including completed ones, overstates every total.
        pods = [
            pod(containers=[container(requests={"memory": "1Gi"}, limits={"memory": "2Gi"})]),
            pod(phase="Succeeded", containers=[container(requests={"memory": "5Gi"}, limits={"memory": "5Gi"})]),
        ]
        result = overcommit.totals(pods, "memory")
        self.assertEqual(result["request"], 1024**3)
        self.assertEqual(result["limit"], 2 * 1024**3)
        self.assertEqual(result["pods"], 1)

    def test_grouped_by_namespace(self):
        a = pod(containers=[container(limits={"memory": "1Gi"})])
        a["metadata"]["namespace"] = "a"
        b = pod(containers=[container(limits={"memory": "2Gi"})])
        b["metadata"]["namespace"] = "b"
        result = overcommit.totals([a, b], "memory")
        self.assertEqual(result["by_namespace"]["a"][1], 1024**3)
        self.assertEqual(result["by_namespace"]["b"][1], 2 * 1024**3)

    def test_empty_input(self):
        result = overcommit.totals([], "memory")
        self.assertEqual((result["request"], result["limit"], result["pods"]), (0, 0, 0))


class Uncapped(unittest.TestCase):
    def test_init_container_without_limit_is_reported(self):
        p = pod(containers=[container(limits={"memory": "1Gi"})],
                init_containers=[container("init", requests={"memory": "64Mi"})])
        self.assertEqual(overcommit.uncapped([p]), [("ns", "p", "init")])

    def test_a_container_with_no_memory_limit_is_reported(self):
        p = pod(containers=[container("nolimit", requests={"memory": "64Mi"})])
        found = overcommit.uncapped([p])
        self.assertEqual(found, [("ns", "p", "nolimit")])

    def test_a_container_with_a_limit_is_not_reported(self):
        p = pod(containers=[container(limits={"memory": "1Gi"})])
        self.assertEqual(overcommit.uncapped([p]), [])

    def test_a_finished_pods_uncapped_container_does_not_count(self):
        p = pod(phase="Succeeded", containers=[container(requests={"memory": "64Mi"})])
        self.assertEqual(overcommit.uncapped([p]), [])

    def test_only_the_requested_resource_is_checked(self):
        p = pod(containers=[container(limits={"cpu": "500m"})])  # memory limit absent
        self.assertEqual(overcommit.uncapped([p], resource="memory"), [("ns", "p", "c")])
        self.assertEqual(overcommit.uncapped([p], resource="cpu"), [])


class Usage(unittest.TestCase):
    @staticmethod
    def series(pod_name, value):
        return {"metric": {"namespace": "ns", "pod": pod_name, "container": "main"},
                "value": [0, str(value)]}

    def test_same_container_name_in_different_pods_stays_separate(self):
        with patch.object(overcommit, "vm_query", side_effect=[
            [self.series("a", 1024), self.series("b", 2048)],
            [self.series("a", 256), self.series("b", 512)],
            [self.series("a", 512), self.series("b", 1024)],
            [self.series("a", 400), self.series("b", 800)],
        ]) as query:
            rows = overcommit.usage_rows()
        self.assertEqual({r["pod"]: (r["limit"], r["max"]) for r in rows},
                         {"a": (1024, 512), "b": (2048, 1024)})
        self.assertTrue(all("namespace,pod,container" in c.args[0] for c in query.call_args_list))

    def test_missing_samples_are_not_counted_as_unused_headroom(self):
        with patch.object(overcommit, "vm_query", side_effect=[
            [self.series("a", 1024)], [], [], []]):
            rows = overcommit.usage_rows()
        self.assertIsNone(rows[0]["max"])
        text = overcommit.report(2048, 2, {"request": 0, "limit": 0, "pods": 1,
                                            "by_namespace": {}},
                                 {"request": 0, "limit": 0}, [], 5, rows)
        self.assertIn("headroom is unknown", text)
        self.assertIn("0 measured containers", text)


if __name__ == "__main__":
    unittest.main()
