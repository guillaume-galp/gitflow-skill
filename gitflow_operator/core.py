from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


SCM1_HOST = "scm1.ulysses.galp.internal"


class OperatorError(Exception):
    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


@dataclass(frozen=True)
class Remote:
    provider: str
    host: str
    project: str
    token_env: str | None = None


@dataclass(frozen=True)
class MergeRequest:
    number: str
    url: str
    title: str
    source_branch: str
    target_branch: str
    source_sha: str
    draft: bool
    state: str
    review_decision: str = ""


def git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperatorError("git executable is not installed") from exc
    if result.returncode:
        raise OperatorError(f"git {args[0]} failed with exit status {result.returncode}")
    return result.stdout.strip()


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def current_commit(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def dirty_files(repo: Path) -> list[str]:
    return [line for line in git(repo, "status", "--short").splitlines() if line.strip()]


def parse_remote(value: str) -> tuple[str, str]:
    if not value or "\n" in value or "\r" in value:
        raise OperatorError("origin remote is empty or ambiguous")
    if re.match(r"^[^/@:]+@[^/:]+:", value):
        match = re.match(r"^[^/@:]+@([^/:]+):(.+)$", value)
        if not match:
            raise OperatorError("origin remote is unsupported")
        host, path = match.groups()
    else:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise OperatorError("origin remote is unsupported") from exc
        if parsed.scheme not in {"https", "ssh"} or not hostname:
            raise OperatorError("origin remote must use HTTPS or SSH")
        if parsed.query or parsed.fragment:
            raise OperatorError("origin remote must not contain query parameters or fragments")
        if parsed.scheme == "https" and (parsed.username is not None or parsed.password is not None):
            raise OperatorError("origin HTTPS remote must not embed credentials")
        host = hostname
        default_port = 443 if parsed.scheme == "https" else 22
        if port and port != default_port:
            host = f"{host}:{port}"
        path = unquote(parsed.path.lstrip("/"))
    path = path.removesuffix(".git").strip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise OperatorError("origin remote has an invalid project path")
    return host.lower(), path


def detect_remote(repo: Path, environ: dict[str, str] | None = None) -> Remote:
    env = os.environ if environ is None else environ
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "--all", "origin"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperatorError("git executable is not installed") from exc
    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not urls:
        raise OperatorError("repository must have exactly one origin remote")
    if len(urls) != 1:
        raise OperatorError("origin remote is ambiguous: multiple URLs are configured")
    host, project = parse_remote(urls[0])
    if host == "github.com":
        if len(project.split("/")) != 2:
            raise OperatorError("GitHub origin must identify exactly owner/repository")
        return Remote("github", host, project)
    if host == SCM1_HOST:
        return Remote("scm1", host, project, "GITLAB_LGC_TOKEN")
    configured_hosts = {
        entry.strip().lower()
        for entry in env.get("GITFLOW_GITLAB_HOSTS", "").split(",")
        if entry.strip()
    }
    if host in configured_hosts:
        return Remote("gitlab", host, project, "GITLAB_TOKEN")
    raise OperatorError(f"unsupported SCM host: {host}")


def evidence(repo: Path, item_id: str | None = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo": str(repo),
        "branch": current_branch(repo),
        "commit": current_commit(repo),
        "dirty_files": dirty_files(repo),
    }
    if item_id:
        data["item_id"] = item_id
    data.update(extra)
    return data


def redact(value: Any, environ: dict[str, str] | None = None) -> Any:
    env = os.environ if environ is None else environ
    secrets = [
        secret
        for name, secret in env.items()
        if secret and any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD"))
    ]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, env) for item in value]
    if isinstance(value, tuple):
        return [redact(item, env) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, env) for key, item in value.items()}
    return value


def validate_branch(repo: Path, branch: str) -> str:
    if not branch or branch.startswith("-") or branch == "HEAD":
        raise OperatorError(f"invalid branch name: {branch!r}")
    git(repo, "check-ref-format", f"refs/heads/{branch}")
    return branch


def _optional_git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True, capture_output=True, check=False,
        )
    except FileNotFoundError as exc:
        raise OperatorError("git executable is not installed") from exc
    if result.returncode == 1:
        return None
    if result.returncode:
        raise OperatorError(f"git {args[0]} failed with exit status {result.returncode}")
    return result.stdout.strip()


def resolve_base(repo: Path, explicit: str | None = None) -> str:
    """Resolve repository conventions offline; never create or rename a base."""
    if explicit is not None:
        return validate_branch(repo, explicit)
    for key in ("gitflow.operator.base", "gitflow.branch.develop"):
        candidate = _optional_git(repo, "config", "--get", key)
        if candidate is not None:
            return validate_branch(repo, candidate)
    rows = git(
        repo, "for-each-ref", "--format=%(refname) %(symref)",
        "refs/heads/", "refs/remotes/origin/",
    ).splitlines()
    branches: set[str] = set()
    default_ref = _optional_git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    default = default_ref.removeprefix("refs/remotes/origin/") if default_ref else None
    for row in rows:
        parts = row.split()
        ref = parts[0]
        if ref != "refs/remotes/origin/HEAD" and len(parts) == 1:
            branches.add(ref.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/"))
    candidates = sorted(branches & {"develop", "development"})
    if len(candidates) > 1:
        raise OperatorError("ambiguous integration branches: develop, development; specify --base/--target or gitflow.operator.base")
    if candidates:
        return candidates[0]
    primary = _optional_git(repo, "config", "--get", "gitflow.branch.master")
    if primary is not None:
        return validate_branch(repo, primary)
    if default is not None:
        if default not in branches:
            raise OperatorError("origin/HEAD points to a missing cached branch; fetch it or specify --base/--target")
        return validate_branch(repo, default)
    candidates = sorted(branches & {"main", "master"})
    if len(candidates) == 1:
        return candidates[0]
    raise OperatorError("cannot determine an unambiguous base branch; specify --base/--target or gitflow.operator.base")


def require_source_branch(branch: str, target: str) -> None:
    if branch == "HEAD":
        raise OperatorError("detached HEAD: switch to a work branch first")
    if branch == target:
        raise OperatorError(f"source and target are both {target}; switch to a work branch first")


def remote_evidence(remote: Remote, target: str | None = None, source_sha: str | None = None) -> dict[str, str]:
    result = {"provider": remote.provider, "project": remote.project}
    if target is not None:
        result["target"] = target
    if source_sha is not None:
        result["source_revision"] = source_sha
    return result
