#!/usr/bin/env python3
"""Verify scheduled restore drills meet recovery-policy's maximum intervals."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

import yaml


OVERLAYS = ("1-node-config", "3-node-config")


def table(text: str) -> list[list[str]]:
    rows = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            rows.append(line.split())
    return rows


def interval_seconds(value: str) -> int:
    units = {"h": 3600, "d": 86400}
    try:
        return int(value[:-1]) * units[value[-1]]
    except (KeyError, ValueError):
        raise ValueError(f"unsupported recovery interval {value!r}") from None


def max_schedule_gap(schedule: str) -> int:
    """Return the largest gap for the simple weekly/monthly schedules used here."""
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"invalid cron schedule {schedule!r}")
    minute, hour, day, month, weekday = fields
    if month != "*" or not minute.isdigit() or not hour.isdigit():
        raise ValueError(f"unsupported cron schedule {schedule!r}")
    if (day == "*") == (weekday == "*"):
        raise ValueError(f"schedule must select one day-of-month or weekday: {schedule!r}")

    occurrences = []
    current = dt.date(2024, 1, 1)
    end = dt.date(2029, 1, 1)
    while current < end:
        cron_weekday = (current.weekday() + 1) % 7
        matches = current.day == int(day) if day != "*" else cron_weekday == int(weekday)
        if matches:
            occurrences.append(dt.datetime.combine(current, dt.time(int(hour), int(minute)), tzinfo=dt.timezone.utc))
        current += dt.timedelta(days=1)
    return int(max((right - left).total_seconds() for left, right in zip(occurrences, occurrences[1:])))


def mappings(value: object):
    """Yield every mapping below a rendered workflow template."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from mappings(child)


def validate(overlay: str) -> list[str]:
    result = subprocess.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", f"cluster/overlays/{overlay}"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return [f"kubectl kustomize failed: {result.stderr.strip()}"]
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    policy = next((doc for doc in docs if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == "recovery-policy"), None)
    if policy is None:
        return ["ConfigMap/recovery-policy is missing"]

    profiles = {row[0]: interval_seconds(row[3]) for row in table(policy["data"]["profiles"]) if row[3] != "-"}
    applications = {row[0]: row[2] for row in table(policy["data"]["applications"])}
    drill_applications = {}
    for doc in docs:
        if doc.get("kind") != "WorkflowTemplate":
            continue
        for node in mappings(doc.get("spec", {}).get("templates", [])):
            if node.get("templateRef", {}).get("name") != "drill-evidence":
                continue
            args = {item["name"]: item.get("value") for item in node.get("arguments", {}).get("parameters", [])}
            drill_applications[doc["metadata"]["name"]] = args.get("application")

    scheduled = {}
    errors = []
    for doc in docs:
        if doc.get("kind") != "CronWorkflow" or not doc["metadata"]["name"].startswith("drill-"):
            continue
        spec = doc.get("spec", {})
        reference = spec.get("workflowSpec", {}).get("workflowTemplateRef", {}).get("name")
        app = drill_applications.get(reference)
        if not app:
            errors.append(f"{doc['metadata']['name']} does not reference a drill with shared evidence")
            continue
        if app in scheduled:
            errors.append(f"{app} has more than one scheduled drill")
            continue
        schedules = spec.get("schedules", [])
        if len(schedules) != 1:
            errors.append(f"{doc['metadata']['name']} must have exactly one schedule")
            continue
        scheduled[app] = doc["metadata"]["name"]
        try:
            gap = max_schedule_gap(schedules[0])
            limit = profiles[applications[app]]
        except (KeyError, ValueError) as exc:
            errors.append(f"{doc['metadata']['name']}: {exc}")
            continue
        if gap > limit:
            errors.append(f"{doc['metadata']['name']} maximum gap {gap // 86400}d exceeds policy {limit // 86400}d")
        if spec.get("startingDeadlineSeconds", 0) < gap:
            errors.append(f"{doc['metadata']['name']} cannot catch up across its maximum schedule interval")

    expected = {app for app, profile in applications.items() if profile in profiles}
    for app in sorted(expected - scheduled.keys()):
        errors.append(f"{app} has a restore-test policy but no scheduled drill")
    for app in sorted(scheduled.keys() - expected):
        errors.append(f"{app} has a scheduled drill but no restore-test policy")
    return errors


def main() -> int:
    errors = []
    for overlay in OVERLAYS:
        overlay_errors = validate(overlay)
        errors.extend(f"[{overlay}] {error}" for error in overlay_errors)
        if not overlay_errors:
            print(f"[{overlay}] every restore-test policy has one drill within its maximum interval")
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
