import json
import subprocess

import pytest

from gitflow_operator import operations
from gitflow_operator.core import MergeRequest, OperatorError, Remote


class FakeProvider:
    def __init__(self, mr, pipeline="passed", unresolved=None, approvals=(1, 1)):
        self.mr = mr
        self.pipeline = pipeline
        self.unresolved = unresolved or []
        self.approvals = approvals
        self.merged = False

    def find_merge_request(self, branch, target):
        self.requested_target = target
        return self.mr

    def wait_for_pipeline(self, mr):
        return

    def pipeline_status(self, mr):
        return self.pipeline, [{"name": "unit", "status": self.pipeline}]

    def unresolved_discussions(self, mr):
        return self.unresolved

    def approval_status(self, mr):
        return self.approvals

    def protected_branch(self, target):
        return {"protected": True, "allow_to_merge": [{"access_level": 40}]}

    def squash_merge(self, mr):
        self.merged = True


def init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(path), "branch", "develop"], check=True)
    subprocess.run(["git", "-C", str(path), "checkout", "-qb", "feature/task"], check=True)


def matching_mr(repo, **changes):
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    values = {
        "number": "12",
        "url": "https://scm1.example/group/repo/-/merge_requests/12",
        "title": "Feature",
        "source_branch": "feature/task",
        "target_branch": "develop",
        "source_sha": commit,
        "draft": False,
        "state": "opened",
    }
    values.update(changes)
    return MergeRequest(**values)


def use_provider(monkeypatch, provider):
    remote = Remote("scm1", "scm1.ulysses.galp.internal", "group/repo", "GITLAB_LGC_TOKEN")
    monkeypatch.setattr(operations, "detect_remote", lambda repo, environ=None: remote)
    monkeypatch.setattr(operations, "create_provider", lambda remote, repo, environ=None: provider)


def test_squash_merge_success_records_gates_and_outcome(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(matching_mr(repo))
    use_provider(monkeypatch, provider)

    result = operations.squash_merge(repo, "TH9.E1", False, {})

    assert provider.merged is True
    assert result["provider"] == "scm1"
    assert result["project"] == "group/repo"
    assert result["target"] == "develop"
    assert result["source_revision"] == result["commit"]
    assert result["merge_request"]["id"] == "12"
    assert result["gates"]["pipeline"]["status"] == "passed"
    assert result["gates"]["approvals"] == {"required": 1, "approved": 1}
    assert result["gates"]["protected_branch"]["protected"] is True


@pytest.mark.parametrize(
    ("mr_changes", "pipeline", "approvals", "expected"),
    [
        ({"source_sha": "stale"}, "passed", (1, 1), "stale merge request head"),
        ({"draft": True}, "passed", (1, 1), "draft merge requests"),
        ({}, "failed", (1, 1), "CI is failed"),
        ({}, "pending", (1, 1), "CI is pending"),
        ({}, "incomplete", (1, 1), "CI is incomplete"),
        ({}, "passed", (2, 1), "required approvals"),
    ],
)
def test_squash_merge_refuses_unsafe_state(
    tmp_path, monkeypatch, mr_changes, pipeline, approvals, expected
):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(
        matching_mr(repo, **mr_changes),
        pipeline=pipeline,
        approvals=approvals,
    )
    use_provider(monkeypatch, provider)

    with pytest.raises(OperatorError, match=expected):
        operations.squash_merge(repo, "TH9.E1", False, {})
    assert provider.merged is False


def test_squash_merge_refuses_unresolved_discussions(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(matching_mr(repo), unresolved=["thread-1"])
    use_provider(monkeypatch, provider)

    with pytest.raises(OperatorError, match="unresolved discussion"):
        operations.squash_merge(repo, None, False, {})
    assert provider.merged is False


def test_watch_ci_preserves_json_evidence_and_fails_nonpassing_ci(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(matching_mr(repo), pipeline="pending")
    use_provider(monkeypatch, provider)

    result = operations.watch_ci(repo, "TH9.E1", False, {})

    assert result["operation"] == "watch-ci"
    assert result["status"] == "pending"
    assert result["provider"] == "scm1"
    assert result["merge_request"]["id"] == "12"
    assert result["pipeline"]["status"] == "pending"


def test_create_merge_request_targets_develop_and_records_revision(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(matching_mr(repo))
    use_provider(monkeypatch, provider)
    provider.create_merge_request = lambda branch, title, body, target: provider.mr

    result = operations.create_merge_request(repo, "TH9.E1", "Feature", "Body", False, {})

    assert result["status"] == "created"
    assert result["target"] == "develop"
    assert result["source_revision"] == result["commit"]
    assert result["merge_request"]["id"] == "12"


def test_existing_local_operations_keep_evidence_shape(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    status = operations.status(repo, "TH9")
    notes = operations.release_notes(repo, "TH9", "Ship", "pytest")

    assert {"timestamp", "repo", "branch", "commit", "dirty_files", "item_id"} <= status.keys()
    assert status["operation"] == "status"
    assert notes["operation"] == "prepare-release-notes"
    assert "pytest" in notes["release_notes"]


def test_legacy_branch_command_uses_master_when_develop_is_absent(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "-D", "develop"], check=True)

    result = operations.branch_from_develop(repo, "TH9.E1", "feature/new")

    assert result["status"] == "created"
    assert result["base_branch"] == "master"
    assert result["branch"] == "feature/new"


def test_branch_from_develop_creates_requested_feature_branch(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    result = operations.branch_from_develop(repo, "TH9.E1", "feature/new")

    assert result["status"] == "created"
    assert result["branch"] == "feature/new"
    assert result["target_branch"] == "feature/new"


@pytest.mark.parametrize("target", ["development", "master", "integration"])
@pytest.mark.parametrize("operation", ["create", "watch", "merge"])
def test_resolved_target_flows_through_provider_and_evidence(tmp_path, monkeypatch, target, operation):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(matching_mr(repo, target_branch=target))
    use_provider(monkeypatch, provider)
    subprocess.run(["git", "-C", str(repo), "config", "gitflow.operator.base", target], check=True)

    def create(branch, title, body, target):
        provider.requested_target = target
        return provider.mr

    provider.create_merge_request = create
    if operation == "create":
        result = operations.create_merge_request(repo, None, "Feature", "Body", False, {})
    elif operation == "watch":
        result = operations.watch_ci(repo, None, False, {})
    else:
        result = operations.squash_merge(repo, None, False, {})

    assert provider.requested_target == target
    assert result["target"] == target
    assert result["source_revision"] == result["commit"]
    assert result["status"] in {"created", "passed", "merged"}


def test_wrong_target_is_still_refused_after_branch_resolution(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    provider = FakeProvider(matching_mr(repo))
    use_provider(monkeypatch, provider)
    with pytest.raises(OperatorError, match="source or target"):
        operations.squash_merge(repo, None, False, {}, target="development")
    assert not provider.merged


def test_nonstandard_topic_name_is_allowed_but_target_itself_is_not(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "-m", "bugfix/task"], check=True)
    provider = FakeProvider(matching_mr(repo, source_branch="bugfix/task"))
    use_provider(monkeypatch, provider)
    result = operations.watch_ci(repo, None, False, {})
    assert result["status"] == "passed"
    with pytest.raises(OperatorError, match="source and target"):
        operations.watch_ci(repo, None, False, {}, target="bugfix/task")
