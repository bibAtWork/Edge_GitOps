# Future: Coraza WAF

## What it is

[Coraza](https://coraza.io) (CNCF sandbox) is an open-source Web Application Firewall engine
that runs as an Envoy Wasm filter (`coraza-proxy-wasm`).  It implements the OWASP Core Rule Set
(CRS) and can detect/block OWASP Top 10 attacks (SQLi, XSS, RCE, path traversal, etc.) without
a separate proxy hop.

## How to add it

### 1. Mount the Wasm binary

Add a Wasm filter binary as a ConfigMap (or download it via initContainer):

```yaml
# In 25-gateway-authz/kustomization.yaml, add:
# - coraza-wasm-configmap.yaml
```

### 2. Add the filter to envoy-config.yaml

Insert **before** `envoy.filters.http.local_ratelimit` in the `http_filters` list:

```yaml
# 0. Coraza WAF — OWASP CRS enforcement via Wasm.
- name: envoy.filters.http.wasm
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.wasm.v3.Wasm
    config:
      name: coraza_waf
      root_id: coraza
      vm_config:
        runtime: envoy.wasm.runtime.v8
        code:
          local:
            filename: /var/lib/coraza/coraza-proxy-wasm.wasm
        allow_precompiled: true
      configuration:
        "@type": type.googleapis.com/google.protobuf.StringValue
        value: |
          {
            "rules": [
              "Include @recommended-conf",
              "Include @owasp_crs/*.conf",
              "SecRuleEngine On"
            ]
          }
```

### 3. Mount the Wasm file into Cilium's Envoy

This requires a `hostPath` volume on all nodes where Cilium runs, or a DaemonSet that distributes
the `.wasm` binary.  Cilium's Envoy sidecar can mount host paths configured via the Cilium DaemonSet
`extraVolumes` / `extraVolumeMounts` values.

### 4. Tune the ruleset

Start with `SecRuleEngine DetectionOnly` to observe what would be blocked, then switch to `On`.
Use `SecRuleRemoveById` to suppress false-positives specific to your applications.

## Why it was deferred

- Distributing the Wasm binary to all cluster nodes requires either a DaemonSet pre-loader or
  a direct Cilium DaemonSet volume mount, both of which add operational complexity.
- False-positive tuning is iterative; better done after the auth layer is stable.
- The existing Cilium L4/L7 network policies already block the vast majority of unauthorized
  traffic before it reaches the WAF layer.

## References

- https://coraza.io/docs/tutorials/envoy/
- https://github.com/corazawaf/coraza-proxy-wasm
- https://owasp.org/www-project-modsecurity-core-rule-set/
