#!/usr/bin/env python3
"""
dr.py — Disaster recovery orchestrator for the Talos + Flux home lab.

Scenarios
---------
  full        Full cluster rebuild: re-provision Talos → bootstrap a fresh etcd
              → re-bootstrap Flux → pause the recovery system's schedules, so that
              application data can be restored by hand from the AWS recovery vault
              (docs/runbooks/backup-recovery.md, Part A, A7)
  add-node    Attach a replacement node to an existing 3-node cluster without
              bootstrapping a new etcd cluster (requires etcd quorum on surviving nodes)

Usage
-----
  python3 scripts/dr.py full --profile 3-node --dry-run
  python3 scripts/dr.py add-node --existing-node-ip 192.168.1.10 --new-node-ip 192.168.1.13
"""

from __future__ import annotations

import argparse
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
    "full":      ["talosctl", "kubectl", "flux"],
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
            "  Without it Flux cannot decrypt the cluster's secrets."
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


def current_git_branch(repo_root: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else None


def read_versions_env(repo_root: Path) -> dict:
    """versions.env's TALOS_VERSION/KUBERNETES_VERSION -- the same file the
    live cluster's own upgrade Plans read (15-system-upgrade-controller).
    A rebuilt cluster that skips this boots whatever the operator's
    workstation happens to have `talosctl` default to, which drifts from
    what Git says the cluster should run the moment either one is bumped."""
    path = repo_root / "cluster/base/infrastructure/15-system-upgrade-controller/config/versions.env"
    values = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


# ── Scenario: full cluster rebuild ────────────────────────────────────────────

def scenario_full(runner: Runner, args: argparse.Namespace) -> None:
    phase("Scenario: Full Cluster Rebuild")

    repo_root = Path(__file__).resolve().parent.parent.parent
    profile   = args.profile

    # ── Gather required inputs ────────────────────────────────────────────────

    secrets     = Path(args.secrets     or ask("Path to Talos secrets.yaml (from offline storage)"))
    github_owner = args.github_owner    or ask("GitHub owner")
    github_repo  = args.github_repo     or ask("GitHub repo", "Edge_GitOps")
    github_token = args.github_token    or os.environ.get("GITHUB_TOKEN") or ask("GitHub token")
    # No --branch given -> `flux bootstrap` defaults to the repository's
    # default branch (main), not whatever this checkout is actually on. This
    # repo's live config lives on ops/talos_linux; main's copy of cluster/ is
    # months stale. Falling back to the CURRENT checkout's branch (same
    # pattern as bootstrap-1node.sh/bootstrap-3node.sh's GITHUB_BRANCH) rather
    # than hardcoding a name: whichever branch this script is actually being
    # run from is the one the operator means to deploy. Aborts rather than
    # silently defaulting to main if that can't be determined at all (a
    # detached HEAD, or dr.py copied out of a git checkout).
    github_branch = args.branch or current_git_branch(repo_root)
    if not github_branch:
        abort(
            "Could not determine which branch to bootstrap (not run from a git checkout, "
            "or HEAD is detached). Pass --branch explicitly."
        )

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
    preflight_secrets_bundle(secrets)

    print(f"""
  {_c(BOLD, 'Recovery plan')}
  Profile   : {profile}
  Nodes     : {', '.join(nodes)}
  Endpoint  : {endpoint}
  Flux path : {overlay_path}
  GitHub    : {github_owner}/{github_repo}@{github_branch}
    """)

    if not confirm("This will WIPE and re-provision the nodes. Continue?"):
        abort("Cancelled.")

    with tempfile.TemporaryDirectory(prefix="dr-") as tmpdir:
        tmp = Path(tmpdir)

        # ── Phase 1: Talos config generation ─────────────────────────────────

        phase("Phase 1: Generate Talos machine configs")
        versions = read_versions_env(repo_root)
        talos_version = versions.get("TALOS_VERSION")
        kubernetes_version = versions.get("KUBERNETES_VERSION")
        if not talos_version or not kubernetes_version:
            abort(f"Could not read TALOS_VERSION/KUBERNETES_VERSION from versions.env: {versions}")
        info(f"Pinning generated config to versions.env: Talos {talos_version}, Kubernetes {kubernetes_version}")
        generated = tmp / "generated"
        runner.run([
            "talosctl", "gen", "config", "homelab", endpoint,
            "--with-secrets", str(secrets),
            "--talos-version", talos_version,
            "--kubernetes-version", kubernetes_version.lstrip("v"),
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

        # ── Phase 3: Bootstrap etcd ───────────────────────────────────────────
        # A fresh etcd: everything in it is declared in Git and rebuilt by
        # Flux. Application data comes back from the recovery system instead
        # (phase 7).
        #
        # --endpoints is required, not redundant with --nodes: talosctl reads
        # its endpoint list from the talosconfig context if none is given on
        # the command line, and a freshly `gen config`'d one has none (checked
        # live -- `endpoints: []`). Without it, talosctl has nothing to dial
        # and fails with "failed to determine endpoints" before ever reaching
        # the node.
        phase("Phase 3: Bootstrap etcd")
        runner.run([
            "talosctl", "bootstrap",
            "--nodes", nodes[0],
            "--endpoints", nodes[0],
            "--talosconfig", str(talosconfig),
        ])

        step("Waiting 120s for cluster to form")
        if not runner.dry_run:
            time.sleep(120)

        # ── Phase 4: Get kubeconfig ───────────────────────────────────────────

        phase("Phase 4: Retrieve kubeconfig")
        runner.run([
            "talosctl", "kubeconfig",
            "--nodes", nodes[0],
            "--endpoints", nodes[0],
            "--talosconfig", str(talosconfig),
            "--force",
        ])
        ok("kubeconfig updated")

        # Verify nodes are visible
        runner.run(["kubectl", "get", "nodes"])

        # ── Phase 5: Re-bootstrap Flux ────────────────────────────────────────

        phase("Phase 5: Re-bootstrap Flux")
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
            "--branch", github_branch,
            "--path", overlay_path,
            "--personal",
            "--components-extra=image-reflector-controller,image-automation-controller",
        ], env={"GITHUB_TOKEN": github_token})

        # ── Phase 6: Pause the recovery system ────────────────────────────────
        # Before any data is restored: a rebuilt, still-empty application passes
        # its restore checks as readily as a real one, and the next scheduled
        # point would promote it to AWS (runbook A7). Git does not set
        # `suspend`, so the patch holds until it is lifted by hand.
        phase("Phase 6: Pause the recovery system's schedules")
        # Two waits, not one: `kubectl wait` on a condition errors out
        # immediately if the object doesn't exist yet rather than waiting for
        # it to appear, and Flux has only just been (re)bootstrapped -- it
        # has not necessarily created this Deployment the moment `flux
        # bootstrap` returns. --for=create waits for the object itself to
        # show up; only once it exists does waiting for Available mean
        # anything.
        step("Waiting for the Argo Workflows controller to exist (up to 15 min)")
        runner.run([
            "kubectl", "wait", "--for=create", "deployment",
            "argo-workflows-workflow-controller", "-n", "backup-system", "--timeout=900s",
        ])
        step("Waiting for the Argo Workflows controller to become available (up to 15 min)")
        runner.run([
            "kubectl", "wait", "--for=condition=available", "deployment",
            "argo-workflows-workflow-controller", "-n", "backup-system", "--timeout=900s",
        ])

        # The Deployment existing does not mean every CronWorkflow does too --
        # they're separate objects in the same Flux Kustomization, applied in
        # whatever order the API server happens to process them. An empty
        # list here is not "nothing to suspend", it's "asked too early": Git
        # does not set suspend:true on any of them, so an empty result would
        # report false success while `reconcile` (fires hourly at :30) and
        # every recovery-point/promote CronWorkflow sit unsuspended and live.
        # Retry until the expected set is actually there rather than trust a
        # single read.
        step("Waiting for CronWorkflows to exist (up to 5 min)")
        crons: List[str] = []
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            crons = runner.output([
                "kubectl", "get", "cronworkflows", "-n", "backup-system", "-o", "name",
            ]).split()
            if crons or runner.dry_run:
                break
            time.sleep(10)
        if not crons and not runner.dry_run:
            abort(
                "No CronWorkflows found in backup-system after 5 minutes -- Flux has not "
                "created them yet, or the Kustomization is failing. Suspending nothing here "
                "would leave the recovery system live against still-empty applications; "
                "investigate (`flux get kustomizations -A`) before continuing by hand."
            )
        for cron in crons:
            runner.run([
                "kubectl", "patch", "-n", "backup-system", cron,
                "--type", "merge", "-p", '{"spec":{"suspend":true}}',
            ])
        ok(f"{len(crons)} CronWorkflow(s) suspended")

    # ── Phase 7: Restore application data ────────────────────────────────────

    phase("Phase 7: Restore application data")
    info("Application data is restored by hand from the AWS recovery vault, in the")
    info("order and with the checks in docs/runbooks/backup-recovery.md, Part A, A7.")
    info("Resume the CronWorkflows (the same patch with suspend:false) once every")
    info("application is back and verified.")

    # ── Phase 8: Verification ─────────────────────────────────────────────────

    phase("Phase 8: Verification")
    runner.run(["kubectl", "get", "nodes"])
    runner.run(["kubectl", "get", "pods", "-A", "--field-selector=status.phase!=Running",
                "--field-selector=status.phase!=Succeeded"])
    runner.run(["flux", "get", "all"])

    phase("Recovery Complete")
    ok("Full cluster rebuild finished.")
    warn("Reminder: remove any offline key files that were temporarily copied to this machine.")


# ── Scenario: add / replace node ─────────────────────────────────────────────

def scenario_add_node(runner: Runner, args: argparse.Namespace) -> None:
    phase("Scenario: Add / Replace Node (3-node)")

    repo_root = Path(__file__).resolve().parent.parent.parent

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
    versions = read_versions_env(repo_root)
    talos_version = versions.get("TALOS_VERSION")
    kubernetes_version = versions.get("KUBERNETES_VERSION")
    if not talos_version or not kubernetes_version:
        abort(f"Could not read TALOS_VERSION/KUBERNETES_VERSION from versions.env: {versions}")
    info(f"Pinning generated config to versions.env: Talos {talos_version}, Kubernetes {kubernetes_version}")
    with tempfile.TemporaryDirectory(prefix="dr-addnode-") as tmpdir:
        tmp = Path(tmpdir)

        runner.run([
            "talosctl", "gen", "config", "homelab", endpoint,
            "--with-secrets", str(secrets),
            "--talos-version", talos_version,
            "--kubernetes-version", kubernetes_version.lstrip("v"),
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
        # -e/--endpoints, not just -n/--nodes: a freshly `gen config`'d
        # talosconfig has no endpoints of its own (checked live -- `endpoints:
        # []`), and -n alone selects which node the query is ABOUT, not which
        # node's Talos API to dial. Without -e, talosctl fails with "failed to
        # determine endpoints" before this ever reaches existing_ip.
        runner.run([
            "talosctl", "-n", existing_ip, "-e", existing_ip,
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

    # ── full ───────────────────────────────────────────────────────────────────
    fl = sub.add_parser("full", help="Full cluster rebuild from Git; application data restored by hand from the recovery system")
    fl.add_argument("--profile",       choices=["3-node", "1-node"], default="3-node")
    fl.add_argument("--sops-age-key",  help="Path to SOPS age private key (for Flux decryption)")
    fl.add_argument("--secrets",       help="Path to Talos secrets.yaml bundle")
    fl.add_argument("--github-owner",  help="GitHub repository owner")
    fl.add_argument("--github-repo",   help="GitHub repository name")
    fl.add_argument("--github-token",  help="GitHub token (default: $GITHUB_TOKEN)")
    fl.add_argument("--branch",        help="Branch to bootstrap Flux from (default: this checkout's current branch)")
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
        if args.scenario == "full":
            scenario_full(runner, args)
        elif args.scenario == "add-node":
            scenario_add_node(runner, args)
    except KeyboardInterrupt:
        print()
        abort("Interrupted by user.")


if __name__ == "__main__":
    main()
