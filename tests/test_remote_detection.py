import subprocess

import pytest

from gitflow_operator.core import OperatorError, detect_remote, parse_remote
from gitflow_operator.providers import LegacyGitLabProvider, create_provider


def init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "git@github.com:org/repo.git"], check=True)


@pytest.mark.parametrize(
    ("url", "expected_host", "expected_project"),
    [
        ("https://github.com/org/repo.git", "github.com", "org/repo"),
        ("git@github.com:org/repo.git", "github.com", "org/repo"),
        ("ssh://git@github.com/org/repo.git", "github.com", "org/repo"),
        ("https://scm1.ulysses.galp.internal/group/sub/repo.git", "scm1.ulysses.galp.internal", "group/sub/repo"),
        ("git@scm1.ulysses.galp.internal:group/sub/repo.git", "scm1.ulysses.galp.internal", "group/sub/repo"),
    ],
)
def test_remote_forms_parse_https_and_ssh(url, expected_host, expected_project):
    assert parse_remote(url) == (expected_host, expected_project)


def test_detects_github_and_scm1(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/org/repo.git"],
        check=True,
    )
    assert detect_remote(repo).provider == "github"

    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "git@scm1.ulysses.galp.internal:group/repo.git"],
        check=True,
    )
    remote = detect_remote(repo)
    assert remote.provider == "scm1"
    assert remote.token_env == "GITLAB_LGC_TOKEN"


def test_self_managed_gitlab_requires_explicit_host_allowlist(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "https://legacy.example.internal/group/repo.git"],
        check=True,
    )

    with pytest.raises(OperatorError, match="unsupported SCM host"):
        detect_remote(repo, {})

    remote = detect_remote(repo, {"GITFLOW_GITLAB_HOSTS": "legacy.example.internal"})
    assert remote.provider == "gitlab"
    assert remote.token_env == "GITLAB_TOKEN"
    assert isinstance(
        create_provider(remote, str(repo), {"GITLAB_TOKEN": "test-token"}),
        LegacyGitLabProvider,
    )


def test_rejects_multiple_origin_urls_and_embedded_https_credentials(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "--add", "origin", "git@scm1.ulysses.galp.internal:group/repo.git"],
        check=True,
    )
    with pytest.raises(OperatorError, match="ambiguous"):
        detect_remote(repo)

    with pytest.raises(OperatorError, match="must not embed credentials"):
        parse_remote("https://secret@example.com/group/repo.git")


def test_unknown_host_fails_explicitly(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "https://unknown.example/repo.git"],
        check=True,
    )
    with pytest.raises(OperatorError, match="unsupported SCM host: unknown.example"):
        detect_remote(repo)
