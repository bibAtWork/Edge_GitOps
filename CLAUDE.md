# Claude Code Instructions

## Git Commits

Always use atomic commits following the Single Responsibility Principle:

- One commit per logical change (one feature, one fix, one config update, one doc change, etc.)
- Never bundle unrelated changes into a single commit
- Commit message body must explain **why**, not just what
- Use conventional commit prefixes: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`
- Scope the prefix when helpful: `feat(cilium):`, `fix(seaweedfs):`, `docs(readme):`

If multiple files belong to the same logical change (e.g., a new resource + its kustomization entry), they go in one commit. If they represent different concerns, split them.

## Architecture Decisions

### SeaweedFS authentication

SeaweedFS S3 access uses **a single admin credential** (`seaweedfs-s3-secret`) combined with a **Cilium network policy** that restricts port 8333 to authorized pods only (`allow-seaweedfs-internal.yaml`).

This is intentional. Do not add per-bucket IAM users or switch to anonymous access. The network policy is the primary auth boundary inside the cluster; the credential is a second layer for defence-in-depth.

If the credential needs rotating, use:
```bash
./scripts/rotate-secrets.py credential \
  --file cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml \
  --key admin_access_key_id
```
