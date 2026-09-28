# Kyverno policy regression tests

Run `python3 scripts/ci/kyverno_policy_tests.py` before changing CEL admission
policies. The **Kyverno CEL policy regressions** PR check runs the same command.
The check and **Trivy scan capacity** are required by the target branch ruleset.
It evaluates the actual policy files with the Kyverno CLI release pinned by the
admission controller's HelmRelease. It requires Python with PyYAML and network
access to the official release download; no kubeconfig or secrets are needed.
Set `KYVERNO_CLI` to a local matching CLI binary to avoid downloading it again.

The 133 fixtures in `scripts/ci/kyverno_policy_cases.py` cover the ten validation
and mutation policies: compliant and noncompliant resources, regular and init
containers, namespace exclusions, recovery identities, CREATE/UPDATE/DELETE,
Workflow arguments and preservation of explicit revision-history limits. Audit
policy failures are checked as failures, while their Audit action is preserved.
Add both pass and fail fixtures for a new ValidatingPolicy; CI rejects missing
coverage. Compare the whole mutated object when adding mutation cases.

When upgrading Kyverno, record the Linux and Windows x86_64 archive SHA256 sums
from the official release's `checksums.txt` in
`scripts/ci/kyverno-cli-checksums.json`, and run the suite with that CLI version.
The runner verifies the downloaded archive and refuses version skew. Flux image
signing trust continues to use the separate Flux image signature check.

The CLI constructs real admission-shaped CEL requests: DELETE has `object: null`
and the fixture in `oldObject`; UPDATE uses the fixture for both objects. It does
not run API-server defaulting, RBAC or registered webhooks. The Deployment
fixtures explicitly set Kubernetes' revision-history default of ten. Continue
running `python3 scripts/test-argo-operator-scope.py` against the cluster after
policy deployment to verify live RBAC and admission together. For a mutation
change, also dry-run a Deployment with the history field omitted against the API
server and verify the default becomes three.

To verify that the suite catches a regression, copy the policy directory to a
temporary directory, replace one validation expression with `true`, then run
`python3 scripts/ci/kyverno_policy_tests.py --policy-dir <temporary-directory>`.
Its denied fixtures must fail. Do not change the committed policy for this test.
