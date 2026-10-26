"""Behavioral acceptance for the screener's recoverable PostgreSQL history."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, kustomize, parse_docs  # noqa: E402


def render(path):
    text, error = kustomize(ROOT / path)
    if error:
        raise AssertionError(error)
    return parse_docs(text)


def resource(docs, kind, name, namespace):
    return next(d for d in docs if d.get("kind") == kind
                and d.get("metadata", {}).get("name") == name
                and d.get("metadata", {}).get("namespace") == namespace)


class ScreenerRecoveryBehavior(unittest.TestCase):
    def test_given_unique_screener_history_when_manifests_render_then_a_separate_archive_is_restored_and_promoted(self):
        app = render("cluster/base/applications/real-estate-screener")
        deferred = render("cluster/base/applications/real-estate-screener/deferred")
        recovery = render("cluster/base/infrastructure/37-backup-system/deferred")
        access = render("cluster/base/infrastructure/37-backup-system")

        cluster = resource(deferred, "Cluster", "screener-pg", "real-estate-screener")
        self.assertIn(":16.15-minimal-bookworm@sha256:", cluster["spec"]["imageName"])
        self.assertTrue(any(p.get("name") == "barman-cloud.cloudnative-pg.io"
                            and p.get("isWALArchiver") for p in cluster["spec"]["plugins"]))
        archive = resource(deferred, "ObjectStore", "recovery", "real-estate-screener")
        self.assertEqual(archive["spec"]["configuration"]["destinationPath"],
                         "s3://screener-recovery/postgres/")
        self.assertEqual(archive["spec"]["configuration"]["s3Credentials"]["accessKeyId"]["name"],
                         "recovery-object-store")
        secret = resource(app, "Secret", "recovery-object-store", "real-estate-screener")
        self.assertTrue(secret["stringData"]["secret_access_key"].startswith("ENC["))

        policy = resource(recovery, "ConfigMap", "recovery-policy", "backup-system")["data"]
        self.assertIn("real-estate-screener important important", policy["applications"])
        self.assertIn("screener-db real-estate-screener cnpg cnpg:real-estate-screener/screener-pg screener-recovery",
                      " ".join(policy["datasets"].split()))
        self.assertIn("screener-db", policy["restore-checks"])
        for prefix, template in (("recovery-point", "recovery-point"), ("promote", "promote")):
            cron = resource(recovery, "CronWorkflow", f"{prefix}-real-estate-screener", "backup-system")
            self.assertEqual(cron["spec"]["workflowSpec"]["workflowTemplateRef"]["name"], template)
            self.assertEqual(cron["spec"]["workflowSpec"]["arguments"]["parameters"][0]["value"],
                             "real-estate-screener")
        workflow = resource(recovery, "WorkflowTemplate", "recovery-point", "backup-system")
        protect = next(t for t in workflow["spec"]["templates"] if t["name"] == "protect-dataset")
        cnpg = next(step for group in protect["steps"] for step in group if step["name"] == "cnpg")
        self.assertIn({"name": "archive-bucket", "value": "{{inputs.parameters.paths}}"},
                      cnpg["arguments"]["parameters"])
        dataset = resource(recovery, "WorkflowTemplate", "cnpg-dataset", "backup-system")
        stage = next(t for t in dataset["spec"]["templates"] if t["name"] == "stage")
        self.assertIn({"name": "archive-bucket"}, stage["inputs"]["parameters"])
        self.assertIn('SRC="local:$BUCKET/postgres/$SRV"', stage["initContainers"][0]["args"][0])
        role = resource(access, "Role", "recovery-base-backups", "real-estate-screener")
        self.assertTrue(any("backups" in rule["resources"] for rule in role["rules"]))
        resource(access, "RoleBinding", "recovery-base-backups", "real-estate-screener")

    def test_given_screener_recovery_policy_when_manifests_render_then_operator_and_monthly_drill_cover_it(self):
        docs = render("cluster/overlays/1-node-config")
        scope = resource(docs, "ValidatingPolicy", "argo-operator-workflow-scope", None)
        allowed = next(v["expression"] for v in scope["spec"]["variables"]
                       if v["name"] == "allowedApplications")
        self.assertIn("real-estate-screener", allowed)
        cron = resource(docs, "CronWorkflow", "drill-screener-pitr", "backup-system")
        self.assertEqual(cron["spec"]["workflowSpec"]["workflowTemplateRef"]["name"], "drill-screener-pitr")
        drill = resource(docs, "WorkflowTemplate", "drill-screener-pitr", "backup-system")
        self.assertEqual(drill["spec"]["onExit"], "cleanup")
        cluster = resource(docs, "Cluster", "screener-pg", "real-estate-screener")
        self.assertIn(cluster["spec"]["imageName"], str(drill["spec"]["templates"]))
        self.assertTrue(any(step.get("templateRef", {}).get("name") == "drill-evidence"
                            and step["arguments"]["parameters"][0]["value"] == "real-estate-screener"
                            for template in drill["spec"]["templates"]
                            for group in template.get("steps", []) for step in group))
        archive = resource(docs, "ObjectStore", "restore-screener-archive", "backup-system")
        self.assertEqual(archive["spec"]["configuration"]["destinationPath"],
                         "s3://screener-recovery/postgres/")
