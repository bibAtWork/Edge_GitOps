#!/usr/bin/env python3
"""
dr.py — Disaster recovery orchestrator for the Talos + Flux home lab.

Scenarios
---------
  namespace   Restore one namespace from a Velero backup (cluster must be running)
  full        Full cluster rebuild: re-provision Talos → recover etcd from snapshot
              → re-bootstrap Flux → restore apps from Velero
  add-node    Attach a replacement node to an existing 3-node cluster without
              bootstrapping a new etcd cluster (requires etcd quorum on surviving nodes)

Usage
-----
  python3 scripts/dr.py namespace
  python3 scripts/dr.py full --profile 3-node --dry-run
  python3 scripts/dr.py add-node --existing-node-ip 192.168.1.10 --new-node-ip 192.168.1.13
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence

# ── Terminal colours (no external deps) ──────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
DIM    = "\033[2m"

def _c(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text

def ok(msg: str)    -> None: print(_c(GREEN,  f"  ✓ {msg}"))
def warn(msg: str)  -> None: print(_c(YELLOW, f"  ⚠ {msg}"))
def err(msg: str)   -> None: print(_c(RED,    f"  ✗ {msg}"), file=sys.stderr)
def info(msg: str)  -> None: print(_c(DIM,    f"    {msg}"))
def step(msg: str)  -> None: print(_c(CYAN,   f"\n▸ {msg}"))
def phase(msg: str) -> None: print(_c(BOLD,   f"\n{'═'*60}\n  {msg}\n{'═'*60}"))

def abort(msg: str) -> None:
    err(msg)
    sys.exit(1)

def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(_c(YELLOW, f"\n  {prompt}{suffix}")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        abort("Aborted by user.")
    if not answer:
        return default
    return answer in ("y", "yes")

def choose(prompt: str, options: List[str]) -> str:
    """Interactive numbered selection from a list."""
    print(_c(CYAN, f"\n  {prompt}"))
    for i, opt in enumerate(options, 1):
        print(f"    {_c(BOLD, str(i))}. {opt}")
    while True:
        try:
            raw = input(_c(YELLOW, "  Enter number: ")).strip()
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except (ValueError, EOFError, KeyboardInterrupt):
            pass
        warn("Invalid selection, try again.")

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(_c(YELLOW, f"  {prompt}{suffix}: ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        abort("Aborted by user.")
    return val or default


# ── Command runner ────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def run(
        self,
        cmd: Sequence[str],
        *,
        check: bool = True,
        capture: bool = False,
        env: Optional[dict] = None,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        display = " ".join(str(c) for c in cmd)
        if self.dry_run:
            info(f"[dry-run] {display}")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        info(f"$ {display}")
        merged_env = {**os.environ, **(env or {})}
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=capture,
            text=True,
            env=merged_env,
            input=input,
        )
        if check and result.returncode != 0:
            if capture:
                err(result.stderr.strip() or result.stdout.strip())
            abort(f"Command failed (exit {result.returncode}): {display}")
        return result

    def run_shell(self, script: str, *, check: bool = True, env: Optional[dict] = None) -> subprocess.CompletedProcess:
        info(f"$ {script[:120]}{'...' if len(script) > 120 else ''}")
        if self.dry_run:
            return subprocess.CompletedProcess(["sh"], 0, stdout="", stderr="")
        result = subprocess.run(
            script, shell=True, check=False, text=True,
            env={**os.environ, **(env or {})},
        )
        if check and result.returncode != 0:
            abort(f"Shell script failed (exit {result.returncode})")
        return result

    def output(self, cmd: Sequence[str]) -> str:
        result = self.run(cmd, capture=True, check=False)
        return result.stdout.strip()


# ── Pre-flight checks ─────────────────────────────────────────────────────────

REQUIRED_TOOLS = {
    "namespace": ["kubectl", "velero"],
    "full":      ["talosctl", "kubectl", "flux", "age", "velero", "aws"],
    "add-node":  ["talosctl", "kubectl"],
}

def preflight_tools(scenario: str) -> None:
    step("Pre-flight: checking required tools")
    missing = []
    for tool in REQUIRED_TOOLS.get(scenario, []):
        if shutil.which(tool) is None:
            missing.append(tool)
        else:
            ok(tool)
    if missing:
        abort(
            f"Missing tools: {', '.join(missing)}\n"
            "  Run: ansible-playbook -i ansible/inventory.yml ansible/install-tools.yml"
        )

def preflight_age_key(age_key: Path) -> None:
    step("Pre-flight: validating age private key")
    if not age_key.exists():
        abort(
            f"Age key not found at: {age_key}\n"
            "  This key must be retrieved from your offline storage (password manager).\n"
            "  Without it you cannot decrypt the etcd snapshot."
        )
    ok(f"Age key found: {age_key}")

def preflight_secrets_bundle(secrets: Path) -> None:
    step("Pre-flight: validating Talos secrets bundle")
    if not secrets.exists():
        abort(
            f"Talos secrets.yaml not found at: {secrets}\n"
            "  Retrieve from your password manager.\n"
            "  Without it the new nodes will form a different cluster."
        )
    ok(f"Secrets bundle found: {secrets}")

def preflight_cluster_reachable(runner: Runner, node_ip: str) -> None:
    step("Pre-flight: checking cluster connectivity")
    result = runner.run(
        ["kubectl", "get", "nodes", "-o", "name"],
        capture=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        ok("kubectl can reach cluster")
    else:
        warn("kubectl cannot reach cluster — expected for full rebuild scenario")


# ── Velero helpers ────────────────────────────────────────────────────────────

def list_velero_backups(runner: Runner) -> List[str]:
    result = runner.run(
        ["velero", "backup", "get", "-o", "json"],
        capture=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
        items = data.get("items", [])
        return [
            item["metadata"]["name"]
            for item in items
            if item.get("status", {}).get("phase") == "Completed"
        ]
    except (json.JSONDecodeError, KeyError):
        return []

def list_s3_backups(runner: Runner, bucket: str, prefix: str = "") -> List[str]:
    result = runner.run(
        ["aws", "s3", "ls", f"s3://{bucket}/{prefix}"],
        capture=True, check=False,
    )
    if result.returncode != 0:
        return []
    lines = [l.strip().split()[-1] for l in result.stdout.strip().splitlines() if l.strip()]
    return sorted(lines, reverse=True)


# ── Scenario: namespace restore ───────────────────────────────────────────────

def scenario_namespace(runner: Runner, args: argparse.Namespace) -> None:
    phase("Scenario: Namespace Restore")

    preflight_tools("namespace")
    preflight_cluster_reachable(runner, "")

    step("Listing completed Velero backups")
    backups = list_velero_backups(runner)
    if not backups:
        abort("No completed Velero backups found. Is Velero running?")
    for b in backups[:10]:
        info(b)

    backup_name = args.backup_name or choose("Select backup to restore from", backups[:10])
    namespace   = args.namespace or ask("Namespace to restore")
    restore_name = f"dr-{namespace}-{int(time.time())}"

    if not confirm(f"Restore namespace '{namespace}' from backup '{backup_name}'?"):
        abort("Cancelled.")

    step(f"Creating Velero restore: {restore_name}")
    runner.run([
        "velero", "restore", "create", restore_name,
        "--from-backup", backup_name,
        "--include-namespaces", namespace,
        "--restore-volumes=true",
        "--wait",
    ])

    step("Verifying restore")
    runner.run(["velero", "restore", "describe", restore_name])
    runner.run(["kubectl", "get", "pods", "-n", namespace])

    ok(f"Namespace '{namespace}' restored from '{backup_name}'.")


# ── Scenario: full cluster rebuild ────────────────────────────────────────────

def scenario_full(runner: Runner, args: argparse.Namespace) -> None:
    phase("Scenario: Full Cluster Rebuild")

    repo_root = Path(__file__).parent.parent
    profile   = args.profile

    # ── Gather required inputs ────────────────────────────────────────────────

    age_key     = Path(args.age_key     or ask("Path to age private key (from offline storage)"))
    secrets     = Path(args.secrets     or ask("Path to Talos secrets.yaml (from offline storage)"))
    etcd_bucket = args.etcd_bucket      or ask("AWS S3 bucket for etcd backups", "homelab-etcd-backups-offsite")
    github_owner = args.github_owner    or ask("GitHub owner")
    github_repo  = args.github_repo     or ask("GitHub repo", "Edge_GitOps")
    github_token = args.github_token    or os.environ.get("GITHUB_TOKEN") or ask("GitHub token")

    if profile == "3-node":
        node1 = args.node1_ip or ask("Node 1 IP")
        node2 = args.node2_ip or ask("Node 2 IP")
        node3 = args.node3_ip or ask("Node 3 IP")
        vip   = args.vip      or ask("VIP / first control-plane endpoint IP")
        nodes  = [node1, node2, node3]
        endpoint = f"https://{vip}:6443"
        overlay_path = "cluster/overlays/3-node"
        machineconfig = repo_root / "cluster/overlays/3-node/talos-machineconfigs/controlplane.yaml"
    else:
        node1 = args.node_ip or ask("Node IP")
        nodes  = [node1]
        endpoint = f"https://{node1}:6443"
        overlay_path = "cluster/overlays/1-node"
        machineconfig = repo_root / "cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml"

    preflight_tools("full")
    preflight_age_key(age_key)
    preflight_secrets_bundle(secrets)

    print(f"""
  {_c(BOLD, 'Recovery plan')}
  Profile   : {profile}
  Nodes     : {', '.join(nodes)}
  Endpoint  : {endpoint}
  etcd S3   : s3://{etcd_bucket}
  Flux path : {overlay_path}
  GitHub    : {github_owner}/{github_repo}
    """)

    if not confirm("This will WIPE and re-provision the nodes. Continue?"):
        abort("Cancelled.")

    with tempfile.TemporaryDirectory(prefix="dr-") as tmpdir:
        tmp = Path(tmpdir)

        # ── Phase 1: Talos config generation ─────────────────────────────────

        phase("Phase 1: Generate Talos machine configs")
        generated = tmp / "generated"
        runner.run([
            "talosctl", "gen", "config", "homelab", endpoint,
            "--with-secrets", str(secrets),
            "--config-patch-control-plane", f"@{machineconfig}",
            "--output-dir", str(generated),
        ])
        ok("Machine configs generated")

        talosconfig = generated / "talosconfig"
        cp_config   = generated / "controlplane.yaml"

        # ── Phase 2: Apply Talos config to nodes ─────────────────────────────

        phase("Phase 2: Apply Talos config to nodes")
        for node in nodes:
            step(f"Applying config to {node}")
            runner.run([
                "talosctl", "apply-config",
                "--insecure", "--nodes", node,
                "--file", str(cp_config),
            ])
            ok(f"{node} configured")

        step("Waiting 90s for nodes to boot Talos")
        if not runner.dry_run:
            time.sleep(90)

        # ── Phase 3: Fetch and decrypt etcd snapshot ──────────────────────────

        phase("Phase 3: Fetch and decrypt etcd snapshot from S3")
        step(f"Listing snapshots in s3://{etcd_bucket}/")
        snapshots = list_s3_backups(runner, etcd_bucket)
        if not snapshots:
            abort(f"No snapshots found in s3://{etcd_bucket}/")

        latest_encrypted = snapshots[0]
        info(f"Latest: {latest_encrypted}")
        if len(snapshots) > 1 and confirm("Use latest snapshot? (No to choose manually)", default=True):
            chosen_snapshot = latest_encrypted
        elif len(snapshots) > 1:
            chosen_snapshot = choose("Select snapshot", snapshots[:10])
        else:
            chosen_snapshot = latest_encrypted

        encrypted_path = tmp / "etcd.snapshot.age"
        decrypted_path = tmp / "etcd.snapshot"

        step(f"Downloading s3://{etcd_bucket}/{chosen_snapshot}")
        runner.run(["aws", "s3", "cp", f"s3://{etcd_bucket}/{chosen_snapshot}", str(encrypted_path)])

        step("Decrypting snapshot with age key")
        runner.run([
            "age", "--decrypt",
            "-i", str(age_key),
            "-o", str(decrypted_path),
            str(encrypted_path),
        ])
        ok("Snapshot decrypted")

        # ── Phase 4: Bootstrap etcd from snapshot ─────────────────────────────

        phase("Phase 4: Bootstrap etcd from snapshot")
        runner.run([
            "talosctl", "bootstrap",
            "--nodes", nodes[0],
            "--talosconfig", str(talosconfig),
            f"--recover-from={decrypted_path}",
        ])

        step("Waiting 120s for cluster to form")
        if not runner.dry_run:
            time.sleep(120)

        # ── Phase 5: Get kubeconfig ───────────────────────────────────────────

        phase("Phase 5: Retrieve kubeconfig")
        runner.run([
            "talosctl", "kubeconfig",
            "--nodes", nodes[0],
            "--talosconfig", str(talosconfig),
            "--force",
        ])
        ok("kubeconfig updated")

        # Verify nodes are visible
        runner.run(["kubectl", "get", "nodes"])

        # ── Phase 6: Re-bootstrap Flux ────────────────────────────────────────

        phase("Phase 6: Re-bootstrap Flux")
        sops_age_key = Path(args.sops_age_key) if args.sops_age_key else None
        if sops_age_key is None:
            warn("SOPS age key path not provided — Flux will reconcile but cannot decrypt secrets.")
            warn("Pass --sops-age-key to fully restore encrypted secrets.")
        else:
            preflight_age_key(sops_age_key)
            step("Creating SOPS age secret in flux-system")
            runner.run(["kubectl", "create", "namespace", "flux-system",
                        "--dry-run=client", "-o", "yaml"], capture=True)
            runner.run_shell(
                f"kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -"
            )
            runner.run_shell(
                f"kubectl create secret generic sops-age "
                f"--namespace=flux-system "
                f"--from-file=age.agekey={sops_age_key} "
                f"--dry-run=client -o yaml | kubectl apply -f -"
            )
            ok("SOPS age secret applied")

        step("Running flux bootstrap")
        runner.run([
            "flux", "bootstrap", "github",
            "--owner", github_owner,
            "--repository", github_repo,
            "--path", overlay_path,
            "--personal",
            "--components-extra=image-reflector-controller,image-automation-controller",
        ], env={"GITHUB_TOKEN": github_token})

        # ── Phase 7: Wait for SeaweedFS ───────────────────────────────────────

        phase("Phase 7: Wait for SeaweedFS and recreate buckets")
        step("Waiting for SeaweedFS filer to become ready (up to 10 min)")
        runner.run([
            "kubectl", "wait",
            "--for=condition=ready", "pod",
            "-l", "app.kubernetes.io/component=filer",
            "-n", "seaweedfs",
            "--timeout=600s",
        ])

        s3_key    = runner.output(["kubectl", "get", "secret", "seaweedfs-s3-secret",
                                   "-n", "seaweedfs", "-o",
                                   "jsonpath={.data.admin_access_key_id}"]).strip()
        s3_secret = runner.output(["kubectl", "get", "secret", "seaweedfs-s3-secret",
                                   "-n", "seaweedfs", "-o",
                                   "jsonpath={.data.admin_secret_access_key}"]).strip()
        if s3_key:
            import base64
            s3_key    = base64.b64decode(s3_key).decode()
            s3_secret = base64.b64decode(s3_secret).decode()

        step("Creating SeaweedFS S3 buckets")
        bucket_cmd = (
            "aws s3 mb s3://etcd-backups   --endpoint-url http://seaweedfs-s3.seaweedfs.svc:8333 && "
            "aws s3 mb s3://velero-backups --endpoint-url http://seaweedfs-s3.seaweedfs.svc:8333 && "
            "aws s3 mb s3://zot-registry   --endpoint-url http://seaweedfs-s3.seaweedfs.svc:8333 || true"
        )
        runner.run([
            "kubectl", "run", "dr-bucket-init", "--rm", "-it",
            "--image=amazon/aws-cli", "--restart=Never",
            f"--env=AWS_ACCESS_KEY_ID={s3_key}",
            f"--env=AWS_SECRET_ACCESS_KEY={s3_secret}",
            "--", "sh", "-c", bucket_cmd,
        ])

        if profile == "1-node":
            step("Configuring 1-node bucket-to-collection routing")
            weed_cmds = (
                "s3.bucket.create -name pvs            -collection primary\n"
                "s3.bucket.create -name zot-registry   -collection primary\n"
                "s3.bucket.create -name etcd-backups   -collection backup\n"
                "s3.bucket.create -name velero-backups -collection backup\n"
            )
            runner.run([
                "kubectl", "exec", "-n", "seaweedfs", "seaweedfs-master-0",
                "--", "weed", "shell",
            ], input=weed_cmds)

        # ── Phase 8: Velero restore ───────────────────────────────────────────

        phase("Phase 8: Restore applications from Velero")
        step("Waiting for Velero to become ready")
        runner.run([
            "kubectl", "wait",
            "--for=condition=ready", "pod",
            "-l", "app.kubernetes.io/name=velero",
            "-n", "velero",
            "--timeout=300s",
        ])

        backups = list_velero_backups(runner)
        if not backups:
            warn("No Velero backups found yet — Velero may still be syncing from S3.")
            warn("Run `velero restore create` manually once backups appear.")
        else:
            backup_name  = choose("Select Velero backup to restore from", backups[:10])
            restore_name = f"dr-full-{int(time.time())}"

            if confirm(f"Restore all namespaces from '{backup_name}'?", default=True):
                runner.run([
                    "velero", "restore", "create", restore_name,
                    "--from-backup", backup_name,
                    "--restore-volumes=true",
                    "--wait",
                ])
                runner.run(["velero", "restore", "describe", restore_name])
                ok("Application restore complete")

    # ── Phase 9: Verification ─────────────────────────────────────────────────

    phase("Phase 9: Verification")
    runner.run(["kubectl", "get", "nodes"])
    runner.run(["kubectl", "get", "pods", "-A", "--field-selector=status.phase!=Running",
                "--field-selector=status.phase!=Succeeded"])
    runner.run(["flux", "get", "all"])
    runner.run(["velero", "backup-location", "get"])

    phase("Recovery Complete")
    ok("Full cluster rebuild finished.")
    warn("Reminder: remove any offline key files that were temporarily copied to this machine.")


# ── Scenario: add / replace node ─────────────────────────────────────────────

def scenario_add_node(runner: Runner, args: argparse.Namespace) -> None:
    phase("Scenario: Add / Replace Node (3-node)")

    repo_root = Path(__file__).parent.parent

    existing_ip = args.existing_node_ip or ask("IP of an existing, healthy node")
    new_ip      = args.new_node_ip      or ask("IP of the new/replacement node")
    secrets     = Path(args.secrets     or ask("Path to Talos secrets.yaml"))
    role        = args.node_role        or choose("Node role", ["controlplane", "worker"])

    preflight_tools("add-node")
    preflight_secrets_bundle(secrets)

    machineconfig_path = (
        repo_root / "cluster/overlays/3-node/talos-machineconfigs/controlplane.yaml"
        if role == "controlplane"
        else repo_root / "cluster/overlays/3-node/talos-machineconfigs/worker.yaml"
    )
    endpoint = f"https://{existing_ip}:6443"

    print(f"""
  {_c(BOLD, 'Add-node plan')}
  Existing node : {existing_ip}
  New node      : {new_ip}
  Role          : {role}
  Machineconfig : {machineconfig_path}
    """)

    if not confirm(f"Apply Talos config to {new_ip} and join it to the cluster?"):
        abort("Cancelled.")

    # ── Generate config using the original secrets bundle ─────────────────────

    phase("Phase 1: Generate machine config with original secrets")
    with tempfile.TemporaryDirectory(prefix="dr-addnode-") as tmpdir:
        tmp = Path(tmpdir)

        runner.run([
            "talosctl", "gen", "config", "homelab", endpoint,
            "--with-secrets", str(secrets),
            f"--config-patch-control-plane=@{machineconfig_path}",
            "--output-dir", str(tmp),
        ])
        ok("Config generated with original secrets — new node will join existing etcd")

        config_file = tmp / f"{role}.yaml"
        talosconfig = tmp / "talosconfig"

        # ── Apply config — do NOT bootstrap ───────────────────────────────────

        phase("Phase 2: Apply config to new node (no bootstrap)")
        info("Note: talosctl bootstrap is NOT run here. Running it would create a new cluster.")
        runner.run([
            "talosctl", "apply-config",
            "--insecure", "--nodes", new_ip,
            "--file", str(config_file),
        ])
        ok(f"Config applied to {new_ip}")

        step("Waiting 60s for node to boot")
        if not runner.dry_run:
            time.sleep(60)

        # ── Verify etcd membership expanded ───────────────────────────────────

        phase("Phase 3: Verify etcd membership")
        runner.run([
            "talosctl", "-n", existing_ip,
            "--talosconfig", str(talosconfig),
            "etcd", "members",
        ])
        ok("Check that 3 members are listed above, all healthy")

    # ── Rebalance SeaweedFS volumes if control-plane ───────────────────────────

    if role == "controlplane":
        phase("Phase 4: Rebalance SeaweedFS volumes")
        if confirm("Run SeaweedFS volume rebalance? (recommended after adding a volume server)", default=True):
            runner.run([
                "kubectl", "exec", "-n", "seaweedfs", "seaweedfs-master-0",
                "--", "weed", "shell",
            ], input="volume.balance\nvolume.fix.replication\n")
            ok("Volume rebalance triggered — this runs in the background")

    phase("Node Addition Complete")
    ok(f"Node {new_ip} has joined the cluster.")


# ── Entry point ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Disaster recovery orchestrator for the Talos + Flux home lab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without executing them")

    sub = p.add_subparsers(dest="scenario", required=True)

    # ── namespace ──────────────────────────────────────────────────────────────
    ns = sub.add_parser("namespace", help="Restore a single namespace from Velero")
    ns.add_argument("--namespace",    help="Namespace to restore")
    ns.add_argument("--backup-name",  dest="backup_name", help="Velero backup name")

    # ── full ───────────────────────────────────────────────────────────────────
    fl = sub.add_parser("full", help="Full cluster rebuild from etcd snapshot + Velero")
    fl.add_argument("--profile",       choices=["3-node", "1-node"], default="3-node")
    fl.add_argument("--age-key",       help="Path to talos-backup age private key")
    fl.add_argument("--sops-age-key",  help="Path to SOPS age private key (for Flux decryption)")
    fl.add_argument("--secrets",       help="Path to Talos secrets.yaml bundle")
    fl.add_argument("--etcd-bucket",   help="AWS S3 bucket containing etcd snapshots")
    fl.add_argument("--github-owner",  help="GitHub repository owner")
    fl.add_argument("--github-repo",   help="GitHub repository name")
    fl.add_argument("--github-token",  help="GitHub token (default: $GITHUB_TOKEN)")
    fl.add_argument("--node1-ip",      dest="node1_ip")
    fl.add_argument("--node2-ip",      dest="node2_ip")
    fl.add_argument("--node3-ip",      dest="node3_ip")
    fl.add_argument("--node-ip",       dest="node_ip",  help="Single-node IP (1-node profile)")
    fl.add_argument("--vip",           help="Virtual IP / first control-plane endpoint (3-node)")

    # ── add-node ───────────────────────────────────────────────────────────────
    an = sub.add_parser("add-node", help="Attach a replacement node to an existing 3-node cluster")
    an.add_argument("--existing-node-ip", dest="existing_node_ip")
    an.add_argument("--new-node-ip",      dest="new_node_ip")
    an.add_argument("--secrets",          help="Path to Talos secrets.yaml bundle")
    an.add_argument("--node-role",        dest="node_role", choices=["controlplane", "worker"])

    return p


def main() -> None:
    args = build_parser().parse_args()
    runner = Runner(dry_run=args.dry_run)

    if args.dry_run:
        warn("DRY-RUN mode — no commands will be executed\n")

    try:
        if args.scenario == "namespace":
            scenario_namespace(runner, args)
        elif args.scenario == "full":
            scenario_full(runner, args)
        elif args.scenario == "add-node":
            scenario_add_node(runner, args)
    except KeyboardInterrupt:
        print()
        abort("Interrupted by user.")


if __name__ == "__main__":
    main()
