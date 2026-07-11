# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding for a future GitHub integration (issues,
PRs, repo search/automation). No code exists here yet.

**Intended scope (not yet designed in detail):**
- A GitHub REST/GraphQL API client (likely PAT or GitHub App auth)
- Tools exposed via `TOOL_REGISTRY` (e.g. `github_create_issue`,
  `github_search_code`) for use by `EngineeringAgent`, gated through
  `ACTION_GUARD` the same as any other external-action call
- This would likely be the primary consumer-facing hook for
  `agents/engineering/engineering_agent.py`'s software-development workflows,
  alongside `workflows/software_development/` once that's built out

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
