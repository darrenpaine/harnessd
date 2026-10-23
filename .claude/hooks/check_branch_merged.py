"""PreToolUse hook: before Claude deletes a git branch, check its PR is merged.

Deleting a PR's head branch closes the PR on GitHub, so a branch is only
deleted without asking once `gh` reports a merged PR for it. Otherwise the
call goes to the user to approve. Covers `git branch -d/-D/--delete` and
`git push <remote> --delete <branch>` / `git push <remote> :<branch>`.
"""

import json
import re
import shlex
import subprocess
import sys

SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree"}


def deleted_branches(command: str) -> list[str]:
    branches: list[str] = []
    for part in SEPARATORS.split(command):
        try:
            words = shlex.split(part)
        except ValueError:
            continue
        if "git" not in words:
            continue
        words = words[words.index("git") + 1 :]
        while words and words[0].startswith("-"):
            skip = 2 if words[0] in GIT_OPTIONS_WITH_VALUE else 1
            words = words[skip:]
        if not words:
            continue
        subcommand, args = words[0], words[1:]
        names = [a for a in args if not a.startswith("-")]
        if subcommand == "branch" and {"-d", "-D", "--delete"} & set(args):
            branches += names
        elif subcommand == "push":
            refs = names[1:]  # names[0] is the remote
            if {"-d", "--delete"} & set(args):
                branches += refs
            else:
                branches += [r[1:] for r in refs if r.startswith(":")]
    return branches


def merged(branch: str) -> bool | None:
    """True if gh lists a merged PR from this branch; None if gh failed."""
    command = ["gh", "pr", "list", "--head", branch, "--state", "merged"]
    try:
        result = subprocess.run(
            [*command, "--json", "number", "--jq", "length"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() not in ("", "0")


def main() -> None:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    problems: list[str] = []
    for branch in dict.fromkeys(deleted_branches(command)):
        status = merged(branch)
        if status is None:
            problems.append(f"{branch}: could not check its PR with gh")
        elif not status:
            problems.append(f"{branch}: no merged PR (deleting it closes an open PR)")
    if problems:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "Branch delete before merge: "
                        + "; ".join(problems),
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
