import json
import subprocess

from gitflow_operator import cli, operations
from gitflow_operator.core import OperatorError, Remote


def init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "git@scm1.ulysses.galp.internal:group/repo.git"],
        check=True,
    )


def test_blocked_api_operation_returns_nonzero_json_and_redacts_credentials(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    init_repo(repo)
    secret = "secret-token-value"
    monkeypatch.setenv("GITLAB_LGC_TOKEN", secret)
    monkeypatch.setattr(cli, "detect_remote", lambda repo: Remote(
        "scm1", "scm1.ulysses.galp.internal", "group/repo", "GITLAB_LGC_TOKEN"
    ))
    monkeypatch.setattr(
        operations,
        "create_merge_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(OperatorError(f"HTTP 403 {secret}")),
    )

    exit_code = cli.main([
        "--repo", str(repo), "create-merge-request", "--title", "Feature",
    ])

    output = capsys.readouterr().out
    evidence = json.loads(output)
    assert exit_code == 1
    assert evidence["status"] == "blocked"
    assert evidence["provider"] == "scm1"
    assert evidence["project"] == "group/repo"
    assert "HTTP 403" in evidence["reason"]
    assert secret not in output
    assert "[REDACTED]" in output


def test_unsupported_remote_emits_explicit_blocked_evidence(tmp_path, capsys):
    repo = tmp_path / "repo"
    init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "https://unknown.example/org/repo.git"],
        check=True,
    )

    exit_code = cli.main(["--repo", str(repo), "create-merge-request", "--title", "Feature"])

    evidence = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert evidence["status"] == "blocked"
    assert "unsupported SCM host" in evidence["reason"]


def test_pending_ci_returns_nonzero_with_json_evidence(monkeypatch, capsys, tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.setattr(operations, "watch_ci", lambda *args, **kwargs: {
        "operation": "watch-ci",
        "status": "pending",
        "provider": "scm1",
        "project": "group/repo",
        "outcome": "pending",
    })

    exit_code = cli.main(["--repo", str(repo), "watch-ci"])

    evidence = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert evidence["status"] == "pending"
