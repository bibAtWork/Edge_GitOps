---
name: screener-pr-draft
description: Future isolated GitHub PR member; deliberately not installed
role: sre_pr
provider: opencode_cli
---

# PR member contract (future)

This profile must run in a separate pod with no Kubernetes service account
token and a narrowly scoped GitHub credential. Its input is a validated RCA
handoff. It may propose a branch and draft PR with the evidence and change
plan. It must never merge or deploy. The PoC does not install this profile or
provide any GitHub credential.
