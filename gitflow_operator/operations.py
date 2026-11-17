from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .core import (
    MergeRequest,
    OperatorError,
    current_branch,
    current_commit,
    detect_remote,
    dirty_files,
    evidence,
    git,
    remote_evidence,
    require_source_branch,
    resolve_base,
    validate_branch,
)
from .providers import Provider, create_provider


def _find_and_validate(
    repo: Path,
    provider: Provider,
    target: str,
) -> MergeRequest:
    branch = current_branch(repo)
    require_source_branch(branch, target)
    mr = provider.find_merge_request(branch, target)
    mr_evidence = {"merge_request": {"id": mr.number, "url": mr.url}}
    if mr.source_branch != branch or mr.target_branch != target:
        raise OperatorError("merge request source or target does not match the current Gitflow branch", details=mr_evidence)
    if mr.state not in {"open", "opened"}:
        raise OperatorError("merge request is not open", details=mr_evidence)
    if mr.draft:
        raise OperatorError("draft merge requests cannot be watched or merged", details=mr_evidence)
    if mr.review_decision == "CHANGES_REQUESTED":
        raise OperatorError("merge request has a changes-requested review", details=mr_evidence)
    head = current_commit(repo)
    if not mr.source_sha:
        raise OperatorError("merge request source revision is unavailable", details=mr_evidence)
    if mr.source_sha != head:
        raise OperatorError("stale merge request head does not match the current source revision", details=mr_evidence)
    return mr


def status(repo: Path, item_id: str | None) -> dict[str, Any]:
    return evidence(repo, item_id, operation="status")


def branch_from_develop(
    repo: Path, item_id: str | None, branch: str, base: str | None = None,
) -> dict[str, Any]:
    target = resolve_base(repo, base)
    validate_branch(repo, branch)
    if dirty_files(repo):
        raise OperatorError("working tree has changes; commit or explicitly preserve them before switching branches")
    if _branch_exists(repo, branch):
        return evidence(repo, item_id, operation="branch-from-develop", status="blocked", reason=f"branch already exists: {branch}")
    if _branch_exists(repo, target):
        base_ref = f"refs/heads/{target}"
    elif _branch_exists(repo, target, remote=True):
        base_ref = f"refs/remotes/origin/{target}"
    else:
        raise OperatorError(f"base branch {target!r} is not available locally; fetch it or specify an existing --base")
    git(repo, "checkout", "--no-track", "-b", branch, base_ref)
    return evidence(
        repo, item_id, operation="branch-from-develop", status="created",
        target_branch=branch, base_branch=target, base_ref=base_ref,
        base_revision=current_commit(repo), freshness="local-cache; no fetch performed",
    )


def _branch_exists(repo: Path, branch: str, remote: bool = False) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
             f"refs/remotes/origin/{branch}" if remote else f"refs/heads/{branch}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperatorError("git executable is not installed") from exc
    if result.returncode not in {0, 1}:
        raise OperatorError(f"cannot inspect branch: {branch}")
    return result.returncode == 0


def commit(repo: Path, item_id: str | None, message: str, paths: list[str]) -> dict[str, Any]:
    if not dirty_files(repo):
        return evidence(repo, item_id, operation="commit", status="blocked", reason="no changes to commit")
    from .core import git

    git(repo, "add", *paths)
    git(repo, "commit", "-m", message)
    return evidence(repo, item_id, operation="commit", status="committed", message=message)


def release_notes(repo: Path, item_id: str | None, summary: str, tests: str | None) -> dict[str, Any]:
    lines = [
        f"## Delivery {item_id or 'item'}",
        "",
        f"- Summary: {summary}",
        f"- Branch: {current_branch(repo)}",
        f"- Commit: {current_commit(repo)}",
    ]
    if tests:
        lines.append(f"- Tests: {tests}")
    return evidence(repo, item_id, operation="prepare-release-notes", status="prepared", release_notes="\n".join(lines))


def create_merge_request(
    repo: Path,
    item_id: str | None,
    title: str,
    body: str,
    dry_run: bool,
    environ: dict[str, str] | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    remote = detect_remote(repo, environ)
    target = resolve_base(repo, target)
    branch = current_branch(repo)
    source_sha = current_commit(repo)
    require_source_branch(branch, target)
    base = remote_evidence(remote, target, source_sha)
    if dry_run:
        return evidence(repo, item_id, operation="create-merge-request", status="dry-run", outcome="dry-run", **base, title=title, body=body)
    provider = create_provider(remote, str(repo), environ)
    mr = provider.create_merge_request(branch, title, body, target=target)
    if mr.source_branch != branch or mr.target_branch != target:
        raise OperatorError(
            "created merge request source or target did not match the requested Gitflow branches",
            details={"merge_request": {"id": mr.number, "url": mr.url}},
        )
    if not mr.source_sha or mr.source_sha != source_sha:
        raise OperatorError(
            "created merge request head does not match the current source revision",
            details={"merge_request": {"id": mr.number, "url": mr.url}},
        )
    return evidence(
        repo,
        item_id,
        operation="create-merge-request",
        status="created",
        outcome="created",
        **base,
        merge_request={"id": mr.number, "url": mr.url},
        url=mr.url,
    )


def watch_ci(
    repo: Path,
    item_id: str | None,
    dry_run: bool,
    environ: dict[str, str] | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    remote = detect_remote(repo, environ)
    target = resolve_base(repo, target)
    branch = current_branch(repo)
    source_sha = current_commit(repo)
    require_source_branch(branch, target)
    base = remote_evidence(remote, target, source_sha)
    if dry_run:
        return evidence(repo, item_id, operation="watch-ci", status="dry-run", outcome="dry-run", **base)
    provider = create_provider(remote, str(repo), environ)
    mr = _find_and_validate(repo, provider, target)
    try:
        provider.wait_for_pipeline(mr)
        pipeline, checks = provider.pipeline_status(mr)
    except OperatorError as exc:
        raise OperatorError(
            str(exc),
            details={"merge_request": {"id": mr.number, "url": mr.url}},
        ) from exc
    status_value = "passed" if pipeline == "passed" else pipeline
    result = evidence(
        repo,
        item_id,
        operation="watch-ci",
        status=status_value,
        outcome=status_value,
        **base,
        merge_request={"id": mr.number, "url": mr.url},
        pipeline={"status": pipeline, "checks": checks},
    )
    if pipeline != "passed":
        result["reason"] = f"CI is {pipeline}; merge requires completed successful checks"
    return result


def _verify_merge_gates(provider: Provider, mr: MergeRequest) -> dict[str, Any]:
    mr_evidence = {"merge_request": {"id": mr.number, "url": mr.url}}
    try:
        pipeline, checks = provider.pipeline_status(mr)
    except OperatorError as exc:
        raise OperatorError(str(exc), details=mr_evidence) from exc
    if pipeline != "passed":
        raise OperatorError(f"CI is {pipeline}; squash merge requires completed successful checks", details=mr_evidence)
    try:
        unresolved = provider.unresolved_discussions(mr)
    except OperatorError as exc:
        raise OperatorError(str(exc), details=mr_evidence) from exc
    if unresolved:
        raise OperatorError(
            f"merge request has {len(unresolved)} unresolved discussion thread(s)",
            details={**mr_evidence, "discussions": {"unresolved": len(unresolved)}},
        )
    try:
        required, approved = provider.approval_status(mr)
    except OperatorError as exc:
        raise OperatorError(str(exc), details=mr_evidence) from exc
    if approved < required:
        raise OperatorError(
            f"merge request has {approved} of {required} required approvals",
            details={**mr_evidence, "approvals": {"required": required, "approved": approved}},
        )
    try:
        protection = provider.protected_branch(mr.target_branch)
    except OperatorError as exc:
        raise OperatorError(str(exc), details=mr_evidence) from exc
    return {
        "pipeline": {"status": pipeline, "checks": checks},
        "approvals": {"required": required, "approved": approved},
        "protected_branch": protection,
        "discussions": {"unresolved": 0},
    }


def squash_merge(
    repo: Path,
    item_id: str | None,
    dry_run: bool,
    environ: dict[str, str] | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    remote = detect_remote(repo, environ)
    target = resolve_base(repo, target)
    branch = current_branch(repo)
    source_sha = current_commit(repo)
    require_source_branch(branch, target)
    base = remote_evidence(remote, target, source_sha)
    if dry_run:
        return evidence(repo, item_id, operation="squash-merge-to-develop", status="dry-run", outcome="dry-run", **base)
    provider = create_provider(remote, str(repo), environ)
    mr = _find_and_validate(repo, provider, target)
    gates = _verify_merge_gates(provider, mr)
    try:
        provider.squash_merge(mr)
    except OperatorError as exc:
        raise OperatorError(str(exc), details={"merge_request": {"id": mr.number, "url": mr.url}, "gates": gates}) from exc
    return evidence(
        repo,
        item_id,
        operation="squash-merge-to-develop",
        status="merged",
        outcome="merged",
        **base,
        merge_request={"id": mr.number, "url": mr.url},
        gates=gates,
    )
