#!/usr/bin/env python3
"""talos-fleet-health's schedule must not fire inside a windowed upgrade Plan's window.

Code review on PR #386 (round 4), blocking: 4f5f64b moved the OS upgrade windows to
midday without checking whether talos-fleet-health's schedule still avoided them --
12:35 UTC landed inside the control-plane window (12:00-14:00), which would have
paged TalosFleetUnreadyNodes as critical on every routine Sunday upgrade. That
relationship (schedule clear of every windowed Plan's window) was documented as an
assumption in a comment, not enforced anywhere, so nothing caught the window move
breaking it. This makes it a checked invariant instead: render for real, expand the
CronJob's cron schedule and each Plan's window, and fail if any fire time falls
inside any window.

Independent of TalosFleetVersionSkew/K8sVersionSkew/UnreadyNodes carrying `for: 8h`
headroom (04-grafana/helmrelease.yaml) that tolerates a single mid-window sample
regardless -- that makes a collision non-fatal to the alerting, this stops the
collision existing in the first place. Defense in depth, not redundant: re-picking a
schedule alone "leaves the same trap armed for the next person", and `for:` headroom
alone still means a genuine problem is detected up to 8h later than it could be.

Round 5 code review found the first version could pass without checking anything,
two ways: (1) the Plan list was hardcoded, so a renamed Plan shrank what got
compared -- and an empty `days` made ''.split(',') == [''], so the loop skipped it
and still printed "ok" having compared nothing for that Plan; (2) it only reasoned
about Sunday and the literal token 'su', so a window moved to another day, or spelled
out ("sunday"), was silently never checked. Fixed by deriving the Plan list from the
rendered output and making everything fail closed: every branch that cannot compare
something exits non-zero rather than falling through to "ok", and `checked` is what
makes (1) impossible to reintroduce -- the step cannot pass unless it compared at
least one Plan-day.

Time zones are checked, not assumed: the comparison is in minutes-of-day, meaningful
only if both sides are UTC. A CronJob with no spec.timeZone is UTC (Kubernetes'
default); a Plan's window.timeZone has no default and must say UTC. Every CronJob in
this cluster is UTC, after a Berlin window drifted an hour against all of them twice
a year at the DST boundaries.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, kustomize, parse_docs  # noqa: E402

CONFIG = "cluster/base/infrastructure/15-system-upgrade-controller/config"

DAY_TOKENS = {
    "su": 0, "sun": 0, "sunday": 0,
    "mo": 1, "mon": 1, "monday": 1,
    "tu": 2, "tue": 2, "tues": 2, "tuesday": 2,
    "we": 3, "wed": 3, "weds": 3, "wednesday": 3,
    "th": 4, "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
    "fr": 5, "fri": 5, "friday": 5,
    "sa": 6, "sat": 6, "saturday": 6,
}
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


class Refused(Exception):
    """A guard that cannot compare something must fail rather than pass vacuously."""


def expand_field(field, lo, hi, names=None):
    values = set()
    for part in field.split(","):
        part = part.strip()
        if names:
            part = part.lower()
            # Longest token first: a naive pass would turn 'sun' into '0n' via
            # 'su' before ever trying 'sun' itself.
            for token in sorted(names, key=len, reverse=True):
                part = part.replace(token, str(names[token]))
        try:
            if part == "*":
                values.update(range(lo, hi + 1))
            elif "/" in part:
                base, step = part.split("/")
                step = int(step)
                if "-" in base:
                    a, b = base.split("-")
                    start, stop = int(a), int(b)
                else:
                    start, stop = (lo if base == "*" else int(base)), hi
                values.update(range(start, stop + 1, step))
            elif "-" in part:
                a, b = part.split("-")
                values.update(range(int(a), int(b) + 1))
            else:
                values.add(int(part))
        except ValueError:
            raise Refused(f"cron field '{field}' contains a part this guard cannot parse ('{part}'). "
                          f"Rather than skip the check, this fails: extend the guard or simplify the schedule.")
    out = [v for v in sorted(values) if lo <= v <= hi]
    if not out:
        raise Refused(f"cron field '{field}' expanded to nothing in range {lo}-{hi}.")
    return out


def cron_fires(cron):
    """-> {weekday: [minute-of-day, ...]}, cron convention 0=Sunday."""
    parts = cron.split()
    if len(parts) != 5:
        raise Refused(f"fleet-health schedule '{cron}' is not a 5-field cron expression.")
    minute_f, hour_f, dom_f, _month_f, dow_f = parts
    if dom_f != "*":
        raise Refused(f"fleet-health schedule '{cron}' restricts day-of-month; this guard only reasons "
                      f"about day-of-week. Failing rather than passing vacuously.")
    dows = {0 if d == 7 else d for d in expand_field(dow_f, 0, 7, names=DAY_TOKENS)}
    fires = sorted(h * 60 + m for h in expand_field(hour_f, 0, 23) for m in expand_field(minute_f, 0, 59))
    return {d: fires for d in dows}


def to_minutes(hhmm, what):
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        raise Refused(f"{what} is '{hhmm}', not HH:MM.")


def collisions(cron, cron_tz, windows):
    """windows: [(plan, days, start, end, tz)]. -> (errors, plan-days compared).

    Raises Refused wherever it cannot compare, so that it never passes vacuously.
    """
    if not cron:
        raise Refused("could not read talos-fleet-health's schedule from the rendered output. The CronJob may "
                      "have been renamed -- this guard must not pass without comparing anything.")
    if not windows:
        raise Refused("found no Plan with a spec.window in the rendered output. Either every windowed Plan was "
                      "renamed/removed, or the selector is wrong -- this guard must not pass without "
                      "comparing anything.")
    if cron_tz not in ("", "UTC"):
        raise Refused(f"talos-fleet-health's CronJob sets spec.timeZone: '{cron_tz}'. This guard compares its "
                      f"fire times against every windowed Plan's spec.window.timeZone as UTC minutes-of-day; "
                      f"a differently-zoned CronJob makes that comparison meaningless. Either revert it to UTC "
                      f"(every other CronJob in this cluster is unset/UTC) or extend this guard to convert "
                      f"zones before comparing.")

    fires_by_day = cron_fires(cron)
    errors, checked = [], 0
    for name, days, start, end, tz in windows:
        if not days or not start or not end:
            raise Refused(f"Plan '{name}' has a spec.window with missing days/startTime/endTime "
                          f"(days='{days}' start='{start}' end='{end}').")
        if tz != "UTC":
            raise Refused(f"Plan '{name}' has spec.window.timeZone '{tz}' (expected 'UTC', explicitly -- this "
                          f"field has no Kubernetes-side default the way a CronJob's does). This guard treats "
                          f"every window's startTime/endTime as UTC minutes-of-day; a differently-zoned window "
                          f"would be compared against the wrong fire times without anyone noticing, which is "
                          f"exactly the DST-drift class of bug talos-controlplane-upgrade.yaml's own window "
                          f"comment already had to fix once.")
        s, e = to_minutes(start, f"{name} startTime"), to_minutes(end, f"{name} endTime")
        if e < s:
            raise Refused(f"Plan '{name}' window {start}-{end} wraps past midnight; this guard does not model "
                          f"that. Failing rather than passing vacuously.")
        for token in days.lower().split(","):
            token = token.strip()
            if token not in DAY_TOKENS:
                raise Refused(f"Plan '{name}' declares window day '{token}', which this guard does not recognise. "
                              f"Failing rather than silently skipping the check.")
            day = DAY_TOKENS[token]
            checked += 1
            for fire in fires_by_day.get(day, []):
                if s <= fire <= e:
                    errors.append(f"talos-fleet-health fires at {fire // 60:02d}:{fire % 60:02d} UTC on "
                                  f"{DAY_NAMES[day]}, inside {name}'s window ({start}-{end}). A check that "
                                  f"samples mid-window can report a legitimately cordoned/rebooting node as "
                                  f"unready -- move the schedule clear of every windowed Plan's window.")
    if checked == 0:
        raise Refused("no Plan window/day pair was actually compared.")
    return errors, checked


def extract(docs):
    """(cron, cron_tz, windows) from the rendered SUC config."""
    cron = cron_tz = ""
    for d in docs:
        if d.get("kind") == "CronJob" and d["metadata"]["name"] == "talos-fleet-health":
            cron = d["spec"].get("schedule", "")
            cron_tz = d["spec"].get("timeZone") or ""
    windows = []
    for d in docs:
        window = d.get("spec", {}).get("window") if d.get("kind") == "Plan" else None
        if window is not None:
            windows.append((d["metadata"]["name"], ",".join(window.get("days") or []), window.get("startTime") or "",
                            window.get("endTime") or "", window.get("timeZone") or ""))
    return cron, cron_tz, windows


def main():
    text, error = kustomize(ROOT / CONFIG)
    if error is not None:
        print(f"::error::kubectl kustomize failed for {CONFIG}: {error}")
        return 1
    cron, cron_tz, windows = extract(parse_docs(text))
    print(f"fleet-health schedule: {cron}")
    print("windowed Plans:")
    for name, days, start, end, tz in windows:
        print(f"{name}|{days}|{start}|{end}|{tz}")
    try:
        errors, checked = collisions(cron, cron_tz, windows)
    except Refused as refusal:
        print(f"::error::{refusal}")
        return 1
    for error in errors:
        print(f"::error::{error}")
    if errors:
        return 1
    print(f"ok    schedule clear of all {checked} windowed Plan-day(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
