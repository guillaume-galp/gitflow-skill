# Changelog

## v0.1.0 — 2026-11-16

- Add a standalone, provider-neutral Gitflow operator and composable skill.
- Support GitHub, SCM1 GitLab, and allowlisted self-managed GitLab.
- Enforce source revision, target, pipeline, discussion, approval, and protected
  branch checks before squash merge.
- Emit JSON evidence and fail closed on ambiguous providers and unsafe states.
