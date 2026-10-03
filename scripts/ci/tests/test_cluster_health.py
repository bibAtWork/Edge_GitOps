"""Regression checks for active storage and image-pin health policy."""
import importlib.util
from pathlib import Path
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "cluster-health.py"
spec = importlib.util.spec_from_file_location("cluster_health", SCRIPT)
health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = health
spec.loader.exec_module(health)


class FakeCluster:
    def __init__(self, *, volumes=None, filer_ready=True, driver=True, csi_unready=None, pins=None):
        self.volumes = volumes if volumes is not None else [
            {"metadata": {"name": "live"}, "status": {"state": "attached", "robustness": "healthy"}},
            {"metadata": {"name": "old"}, "status": {"state": "detached", "robustness": "unknown",
                                                    "kubernetesStatus": {"pvStatus": "Released"}}},
        ]
        self.filer_ready = filer_ready
        self.driver = driver
        self.csi_unready = csi_unready or set()
        self.pins = pins or []

    def items(self, kind, *args):
        if kind == "helmrelease":
            return self.pins
        if kind == "csidrivers":
            return [{"metadata": {"name": "driver.longhorn.io"}}] if self.driver else []
        if kind == "volumes.longhorn.io":
            return self.volumes
        if kind == "pods" and "seaweedfs" in args:
            label = args[-1] if "-l" in args else ""
            if "component=" in label:
                component = label.split("component=")[1]
                ready = self.filer_ready if component == "filer" else True
                return [{"metadata": {"name": f"seaweedfs-{component}-0"},
                         "status": {"phase": "Running", "conditions": [{"type": "Ready", "status":
                                                                           "True" if ready else "False"}],
                                    "containerStatuses": [{"ready": ready}]}}]
            return []
        if kind == "pods" and "local-path-storage" in args:
            return [{"status": {"phase": "Running"}}]
        raise AssertionError((kind, args))

    def get_json(self, kind, name, *args):
        if name in {"longhorn-csi-plugin", "csi-attacher", "csi-provisioner",
                    "csi-resizer", "csi-snapshotter"}:
            if args != ("-n", "longhorn-system"):
                raise RuntimeError("wrong namespace")
            ready = 0 if name in self.csi_unready else 1
            if kind == "daemonsets":
                return {"status": {"desiredNumberScheduled": 1, "numberReady": ready}}
            if kind == "deployments":
                return {"spec": {"replicas": 1}, "status": {"readyReplicas": ready}}
            raise RuntimeError("wrong workload kind")
        if kind == "daemonsets" and name == "otel-agent":
            if args != ("-n", "monitoring-agents"):
                raise RuntimeError("not found")
            return {"status": {"desiredNumberScheduled": 1, "numberReady": 1}}
        return {"spec": {"replicas": 1, "instances": 1},
                "status": {"readyReplicas": 1, "readyInstances": 1}}


def pin(version, app_version):
    return {"metadata": {"name": "reloader"},
            "spec": {"values": {"image": {"tag": version}}},
            "status": {"history": [{"appVersion": app_version}]}}


class StorageHealth(unittest.TestCase):
    def test_active_storage_healthy_and_old_detached_volume_is_not_critical(self):
        results = health.check_storage(FakeCluster())
        self.assertFalse(any(not r.passed and r.severity == "critical" for r in results))
        self.assertNotIn("seaweedfs/csi-controller", [r.name for r in results])

    def test_unready_filer_cannot_be_hidden_by_filer_meta_database(self):
        results = health.check_storage(FakeCluster(filer_ready=False))
        self.assertFalse(next(r for r in results if r.name == "seaweedfs/filer").passed)
        self.assertFalse(next(r for r in results if r.name == "seaweedfs/filer-containers-ready").passed)

    def test_missing_longhorn_csi_and_faulted_attached_volume_fail(self):
        bad = [{"metadata": {"name": "live"}, "status": {"state": "attached", "robustness": "faulted"}}]
        results = health.check_storage(FakeCluster(volumes=bad, driver=False))
        self.assertFalse(next(r for r in results if r.name == "longhorn/csi-driver").passed)
        self.assertFalse(next(r for r in results if r.name == "longhorn/attached-volumes").passed)

    def test_unready_longhorn_csi_workload_is_critical(self):
        workloads = ("longhorn-csi-plugin", "csi-attacher", "csi-provisioner",
                     "csi-resizer", "csi-snapshotter")
        for name in workloads:
            with self.subTest(name=name):
                results = health.check_storage(FakeCluster(csi_unready={name}))
                result = next(r for r in results if r.name == f"longhorn/{name}")
                self.assertFalse(result.passed)
                self.assertEqual(result.severity, "critical")

    def test_otel_agent_uses_the_monitoring_agents_namespace(self):
        result = next(r for r in health.check_apps(FakeCluster())
                      if r.name == "monitoring-agents/otel-agent")
        self.assertTrue(result.passed)


class PinPolicy(unittest.TestCase):
    def test_same_major_lag_warns_without_failing_pre_update_gate(self):
        results = health.check_pins(FakeCluster(pins=[pin("v1.4.21", "1.4.22")]))
        lag = next(r for r in results if r.name == "reloader/image.tag")
        self.assertEqual(lag.severity, "warning")
        self.assertTrue(next(r for r in results if r.name == "summary").passed)

    def test_cross_major_lag_remains_critical(self):
        results = health.check_pins(FakeCluster(pins=[pin("v1.4.21", "2.0.0")]))
        self.assertEqual(next(r for r in results if r.name == "reloader/image.tag").severity, "critical")
        self.assertFalse(next(r for r in results if r.name == "summary").passed)


if __name__ == "__main__":
    unittest.main()
