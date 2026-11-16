import io
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from gitflow_operator.core import MergeRequest, OperatorError, Remote
from gitflow_operator.providers import GitLabProvider, _NoRedirectHandler


class Response:
    def __init__(self, value):
        self.content = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.content


class GitLabTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def __call__(self, request, timeout, context):
        self.calls.append((request, timeout, context))
        route = (request.get_method(), urlsplit(request.full_url).path)
        if route in self.responses:
            response = self.responses[route]
            if isinstance(response, Exception):
                raise response
            return Response(response)
        if route[0] == "POST" and route[1].endswith("/merge_requests"):
            return Response({
                "iid": 12,
                "web_url": "https://scm1.example/group/repo/-/merge_requests/12",
                "title": "Feature",
                "source_branch": "feature/task",
                "target_branch": "develop",
                "sha": "abc123",
                "state": "opened",
                "draft": False,
            })
        if route[0] == "PUT":
            return Response({"iid": 12, "state": "merged", "merged_at": "2026-01-01T00:00:00Z"})
        return Response([])


def provider(transport, token="test-token"):
    return GitLabProvider(
        Remote("scm1", "scm1.ulysses.galp.internal", "group/sub/repo", "GITLAB_LGC_TOKEN"),
        "/tmp/repo",
        environ={"GITLAB_LGC_TOKEN": token},
        transport=transport,
    )


def mr():
    return MergeRequest("12", "https://scm1.example/mr/12", "Feature", "feature/task", "develop", "abc123", False, "opened")


def test_successful_gitlab_create_and_gated_squash_merge():
    transport = GitLabTransport()
    api = provider(transport)
    created = api.create_merge_request("feature/task", "Feature", "Body")
    assert created.number == "12"
    assert created.source_sha == "abc123"
    post_request = transport.calls[0][0]
    assert "/projects/group%2Fsub%2Frepo/merge_requests" in post_request.full_url
    assert "test-token" not in post_request.full_url
    assert post_request.get_header("Private-token") == "test-token"
    assert transport.calls[0][2].verify_mode != 0

    encoded_project = "group%2Fsub%2Frepo"
    transport.responses[("GET", f"/api/v4/projects/{encoded_project}/merge_requests/12/pipelines")] = [
        {"id": 8, "sha": "abc123", "status": "success"}
    ]
    transport.responses[("GET", f"/api/v4/projects/{encoded_project}/merge_requests/12/discussions")] = [
        {"id": "discussion-1", "notes": [{"resolvable": True, "resolved": True}]}
    ]
    transport.responses[("GET", f"/api/v4/projects/{encoded_project}/merge_requests/12/approvals")] = {
        "approvals_required": 2,
        "approvals_left": 0,
    }
    transport.responses[("GET", f"/api/v4/projects/{encoded_project}/protected_branches/develop")] = [
        {"merge_access_levels": [{"access_level": 40}], "push_access_levels": []}
    ][0]

    assert api.pipeline_status(mr())[0] == "passed"
    assert api.unresolved_discussions(mr()) == []
    assert api.approval_status(mr()) == (2, 2)
    assert api.protected_branch("develop")["protected"] is True
    api.squash_merge(mr())
    merge_request, _, _ = transport.calls[-1]
    assert merge_request.get_method() == "PUT"
    assert json.loads(merge_request.data)["sha"] == "abc123"
    assert json.loads(merge_request.data)["squash"] is True
    assert json.loads(merge_request.data)["merge_when_pipeline_succeeds"] is False


def test_gitlab_pipeline_must_match_current_source_head():
    transport = GitLabTransport()
    api = provider(transport)
    path = "/api/v4/projects/group%2Fsub%2Frepo/merge_requests/12/pipelines"
    transport.responses[("GET", path)] = [{"id": 4, "sha": "stale", "status": "success"}]
    assert api.pipeline_status(mr())[0] == "incomplete"

    transport.responses[("GET", path)] = [{"id": 5, "sha": "abc123", "status": "running"}]
    assert api.pipeline_status(mr())[0] == "pending"

    transport.responses[("GET", path)] = [{"id": 6, "sha": "abc123", "status": "failed"}]
    assert api.pipeline_status(mr())[0] == "failed"


def test_gitlab_unresolved_discussions_and_required_approvals_are_visible():
    transport = GitLabTransport()
    api = provider(transport)
    encoded_project = "group%2Fsub%2Frepo"
    transport.responses[("GET", f"/api/v4/projects/{encoded_project}/merge_requests/12/discussions")] = [
        {"id": "todo", "notes": [{"resolvable": True, "resolved": False}]}
    ]
    transport.responses[("GET", f"/api/v4/projects/{encoded_project}/merge_requests/12/approvals")] = {
        "approvals_required": 3,
        "approvals_left": 2,
    }
    assert api.unresolved_discussions(mr()) == ["todo"]
    assert api.approval_status(mr()) == (3, 1)


def test_gitlab_api_authorization_error_is_explicit_and_token_redacted():
    transport = GitLabTransport()
    api = provider(transport, token="never-print-this")
    path = "/api/v4/projects/group%2Fsub%2Frepo/merge_requests"
    transport.responses[("GET", path)] = HTTPError(
        "https://scm1.example/api",
        403,
        "Forbidden",
        {},
        io.BytesIO(b"never-print-this"),
    )

    with pytest.raises(OperatorError, match="HTTP 403") as error:
        api.find_merge_request("feature/task", "develop")
    assert "never-print-this" not in str(error.value)


def test_gitlab_missing_credential_is_explicit():
    with pytest.raises(OperatorError, match="GITLAB_LGC_TOKEN"):
        GitLabProvider(
            Remote("scm1", "scm1.ulysses.galp.internal", "group/repo", "GITLAB_LGC_TOKEN"),
            "/tmp/repo",
            environ={},
            transport=GitLabTransport(),
        )


def test_gitlab_duplicate_merge_requests_are_ambiguous():
    transport = GitLabTransport()
    api = provider(transport)
    path = "/api/v4/projects/group%2Fsub%2Frepo/merge_requests"
    transport.responses[("GET", path)] = [
        {
            "iid": index,
            "web_url": f"https://scm1.example/mr/{index}",
            "source_branch": "feature/task",
            "target_branch": "develop",
            "sha": "abc123",
            "state": "opened",
        }
        for index in (12, 13)
    ]

    with pytest.raises(OperatorError, match="ambiguous merge request"):
        api.find_merge_request("feature/task", "develop")


def test_gitlab_redirects_are_not_followed():
    handler = _NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/") is None
