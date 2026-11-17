import json
import subprocess

import pytest

from gitflow_operator import cli, operations
from gitflow_operator.core import OperatorError, git, resolve_base


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "master", str(tmp_path)], check=True)
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "commit", "--allow-empty", "-qm", "initial")
    return tmp_path


@pytest.mark.parametrize("branch", ["main", "master", "develop", "development"])
def test_native_repository_needs_no_method_layout(repo, branch):
    git(repo, "branch", "-m", branch)
    assert resolve_base(repo) == branch
    result = operations.branch_from_develop(repo, None, "bugfix/example")
    assert result["status"] == "created"
    assert result["base_branch"] == branch
    assert not (repo / "docs").exists()
    assert "item_id" not in result


def test_integration_branch_precedes_primary_default(repo):
    git(repo, "branch", "development")
    git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    assert resolve_base(repo) == "development"


def test_configured_and_explicit_names_precede_conventions(repo):
    git(repo, "branch", "develop")
    git(repo, "config", "gitflow.branch.develop", "integration")
    assert resolve_base(repo) == "integration"
    git(repo, "config", "gitflow.operator.base", "trunk")
    assert resolve_base(repo) == "trunk"
    assert resolve_base(repo, "development") == "development"


def test_ambiguous_branches_require_specific_choice(repo):
    git(repo, "branch", "develop")
    git(repo, "branch", "development")
    with pytest.raises(OperatorError, match="ambiguous integration"):
        resolve_base(repo)
    assert resolve_base(repo, "development") == "development"


def test_custom_origin_default_and_remote_only_integration(repo):
    git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert resolve_base(repo) == "trunk"
    git(repo, "update-ref", "refs/remotes/origin/development", "HEAD")
    assert resolve_base(repo) == "development"
    result = operations.branch_from_develop(repo, None, "topic")
    assert result["base_ref"] == "refs/remotes/origin/development"
    assert result["base_revision"] == git(repo, "rev-parse", "origin/development")
    assert result["freshness"] == "local-cache; no fetch performed"


def test_explicit_missing_base_does_not_fall_back(repo):
    with pytest.raises(OperatorError, match="not available locally"):
        operations.branch_from_develop(repo, None, "topic", base="development")
    assert git(repo, "branch", "--show-current") == "master"


def test_local_base_preserves_unpushed_history(repo):
    git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    git(repo, "commit", "--allow-empty", "-qm", "local base work")
    expected = git(repo, "rev-parse", "HEAD")
    result = operations.branch_from_develop(repo, None, "topic")
    assert result["base_ref"] == "refs/heads/master"
    assert result["base_revision"] == expected


def test_primary_configuration_and_ambiguous_primary(repo):
    git(repo, "branch", "main")
    with pytest.raises(OperatorError, match="unambiguous"):
        resolve_base(repo)
    git(repo, "config", "gitflow.branch.master", "master")
    assert resolve_base(repo) == "master"


@pytest.mark.parametrize("branch", ["-bad", "HEAD", "bad..name", "a b"])
def test_invalid_explicit_branches_are_rejected(repo, branch):
    with pytest.raises(OperatorError):
        resolve_base(repo, branch)


def test_dirty_tree_is_preserved_and_no_branch_created(repo):
    (repo / "uncommitted.txt").write_text("keep me\n")
    with pytest.raises(OperatorError, match="working tree has changes"):
        operations.branch_from_develop(repo, None, "topic")
    assert (repo / "uncommitted.txt").read_text() == "keep me\n"
    assert git(repo, "branch", "--show-current") == "master"
    assert "topic" not in git(repo, "branch", "--list")


@pytest.mark.parametrize("command", ["branch", "branch-from-develop"])
def test_cli_branch_aliases_use_real_base_without_item_or_docs(repo, command, capsys):
    git(repo, "branch", "-m", "development")
    assert cli.main(["--repo", str(repo), command, "--branch", "topic"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == command
    assert result["base_branch"] == "development"


@pytest.mark.parametrize("command", ["create-merge-request", "watch-ci", "squash-merge", "squash-merge-to-develop"])
def test_cli_dry_run_uses_requested_target(repo, command, capsys):
    git(repo, "checkout", "-qb", "topic")
    git(repo, "remote", "add", "origin", "git@github.com:org/repo.git")
    args = ["--repo", str(repo), command, "--target", "development", "--dry-run"]
    if command == "create-merge-request":
        args += ["--title", "Fix"]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["target"] == "development"
    assert result["operation"] == command
    assert result["status"] == "dry-run"


def test_stale_origin_head_has_explicit_remediation(repo):
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/deleted")
    with pytest.raises(OperatorError, match="missing cached branch"):
        resolve_base(repo)
