from __future__ import annotations

import json
import os
import ssl
import subprocess
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (
    HTTPSHandler,
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)

from .core import MergeRequest, OperatorError, Remote, redact


class Provider:
    def create_merge_request(self, branch: str, title: str, body: str) -> MergeRequest:
        raise NotImplementedError

    def find_merge_request(self, branch: str, target: str) -> MergeRequest:
        raise NotImplementedError

    def pipeline_status(self, mr: MergeRequest) -> tuple[str, list[dict[str, Any]]]:
        raise NotImplementedError

    def wait_for_pipeline(self, mr: MergeRequest) -> None:
        return

    def unresolved_discussions(self, mr: MergeRequest) -> list[str]:
        raise NotImplementedError

    def approval_status(self, mr: MergeRequest) -> tuple[int, int]:
        raise NotImplementedError

    def protected_branch(self, target: str) -> dict[str, Any]:
        raise NotImplementedError

    def squash_merge(self, mr: MergeRequest) -> None:
        raise NotImplementedError


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _gh(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperatorError("GitHub CLI executable 'gh' is not installed") from exc
    if result.returncode:
        detail = str(redact(result.stderr.strip()))
        if len(detail) > 300:
            detail = detail[:300]
        reason = detail or f"exit status {result.returncode}"
        raise OperatorError(f"GitHub CLI request failed: {reason}")
    return result.stdout


def _gh_json(args: list[str], cwd: str) -> Any:
    try:
        output = _gh(args, cwd)
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise OperatorError("GitHub CLI returned invalid JSON") from exc


class GitHubProvider(Provider):
    def __init__(self, remote: Remote, repo: str):
        self.remote = remote
        self.repo = repo
        self.owner, self.name = remote.project.split("/")

    def create_merge_request(self, branch: str, title: str, body: str) -> MergeRequest:
        output = _gh(
            ["pr", "create", "--base", "develop", "--head", branch, "--title", title, "--body", body],
            self.repo,
        )
        url = next((line.strip() for line in output.splitlines() if line.strip().startswith("https://")), "")
        if not url:
            raise OperatorError("GitHub CLI did not return a merge-request URL")
        # Fetch the created request using the same unambiguous source/target rules.
        return self.find_merge_request(branch, "develop")

    def find_merge_request(self, branch: str, target: str) -> MergeRequest:
        rows = _gh_json(
            ["pr", "list", "--state", "open", "--limit", "100", "--json",
             "number,url,title,baseRefName,headRefName,headRefOid,isDraft,state,reviewDecision"],
            self.repo,
        )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise OperatorError("GitHub merge-request response is not a list")
        if len(rows) == 100:
            raise OperatorError("GitHub merge-request results exceed the safe selection limit")
        matches = [
            row for row in rows
            if row.get("headRefName") == branch and row.get("baseRefName") == target
        ]
        if not matches:
            raise OperatorError(f"no open merge request found for {branch} -> {target}")
        if len(matches) != 1:
            raise OperatorError(f"ambiguous merge request selection for {branch} -> {target}")
        row = matches[0]
        number = str(row.get("number", ""))
        url = str(row.get("url", ""))
        if not number.isdigit() or not url:
            raise OperatorError("GitHub returned incomplete merge-request identity")
        return MergeRequest(
            number,
            url,
            str(row.get("title", "")),
            str(row["headRefName"]),
            str(row["baseRefName"]),
            str(row.get("headRefOid", "")),
            bool(row.get("isDraft")),
            str(row.get("state", "OPEN")).lower(),
            str(row.get("reviewDecision", "")),
        )

    def pipeline_status(self, mr: MergeRequest) -> tuple[str, list[dict[str, Any]]]:
        check_data = self._gh_api(
            f"repos/{self.remote.project}/commits/{mr.source_sha}/check-runs?per_page=100"
        )
        status_data = self._gh_api(
            f"repos/{self.remote.project}/commits/{mr.source_sha}/status"
        )
        rows = check_data.get("check_runs") if isinstance(check_data, dict) else None
        contexts = status_data.get("statuses") if isinstance(status_data, dict) else None
        if (
            not isinstance(rows, list)
            or not all(isinstance(row, dict) for row in rows)
            or not isinstance(contexts, list)
            or not all(isinstance(row, dict) for row in contexts)
        ):
            raise OperatorError("GitHub CI state is unavailable")
        if len(rows) == 100 or len(contexts) == 100:
            raise OperatorError("GitHub CI results exceed the safe inspection limit")
        checks = [
            {
                "name": str(row.get("name", "")),
                "status": str(row.get("conclusion") or row.get("status") or "").lower(),
                "url": str(row.get("html_url", "")),
            }
            for row in rows
        ]
        checks.extend(
            {
                "name": str(row.get("context", "")),
                "status": str(row.get("state", "")).lower(),
                "url": str(row.get("target_url", "")),
            }
            for row in contexts
        )
        if not checks:
            return "incomplete", checks
        if any(check["status"] in {"queued", "in_progress", "pending", "requested", "waiting"} for check in checks):
            return "pending", checks
        if any(check["status"] != "success" for check in checks):
            return "failed", checks
        return "passed", checks

    def wait_for_pipeline(self, mr: MergeRequest) -> None:
        result = subprocess.run(
            ["gh", "pr", "checks", mr.number, "--watch"],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode and not result.stdout and not result.stderr:
            raise OperatorError("GitHub CI watcher failed without a diagnostic")

    def unresolved_discussions(self, mr: MergeRequest) -> list[str]:
        query = (
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}pageInfo{hasNextPage}}}}}"
        )
        result = _gh_json(
            ["api", "graphql", "-f", f"query={query}", "-F", f"owner={self.owner}",
             "-F", f"name={self.name}", "-F", f"number={mr.number}"],
            self.repo,
        )
        threads = result.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads")
        if (
            not isinstance(threads, dict)
            or not isinstance(threads.get("nodes"), list)
            or not all(isinstance(row, dict) for row in threads["nodes"])
            or not isinstance(threads.get("pageInfo"), dict)
        ):
            raise OperatorError("GitHub discussion state is unavailable")
        if threads.get("pageInfo", {}).get("hasNextPage"):
            raise OperatorError("GitHub discussion results exceed the safe inspection limit")
        return [f"thread-{index + 1}" for index, row in enumerate(threads["nodes"]) if not row.get("isResolved", False)]

    def approval_status(self, mr: MergeRequest) -> tuple[int, int]:
        branch = self._gh_api(f"repos/{self.remote.project}/branches/{quote(mr.target_branch, safe='')}")
        if not isinstance(branch, dict):
            raise OperatorError("GitHub branch protection state is unavailable")
        required = 0
        if branch.get("protected"):
            protection = self._gh_api(f"repos/{self.remote.project}/branches/{quote(mr.target_branch, safe='')}/protection")
            if not isinstance(protection, dict):
                raise OperatorError("GitHub branch protection state is unavailable")
            reviews_policy = protection.get("required_pull_request_reviews") or {}
            try:
                required = int(reviews_policy.get("required_approving_review_count", 0))
            except (AttributeError, TypeError, ValueError) as exc:
                raise OperatorError("GitHub required-approval policy is invalid") from exc
        reviews = self._gh_api(f"repos/{self.remote.project}/pulls/{mr.number}/reviews")
        if not isinstance(reviews, list) or not all(isinstance(row, dict) for row in reviews):
            raise OperatorError("GitHub approval state is unavailable")
        if len(reviews) == 100:
            raise OperatorError("GitHub review history exceeds the safe inspection limit")
        latest: dict[str, dict[str, Any]] = {}
        for review in reviews:
            reviewer = review.get("user")
            if not isinstance(reviewer, dict):
                raise OperatorError("GitHub approval state is incomplete")
            user = reviewer.get("login")
            if user:
                latest[user] = review
        approved = sum(
            1 for review in latest.values()
            if review.get("state") == "APPROVED" and review.get("commit_id") == mr.source_sha
        )
        if mr.review_decision == "REVIEW_REQUIRED":
            required = max(required, 1)
        return required, approved

    def protected_branch(self, target: str) -> dict[str, Any]:
        branch = self._gh_api(f"repos/{self.remote.project}/branches/{quote(target, safe='')}")
        if not isinstance(branch, dict):
            raise OperatorError("GitHub branch protection state is unavailable")
        protected = bool(branch.get("protected"))
        detail: dict[str, Any] = {"protected": protected}
        if protected:
            protection = self._gh_api(f"repos/{self.remote.project}/branches/{quote(target, safe='')}/protection")
            if not isinstance(protection, dict):
                raise OperatorError("GitHub branch protection state is unavailable")
            detail["required_status_checks"] = protection.get("required_status_checks")
            detail["required_pull_request_reviews"] = protection.get("required_pull_request_reviews")
            detail["restrictions"] = protection.get("restrictions")
        return detail

    def squash_merge(self, mr: MergeRequest) -> None:
        _gh(["pr", "merge", mr.number, "--squash"], self.repo)
        row = _gh_json(
            ["pr", "view", mr.number, "--json", "state,mergedAt"],
            self.repo,
        )
        if not isinstance(row, dict):
            raise OperatorError("GitHub merge state is unavailable")
        if str(row.get("state", "")).lower() != "merged" or not row.get("mergedAt"):
            raise OperatorError("GitHub did not confirm that the merge request was merged")

    def _gh_api(self, path: str) -> Any:
        return _gh_json(["api", path], self.repo)


class GitLabProvider(Provider):
    def __init__(
        self,
        remote: Remote,
        repo: str,
        environ: dict[str, str] | None = None,
        transport: Callable[..., Any] = urlopen,
    ):
        self.remote = remote
        self.repo = repo
        self.environ = os.environ if environ is None else environ
        self.transport = transport
        self.project_id = quote(remote.project, safe="")
        token_name = remote.token_env or "GITLAB_TOKEN"
        token = self.environ.get(token_name)
        if not token:
            raise OperatorError(f"missing credential environment variable {token_name}")
        self.token_name = token_name
        self.token = token
        self.base_url = f"https://{remote.host}/api/v4"

    def create_merge_request(self, branch: str, title: str, body: str) -> MergeRequest:
        row = self._request(
            "POST",
            f"/projects/{self.project_id}/merge_requests",
            {"source_branch": branch, "target_branch": "develop", "title": title, "description": body},
        )
        if not isinstance(row, dict):
            raise OperatorError("GitLab returned an invalid merge-request response")
        return self._normalize(row)

    def find_merge_request(self, branch: str, target: str) -> MergeRequest:
        rows = self._all_pages(
            f"/projects/{self.project_id}/merge_requests",
            f"state=opened&source_branch={quote(branch)}&target_branch={quote(target)}&per_page=100",
        )
        if not all(isinstance(row, dict) for row in rows):
            raise OperatorError("GitLab API returned an invalid merge-request collection")
        matches = [
            row for row in rows
            if row.get("source_branch") == branch and row.get("target_branch") == target and row.get("state") == "opened"
        ]
        if not matches:
            raise OperatorError(f"no open merge request found for {branch} -> {target}")
        if len(matches) != 1:
            raise OperatorError(f"ambiguous merge request selection for {branch} -> {target}")
        return self._normalize(matches[0])

    def pipeline_status(self, mr: MergeRequest) -> tuple[str, list[dict[str, Any]]]:
        pipelines = self._all_pages(
            f"/projects/{self.project_id}/merge_requests/{mr.number}/pipelines",
            "per_page=100&order_by=id&sort=desc",
        )
        relevant = [row for row in pipelines if row.get("sha") == mr.source_sha]
        if not relevant:
            return "incomplete", []
        try:
            latest = max(relevant, key=lambda row: int(row.get("id", 0)))
        except (TypeError, ValueError) as exc:
            raise OperatorError("GitLab pipeline identity is invalid") from exc
        status = str(latest.get("status", "unknown")).lower()
        checks = [{"id": str(latest.get("id", "")), "status": status, "sha": mr.source_sha}]
        if status == "success":
            return "passed", checks
        if status in {"pending", "running", "created", "preparing", "waiting_for_resource", "scheduled"}:
            return "pending", checks
        return "failed", checks

    def unresolved_discussions(self, mr: MergeRequest) -> list[str]:
        rows = self._all_pages(
            f"/projects/{self.project_id}/merge_requests/{mr.number}/discussions",
            "per_page=100",
        )
        if not all(isinstance(row, dict) for row in rows):
            raise OperatorError("GitLab API returned an invalid discussion collection")
        unresolved = []
        for index, discussion in enumerate(rows):
            notes = discussion.get("notes")
            if not isinstance(notes, list) or not all(isinstance(note, dict) for note in notes):
                raise OperatorError("GitLab discussion state is incomplete")
            for note in notes:
                if note.get("resolvable") and not note.get("resolved"):
                    unresolved.append(str(discussion.get("id", f"discussion-{index + 1}")))
                    break
        return unresolved

    def approval_status(self, mr: MergeRequest) -> tuple[int, int]:
        approval = self._request(
            "GET",
            f"/projects/{self.project_id}/merge_requests/{mr.number}/approvals",
        )
        if not isinstance(approval, dict):
            raise OperatorError("GitLab approval state is unavailable")
        if "approvals_required" not in approval or "approvals_left" not in approval:
            raise OperatorError("GitLab approval state is unavailable")
        try:
            required = int(approval["approvals_required"])
            left = int(approval["approvals_left"])
        except (TypeError, ValueError) as exc:
            raise OperatorError("GitLab approval state is invalid") from exc
        return required, max(0, required - left)

    def protected_branch(self, target: str) -> dict[str, Any]:
        try:
            row = self._request(
                "GET",
                f"/projects/{self.project_id}/protected_branches/{quote(target, safe='')}",
            )
        except OperatorError as exc:
            if str(exc) == "GitLab API returned HTTP 404":
                return {"protected": False}
            raise
        if not isinstance(row, dict):
            raise OperatorError("GitLab protected-branch state is unavailable")
        return {
            "protected": True,
            "allow_to_merge": row.get("merge_access_levels", []),
            "allow_to_push": row.get("push_access_levels", []),
        }

    def squash_merge(self, mr: MergeRequest) -> None:
        row = self._request(
            "PUT",
            f"/projects/{self.project_id}/merge_requests/{mr.number}/merge",
            {
                "sha": mr.source_sha,
                "squash": True,
                "merge_when_pipeline_succeeds": False,
            },
        )
        if not isinstance(row, dict):
            raise OperatorError("GitLab merge response is invalid")
        if str(row.get("state", "")).lower() != "merged" and not row.get("merged_at"):
            raise OperatorError("GitLab did not confirm that the merge request was merged")

    def _normalize(self, row: dict[str, Any]) -> MergeRequest:
        diff_refs = row.get("diff_refs") or {}
        if not isinstance(diff_refs, dict):
            raise OperatorError("GitLab returned invalid merge-request revisions")
        mr = MergeRequest(
            str(row.get("iid", row.get("number", ""))),
            str(row.get("web_url", row.get("url", ""))),
            str(row.get("title", "")),
            str(row.get("source_branch", "")),
            str(row.get("target_branch", "")),
            str(row.get("sha", diff_refs.get("head_sha", ""))),
            bool(row.get("draft", row.get("work_in_progress", False))),
            str(row.get("state", "")),
            str(row.get("review_decision", "")),
        )
        if (
            not mr.number.isdigit()
            or not mr.url
            or not mr.source_branch
            or not mr.target_branch
        ):
            raise OperatorError("GitLab returned incomplete merge-request identity")
        return mr

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: str = "",
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            if self.transport is urlopen:
                opener = build_opener(
                    _NoRedirectHandler(),
                    HTTPSHandler(context=ssl.create_default_context()),
                )
                response_context = opener.open(request, timeout=30)
            else:
                response_context = self.transport(request, timeout=30, context=ssl.create_default_context())
            with response_context as response:
                content = response.read()
                if not content:
                    return {}
                return json.loads(content.decode("utf-8"))
        except HTTPError as exc:
            raise OperatorError(f"GitLab API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise OperatorError("GitLab API connection failed") from exc
        except TimeoutError as exc:
            raise OperatorError("GitLab API request timed out") from exc
        except OSError as exc:
            raise OperatorError("GitLab API request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperatorError("GitLab API returned invalid JSON") from exc

    def _all_pages(self, path: str, query: str, page_limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, page_limit + 1):
            page_rows = self._request("GET", path, query=f"{query}&page={page}")
            if not isinstance(page_rows, list) or not all(isinstance(row, dict) for row in page_rows):
                raise OperatorError("GitLab API returned an unexpected collection")
            rows.extend(page_rows)
            if len(page_rows) < 100:
                return rows
        raise OperatorError("GitLab API pagination exceeded the safe inspection limit")


class SCM1Provider(GitLabProvider):
    """GitLab v4 adapter for SCM1 and its dedicated credential variable."""


class LegacyGitLabProvider(GitLabProvider):
    """GitLab v4 adapter for explicitly allowlisted self-managed hosts."""


def create_provider(remote: Remote, repo: str, environ: dict[str, str] | None = None) -> Provider:
    if remote.provider == "github":
        return GitHubProvider(remote, repo)
    if remote.provider == "scm1":
        return SCM1Provider(remote, repo, environ=environ)
    if remote.provider == "gitlab":
        return LegacyGitLabProvider(remote, repo, environ=environ)
    raise OperatorError(f"unsupported provider: {remote.provider}")
