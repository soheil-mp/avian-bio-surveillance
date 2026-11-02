#!/usr/bin/env python3
"""Export Docker Compose logs with a recent-window default and issue summary."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_OUTPUT = Path("docker_logs.txt")
DEFAULT_SINCE = "12h"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
ERROR_KEYWORDS = re.compile(
    r"\b(error|warn|warning|exception|traceback|critical|fail|unavailable|timeout|cancelled)\b",
    re.IGNORECASE,
)
TIMESTAMP_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[^-]+-\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export Docker Compose logs and summarize repeated issues. "
            "Defaults to the last 12 hours to avoid mixing stale incidents with "
            "current runtime faults."
        )
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=(
            "Docker Compose log window, passed to `docker compose logs --since`. "
            "Use 'all' to disable time filtering. Default: 12h."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output file path. Default: docker_logs.txt.",
    )
    return parser.parse_args()


def _compose_log_command(since: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env",
        "--env-file",
        "config/runtime.env",
        "-f",
        "infrastructure/docker/docker-compose.yml",
        "logs",
        "--no-color",
    ]
    if since.lower() != "all":
        command.extend(["--since", since])
    return command


def _clean_message(message: str) -> str:
    return ANSI_ESCAPE_RE.sub("", message).rstrip()


def _clean_output(output: str) -> str:
    return ANSI_ESCAPE_RE.sub("", output)


def _normalize_issue(message: str) -> str:
    normalized = _clean_message(message).strip()
    normalized = TIMESTAMP_PREFIX_RE.sub("", normalized)
    normalized = re.sub(r"0x[0-9a-fA-F]+", "0x...", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _capture_continuation(message: str) -> bool:
    stripped = message.lstrip()
    return stripped.startswith(
        (
            "Traceback",
            "File ",
            "The above exception",
            "The above exception was",
            "The above error",
            "details =",
            "debug_error_string =",
            "status =",
            "grpc._channel",
        )
    )


def main() -> int:
    args = parse_args()
    command = _compose_log_command(args.since)
    output = subprocess.check_output(command, text=True)
    lines = output.splitlines()

    errors_by_container: dict[str, list[str]] = defaultdict(list)
    issue_counts: dict[str, Counter[str]] = defaultdict(Counter)
    current_container: str | None = None
    capture_mode = False

    for line in lines:
        parts = line.split("|", 1)
        if len(parts) != 2:
            capture_mode = False
            current_container = None
            continue

        container = parts[0].strip()
        message = _clean_message(parts[1])
        stripped = message.strip()

        starts_capture = bool(ERROR_KEYWORDS.search(stripped)) or stripped.startswith(
            "Traceback"
        )
        continues_capture = (
            capture_mode
            and container == current_container
            and _capture_continuation(message)
        )

        if starts_capture or continues_capture:
            errors_by_container[container].append(stripped)
            current_container = container
            capture_mode = True
            if starts_capture:
                issue_counts[container][_normalize_issue(stripped)] += 1
            continue

        capture_mode = False
        current_container = None

    output_path = Path(args.output)
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write("--- EXTRACTED ISSUES ---\n")
        handle.write(
            f"Window: last {args.since}\n"
            if args.since.lower() != "all"
            else "Window: all available compose logs\n"
        )

        if not issue_counts:
            handle.write("\nNo matching warning/error patterns found.\n")
        else:
            for container in sorted(issue_counts):
                handle.write(f"\n--- {container} ---\n")
                for issue, count in issue_counts[container].most_common(10):
                    suffix = "time" if count == 1 else "times"
                    handle.write(f"[{count} {suffix}] {issue}\n")

                unique_messages: set[str] = set()
                sample_budget = 0
                for message in errors_by_container[container]:
                    normalized = _normalize_issue(message)
                    if normalized in unique_messages:
                        continue
                    unique_messages.add(normalized)
                    handle.write(message + "\n")
                    sample_budget += 1
                    if sample_budget >= 5:
                        break

        handle.write("\n\n--- FULL LOGS ---\n")
        handle.write(_clean_output(output))

    print(f"Wrote {output_path} using window={args.since}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
