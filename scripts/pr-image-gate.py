#!/usr/bin/env python3
"""Resolve every image reference a pull request changes.

The auto-merge gate used to answer this question by grepping the PR diff for
`image:` lines. That was wrong in three separate ways, all three confirmed in
production on 2026-09-09:

1. Every ADR-009 pin is a `registry`/`repository`/`tag` triple in HelmRelease
   values, not an `image:` line. The grep matched none of them, so the gate
   classified real image updates as "chart-only" and merged them with no CVE
   scan at all -- seen on #520, #523, #530 and #535.
2. It took `head -1`, so only the first changed image was ever scanned. Now
   that charts and their images are grouped into one PR, multi-image proposals
   are the normal case rather than the exception.
3. A diff only shows what *we* wrote. A chart that changes its own default
   registry or repository between versions moves the running image without
   producing a single changed line here.

So this does not read the diff. It resolves the full set of image references
at both revisions and compares those sets, which makes a tag change, a
registry change, a repository rename and a chart-side default change all the
same kind of event.

Three sources are resolved, because no single one covers everything:

  static     -- what this repository writes: `image:` scalars in plain
                manifests, and {registry?, repository, tag} mappings in values.
  rendered   -- what the chart actually produces, via `helm template` at the
                pinned chart version with our own values applied. This is the
                only source that sees an image we never mention.
  appVersion -- the chart's own idea of which application version it packages,
                compared against the pin that overrides it.

The structural rule for static values matters more than it looks. Keying on a
block *named* `image` would miss ten of this repository's pins: Longhorn writes
them under `engine`, `instanceManager`, `attacher`, `provisioner` and five
more. The rule is therefore "any mapping carrying a non-empty repository and
tag", which also excludes the CRD schemas in gotk-components.yaml, where
`repository:` and `tag:` are property names with no value.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Which pin tracks the chart's appVersion ─────────────────────────────────────
#
# Loaded from cluster-health.py rather than restated, so the merge-time check
# and the running-cluster check can never disagree about which images are the
# chart's own and which are companions from another project. Comparing a
# companion against appVersion reads one project's version against another's
# and calls the difference drift -- a false critical that trains people to
# ignore the check, which has happened here once already.
def _load_app_image_paths() -> Tuple[Dict[str, List[str]], Tuple[str, ...]]:
    path = REPO_ROOT / "scripts" / "cluster-health.py"
    try:
        spec = importlib.util.spec_from_file_location("_cluster_health", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_cluster_health"] = mod
        spec.loader.exec_module(mod)
        return mod.APP_IMAGE_PATHS, tuple(mod.DEFAULT_APP_IMAGE_PATHS)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"WARNING: could not load APP_IMAGE_PATHS ({exc}); using defaults",
              file=sys.stderr)
        return {}, ("image.tag",)


APP_IMAGE_PATHS, DEFAULT_APP_IMAGE_PATHS = _load_app_image_paths()


# ── git helpers ────────────────────────────────────────────────────────────────

def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=True).stdout


def git_show(ref: str, path: str) -> Optional[str]:
    """File content at a revision, or None when it does not exist there."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO_ROOT,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else None


def changed_yaml_files(base: str, head: str) -> List[str]:
    out = git("diff", "--name-only", f"{base}...{head}", "--", "*.yaml", "*.yml")
    return [l.strip() for l in out.splitlines() if l.strip()]


# ── YAML loading ───────────────────────────────────────────────────────────────

def load_docs(text: Optional[str]) -> List[Any]:
    if not text:
        return []
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, (dict, list))]
    except Exception:
        return []


def _is_set(v: Any) -> bool:
    return isinstance(v, str) and v.strip() not in ("", "null", "~")


# ── Static extraction ──────────────────────────────────────────────────────────

def _walk(node: Any, path: str, out: Dict[str, str], tags: Dict[str, str]) -> None:
    if isinstance(node, dict):
        repo = node.get("repository")
        tag = node.get("tag")
        registry = node.get("registry")
        digest = node.get("digest")

        # A mapping carrying a repository and a tag IS an image block, whatever
        # its parent key happens to be called.
        if _is_set(repo) and _is_set(tag):
            base = f"{registry.strip()}/{repo.strip()}" if _is_set(registry) else repo.strip()
            out[path or "."] = f"{base}:{tag.strip()}"
            tags[f"{path}.tag" if path else "tag"] = tag.strip()
        elif _is_set(repo) and _is_set(digest):
            base = f"{registry.strip()}/{repo.strip()}" if _is_set(registry) else repo.strip()
            out[path or "."] = f"{base}@{digest.strip()}"

        for k, v in node.items():
            child = f"{path}.{k}" if path else str(k)
            # `image: repo:tag` as a scalar -- plain manifests, and two
            # HelmRelease values (velero's plugin, kubeopencode).
            if k == "image" and _is_set(v) and ("/" in v or ":" in v):
                out[child] = v.strip()
            else:
                _walk(v, child, out, tags)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]", out, tags)


def static_images(doc: Any) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (path -> image ref, path -> raw tag) for one document.

    For a HelmRelease the paths are rooted at spec.values, matching the dotted
    paths cluster-health.py uses, so APP_IMAGE_PATHS applies unchanged.
    """
    refs: Dict[str, str] = {}
    tags: Dict[str, str] = {}
    if isinstance(doc, dict) and doc.get("kind") == "HelmRelease":
        _walk((doc.get("spec") or {}).get("values") or {}, "", refs, tags)
    else:
        _walk(doc, "", refs, tags)
    return refs, tags


def doc_identity(doc: Any, index: int) -> str:
    """A stable name for one document inside a multi-document file."""
    if isinstance(doc, dict):
        kind = doc.get("kind") or "doc"
        name = (doc.get("metadata") or {}).get("name") or str(index)
        return f"{kind}/{name}"
    return f"doc{index}"


# ── Helm repository resolution ─────────────────────────────────────────────────

_repo_cache: Dict[str, Dict[str, Tuple[str, bool]]] = {}


def helm_repositories(ref: str) -> Dict[str, Tuple[str, bool]]:
    """name -> (url, is_oci), read from the repository at the given revision."""
    if ref in _repo_cache:
        return _repo_cache[ref]
    repos: Dict[str, Tuple[str, bool]] = {}
    # git grep narrows 295 candidate files to the handful that actually declare
    # a HelmRepository, which turns ~590 `git show` calls per run into ~46.
    # It falls back to the full listing rather than failing, because a missed
    # repository would silently disable rendering for that chart.
    try:
        out = git("grep", "-l", "kind: HelmRepository", ref, "--", "cluster/")
        files = [l.split(":", 1)[1] for l in out.splitlines() if ":" in l]
    except subprocess.CalledProcessError:
        files = []
    if not files:
        files = [l for l in git("ls-tree", "-r", "--name-only", ref).splitlines()
                 if l.startswith("cluster/") and l.endswith((".yaml", ".yml"))]
    for f in files:
        for doc in load_docs(git_show(ref, f)):
            if isinstance(doc, dict) and doc.get("kind") == "HelmRepository":
                name = (doc.get("metadata") or {}).get("name")
                spec = doc.get("spec") or {}
                url = spec.get("url", "")
                if name and url:
                    repos[name] = (url, spec.get("type") == "oci" or url.startswith("oci://"))
    _repo_cache[ref] = repos
    return repos


# ── Rendering (case c) ─────────────────────────────────────────────────────────

def _collect_rendered(node: Any, out: set) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image" and _is_set(v):
                out.add(v.strip())
            else:
                _collect_rendered(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_rendered(v, out)


def render_release(hr: dict, repos: Dict[str, Tuple[str, bool]],
                   helm: str) -> Tuple[set, str, str]:
    """helm template one HelmRelease. Returns (images, status, detail)."""
    meta = hr.get("metadata") or {}
    spec = hr.get("spec") or {}
    name = meta.get("name", "release")
    cspec = ((spec.get("chart") or {}).get("spec")) or {}
    chart = cspec.get("chart")
    version = cspec.get("version")
    src = (cspec.get("sourceRef") or {}).get("name")

    if not chart or not version:
        return set(), "skipped", "no chart/version (chartRef or OCIRepository)"
    if src not in repos:
        return set(), "skipped", f"unknown HelmRepository {src!r}"
    if spec.get("valuesFrom"):
        # Values live in a ConfigMap/Secret that does not exist in CI, so a
        # render here would apply different values than the cluster does.
        return set(), "skipped", "valuesFrom cannot be resolved outside the cluster"

    url, is_oci = repos[src]
    with tempfile.TemporaryDirectory() as td:
        vf = Path(td) / "values.yaml"
        vf.write_text(yaml.safe_dump(spec.get("values") or {}), encoding="utf-8")
        target = f"{url.rstrip('/')}/{chart}" if is_oci else chart
        ns = spec.get("targetNamespace") or meta.get("namespace") or "default"
        cmd = [helm, "template", name, target, "--version", str(version),
               "--namespace", ns, "--values", str(vf), "--skip-tests"]
        if not is_oci:
            cmd += ["--repo", url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=420)
        except (subprocess.TimeoutExpired, OSError):
            return set(), "failed", "helm template timed out"
        if r.returncode != 0:
            lines = (r.stderr or "").strip().splitlines()
            return set(), "failed", (lines[-1][:300] if lines else "helm template failed")
        images: set = set()
        for doc in load_docs(r.stdout):
            _collect_rendered(doc, images)
        return images, "ok", f"{len(images)} image(s)"


# ── appVersion resolution (case B) ─────────────────────────────────────────────

_index_cache: Dict[str, Optional[dict]] = {}


def chart_app_version(chart: str, version: str, url: str, is_oci: bool,
                      helm: str) -> Tuple[Optional[str], str]:
    """appVersion for an exact chart version. Returns (appVersion, detail)."""
    if is_oci:
        target = f"{url.rstrip('/')}/{chart}"
        try:
            r = subprocess.run([helm, "show", "chart", target, "--version", str(version)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
        except (subprocess.TimeoutExpired, OSError):
            return None, "helm show chart timed out"
        if r.returncode != 0:
            return None, f"helm show chart failed for {target}"
        for doc in load_docs(r.stdout):
            if isinstance(doc, dict) and doc.get("appVersion") is not None:
                return str(doc["appVersion"]), "OCI chart metadata"
        return None, "no appVersion in chart metadata"

    idx_url = url.rstrip("/") + "/index.yaml"
    if idx_url not in _index_cache:
        try:
            with urllib.request.urlopen(idx_url, timeout=180) as resp:
                _index_cache[idx_url] = yaml.safe_load(resp.read())
        except Exception as exc:
            _index_cache[idx_url] = None
            return None, f"index.yaml unreachable: {exc}"
    idx = _index_cache[idx_url]
    if not idx:
        return None, "index.yaml unreachable"
    for entry in (idx.get("entries") or {}).get(chart, []) or []:
        if str(entry.get("version")) == str(version):
            av = entry.get("appVersion")
            if av is None:
                return None, "entry has no appVersion"
            return str(av), "index.yaml"
    return None, f"version {version} not present in index.yaml"


def semver(raw: Any) -> Optional[Tuple[int, int, int]]:
    """Parse a version, treating a missing patch component as zero.

    The patch part has to be optional: SeaweedFS ships two-component tags
    (4.45, 4.46), and requiring three made every one of them unparseable, so
    the gate called an ordinary minor step a major one and blocked it. The
    gate this replaced had it right -- the regression came in with the
    rewrite.

    A tag that merely starts with digits is still not a version: immich's
    `17-vectorchord0.3.0-pgvectors0.3.0` has no dot after 17, so it stays
    unorderable and is reported as such rather than guessed at.
    """
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", str(raw or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


# ── Reference helpers ──────────────────────────────────────────────────────────

def split_ref(ref: str) -> Tuple[str, str]:
    """(repository, tag-or-digest). Handles registry:port and digests."""
    if "@" in ref:
        r, d = ref.split("@", 1)
        return r, d
    head, sep, tail = ref.rpartition(":")
    if sep and head and "/" not in tail:
        return head, tail
    return ref, ""


# ── CVE scanning and the merge decision ────────────────────────────────────────

def scan_image(image: str, trivy: str) -> Optional[float]:
    """Highest CRITICAL/HIGH CVSS in an image, or None if it could not be scanned."""
    try:
        r = subprocess.run(
            [trivy, "image", "--format", "json", "--exit-code", "0",
             "--severity", "CRITICAL,HIGH", "--quiet", image],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except Exception:
        return None
    scores = [
        src.get("V3Score") or src.get("V2Score") or 0
        for res in (data.get("Results") or [])
        for vuln in (res.get("Vulnerabilities") or [])
        for src in (vuln.get("CVSS") or {}).values()
    ]
    return float(max(scores)) if scores else 0.0


def better_candidate(image: str, proposed_cvss: float, trivy: str,
                     crane: str) -> Tuple[bool, str]:
    """Condition 3b: is a newer same-major tag available with a lower CVSS?

    Returns (no_better, note). Any failure answers "no better", because this
    check exists to delay a merge for something strictly better -- it must
    never become a reason to block on its own.
    """
    repo, tag = split_ref(image)
    cur = semver(tag)
    if not cur:
        return True, "proposed tag not orderable - 3b skipped"
    try:
        r = subprocess.run([crane, "ls", repo], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except (subprocess.TimeoutExpired, OSError):
        return True, "crane ls timed out - 3b skipped"
    if r.returncode != 0:
        return True, "crane ls failed - 3b skipped"
    newer = sorted(
        (v for v in (semver(t) for t in r.stdout.split())
         if v and v[0] == cur[0] and v > cur), reverse=True)
    if not newer:
        return True, "proposed is latest in major - 3b satisfied"
    prefix = "v" if tag.startswith("v") else ""
    best = f"{repo}:{prefix}{'.'.join(str(x) for x in newer[0])}"
    best_cvss = scan_image(best, trivy)
    if best_cvss is None:
        return True, f"could not scan {best} - 3b skipped"
    if best_cvss >= proposed_cvss:
        return True, f"best newer {best} CVSS={best_cvss} - proposed is as good or better"
    return False, f"WAIT: {best} (CVSS {best_cvss}) is better than proposed (CVSS {proposed_cvss})"


def evaluate(result: Dict[str, Any], trivy: str, crane: str,
             labels: List[str]) -> Dict[str, Any]:
    """Scan every changed pair and decide whether the PR may auto-merge."""
    verdict: Dict[str, Any] = {"pairs": [], "blockers": [], "merge": False}

    if "manual-review" in labels:
        verdict["blockers"].append(
            "manual-review label present - unrecoverable component, human sign-off required")

    # ADR-009: a chart bump that moves appVersion past a static pin turns that
    # pin into a downgrade. It produces no image diff, so without this it is
    # the change the gate waves through fastest.
    for a in result.get("appversion", []):
        if a["verdict"] == "behind":
            verdict["blockers"].append(
                f"{a['release']}: chart {a['from']} -> {a['to']} moves appVersion to "
                f"{a['appVersion']}, past the pin {a['pin']} at {a['path']} - "
                f"the pin is now a downgrade (ADR-009)")
        elif a["verdict"] in ("unorderable", "unresolved"):
            verdict["blockers"].append(
                f"{a['release']}: cannot compare pin {a['pin']} with appVersion "
                f"{a['appVersion'] or '(unresolved)'} ({a['detail']})")

    # An image that appears without a predecessor cannot be compared, and a
    # registry move or rename is a change of supply chain, not of version.
    for m in result.get("moves", []):
        verdict["blockers"].append(
            f"{m['release']}: image moved {m['old']} -> {m['new']} - "
            f"registry or repository changed, not a version bump")
    for a in result.get("added", []):
        if a.get("kind") == "rendered":
            verdict["blockers"].append(
                f"new image appeared in rendered output: {a['new']} - no predecessor to compare")

    for pair in result.get("pairs", []):
        old, new = pair["old"], pair["new"]
        ov, nv = semver(split_ref(old)[1]), semver(split_ref(new)[1])
        is_minor = bool(ov and nv and ov[0] == nv[0])
        old_cvss = scan_image(old, trivy)
        new_cvss = scan_image(new, trivy)
        row = {**pair, "is_minor": is_minor, "old_cvss": old_cvss, "new_cvss": new_cvss}

        if split_ref(old)[0] != split_ref(new)[0]:
            # Not a version bump at all: the image now comes from a different
            # repository or registry. Comparing CVSS across two different
            # projects says nothing, and a supply-chain change deserves a
            # person regardless of how the scores land.
            row["ok"] = False
            row["note"] = (f"repository changed ({split_ref(old)[0]} -> "
                           f"{split_ref(new)[0]}) - not a version bump")
        elif old_cvss is None or new_cvss is None:
            row["ok"] = False
            row["note"] = "image could not be scanned"
        elif not is_minor:
            row["ok"] = False
            row["note"] = "major version step (or unorderable tag)"
        elif new_cvss > old_cvss:
            row["ok"] = False
            row["note"] = f"CVE posture worsens ({old_cvss} -> {new_cvss})"
        else:
            no_better, note = better_candidate(new, new_cvss, trivy, crane)
            row["ok"] = no_better
            row["note"] = note
        row["still_high"] = bool(row["ok"] and (new_cvss or 0) >= 7.0)
        if not row["ok"]:
            verdict["blockers"].append(f"{new}: {row['note']}")
        verdict["pairs"].append(row)

    has_scope = "minor-update" in labels or "security" in labels
    verdict["merge"] = has_scope and not verdict["blockers"]
    verdict["still_high"] = any(p.get("still_high") for p in verdict["pairs"])
    return verdict


def markdown(result: Dict[str, Any], verdict: Dict[str, Any]) -> str:
    out = ["## 🔍 Image gate", ""]
    pairs = verdict["pairs"]
    if pairs:
        out += ["| Source | Image | Max CVSS (CRITICAL+HIGH) | |", "|---|---|---|---|"]
        for p in pairs:
            mark = "✅" if p["ok"] else "❌"
            out.append(f"| `{p['kind']}` | `{p['old']}` | {p['old_cvss']} | |")
            out.append(f"| | `{p['new']}` | **{p['new_cvss']}** | {mark} {p['note']} |")
        out.append("")
    else:
        out += ["_No image reference changed at either revision._", ""]

    for a in result.get("appversion", []):
        icon = {"behind": "❌", "ahead": "✅", "equal": "✅"}.get(a["verdict"], "⚠️")
        out.append(f"{icon} **appVersion** `{a['release']}` chart `{a['from']}` → `{a['to']}` "
                   f"ships appVersion `{a['appVersion']}`; pin `{a['pin']}` at "
                   f"`{a['path']}` is **{a['verdict']}**.")
    if result.get("appversion"):
        out.append("")

    skipped = [r for r in result.get("render", [])
               if r["base"]["status"] != "ok" or r["head"]["status"] != "ok"]
    if skipped:
        out.append("> Rendered-image comparison unavailable for: " +
                   ", ".join(f"`{r['release']}` ({r['head']['detail']})" for r in skipped))
        out.append("")

    if verdict["blockers"]:
        out.append("### ⏸️ Manual review required")
        out += [f"- {b}" for b in verdict["blockers"]]
    elif verdict["merge"]:
        out.append("### ⚠️ Auto-merging — still HIGH/CRITICAL CVSS (no clean version available yet)"
                   if verdict["still_high"] else "### ✅ Auto-merging")
    else:
        out.append("### ⏸️ Not auto-merged (no minor-update/security label)")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Resolve every image reference a pull request changes.")
    ap.add_argument("--base", required=True, help="base git ref")
    ap.add_argument("--head", default="HEAD", help="head git ref")
    ap.add_argument("--helm", default="helm", help="helm binary")
    ap.add_argument("--no-render", action="store_true",
                    help="skip helm rendering (static + appVersion only)")
    ap.add_argument("--json", dest="json_out", help="write JSON result here")
    ap.add_argument("--quiet", action="store_true", help="do not print JSON to stdout")
    ap.add_argument("--scan", action="store_true",
                    help="scan changed images with Trivy and emit a merge verdict")
    ap.add_argument("--trivy", default="trivy", help="trivy binary")
    ap.add_argument("--crane", default="crane", help="crane binary")
    ap.add_argument("--label", action="append", default=[],
                    help="a PR label (repeatable); drives scope and manual-review")
    ap.add_argument("--report", help="write the markdown report here")
    ap.add_argument("--github-output", help="append merge/still_high to this file")
    args = ap.parse_args()

    files = changed_yaml_files(args.base, args.head)
    result: Dict[str, Any] = {"files": files, "pairs": [], "added": [], "removed": [],
                              "moves": [], "appversion": [], "render": []}

    repos_head = helm_repositories(args.head)
    repos_base = helm_repositories(args.base)

    for f in files:
        base_docs = load_docs(git_show(args.base, f))
        head_docs = load_docs(git_show(args.head, f))

        # ── static ──
        # Paths are qualified by document identity. Without that, two CronJobs
        # in one file both answer to
        # spec.jobTemplate.spec.template.spec.containers[0].image, the later
        # document overwrites the earlier, and the gate ends up pairing two
        # unrelated images -- observed pairing alpine:3.24 against
        # aws-cli:2.36.40 and calling it a version bump.
        sb: Dict[str, str] = {}
        sh: Dict[str, str] = {}
        tags_by_release: Dict[str, Dict[str, str]] = {}
        for i, d in enumerate(base_docs):
            refs, _ = static_images(d)
            ident = doc_identity(d, i)
            sb.update({f"{ident}::{p}": v for p, v in refs.items()})
        for i, d in enumerate(head_docs):
            refs, tg = static_images(d)
            ident = doc_identity(d, i)
            sh.update({f"{ident}::{p}": v for p, v in refs.items()})
            if isinstance(d, dict) and d.get("kind") == "HelmRelease":
                rel = (d.get("metadata") or {}).get("name", "?")
                tags_by_release.setdefault(rel, {}).update(tg)
        for path in sorted(set(sb) | set(sh)):
            old, new = sb.get(path), sh.get(path)
            if old and new and old != new:
                result["pairs"].append({"kind": "static", "file": f, "path": path,
                                        "old": old, "new": new})
            elif new and not old:
                result["added"].append({"kind": "static", "file": f, "path": path, "new": new})
            elif old and not new:
                result["removed"].append({"kind": "static", "file": f, "path": path, "old": old})

        # ── rendered (case c) + appVersion (case B) ──
        for hd in head_docs:
            if not (isinstance(hd, dict) and hd.get("kind") == "HelmRelease"):
                continue
            rel = (hd.get("metadata") or {}).get("name", "?")
            cspec = (((hd.get("spec") or {}).get("chart") or {}).get("spec")) or {}
            chart = cspec.get("chart")
            new_ver = cspec.get("version")
            src = (cspec.get("sourceRef") or {}).get("name")

            bd = next((d for d in base_docs
                       if isinstance(d, dict) and d.get("kind") == "HelmRelease"
                       and (d.get("metadata") or {}).get("name") == rel), None)
            old_ver = ((((bd or {}).get("spec") or {}).get("chart") or {}).get("spec")
                       or {}).get("version")

            if not args.no_render and bd is not None:
                imgs_b, st_b, dt_b = render_release(bd, repos_base, args.helm)
                imgs_h, st_h, dt_h = render_release(hd, repos_head, args.helm)
                result["render"].append({"file": f, "release": rel,
                                         "base": {"status": st_b, "detail": dt_b},
                                         "head": {"status": st_h, "detail": dt_h}})
                if st_b == "ok" and st_h == "ok":
                    by = {split_ref(i)[0]: i for i in imgs_b}
                    hy = {split_ref(i)[0]: i for i in imgs_h}
                    for repo in sorted(set(by) & set(hy)):
                        if by[repo] != hy[repo]:
                            result["pairs"].append({"kind": "rendered", "file": f,
                                                    "path": f"{rel}:{repo}",
                                                    "old": by[repo], "new": hy[repo]})
                    gone = sorted(set(by) - set(hy))
                    came = sorted(set(hy) - set(by))
                    for repo in gone:
                        # A repository that disappears while a same-named image
                        # appears elsewhere is a registry move or a rename --
                        # case (c). Nothing in the diff shows this.
                        twin = next((c for c in came
                                     if c.rsplit("/", 1)[-1] == repo.rsplit("/", 1)[-1]), None)
                        if twin:
                            came.remove(twin)
                            result["moves"].append({"file": f, "release": rel,
                                                    "old": by[repo], "new": hy[twin]})
                        else:
                            result["removed"].append({"kind": "rendered", "file": f,
                                                      "path": f"{rel}:{repo}", "old": by[repo]})
                    for repo in came:
                        result["added"].append({"kind": "rendered", "file": f,
                                                "path": f"{rel}:{repo}", "new": hy[repo]})

            # appVersion vs pin -- only when the chart version actually moved.
            if chart and new_ver and old_ver and str(old_ver) != str(new_ver) and src in repos_head:
                url, is_oci = repos_head[src]
                av, detail = chart_app_version(chart, str(new_ver), url, is_oci, args.helm)
                wanted = APP_IMAGE_PATHS.get(rel, DEFAULT_APP_IMAGE_PATHS)
                rel_tags = tags_by_release.get(rel, {})
                pins = {p: v for p, v in rel_tags.items() if p in wanted}
                for path, pin in sorted(pins.items()):
                    pv, avv = semver(pin), semver(av)
                    if av is None:
                        verdict = "unresolved"
                    elif pv is None or avv is None:
                        verdict = "unorderable"
                    elif pv < avv:
                        verdict = "behind"
                    elif pv > avv:
                        verdict = "ahead"
                    else:
                        verdict = "equal"
                    result["appversion"].append({
                        "file": f, "release": rel, "chart": chart,
                        "from": str(old_ver), "to": str(new_ver),
                        "appVersion": av, "path": path, "pin": pin,
                        "verdict": verdict, "detail": detail,
                    })

    blocking = [a for a in result["appversion"]
                if a["verdict"] in ("behind", "unorderable", "unresolved")]
    result["summary"] = {
        "changed_images": len(result["pairs"]),
        "added": len(result["added"]),
        "removed": len(result["removed"]),
        "moves": len(result["moves"]),
        "appversion_blocking": len(blocking),
    }

    if args.scan:
        verdict = evaluate(result, args.trivy, args.crane, args.label)
        result["verdict"] = verdict
        report = markdown(result, verdict)
        if args.report:
            Path(args.report).write_text(report, encoding="utf-8")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as fh:
                fh.write(f"should_merge={'true' if verdict['merge'] else 'false'}\n")
                fh.write(f"still_high={'true' if verdict['still_high'] else 'false'}\n")
        print(report, file=sys.stderr)

    text = json.dumps(result, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    if not args.quiet:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
