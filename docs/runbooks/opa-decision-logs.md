# OPA decision-log credential masking

OPA's `system.log.mask` policy removes Authorization, Proxy-Authorization,
Cookie, and X-Api-Key headers before decision logs reach Pod stdout or the
central log pipeline. The source is `cluster/base/infrastructure/24-opa/configmap.yaml`.

Run `python scripts/ci/check_opa_log_mask.py` after edits. After a deployment,
send an invalid **synthetic** Bearer token through a protected route. Confirm
the HTTP 401 response, then search OPA Pod logs and VictoriaLogs for the
synthetic marker. It must appear in neither place. Never use a real credential
for this check.
