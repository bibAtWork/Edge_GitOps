# Real-estate-screener service map

The canonical runbook is `docs/runbooks/real-estate-screener.md` in Edge_GitOps.
This mounted excerpt gives the incident agent stable starting context.

- Namespace: `real-estate-screener`.
- Web: `screener-web`; internal readiness: `http://screener-web.real-estate-screener.svc:3000/health/checks`.
- Metrics: same service at `/metrics`; `screener_last_health_check_success` is the functionality signal.
- PostgreSQL: CloudNativePG cluster `screener-pg`; web readiness includes schema and query checks.
- Collector: CronJob `screener-collector`, every 30 minutes, can fail because of upstream portals as well as application faults.
- Macro import: `screener-macro`, daily. Other CronJobs may also exist; inspect their schedules before calling a run stale.
- Desired deployment comes from Flux on branch `ops/talos_linux`.
- A failed historical Job is not a live outage if a later Job succeeded.
- A healthy process endpoint does not prove that database-backed functionality works.
- No write action is authorized through this agent. Treat rollback and database operations as proposals for human review.
