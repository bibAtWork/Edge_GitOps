# ADR-001: Decoupling L4 and L7 Routing with Cilium and Envoy Gateway

**Date:** 2026-08-13
**Status:** Proposed

## Context

The current Talos OS-based Kubernetes environment requires an ingress and routing architecture capable of handling strict security, identity management, and comprehensive observability. Specifically, the architecture must support robust Layer 7 capabilities, including seamless OpenTelemetry trace propagation, OIDC authentication via Keycloak, authorization via Open Policy Agent (OPA), and advanced rate limiting.

While standardizing on a single unified controller for all network layers is operationally appealing, evaluating the L7 capabilities of in-kernel/shared-proxy hybrid models revealed limitations. A shared proxy managed by the CNI currently lacks native telemetry span injection at the ingress layer and presents rigid barriers when customizing configurations for external authorization (ext_authz). To align with the priority for highly secure, open-source infrastructure tools, the architecture requires dedicated components that excel in their respective networking domains without compromising extensibility.

## Decision

We will implement a decoupled network architecture using the Kubernetes Gateway API:

- **Layer 4 (Data Plane & CNI):** Cilium will provide high-performance, eBPF-driven networking and strict network policies.
- **Layer 7 (Ingress & API Gateway):** Envoy Gateway will be deployed to manage dedicated Envoy proxy fleets for all L7 HTTP/gRPC routing, authentication, and observability.

## Consequences

**Positive:**

- **Unbroken Telemetry:** Envoy Gateway natively injects `traceparent` headers at the edge, guaranteeing complete end-to-end distributed tracing across all backend services.
- **Native Identity & Authorization:** Envoy Gateway's `SecurityPolicy` and `EnvoyExtensionPolicy` provide first-class support for Keycloak (OIDC) and OPA (ext_authz), eliminating the need for brittle, manual configuration patching.
- **Granular Traffic Control:** Access to `BackendTrafficPolicy` allows for sophisticated, multi-tiered rate limiting (local edge dropping and global Redis-backed quotas) directly attached to routing rules.
- **Enhanced Tenant Isolation:** Dedicated proxy fleets per Gateway resource prevent "noisy neighbor" scenarios and limit the blast radius of potential L7 exploits or traffic spikes.
- **Decoupled Lifecycles:** Upgrading L7 ingress capabilities is completely isolated from the base CNI, drastically reducing the risk of cluster-wide network outages during routine maintenance.

**Negative:**

- **Increased Resource Utilization:** Operating dedicated Envoy deployments for L7 traffic incurs a higher CPU and memory baseline compared to a shared per-node proxy model.
- **Operational Sprawl:** Managing and monitoring an additional controller (Envoy Gateway) alongside the CNI adds slight complexity to the deployment lifecycle.

## Alternatives Considered

- **Cilium for All (L4 + L7):** Rejected. While it offers a lower resource footprint and a consolidated toolchain, the inability to natively inject OpenTelemetry root spans at the ingress and the lack of flexible, persistent ext_authz configurations for OPA creates unacceptable friction for the required security and observability baseline.
