# Changelog

## v0.2.0 — 2026-11-17

- Resolve base/target branches from explicit options, Gitflow configuration,
  integration conventions and cached default refs, including `development`
  and `master`; carry the target through GitHub/GitLab operations and evidence.
- Add neutral `branch`/`squash-merge` commands while preserving legacy aliases.
- Support repository topic naming and remote-only bases without requiring a
  method backlog; preserve dirty work and local base history.
- Document repository-native defaults and explicit safe tool fallback.

## v0.1.0 — 2026-11-16

- Add a standalone, provider-neutral Gitflow operator and composable skill.
- Support GitHub, SCM1 GitLab, and allowlisted self-managed GitLab.
- Enforce source revision, target, pipeline, discussion, approval, and protected
  branch checks before squash merge.
- Emit JSON evidence and fail closed on ambiguous providers and unsafe states.
