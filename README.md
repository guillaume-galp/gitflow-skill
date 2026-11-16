# Gitflow Operator

Standalone Gitflow skill and command framework. This project owns the provider
adapters and the provider-neutral JSON CLI. Consumers such as Copilot Autopilot
compose with the installed `gitflow-operator` command; they do not own provider
implementation.

## Providers

- GitHub.com, using the authenticated `gh` CLI.
- SCM1 (`scm1.ulysses.galp.internal`), using the GitLab v4 REST API and
  `GITLAB_LGC_TOKEN`.
- Explicitly configured self-managed GitLab hosts, using the same API and
  `GITLAB_TOKEN`. Set `GITFLOW_GITLAB_HOSTS` to a comma-separated host allowlist.

Only HTTPS and SSH remotes are accepted. GitHub, SCM1 and configured GitLab
hosts are detected from the single `origin` remote. Missing, multiply defined,
unsupported, or credential-bearing remotes fail explicitly. GitLab API calls
use the platform's verified TLS defaults; tokens are sent only in process-local
HTTP headers and never in URLs, arguments, logs, or evidence.

## Install and test

```bash
python -m pip install .
python -m pytest
```

GitLab credentials must be present in the environment when an API operation is
run. For a self-managed host, its exact hostname must also appear in
`GITFLOW_GITLAB_HOSTS`. Tests use fake providers/transports and never mutate
live SCM resources.

## Command surface

```text
gitflow-operator [--repo PATH] [--item-id ID] status
gitflow-operator [--repo PATH] [--item-id ID] branch-from-develop --branch feature/NAME
gitflow-operator [--repo PATH] [--item-id ID] commit --message MESSAGE [PATH ...]
gitflow-operator [--repo PATH] [--item-id ID] create-merge-request --title TITLE [--body BODY]
gitflow-operator [--repo PATH] [--item-id ID] watch-ci
gitflow-operator [--repo PATH] [--item-id ID] prepare-release-notes --summary TEXT [--tests TEXT]
gitflow-operator [--repo PATH] [--item-id ID] squash-merge-to-develop
```

Every command emits JSON evidence with the legacy `repo`, `branch`, `commit`,
`dirty_files`, `item_id`, `operation`, and `status` fields where applicable.
Merge-request operations additionally record provider, project, MR identity,
target, source revision, gate evidence, and outcome. Blocked, failed, pending,
and incomplete operations emit evidence and return nonzero.

Feature/fix requests target `develop`. Before squash merging, the operator
reselects exactly one open request for the current source branch and verifies
its target, head revision, draft state, successful complete CI, resolved
discussions, required approvals, and protected-branch settings. The merge is
submitted without an override and with the exact source SHA where supported;
the server remains authoritative for protected-branch gates. A server rejection
or unconfirmed merge is reported as failure.

`gitflow-operator` skill instructions are in [`SKILL.md`](SKILL.md).
