#!/usr/bin/env python3
"""The Talos installer pins agree with each other, and the installer they name exists.

  talos_pins.py agree      versions.env's TALOS_VERSION and every machine config's
                           install.image name the same Talos version
  talos_pins.py schematic  every schematic ID pinned in the repo is what the Image
                           Factory hashes schematic.yaml to
  talos_pins.py images     every (schematic, version) pair the repo would pull is an
                           installer the Image Factory has built

Why `agree` exists: versions.env is now the single source of truth, and Kustomize
`replacements` project it into every SUC Plan at build time -- so the Plans' own
literals are placeholders, not pins. The one pin Kustomize cannot reach is the
machine config's install.image, applied by hand via talosctl outside Flux
entirely; that still has to agree by hand, which is what this checks. Matched
structurally, on the specific keys, never by grepping for a version pattern:
versions.env's own comments mention several versions in prose, and a blanket grep
would report those as pins.

Why `images` exists: the Image Factory builds an image per (schematic, version)
pair, and system extensions are published per Talos version. So a version bump can
reference an image that has not been built (v1.13.9 had iscsi-tools and
util-linux-tools; v1.14.0 was "version 1.14.0 is not available"). Renovate will
happily bump the tag either way. Without this the failure surfaces at upgrade
time instead: the SUC Job pulls a 404, the upgrade never runs, and the node is
left cordoned in the middle of a plan. That is a bad place to discover a typo.

Why `schematic` exists: the schematic ID is a content hash of the extension list,
not a version-linked value -- the same ID serves v1.12.0 through v1.13.9. It
therefore does NOT need regenerating when the Talos version moves. It DOES need
regenerating whenever schematic.yaml changes: an extension added to schematic.yaml
while the old ID stayed pinned would build and deploy the old extension set,
silently, with the file saying otherwise. Re-POSTing schematic.yaml is the whole
check -- the Factory is a pure function of the content, so if the returned ID
differs from what the repo pins, the repo is stale.
"""
import collections
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, VERSIONS_ENV, fetch, probe, read_versions_env  # noqa: E402

FACTORY = "https://factory.talos.dev"
MACHINE_CONFIG_DIRS = "cluster/overlays/*/talos-machineconfigs"
INSTALL_IMAGE = re.compile(r"image:\s*factory\.talos\.dev/installer/[0-9a-f]{64}:(v[0-9]+\.[0-9]+\.[0-9]+)")
INSTALLER_REF = re.compile(r"factory\.talos\.dev/installer/([0-9a-f]{64}):(v[0-9]+\.[0-9]+\.[0-9]+)")
SCHEMATIC_ID = re.compile(r"[0-9a-f]{64}")


def machine_config_files(root=ROOT):
    return sorted(p for p in Path(root).glob(MACHINE_CONFIG_DIRS + "/**/*") if p.is_file())


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


# ── agree ────────────────────────────────────────────────────────────────────

def agreement(env_version, mc_versions):
    """-> (lines to print, error or None)."""
    if not mc_versions:
        return [], ("No machine config install.image matched cluster/overlays/*/talos-machineconfigs/. "
                    "Either the glob is stale or every overlay is missing its installer pin -- this "
                    "check cannot verify anything without at least one match.")
    pins = [env_version] + list(mc_versions)
    lines = [f"Pins found: {len(pins)}"]
    lines += [f"  {count:7d} {version}" for version, count in sorted(collections.Counter(pins).items())]
    distinct = sorted(set(pins))
    if len(distinct) != 1:
        return lines, (f"Talos version pins disagree ({len(distinct)} distinct values). versions.env's "
                       f"TALOS_VERSION and every machine config's install.image must name the same version. "
                       f"A mismatch means a rebuilt node installs something other than the automated "
                       f"upgrade targets.")
    lines.append(f"All pins agree on {distinct[0]}")
    return lines, None


def cmd_agree():
    match = re.match(r"v[0-9]+\.[0-9]+\.[0-9]+", read_versions_env().get("TALOS_VERSION", ""))
    if not match:
        print(f"::error::TALOS_VERSION in {VERSIONS_ENV.relative_to(ROOT).as_posix()} is missing or not vX.Y.Z")
        return 1
    mc_versions = [m for f in machine_config_files() for m in INSTALL_IMAGE.findall(read_text(f))]
    lines, error = agreement(match.group(0), mc_versions)
    print("\n".join(lines))
    if error:
        print(f"::error::{error}")
        return 1
    return 0


# ── schematic ────────────────────────────────────────────────────────────────

def factory_schematic_id(content, fetch_fn=fetch, attempts=3):
    """What the Image Factory hashes this schematic to. -> (id, None) or (None, why)."""
    status = None
    for _ in range(attempts):
        status, body = fetch_fn(f"{FACTORY}/schematics", data=content, method="POST")
        # Any 2xx: the Factory answers a POST with 201 Created (as curl -f accepted).
        if status is not None and 200 <= status < 300:
            try:
                return json.loads(body)["id"], None
            except (ValueError, KeyError, TypeError):
                return None, f"answered {status} without an id in the body"
        if status is not None and status < 500 and status != 429:
            break
    return None, f"HTTP {status}" if status else "no answer"


def schematic_report(rel, content, found_ids, fetch_fn=fetch):
    """What to print for one schematic.yaml, and whether it failed. -> (lines, failed)."""
    lines = [f"::group::{rel}"]
    expected, why = factory_schematic_id(content, fetch_fn)
    if expected is None:
        lines.append(f"::error::The Image Factory did not hash {rel} ({why}); this says nothing about whether "
                     f"the pinned IDs are stale -- re-run once factory.talos.dev answers.")
        lines.append("::endgroup::")
        return lines, True
    lines.append(f"Factory returns: {expected}")
    failed = False
    for ident in sorted(found_ids):
        if ident == expected:
            lines.append(f"  ok      {ident}")
        else:
            lines.append(f"::error::Stale schematic ID {ident} pinned in the repo; schematic.yaml now hashes to "
                         f"{expected}. Regenerate it everywhere: curl -X POST --data-binary @{rel} {FACTORY}/schematics")
            failed = True
    lines.append("::endgroup::")
    return lines, failed


def cmd_schematic(fetch_fn=fetch):
    found = set()
    for path in machine_config_files() + [VERSIONS_ENV]:
        found |= set(SCHEMATIC_ID.findall(read_text(path)))
    failed = False
    for schematic in sorted(ROOT.glob(MACHINE_CONFIG_DIRS + "/schematic.yaml")):
        lines, bad = schematic_report(schematic.relative_to(ROOT).as_posix(), schematic.read_bytes(), found, fetch_fn)
        print("\n".join(lines))
        failed = failed or bad
    return 1 if failed else 0


# ── images ───────────────────────────────────────────────────────────────────

def installer_pairs(cluster_texts, env):
    """The (schematic, version) pairs the repo would pull, sorted and unique."""
    pairs = {f"{s}:{v}" for text in cluster_texts for s, v in INSTALLER_REF.findall(text)}
    if env.get("TALOS_VERSION") and env.get("SCHEMATIC_ID"):
        pairs.add(f"{env['SCHEMATIC_ID']}:{env['TALOS_VERSION']}")
    return sorted(pairs)


def image_report(pairs, probe_fn=probe):
    """One verdict line per (schematic, version) pair. -> (lines, failed)."""
    lines, failed = [], False
    for pair in pairs:
        sid, _, version = pair.partition(":")
        verdict, status = probe_fn(f"{FACTORY}/v2/installer/{sid}/manifests/{version}",
                                   definitely_missing=(),
                                   headers={"Accept": "application/vnd.oci.image.index.v1+json"})
        if verdict == "exists":
            lines.append(f"  ok    {version}  ({sid[:12]}...)")
        elif status is None or status in (403, 429) or status >= 500:
            lines.append(f"::error::factory.talos.dev did not answer usefully for {version} with schematic "
                         f"{sid[:12]}... (HTTP {status}); that says nothing about whether the installer exists -- "
                         f"re-run once it does.")
            failed = True
        else:
            lines.append(f"::error::factory.talos.dev has no installer for {version} with schematic {sid[:12]}... "
                         f"(HTTP {status}). Either the version is not released yet, or a system extension in the "
                         f"schematic has not been published for it. The upgrade Job would pull a 404 and leave "
                         f"the node cordoned.")
            failed = True
    return lines, failed


def cmd_images(probe_fn=probe):
    texts = [read_text(p) for p in sorted((ROOT / "cluster").rglob("*")) if p.is_file()]
    pairs = installer_pairs(texts, read_versions_env())
    print("Image references to verify:")
    for pair in pairs:
        print(f"  {pair}")
    lines, failed = image_report(pairs, probe_fn)
    print("\n".join(lines))
    return 1 if failed else 0


COMMANDS = {"agree": cmd_agree, "schematic": cmd_schematic, "images": cmd_images}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: talos_pins.py {'|'.join(COMMANDS)}")
    sys.exit(COMMANDS[sys.argv[1]]())
