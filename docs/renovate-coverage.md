# Renovate coverage policy

The `Renovate coverage` job in GitOps Lint runs on every PR targeting
`ops/talos_linux`. It validates `renovate.json`, runs Renovate's native
`--platform=local --dry-run=extract` against the PR checkout, then compares
the extracted dependencies with an independent inventory of committed pins.
No registry lookups, cluster access or GitHub credentials are needed. The local
platform ignores `baseBranchPatterns`, so it checks the proposed files rather
than extracting the remote base branch.

Keep built-in managers for supported formats: Kubernetes images, Flux charts
and HelmRelease image values, Kustomize images, GitHub Actions, npm package
files and Python requirements. Use custom managers only for unsupported fields:
CNPG `imageName`, tag-only image overrides, Ansible tool versions, Talos pins
and release binaries or inline pip installs in workflows. A `# renovate:`
comment alone does not provide coverage; a configured manager must actually
extract the dependency from that source location.

## What CI checks

`scripts/ci/renovate_coverage.py` scans complete source trees under `cluster/`,
`bootstrap/ansible/`, `.github/workflows/`, `docs/runbooks/recovery/` and the
coverage tool's own `.github/renovate-coverage/` directory. Deferred resources,
patches, application templates, init containers and Talos inline manifests are
included. The inventory recognizes:

- Combined `image` and `imageName` references, including digests and registry ports.
- Image mappings carrying `repository`/`registry` and `tag`/`digest`, and tag-only overrides.
- Chart `version` fields, `spec.version` pins, `*_version` fields, Kustomize `newTag`,
  source `tag`/`digest` refs, and Talos embedded `--version` flags.
- Workflow action refs, `*VERSION=` assignments, literal Helm download URLs,
  inline pip `package==version` installs, npm dependencies and pinned requirements.

YAML source nodes retain version spelling and source line numbers. CRD schemas,
metadata and encrypted Secret values are not dependency declarations. Flux's
generated `gotk-components.yaml` is maintained through its Flux release header,
using the built-in Flux manager, instead of independently updating its controllers.

A pin passes when a native extracted dependency matches its file, identity and
current version/digest. Custom-manager results must also contain the source line
in their native `replaceString`. Skipped dependencies and matching unconditional
`enabled: false` package rules do not count as coverage. The credential-free
extraction diagnostic `github-token-required` is allowed: it does not mean a
manager missed the pin, and the installed GitHub App supplies credentials during
normal update runs. Restrictions applying
only to update types, such as disabling major PostgreSQL updates, retain coverage.
The checker supports the current repository's disabling-rule selectors; it fails
on unknown selectors rather than guessing their effect.

Missing, empty, malformed or error-containing extraction output fails the check.
Renovate itself is version-pinned in `package.json` and maintained by the built-in
npm manager. The log adapter is tested and must be reviewed if Renovate changes
the `Extracted dependencies` log record format.

## Adding a dependency

Use a supported format first. An ordinary container image or Helm chart needs no
extra comment. For a custom field, extend an existing custom manager or add a
narrowly scoped manager in `renovate.json`, and run the coverage check. Copying a
comment to a new location without expanding its manager's file pattern will fail.

The CNPG manager covers both `cluster/base/infrastructure` and
`cluster/base/applications`; its previous infrastructure-only pattern missed the
screener database. The workflow-only Helm URL and pip-install managers cover
existing pins the GitHub Actions manager cannot extract.

The native extraction audit also found that Flux skipped the Grafana chart
in its large templated HelmRelease, and that the Helm values heuristic did not
recognize Longhorn's `manager`/`engine`/CSI image blocks (their keys do not end in
`image`). Focused custom managers cover only those fields. The existing Grafana
and Longhorn grouping/review rules apply to these dependencies as before.

## Maintenance decisions

`.github/renovate-coverage/exceptions.json` records exact file/field/value
decisions with a reason. There are no directory-wide exclusions. Entries fail
when their field disappears, their value changes, their reason is empty, or they
are duplicated.

The initial decisions cover only Talos Plan placeholders populated by Kustomize
replacements. Each points to a `versions.env` source that must itself pass native
Renovate coverage. Existing `No unreplaced sentinels` and Talos checks verify the
rendered replacement behavior.

An intentionally manual pin requires `"maintenance": "manual"`, an exact value
and a reason explaining the maintenance procedure. Review these decisions like
dependency configuration; an exception is a maintenance obligation, not evidence
that Renovate updates the field.

## Run locally

Use Node 24, npm and Python 3 with the pinned YAML parser:

```bash
pip install -r .github/renovate-coverage/requirements.txt
python3 scripts/ci/renovate_coverage.py
python3 -m unittest discover -s scripts/ci/tests -p 'test_renovate_coverage.py' -v
```

The script installs the pinned native Renovate CLIs into a temporary directory.
Use `--inventory` to inspect the source inventory without installing Renovate,
or `--extraction-log /path/to/renovate-coverage.jsonl` to replay a native result.
Normal runs save the JSON log to `$RUNNER_TEMP/renovate-coverage.jsonl` (or the
system temporary directory locally).

## Enforcement and limits

After this workflow has reported once, add **Renovate coverage** as a required
status check in the ruleset for `ops/talos_linux`. Keep the workflow free of path
filters so documentation-only PRs also report the required check. This PR does
not change repository rulesets or enable enforcement on `main`.

Coverage proves discovery and the supported repository-level disabling rules;
it does not prove that a registry has a newer release or that every future
release is installable. Those remain lookup, review and deployment concerns.
Arbitrary versions hidden in shell scripts, JSON application configuration,
Markdown examples, vendored CRD schemas or new field conventions cannot be
inferred universally. Extend the inventory and tests when introducing another
dependency-bearing format. API/schema versions and resources without dependency
pins do not need Renovate tags.
