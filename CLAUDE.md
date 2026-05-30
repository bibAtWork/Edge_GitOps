# Claude Code Instructions

## Git Commits

Always use atomic commits following the Single Responsibility Principle:

- One commit per logical change (one feature, one fix, one config update, one doc change, etc.)
- Never bundle unrelated changes into a single commit
- Commit message body must explain **why**, not just what
- Use conventional commit prefixes: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`
- Scope the prefix when helpful: `feat(cilium):`, `fix(seaweedfs):`, `docs(readme):`

If multiple files belong to the same logical change (e.g., a new resource + its kustomization entry), they go in one commit. If they represent different concerns, split them.
