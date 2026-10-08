#!/usr/bin/env python3
"""Compare committed dependency pins with Renovate's native extraction.

Built-in/custom manager selection and dependency extraction belong to Renovate.
This script only inventories source fields and checks the extracted result. No
registry lookups, credentials, writes to GitHub or chart rendering are needed.
See docs/renovate-coverage.md for scope and intentional maintenance exceptions.
"""
import argparse
from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Pin:
    file: str
    field: str
    line: int
    value: str
    name: str = ""
    datasource: str = ""


def image_parts(value):
    """Keep the registry port separate from the image tag."""
    image, sep, digest = value.partition("@")
    if ":" in image.rsplit("/", 1)[-1]:
        name, tag = image.rsplit(":", 1)
    else:
        name, tag = image, ""
    return name, tag, digest if sep else ""


def docker_name(name):
    name = name.removeprefix("docker.io/").removeprefix("index.docker.io/")
    if "/" not in name:
        name = "library/" + name
    return name


def mapping(node):
    return {k.value: v for k, v in node.value} if isinstance(node, MappingNode) else {}


def scalar(node):
    return node.value if isinstance(node, ScalarNode) else ""


def yaml_pins(file, text):
    """Inventory image pins, chart/source refs, custom versions and embedded YAML.

    YAML nodes retain source lines and string spelling (e.g. 16.10 must not
    become the float 16.1). Schema declarations and metadata aren't dependencies.
    """
    pins = []
    header = re.search(r"^# (?:Flux Version|version): (\S+)", text, re.M)
    if header and re.search(r"^# (?:Components|components):", text, re.M):
        return [Pin(file, "flux-system-version", text[:header.start()].count("\n") + 1,
                    header[1], "fluxcd/flux2", "github-releases")]

    def add(node, field, name="", datasource="", value=None, offset=0):
        pins.append(Pin(file, field, node.start_mark.line + 1 + offset,
                        scalar(node) if value is None else value, name, datasource))

    def walk(node, path, offset=0):
        if isinstance(node, MappingNode):
            fields = mapping(node)
            for key, child in node.value:
                field = path + "." + key.value if path else key.value
                value = scalar(child)
                if key.value in ("metadata", "sops", "openAPIV3Schema"):
                    continue
                if key.value in ("image", "imageName") and isinstance(child, ScalarNode):
                    add(child, field, image_parts(value)[0], "docker", offset=offset)
                elif key.value in ("tag", "digest"):
                    # Source refs and image mappings, including tag-only overrides.
                    repo = scalar(fields.get("repository")) or scalar(fields.get("repo"))
                    registry = scalar(fields.get("registry"))
                    name = "/".join(x.strip("/") for x in (registry, repo) if x)
                    add(child, field, name, "docker" if name else "", offset=offset)
                elif key.value == "version" or key.value.endswith("_version"):
                    # Plan versions and Helm chart versions; arbitrary application
                    # config schema versions aren't release pins.
                    if key.value != "version" or scalar(fields.get("chart")) or field == "spec.version":
                        add(child, field, scalar(fields.get("chart")),
                            "helm" if scalar(fields.get("chart")) else "", offset=offset)
                elif key.value == "newTag" or key.value == "newName" and ":" in value:
                    add(child, field, scalar(fields.get("newName")) or scalar(fields.get("name")),
                        "docker", offset=offset)
                elif key.value == "uses" and "@" in value and not value.startswith("./"):
                    name, version = value.rsplit("@", 1)
                    add(child, field, name.removeprefix("docker://"),
                        "docker" if name.startswith("docker://") else "github-tags",
                        version, offset)
                if isinstance(child, ScalarNode) and child.style in ("|", ">"):
                    # Talos inlineManifests and ConfigMap-embedded workload YAML.
                    if re.search(r"^\s*apiVersion:", value, re.M) and re.search(r"^\s*kind:", value, re.M):
                        for embedded in yaml.compose_all(value):
                            if embedded:
                                walk(embedded, field, offset + child.start_mark.line + 1)
                    for match in re.finditer(r"--version=(v?[0-9][\w.+-]*)", value):
                        pins.append(Pin(file, field + ".--version", offset + child.start_mark.line + 2
                                        + value[:match.start()].count("\n"), match[1]))
                walk(child, field, offset)
        elif isinstance(node, SequenceNode):
            for index, child in enumerate(node.value):
                walk(child, f"{path}[{index}]", offset)

    for index, doc in enumerate(yaml.compose_all(text) if not file.endswith(".env") else []):
        if not doc:
            continue
        if scalar(mapping(doc).get("kind")) == "CustomResourceDefinition":
            continue
        # Qualify multi-document fields without tying exceptions to line numbers.
        walk(doc, f"document[{index}]" if index else "")
    # Tool versions in workflow run blocks and versions.env. Comments aren't pins.
    occurrences = {}
    for match in re.finditer(r"^[ \t]*(\w*VERSION)=[\"']?(v?[0-9][\w.+-]*)", text, re.M):
        index = occurrences.get(match[1], 0)
        occurrences[match[1]] = index + 1
        pins.append(Pin(file, f"{match[1]}[{index}]", text[:match.start()].count("\n") + 1, match[2]))
    if file.endswith((".yml", ".yaml")) and file.startswith(".github/workflows/"):
        for index, match in enumerate(re.finditer(r"https://get\.helm\.sh/helm-(v[\d.]+)-", text)):
            pins.append(Pin(file, f"helm-download[{index}]", text[:match.start()].count("\n") + 1,
                            match[1], "helm/helm", "github-releases"))
        for index, match in enumerate(re.finditer(r"[\"']?([\w.-]+)==([\d.]+)", text)):
            pins.append(Pin(file, f"pip-install[{index}]", text[:match.start()].count("\n") + 1,
                            match[2], match[1].lower(), "pypi"))
    return pins


def inventory(files):
    """Pure source inventory; coverage scope is independent of manager patterns."""
    pins = []
    for file, text in sorted(files.items()):
        if file.endswith((".yaml", ".yml", ".env")):
            pins.extend(yaml_pins(file, text))
        elif file.endswith("package.json"):
            data = json.loads(text)
            for section in ("dependencies", "devDependencies", "optionalDependencies"):
                for name, version in data.get(section, {}).items():
                    pins.append(Pin(file, section + "." + name, 1, version, name, "npm"))
        elif file.endswith("requirements.txt"):
            for line, value in enumerate(text.splitlines(), 1):
                match = re.fullmatch(r"([\w.-]+)==([^ #]+)(?:\s+#.*)?", value.strip())
                if match:
                    pins.append(Pin(file, match[1], line, match[2], match[1].lower(), "pypi"))
    return pins


def extracted_packages(log):
    """Fail closed if Renovate never emitted its extraction result or logged errors."""
    snapshots = []
    for line in log.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("level", 0) >= 50:
            raise ValueError("Renovate logged an error: " + entry.get("msg", "unknown error"))
        if entry.get("msg") == "Extracted dependencies":
            snapshots.append(entry["packageFiles"])
    if len(snapshots) != 1 or not isinstance(snapshots[0], dict) or not snapshots[0]:
        raise ValueError("Expected exactly one non-empty Renovate extraction result")
    result = {}
    for manager, packages in snapshots[0].items():
        for package in packages:
            for dep in package["deps"]:
                result.setdefault(package["packageFile"], []).append({**dep, "manager": manager})
    return result


def matches(patterns, value):
    """Renovate's supported glob/regex matcher subset for disabling rules."""
    positives = [p for p in patterns if not p.startswith("!")]
    def match(pattern):
        if pattern.startswith("/") and pattern.endswith("/"):
            return bool(re.search(pattern[1:-1], value))
        return fnmatch.fnmatchcase(value, pattern)
    return (not positives or any(match(p) for p in positives)) and not any(
        match(p[1:]) for p in patterns if p.startswith("!"))


def disabled(dep, file, config):
    """Reject unconditional disables; major/digest-only restrictions retain maintenance.

    This is deliberately conservative. New disabling selectors need an explicit
    implementation instead of silently treating a potentially disabled pin as covered.
    Extraction itself already respects enabledManagers and manager-level enabled.
    """
    enabled = True
    selectors = {"matchManagers": dep["manager"], "matchFileNames": file,
                 "matchPackageNames": dep.get("packageName", dep.get("depName", "")),
                 "matchDatasources": dep.get("datasource", ""),
                 "matchDepNames": dep.get("depName", "")}
    for rule in config.get("packageRules", []):
        if "enabled" not in rule or "matchUpdateTypes" in rule:
            continue
        if not all(matches(rule[key], value) for key, value in selectors.items() if key in rule):
            continue
        unknown = [key for key in rule if key.startswith("match") and key not in selectors]
        if unknown:
            raise ValueError("Unsupported disabling rule selectors: " + ", ".join(unknown))
        enabled = rule["enabled"]
    return not enabled


def covers(pin, dep, text, config):
    if dep.get("skipReason") or not dep.get("datasource") or disabled(dep, pin.file, config):
        return False
    values = {str(dep.get("currentValue", "")), str(dep.get("currentDigest", ""))}
    name, tag, digest = image_parts(pin.value)
    if pin.datasource == "docker" and pin.field.rsplit(".", 1)[-1] in ("image", "imageName"):
        values_match = bool(tag and tag in values or digest and digest in values)
    else:
        values_match = pin.value in values or pin.value.removeprefix("v") in values
    if not values_match:
        return False
    if dep["manager"] == "regex":
        # Match the actual source span Renovate plans to replace, not just a
        # version shared by another dependency in the same file.
        replacement = dep.get("replaceString", "")
        if not replacement:
            return False
        for match in re.finditer(re.escape(replacement), text):
            first = text[:match.start()].count("\n") + 1
            last = first + replacement.count("\n")
            if first <= pin.line <= last:
                return True
        return False
    if not pin.name:
        return False
    names = {dep.get("depName", ""), dep.get("packageName", "")}
    if pin.datasource == "docker":
        return dep["datasource"] == "docker" and docker_name(pin.name) in {docker_name(n) for n in names}
    if pin.datasource == "helm" and dep["datasource"] == "docker":
        # Flux resolves OCI Helm charts to a Docker datasource.
        return any(n.endswith("/" + pin.name) for n in names)
    if pin.datasource == "pypi":
        names = {n.lower().replace("_", "-") for n in names}
    return pin.name in names and pin.datasource == dep["datasource"]


def coverage_errors(pins, extracted, files, config, exceptions):
    """Return actionable failures, including stale/duplicate maintenance exceptions."""
    errors = []
    by_key = {(p.file, p.field): p for p in pins}
    covered = {(p.file, p.field) for p in pins if any(
        covers(p, d, files[p.file], config) for d in extracted.get(p.file, []))}
    decisions = {}
    for entry in exceptions:
        key = (entry["file"], entry["field"])
        if key in decisions:
            errors.append(f"Duplicate maintenance exception: {key}")
        decisions[key] = entry
        if not entry.get("reason", "").strip():
            errors.append(f"Maintenance exception needs a reason: {key}")
        pin = by_key.get(key)
        if not pin:
            errors.append(f"Stale maintenance exception: {key}")
        elif entry.get("value") != pin.value:
            errors.append(f"Maintenance exception value changed: {key}; review its maintenance path")
        elif entry.get("source"):
            source = entry["source"]
            if (source["file"], source["field"]) not in covered:
                errors.append(f"Derived pin {key} has an uncovered source: {source}")
        elif entry.get("maintenance") != "manual":
            errors.append(f"Exception {key} needs a tracked source or maintenance=manual")
    for pin in pins:
        if (pin.file, pin.field) not in covered and (pin.file, pin.field) not in decisions:
            errors.append(f"{pin.file}:{pin.line}: {pin.field}={pin.value!r} is not extracted by an enabled "
                          "Renovate manager; use a built-in manager, add a custom manager, or document maintenance")
    return errors


def source_files(root):
    files = {}
    # Whole source trees, including deferred resources, patches and templates.
    for scope in ("cluster", "bootstrap/ansible", ".github/workflows", "docs/runbooks/recovery",
                  ".github/renovate-coverage"):
        for path in (root / scope).rglob("*"):
            if path.is_file() and path.suffix in (".yaml", ".yml", ".env", ".json", ".txt"):
                files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return files


def run_extraction(root, log_path):
    """Run the installed, version-pinned native CLIs against the PR checkout."""
    tool_dir = Path(tempfile.mkdtemp(prefix="renovate-coverage-tools-"))
    try:
        shutil.copy(root / ".github/renovate-coverage/package.json", tool_dir / "package.json")
        subprocess.run(["npm", "install", "--prefix", str(tool_dir), "--no-audit", "--no-fund"], check=True)
        binaries = tool_dir / "node_modules/.bin"
        subprocess.run([str(binaries / "renovate-config-validator"), "--no-global", "renovate.json"],
                       cwd=root, check=True)
        env = {**os.environ, "LOG_FILE": str(log_path), "LOG_FILE_LEVEL": "debug", "LOG_FORMAT": "json",
               "LOG_LEVEL": "info", "RENOVATE_CONFIG_FILE": str(tool_dir / "config.json")}
        # A trusted global config selects local extraction only. renovate.json is
        # still loaded as repository config; baseBranchPatterns are ignored locally.
        (tool_dir / "config.json").write_text(json.dumps({"platform": "local", "dryRun": "extract",
            "onboarding": False, "requireConfig": "required", "configValidationError": True}), encoding="utf-8")
        log_path.unlink(missing_ok=True)
        subprocess.run([str(binaries / "renovate")], cwd=root, env=env, check=True)
        return extracted_packages(log_path.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tool_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction-log", type=Path, help="Compare a previously captured native JSON log")
    parser.add_argument("--inventory", action="store_true", help="Print the inventory without checking coverage")
    args = parser.parse_args()
    try:
        files = source_files(ROOT)
        pins = inventory(files)
        if not pins:
            raise ValueError("Dependency inventory is empty")
        if args.inventory:
            print(json.dumps([p.__dict__ for p in pins], indent=2))
            return 0
        log_path = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "renovate-coverage.jsonl"
        extracted = (extracted_packages(args.extraction_log.read_text(encoding="utf-8"))
                     if args.extraction_log else run_extraction(ROOT, log_path))
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        exceptions = json.loads((ROOT / ".github/renovate-coverage/exceptions.json").read_text(encoding="utf-8"))
        errors = coverage_errors(pins, extracted, files, config, exceptions)
        for error in errors:
            print("::error::" + error)
        if errors:
            return 1
        print(f"Renovate coverage verified for {len(pins)} committed pins ({len(exceptions)} maintenance decisions).")
        return 0
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        print(f"::error::Renovate coverage could not be verified: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
