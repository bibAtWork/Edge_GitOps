#!/usr/bin/env python3
"""Every control-plane machine config pins the kubelet's node IP to a subnet.

Guards the fault that took the cluster down on 2026-08-26.

Without machine.kubelet.nodeIP.validSubnets the kubelet picks a node IP from
whatever interfaces exist at start, which on a host running a Tailscale subnet
router is not deterministic. hostNetwork pods inherit it as POD_IP, and
kube-apiserver publishes it via --advertise-address=$(POD_IP) into the
`kubernetes` Endpoints. Pick the Tailscale address, lose that interface -- as a
cordon does, by stopping the subnet router pod -- and every pod's route to the API
server dies with it. CoreDNS stops syncing, and with DNS gone almost every
controller fails its startup probe.

Matched on the key path, never by grepping for a subnet: a comment mentioning
192.168.178.0/24 in prose must not satisfy this. (This used to be an awk state
machine walking the file by indentation; it reads the parsed document now, so a
reindent cannot make it blind.)
"""
from pathlib import Path
import re
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402

IPV4_CIDR = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+")


def pinned_subnet(machine_config_text):
    """The first IPv4 CIDR in machine.kubelet.nodeIP.validSubnets, or None."""
    for doc in yaml.safe_load_all(machine_config_text):
        subnets = (((doc or {}).get("machine") or {}).get("kubelet") or {}).get("nodeIP", {})
        for subnet in (subnets or {}).get("validSubnets") or []:
            if IPV4_CIDR.match(str(subnet)):
                return str(subnet)
    return None


def main():
    failed = False
    for path in sorted(ROOT.glob("cluster/overlays/*/talos-machineconfigs/controlplane.yaml")):
        rel = path.relative_to(ROOT).as_posix()
        subnet = pinned_subnet(path.read_text(encoding="utf-8"))
        if subnet:
            print(f"  ok    {rel} pins node IP to {subnet}")
        else:
            print(f"::error::{rel} does not set machine.kubelet.nodeIP.validSubnets to a CIDR. The kubelet will "
                  f"choose a node IP from whatever interfaces exist at start; kube-apiserver advertises that "
                  f"address, and if it belongs to an interface that can disappear (tailscale0), losing it breaks "
                  f"every pod's route to the API server.")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
