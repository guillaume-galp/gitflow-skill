import json
from types import SimpleNamespace

import pytest

from gitflow_operator.core import OperatorError, Remote
from gitflow_operator.providers import GitHubProvider


def test_github_merge_request_creation_preserves_gh_cli_contract(monkeypatch):
    calls = []

    def fake_gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "create"]:
            return "https://github.com/org/repo/pull/42\n"
        return json.dumps([{
            "number": 42,
            "url": "https://github.com/org/repo/pull/42",
            "title": "Feature",
            "baseRefName": "develop",
            "headRefName": "feature/task",
            "headRefOid": "abc123",
            "isDraft": False,
            "state": "OPEN",
        }])

    monkeypatch.setattr("gitflow_operator.providers._gh", fake_gh)
    provider = GitHubProvider(Remote("github", "github.com", "org/repo"), "/tmp/repo")

    mr = provider.create_merge_request("feature/task", "Feature", "Body")

    assert mr.number == "42"
    assert mr.source_sha == "abc123"
    assert calls[0][:5] == ["pr", "create", "--base", "develop", "--head"]
    assert calls[0][-2:] == ["--body", "Body"]


def test_github_checks_require_completed_success(monkeypatch):
    def fake_gh(args, cwd):
        if "check-runs" in args[1]:
            return json.dumps({"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success"}]})
        return json.dumps({"statuses": []})

    monkeypatch.setattr("gitflow_operator.providers._gh", fake_gh)
    provider = GitHubProvider(Remote("github", "github.com", "org/repo"), "/tmp/repo")
    mr = type("MR", (), {"source_sha": "abc123"})()

    assert provider.pipeline_status(mr)[0] == "passed"


def test_github_squash_merge_does_not_use_override(monkeypatch):
    calls = []

    def fake_gh(args, cwd):
        calls.append(args)
        if args[:3] == ["pr", "view", "42"]:
            return json.dumps({"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"})
        return ""

    monkeypatch.setattr("gitflow_operator.providers._gh", fake_gh)
    provider = GitHubProvider(Remote("github", "github.com", "org/repo"), "/tmp/repo")
    mr = type("MR", (), {"number": "42"})()

    provider.squash_merge(mr)

    assert calls[0] == ["pr", "merge", "42", "--squash"]
    assert "--admin" not in calls[0]


def test_github_cli_authorization_errors_are_explicit_and_redacted(monkeypatch):
    secret = "do-not-print-github-token"
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setattr(
        "gitflow_operator.providers.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stderr=f"HTTP 403: invalid token {secret}",
            stdout="",
        ),
    )
    provider = GitHubProvider(Remote("github", "github.com", "org/repo"), "/tmp/repo")

    with pytest.raises(OperatorError, match="HTTP 403") as error:
        provider.find_merge_request("feature/task", "develop")

    assert secret not in str(error.value)
