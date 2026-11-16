from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core import OperatorError, detect_remote, evidence, redact, remote_evidence
from . import operations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provider-neutral Gitflow operator")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--item-id")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    branch = sub.add_parser("branch-from-develop")
    branch.add_argument("--branch", required=True)

    commit = sub.add_parser("commit")
    commit.add_argument("--message", required=True)
    commit.add_argument("paths", nargs="*", default=["."])

    create = sub.add_parser("create-merge-request")
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--dry-run", action="store_true")

    watch = sub.add_parser("watch-ci")
    watch.add_argument("--dry-run", action="store_true")

    notes = sub.add_parser("prepare-release-notes")
    notes.add_argument("--summary", required=True)
    notes.add_argument("--tests")

    merge = sub.add_parser("squash-merge-to-develop")
    merge.add_argument("--dry-run", action="store_true")
    return parser


def _emit(data: dict[str, Any]) -> int:
    print(json.dumps(redact(data), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    try:
        if args.command == "status":
            result = operations.status(repo, args.item_id)
        elif args.command == "branch-from-develop":
            result = operations.branch_from_develop(repo, args.item_id, args.branch)
        elif args.command == "commit":
            result = operations.commit(repo, args.item_id, args.message, args.paths)
        elif args.command == "create-merge-request":
            result = operations.create_merge_request(repo, args.item_id, args.title, args.body, args.dry_run)
        elif args.command == "watch-ci":
            result = operations.watch_ci(repo, args.item_id, args.dry_run)
        elif args.command == "prepare-release-notes":
            result = operations.release_notes(repo, args.item_id, args.summary, args.tests)
        else:
            result = operations.squash_merge(repo, args.item_id, args.dry_run)
        if result.get("status") in {"blocked", "failed", "pending", "incomplete"}:
            return _emit(result) or 1
        return _emit(result)
    except OperatorError as exc:
        operation = args.command
        extra: dict[str, Any] = dict(exc.details)
        try:
            remote = detect_remote(repo)
            extra.update(remote_evidence(remote, "develop", current_commit_for_error(repo)))
        except OperatorError:
            pass
        try:
            result = evidence(
                repo,
                args.item_id,
                operation=operation,
                status="blocked",
                reason=str(exc),
                outcome="blocked",
                **extra,
            )
        except OperatorError:
            result = {
                "repo": str(repo),
                "item_id": args.item_id,
                "operation": operation,
                "status": "blocked",
                "reason": str(exc),
                "outcome": "blocked",
            }
        return _emit(result) or exc.exit_code


def current_commit_for_error(repo: Path) -> str | None:
    try:
        from .core import current_commit

        return current_commit(repo)
    except OperatorError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
