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


def require_feature_branch(branch: str) -> None:
    if not branch.startswith(("feature/", "fix/")):
        raise OperatorError("merge requests must originate from a feature/ or fix/ branch")


def remote_evidence(remote: Remote, target: str | None = None, source_sha: str | None = None) -> dict[str, str]:
    result = {"provider": remote.provider, "project": remote.project}
    if target is not None:
        result["target"] = target
    if source_sha is not None:
        result["source_revision"] = source_sha
    return result
