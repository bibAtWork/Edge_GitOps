"""Behavioral contracts for the lakehouse's database and object recovery."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, kustomize, parse_docs  # noqa: E402


def rendered(path):
    text, error = kustomize(ROOT / path)
    if error:
        raise AssertionError(error)
    return parse_docs(text)


def resource(docs, kind, name, namespace):
    return next(d for d in docs if d.get("kind") == kind
                and d.get("metadata", {}).get("name") == name
                and d.get("metadata", {}).get("namespace") == namespace)


class LakehouseRecovery(unittest.TestCase):
    def test_given_dagster_history_when_recovery_runs_then_database_and_objects_are_restored_before_promotion(self):
        app = rendered("cluster/base/applications/lakehouse")
        deferred = rendered("cluster/base/applications/lakehouse/deferred")
        backup = rendered("cluster/base/infrastructure/37-backup-system/deferred")
        access = rendered("cluster/base/infrastructure/37-backup-system")

        cluster = resource(deferred, "Cluster", "lakehouse-pg", "lakehouse")
        self.assertTrue(any(p.get("name") == "barman-cloud.cloudnative-pg.io"
                            and p.get("isWALArchiver") for p in cluster["spec"]["plugins"]))
        archive = resource(deferred, "ObjectStore", "recovery", "lakehouse")
        self.assertEqual(archive["spec"]["configuration"]["destinationPath"],
                         "s3://lakehouse-data/postgres/")
        self.assertEqual(archive["spec"]["configuration"]["s3Credentials"]["accessKeyId"]["name"],
                         "lakehouse-s3")
        resource(app, "Secret", "lakehouse-s3", "lakehouse")

        policy = resource(backup, "ConfigMap", "recovery-policy", "backup-system")["data"]
        self.assertIn("lakehouse important important", " ".join(policy["applications"].split()))
        datasets = " ".join(policy["datasets"].split())
        self.assertIn("lakehouse-db lakehouse cnpg cnpg:lakehouse/lakehouse-pg lakehouse-data", datasets)
        self.assertIn("lakehouse-objects lakehouse s3-bucket s3:lakehouse/lakehouse-data -", datasets)
        self.assertIn("lakehouse-db", policy["restore-checks"])
        for prefix in ("recovery-point", "promote"):
            cron = resource(backup, "CronWorkflow", f"{prefix}-lakehouse", "backup-system")
            self.assertEqual(cron["spec"]["workflowSpec"]["arguments"]["parameters"][0]["value"],
                             "lakehouse")
        drill = resource(backup, "CronWorkflow", "drill-lakehouse-pitr", "backup-system")
        self.assertEqual(drill["spec"]["workflowSpec"]["workflowTemplateRef"]["name"],
                         "drill-lakehouse-pitr")
        drill_template = resource(backup, "WorkflowTemplate", "drill-lakehouse-pitr", "backup-system")
        self.assertIn("lakehouse", str(drill_template["spec"]["templates"][0]))
        resource(backup, "ObjectStore", "restore-lakehouse-archive", "backup-system")
        role = resource(access, "Role", "recovery-base-backups", "lakehouse")
        self.assertTrue(any("backups" in rule["resources"] for rule in role["rules"]))
        resource(access, "RoleBinding", "recovery-base-backups", "lakehouse")

        workflow = resource(backup, "WorkflowTemplate", "recovery-point", "backup-system")
        protect = next(t for t in workflow["spec"]["templates"] if t["name"] == "protect-dataset")
        self.assertTrue(any(step.get("name") == "s3-bucket" for group in protect["steps"] for step in group))
        obj = resource(backup, "WorkflowTemplate", "s3-bucket-dataset", "backup-system")
        scripts = "\n".join(container["args"][0]
                            for template in obj["spec"]["templates"]
                            for container in template.get("initContainers", []) + [template.get("container", {})]
                            if container.get("args"))
        self.assertIn("--exclude '/postgres/**'", scripts)
        self.assertIn("restic", scripts)
        self.assertIn("sha256sum", scripts)

    def test_given_lakehouse_archiver_when_enabled_then_its_s3_path_is_network_allowed(self):
        lakehouse = rendered("cluster/base/applications/lakehouse/deferred")
        seaweed = rendered("cluster/base/infrastructure/10-network-policies")
        egress = resource(lakehouse, "CiliumNetworkPolicy", "lakehouse-cnpg-s3-egress", "lakehouse")
        self.assertEqual(egress["spec"]["endpointSelector"]["matchLabels"]["cnpg.io/cluster"],
                         "lakehouse-pg")
        self.assertTrue(any(endpoint.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "seaweedfs"
                            for rule in egress["spec"]["egress"]
                            for endpoint in rule.get("toEndpoints", [])))
        ingress = resource(seaweed, "CiliumNetworkPolicy", "allow-seaweedfs-internal", "seaweedfs")
        self.assertTrue(any(endpoint.get("matchLabels", {}).get("cnpg.io/cluster") == "lakehouse-pg"
                            for rule in ingress["spec"]["ingress"]
                            for endpoint in rule.get("fromEndpoints", [])))
