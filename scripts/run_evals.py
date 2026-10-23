"""Run the eval suite against the configured model (SPEC-0011).

    make evals                                   every case, EVAL_REPEATS times
    make evals ARGS="--tag confirmation --repeats 1"   a partial run

Writes one run file to evals/runs/. Needs Redis (`make redis`).
"""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
CASES = Path("evals") / "cases"
RUNS = Path("evals") / "runs"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tag", action="append", default=[], help="run cases with this tag"
    )
    parser.add_argument("--case", action="append", default=[], help="run this case id")
    parser.add_argument(
        "--repeats", type=int, help="repeats per case (default EVAL_REPEATS)"
    )
    parser.add_argument("--cases", type=Path, default=CASES, help="dataset directory")
    parser.add_argument("--runs", type=Path, default=RUNS, help="run file directory")
    return parser.parse_args(argv)


def git_state() -> tuple[str, bool]:
    """The commit evaluated, and whether tracked files had changes."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
        return done.stdout.strip()

    return git("rev-parse", "HEAD") or "unknown", bool(
        git("status", "--porcelain", "--untracked-files=no")
    )


async def run(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from redis.asyncio import Redis

    from agent.llm import build_chat_model
    from agent.persistence import REDIS_ERRORS, RedisThreadGuard, open_checkpointer
    from evals.dataset import EvalDatabaseError, load_cases, select
    from evals.records import write_run
    from evals.runner import CaseRunner, run_cases
    from logs import configure_logging
    from settings import get_settings

    # B-0001: an invalid dataset stops the run before any model call.
    try:
        cases = load_cases(ROOT / args.cases)
    except EvalDatabaseError as exc:
        print("invalid eval dataset:", *exc.problems, sep="\n  ", file=sys.stderr)
        return 2
    chosen = select(cases, args.tag, args.case)
    if not chosen:
        print("no eval cases match", file=sys.stderr)
        return 2

    settings = get_settings()
    configure_logging(settings.model_copy(update={"log_level": "warning"}))
    repeats = args.repeats or settings.eval_repeats
    partial = bool(args.tag or args.case) or repeats != settings.eval_repeats

    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
    except REDIS_ERRORS:
        print(
            f"Redis is unreachable at {settings.redis_url}; run `make redis`",
            file=sys.stderr,
        )
        await redis.aclose()
        return 2

    model = f"{settings.llm_provider} {settings.llm_model}"
    print(f"{len(chosen)} cases x {repeats} on {model}")
    try:
        with TemporaryDirectory() as notes:
            async with open_checkpointer(settings) as saver:
                runner = CaseRunner(
                    build_chat_model(settings),
                    settings,
                    saver,
                    RedisThreadGuard(redis, settings),
                    Path(notes),
                )
                record = await run_cases(
                    runner,
                    chosen,
                    repeats,
                    partial=partial,
                    git=git_state(),
                    progress=print,
                )
    finally:
        await redis.aclose()

    path = write_run(record, ROOT / args.runs)
    total = record.totals
    print(f"score {total.score:.3f}" + (" (partial)" if partial else ""))
    if total.must_pass_failures:
        print("must-pass failures:", ", ".join(total.must_pass_failures))
    print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run(parse_args(sys.argv[1:]))))


if __name__ == "__main__":
    main()
