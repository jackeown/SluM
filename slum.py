#!/usr/bin/env python3
"""Generate readable Bash submission files for solver jobs with Slurm.

Python is used only on the laptop to expand solver configurations and write the
submission files. The remote side consists of ordinary Bash task files, a Slurm
batch script, and runsolver.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import textwrap
from typing import Any, Sequence


VERSION = "0.4.1"
DEFAULT_REMOTE_HOST = "slurmy"
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_PARALLEL = 100

MEMORY_RE = re.compile(
    r"^\s*(?P<number>[0-9]+)\s*[-_ ]?\s*(?P<unit>mb|mib|gb|gib|tb|tib)?\s*$",
    re.IGNORECASE,
)
PROBLEM_PLACEHOLDER_RE = re.compile(r"\{\{problem(?:=([^{}]+))?\}\}")
ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")

CPU_REQUESTS = {
    "2cpu": 64,
    "64core": 64,
    "1cpu": 32,
    "32core": 32,
    "16core": 16,
    "8core": 8,
    "4core": 4,
    "1core": 1,
}


class SlumError(ValueError):
    """An error in the user's SluM configuration."""


def absolute_path(value: str, *, relative_to: Path | None = None) -> Path:
    """Return a normalized absolute path without resolving symlinks."""

    expanded = Path(os.path.expanduser(value))
    if not expanded.is_absolute():
        expanded = (relative_to or Path.cwd()) / expanded
    return Path(os.path.abspath(os.fspath(expanded)))


def mirrored_relative(path: Path) -> str:
    """Map /old/absolute/path to old/absolute/path inside rootfs."""

    if not path.is_absolute():
        raise SlumError(f"expected an absolute path, got {path}")
    try:
        return path.relative_to(path.anchor).as_posix()
    except ValueError as exc:  # pragma: no cover - defensive on non-POSIX hosts
        raise SlumError(f"cannot mirror path {path}") from exc


def parse_positive_seconds(value: str, option: str) -> int:
    if not re.fullmatch(r"[0-9]+", value.strip()):
        raise SlumError(f"{option} must be a positive integer number of seconds")
    seconds = int(value)
    if seconds <= 0:
        raise SlumError(f"{option} must be greater than zero")
    return seconds


def parse_memory_bytes(value: str, option: str) -> int:
    """Parse README memory syntax, distinguishing decimal and binary units."""

    match = MEMORY_RE.fullmatch(value)
    if not match:
        raise SlumError(
            f"{option} must be an integer optionally followed by MB, MiB, GB, "
            "GiB, TB, or TiB"
        )
    number = int(match.group("number"))
    if number <= 0:
        raise SlumError(f"{option} must be greater than zero")
    unit = (match.group("unit") or "mb").lower()
    factors = {
        "mb": 1_000_000,
        "mib": 1 << 20,
        "gb": 1_000_000_000,
        "gib": 1 << 30,
        "tb": 1_000_000_000_000,
        "tib": 1 << 40,
    }
    return number * factors[unit]


def bytes_to_mib_ceil(value: int) -> int:
    return math.ceil(value / (1 << 20))


def bytes_to_mb_ceil(value: int) -> int:
    return math.ceil(value / 1_000_000)


def parse_cpu_request(value: str) -> int:
    normalized = re.sub(r"[-_\s]", "", value).lower()
    try:
        return CPU_REQUESTS[normalized]
    except KeyError as exc:
        choices = "2-CPU, 1-CPU, 64-core, 32-core, 16-core, 8-core, 4-core, 1-core"
        raise SlumError(f"--cpu-request must be one of: {choices}") from exc


def slurm_time(seconds: int) -> str:
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def expand_problem_patterns(patterns: Sequence[str]) -> list[Path]:
    problems: dict[str, Path] = {}
    unmatched: list[str] = []
    for pattern in patterns:
        expanded = os.path.expanduser(pattern)
        matches = glob.glob(expanded, recursive=True)
        if not matches:
            unmatched.append(pattern)
            continue
        for match in matches:
            path = absolute_path(match)
            if not path.is_file():
                raise SlumError(f"problem path is not a regular file: {path}")
            problems[os.fspath(path)] = path
    if unmatched:
        raise SlumError("problem pattern(s) matched nothing: " + ", ".join(unmatched))
    if not problems:
        raise SlumError("--problems did not select any regular files")
    return [problems[key] for key in sorted(problems)]


def replace_limit_placeholders(
    command: str, *, cpu_limit: int, wc_limit: int, mem_limit_mb: int
) -> str:
    replacements = {
        "{{cpu-limit}}": str(cpu_limit),
        "{{wc-limit}}": str(wc_limit),
        "{{mem-limit}}": str(mem_limit_mb),
    }
    for placeholder, replacement in replacements.items():
        command = command.replace(placeholder, replacement)
    return command


def task_key(
    solver_file: Path, config_line: int, command: str, problem: str | None
) -> str:
    material = "\0".join(
        (os.fspath(solver_file), str(config_line), command, problem or "")
    ).encode("utf-8", "surrogateescape")
    return hashlib.sha256(material).hexdigest()[:20]


def system_name(command: str, solver_file: Path) -> str:
    """Return a compact solver name suitable for result tables."""

    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    while words and "=" in words[0] and not words[0].startswith(("/", "./")):
        words.pop(0)
    if words and words[0] == "env":
        words.pop(0)
        while words and "=" in words[0]:
            words.pop(0)
    if words:
        name = Path(words[0]).name
        if name:
            return name
    return solver_file.stem.removesuffix(".solver") or "solver"


def parse_solver_files(
    solver_files: Sequence[str],
    problems: Sequence[Path],
    *,
    cpu_limit: int,
    wc_limit: int,
    mem_limit_mb: int,
) -> tuple[list[dict[str, Any]], list[Path], list[Path]]:
    """Return tasks, solver roots, and extra specifically referenced problems."""

    tasks: list[dict[str, Any]] = []
    solver_roots: list[Path] = []
    specific_problems: dict[str, Path] = {}

    for solver_number, solver_value in enumerate(solver_files):
        solver_file = absolute_path(solver_value)
        if not solver_file.is_file():
            raise SlumError(f"solver description is not a regular file: {solver_file}")
        lines = solver_file.read_text(encoding="utf-8").splitlines()
        if not lines or not lines[0].strip():
            raise SlumError(f"solver description has no root on its first line: {solver_file}")
        solver_root = absolute_path(lines[0].strip(), relative_to=solver_file.parent)
        if solver_root == Path("/"):
            raise SlumError(f"refusing to package / as a solver root in {solver_file}")
        if not solver_root.is_dir():
            raise SlumError(f"solver root is not a directory: {solver_root}")
        solver_roots.append(solver_root)

        configurations = [
            (line_number, line.strip())
            for line_number, line in enumerate(lines[1:], start=2)
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not configurations:
            raise SlumError(f"solver description has no invocation lines: {solver_file}")

        for config_index, (line_number, command) in enumerate(configurations):
            command = replace_limit_placeholders(
                command,
                cpu_limit=cpu_limit,
                wc_limit=wc_limit,
                mem_limit_mb=mem_limit_mb,
            )
            matches = list(PROBLEM_PLACEHOLDER_RE.finditer(command))
            if not matches:
                raise SlumError(
                    f"{solver_file}:{line_number}: invocation must contain {{problem}} "
                    "or {{problem=/path}}"
                )
            generic = [match for match in matches if match.group(1) is None]
            specific = [match for match in matches if match.group(1) is not None]
            if generic and specific:
                raise SlumError(
                    f"{solver_file}:{line_number}: cannot mix {{problem}} and "
                    "{{problem=/path}} in one invocation"
                )

            allowed_tokens = {
                match.group(0)[2:-2] for match in matches
            }
            unknown = [
                token
                for token in ANY_PLACEHOLDER_RE.findall(command)
                if token not in allowed_tokens
            ]
            if unknown:
                raise SlumError(
                    f"{solver_file}:{line_number}: unknown placeholder(s): "
                    + ", ".join(f"{{{{{token}}}}}" for token in unknown)
                )

            common = {
                "solver": solver_number,
                "solver_file": os.fspath(solver_file),
                "solver_root": os.fspath(solver_root),
                "system": system_name(command, solver_file),
                "config": config_index,
                "config_line": line_number,
            }
            if generic:
                for problem in problems:
                    problem_string = os.fspath(problem)
                    item = {
                        **common,
                        "command": command,
                        "problem": problem_string,
                    }
                    item["task_key"] = task_key(
                        solver_file, line_number, command, problem_string
                    )
                    tasks.append(item)
            else:
                def normalize_specific(match: re.Match[str]) -> str:
                    raw = match.group(1)
                    assert raw is not None
                    path = absolute_path(raw.strip(), relative_to=solver_file.parent)
                    if not path.is_file():
                        raise SlumError(
                            f"{solver_file}:{line_number}: specific problem is not a "
                            f"regular file: {path}"
                        )
                    specific_problems[os.fspath(path)] = path
                    return "{{problem=" + os.fspath(path) + "}}"

                normalized_command = PROBLEM_PLACEHOLDER_RE.sub(
                    normalize_specific, command
                )
                item = {
                    **common,
                    "command": normalized_command,
                    "problem": None,
                }
                item["task_key"] = task_key(
                    solver_file, line_number, normalized_command, None
                )
                tasks.append(item)

    if not tasks:
        raise SlumError("no solver invocations were generated")
    for task_id, task in enumerate(tasks):
        task["task_id"] = task_id
    return tasks, solver_roots, list(specific_problems.values())


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def archive_entries(
    solver_roots: Sequence[Path], problem_paths: Sequence[Path]
) -> list[Path]:
    """Remove archive entries already recursively covered by another entry."""

    unique_roots = sorted(
        {os.fspath(path): path for path in solver_roots}.values(),
        key=lambda path: (len(path.parts), os.fspath(path)),
    )
    minimal_roots: list[Path] = []
    for root in unique_roots:
        if not any(is_within(root, parent) for parent in minimal_roots):
            minimal_roots.append(root)

    entries = list(minimal_roots)
    for path in sorted(
        {os.fspath(path): path for path in problem_paths}.values(),
        key=os.fspath,
    ):
        if not any(is_within(path, root) for root in minimal_roots):
            entries.append(path)
    return entries


def validate_sbatch_options(values: Sequence[str]) -> list[str]:
    result = []
    for value in values:
        if "\n" in value or "\r" in value or not value.startswith("--"):
            raise SlumError(
                "each --sbatch-option must be a single line beginning with -- "
                "(use --sbatch-option=--partition=NAME)"
            )
        if value == "--array" or value.startswith("--array="):
            raise SlumError("SluM manages --array; it cannot be an --sbatch-option")
        result.append(value)
    return result


def build_slurm_script(
    *,
    task_count: int,
    batch_size: int,
    cpu_count: int,
    memory_request_mib: int,
    worker_seconds: int,
    cpu_limit: int,
    wc_limit: int,
    mem_limit_mib: int,
    output_limit_mb: int,
    job_name: str,
    sbatch_options: Sequence[str],
) -> bytes:
    directives = "\n".join(f"#SBATCH {option}" for option in sbatch_options)
    directives_token = "__SLUM_EXTRA_SBATCH_DIRECTIVES__"
    output_limit_setup = ""
    if output_limit_mb:
        first_part = min(10, output_limit_mb // 2)
        output_limit_setup = (
            f'RUNSOLVER_OUTPUT_LIMIT=(--output-limit "{first_part},{output_limit_mb}")'
        )
    else:
        output_limit_setup = "RUNSOLVER_OUTPUT_LIMIT=()"

    script = rf"""
        #!/usr/bin/env bash
        #SBATCH --job-name={job_name}
        #SBATCH --nodes=1
        #SBATCH --ntasks=1
        #SBATCH --cpus-per-task={cpu_count}
        #SBATCH --mem={memory_request_mib}M
        #SBATCH --time={slurm_time(worker_seconds)}
        #SBATCH --output=logs/slurm_%A_%a.out
        #SBATCH --signal=B:TERM@60
        {directives_token}

        set -euo pipefail

        JOB_DIR=${{SLURM_SUBMIT_DIR:-$PWD}}
        export JOB_DIR
        RUNSOLVER=$(<"$JOB_DIR/runsolver.path")
        BATCH_ID=$((SLURM_ARRAY_TASK_ID + ${{SLUM_BATCH_OFFSET:-0}}))
        export OMP_NUM_THREADS=${{SLURM_CPUS_PER_TASK:-{cpu_count}}}
        TASK_FIRST=$((BATCH_ID * {batch_size}))
        TASK_LAST=$((TASK_FIRST + {batch_size} - 1))
        TASK_COUNT={task_count}
        if (( TASK_LAST >= TASK_COUNT )); then
            TASK_LAST=$((TASK_COUNT - 1))
        fi

        CPU_LIMIT={cpu_limit}
        WALL_LIMIT={wc_limit}
        MEMORY_LIMIT_MIB={mem_limit_mib}
        {output_limit_setup}

        ACTIVE_PID=
        STOP_REQUESTED=0
        forward_term() {{
            STOP_REQUESTED=1
            if [[ -n "$ACTIVE_PID" ]]; then
                kill -TERM "$ACTIVE_PID" 2>/dev/null || true
            fi
        }}
        trap forward_term TERM INT

        read_var() {{
            local name=$1 file=$2
            awk -F= -v name="$name" '$1 == name {{ print $2; exit }}' "$file" 2>/dev/null || true
        }}

        is_complete() {{
            local task_id=$1 status_file=$2
            [[ -f "$status_file" ]] &&
                awk -F '\t' -v id="$task_id" '$1 == id && $2 == "true" {{ found=1 }} END {{ exit !found }}' "$status_file"
        }}

        RESULTS_DIR="$JOB_DIR/results"
        STATUS_FILE=$(printf '%s/batch_%06d.tsv' "$RESULTS_DIR" "$BATCH_ID")

        # Publish a tiny, atomically replaced progress record for this batch.
        # slum-monitor.py uses it to show the current task and live percentage.
        PROGRESS_DIR="$JOB_DIR/progress"
        PROGRESS_FILE=$(printf '%s/batch_%06d.tsv' "$PROGRESS_DIR" "$BATCH_ID")
        mkdir -p "$PROGRESS_DIR"
        write_progress() {{
            local state=$1 task_id=${{2:-}} started_epoch=${{3:-}} finished=${{4:-0}}
            local temporary="$PROGRESS_FILE.$$.tmp"
            printf '%s\t%s\t%s\t%s\t%s\n' \
                "$state" "$task_id" "$started_epoch" "$(date +%s)" "$finished" \
                > "$temporary"
            mv "$temporary" "$PROGRESS_FILE"
        }}
        write_progress starting "" "" 0

        SCRATCH_PARENT=${{SLURM_TMPDIR:-${{TMPDIR:-/tmp}}}}
        ATTEMPT_DIR=$(mktemp -d "$SCRATCH_PARENT/slum-batch-${{BATCH_ID}}.XXXXXXXX")
        RECORDS_FILE="$ATTEMPT_DIR/records.tsv"
        cleanup() {{
            local exit_code=$?
            rm -rf -- "$ATTEMPT_DIR"
            if (( STOP_REQUESTED )); then
                write_progress interrupted "" "" "${{completed_this_attempt:-0}}"
            elif (( exit_code != 0 )); then
                write_progress failed "" "" "${{completed_this_attempt:-0}}"
            fi
        }}
        trap cleanup EXIT

        batch_file=$(printf '%s/batches/batch_%06d.sh' "$JOB_DIR" "$BATCH_ID")
        if [[ ! -f "$batch_file" ]]; then
            echo "SluM: missing batch file: $batch_file" >&2
            exit 1
        fi
        # Generated batch files contain only quoted Bash array assignments.
        source "$batch_file"

        echo "SluM batch $BATCH_ID: tasks $TASK_FIRST through $TASK_LAST"
        completed_this_attempt=0
        for index in "${{!TASK_IDS[@]}}"; do
            (( STOP_REQUESTED )) && break
            TASK_ID=${{TASK_IDS[$index]}}
            TASK_KEY=${{TASK_KEYS[$index]}}
            TASK_SOLVER_ROOT_REL=${{TASK_SOLVER_ROOT_RELS[$index]}}
            TASK_PROBLEM_REL=${{TASK_PROBLEM_RELS[$index]}}
            TASK_COMMAND=${{TASK_COMMANDS[$index]}}
            task_id=$TASK_ID
            if is_complete "$task_id" "$STATUS_FILE"; then
                echo "SluM task $task_id: already complete; skipping"
                continue
            fi

            stem=$(printf 'task_%09d_%s' "$TASK_ID" "$TASK_KEY")
            solver_log="$ATTEMPT_DIR/$stem.solver.log"
            watcher_log="$ATTEMPT_DIR/$stem.watcher.log"
            controller_log="$ATTEMPT_DIR/$stem.controller.log"
            var_file="$ATTEMPT_DIR/$stem.var"
            solver_root="$JOB_DIR/rootfs/$TASK_SOLVER_ROOT_REL"
            if [[ ! -d "$solver_root" ]]; then
                echo "SluM: mapped solver root does not exist: $solver_root" >&2
                exit 1
            fi

            export SLUM_TASK_ID="$TASK_ID"
            export SLUM_PROBLEM=
            if [[ -n "$TASK_PROBLEM_REL" ]]; then
                SLUM_PROBLEM="$JOB_DIR/rootfs/$TASK_PROBLEM_REL"
                export SLUM_PROBLEM
            fi

            echo "SluM task $TASK_ID: $TASK_COMMAND"
            task_started_epoch=$(date +%s)
            write_progress running "$TASK_ID" "$task_started_epoch" "$completed_this_attempt"
            old_pwd=$PWD
            cd "$solver_root"
            set +e
            "$RUNSOLVER" \
                --cpu-limit "$CPU_LIMIT" \
                --wall-clock-limit "$WALL_LIMIT" \
                --rss-swap-limit "$MEMORY_LIMIT_MIB" \
                --delay 2 \
                --watchdog "$((WALL_LIMIT + 120))" \
                --watcher-data "$watcher_log" \
                --var "$var_file" \
                --solver-data "$solver_log" \
                "${{RUNSOLVER_OUTPUT_LIMIT[@]}}" \
                /bin/bash -c "$TASK_COMMAND" >"$controller_log" 2>&1 &
            ACTIVE_PID=$!
            wait "$ACTIVE_PID"
            runsolver_return_code=$?
            ACTIVE_PID=
            set -e
            cd "$old_pwd"

            # runsolver itself normally exits successfully even when the
            # measured command does not. Its watcher report preserves the
            # command's exit status, which is the value users need to see.
            child_status=$(sed -n 's/^Child status:[[:space:]]*//p' "$watcher_log" 2>/dev/null | tail -n 1)
            if [[ "$child_status" =~ ^[0-9]+$ ]]; then
                return_code=$child_status
            else
                return_code=$runsolver_return_code
            fi

            timeout=$(read_var TIMEOUT "$var_file")
            memout=$(read_var MEMOUT "$var_file")
            wall=$(read_var WCTIME "$var_file")
            cpu=$(read_var CPUTIME "$var_file")
            user=$(read_var USERTIME "$var_file")
            system=$(read_var SYSTEMTIME "$var_file")
            cpu_usage=$(read_var CPUUSAGE "$var_file")
            max_vm=$(read_var MAXVM "$var_file")
            max_memory=$(sed -n 's/^Max\. memory (cumulated for all children) (KiB):[[:space:]]*//p' "$watcher_log" 2>/dev/null | tail -n 1)
            if [[ -z "$max_memory" || "$max_memory" == 0 ]]; then
                max_memory=$(sed -n 's/^maximum resident set size=[[:space:]]*//p' "$watcher_log" 2>/dev/null | tail -n 1)
            fi
            complete=true
            if (( STOP_REQUESTED )); then
                status=interrupted
                complete=false
            elif [[ ! -s "$var_file" ]]; then
                status=worker-error
                complete=false
            elif [[ "$memout" == true ]]; then
                status=memory-limit
            elif [[ "$timeout" == true ]]; then
                status=time-limit
            elif (( return_code == 0 )); then
                status=ok
            else
                status=error
            fi

            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$TASK_ID" "$complete" "$status" "$return_code" \
                "${{wall:-}}" "${{cpu:-}}" "${{user:-}}" "${{system:-}}" \
                "${{cpu_usage:-}}" "${{max_vm:-}}" "${{max_memory:-}}" \
                "${{timeout:-false}}" "${{memout:-false}}" "$TASK_KEY" >> "$RECORDS_FILE"
            completed_this_attempt=$((completed_this_attempt + 1))
            write_progress saving "$TASK_ID" "$task_started_epoch" "$completed_this_attempt"
        done

        if (( completed_this_attempt > 0 )); then
            stamp=$(date +%s)
            archive=$(printf 'batch_%06d_%s_%s.tar.gz' "$BATCH_ID" "${{SLURM_JOB_ID:-local}}" "$stamp")
            tar -czf "$RESULTS_DIR/.$archive.tmp" --exclude=records.tsv -C "$ATTEMPT_DIR" .
            mv "$RESULTS_DIR/.$archive.tmp" "$RESULTS_DIR/$archive"
            while IFS= read -r record; do
                printf '%s\t%s\n' "$record" "$archive" >> "$STATUS_FILE"
            done < "$RECORDS_FILE"
        fi

        write_progress complete "" "" "$completed_this_attempt"
        echo "SluM batch $BATCH_ID: finished $completed_this_attempt tasks"
        (( STOP_REQUESTED == 0 ))
        """
    return (
        textwrap.dedent(script)
        .lstrip("\n")
        .replace(directives_token, directives)
        .encode("utf-8")
    )


def render_remote_command(task: dict[str, Any]) -> str:
    """Render problem placeholders for the rootfs used by the Bash worker."""

    def replace(match: re.Match[str]) -> str:
        explicit = match.group(1)
        problem = explicit if explicit is not None else task.get("problem")
        if not problem:
            raise SlumError(f"task {task['task_id']} has no problem for placeholder")
        relative = mirrored_relative(Path(problem))
        return "${JOB_DIR}/rootfs/" + shlex.quote(relative)

    command = PROBLEM_PLACEHOLDER_RE.sub(replace, task["command"])
    leftovers = ANY_PLACEHOLDER_RE.findall(command)
    if leftovers:
        raise SlumError(
            f"task {task['task_id']} has unresolved placeholders: {leftovers}"
        )
    return command


def bash_array(name: str, values: Sequence[str]) -> str:
    lines = [f"{name}=("]
    lines.extend(f"  {shlex.quote(value)}" for value in values)
    lines.append(")")
    return "\n".join(lines)


def build_batch_file(tasks: Sequence[dict[str, Any]]) -> str:
    sections = [
        "# Generated by SluM. This file contains data only.",
        bash_array("TASK_IDS", [str(task["task_id"]) for task in tasks]),
        bash_array("TASK_KEYS", [str(task["task_key"]) for task in tasks]),
        bash_array("TASK_SYSTEMS", [str(task["system"]) for task in tasks]),
        bash_array(
            "TASK_SOLVER_ROOT_RELS",
            [mirrored_relative(Path(task["solver_root"])) for task in tasks],
        ),
        bash_array(
            "TASK_PROBLEM_RELS",
            [
                mirrored_relative(Path(task["problem"])) if task.get("problem") else ""
                for task in tasks
            ],
        ),
        bash_array("TASK_COMMANDS", [render_remote_command(task) for task in tasks]),
    ]
    return "\n\n".join(sections) + "\n"


def prepare_submission(args: argparse.Namespace) -> dict[str, Any]:
    cpu_limit = parse_positive_seconds(args.cpu_limit, "--cpu-limit")
    wc_limit = parse_positive_seconds(args.wc_limit, "--wc-limit")
    mem_limit_bytes = parse_memory_bytes(args.mem_limit, "--mem-limit")
    memory_request_bytes = parse_memory_bytes(
        args.memory_request, "--memory-request"
    )
    cpu_count = parse_cpu_request(args.cpu_request)
    if args.batch_size <= 0:
        raise SlumError("--batch-size must be greater than zero")
    if args.max_parallel <= 0:
        raise SlumError("--max-parallel must be greater than zero")
    if args.output_limit_mb < 0:
        raise SlumError("--output-limit-mb cannot be negative")
    if memory_request_bytes < mem_limit_bytes:
        raise SlumError("--memory-request must be at least as large as --mem-limit")
    if memory_request_bytes == mem_limit_bytes:
        print(
            "SluM warning: memory-request equals mem-limit; consider adding room "
            "for Bash and runsolver",
            file=sys.stderr,
        )

    patterns = [value for group in args.problems for value in group]
    problems = expand_problem_patterns(patterns)
    tasks, solver_roots, specific_problems = parse_solver_files(
        args.solver,
        problems,
        cpu_limit=cpu_limit,
        wc_limit=wc_limit,
        mem_limit_mb=bytes_to_mb_ceil(mem_limit_bytes),
    )
    entries = archive_entries(solver_roots, list(problems) + specific_problems)
    for path in entries:
        if "\n" in os.fspath(path) or "\r" in os.fspath(path):
            raise SlumError(f"input paths may not contain newlines: {path}")

    batch_count = math.ceil(len(tasks) / args.batch_size)
    largest_batch = min(args.batch_size, len(tasks))
    worker_seconds = largest_batch * wc_limit + args.worker_overhead
    mem_limit_mib = bytes_to_mib_ceil(mem_limit_bytes)
    memory_request_mib = bytes_to_mib_ceil(memory_request_bytes)
    sbatch_options = validate_sbatch_options(args.sbatch_option)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.job_name):
        raise SlumError("--job-name may contain only letters, digits, _, ., and -")

    metadata = {
        "slum_version": VERSION,
        "remote_runtime": "bash+runsolver",
        "job_name": args.job_name,
        "task_count": len(tasks),
        "batch_count": batch_count,
        "batch_size": args.batch_size,
        "max_parallel": args.max_parallel,
        "limits": {
            "cpu_seconds": cpu_limit,
            "wall_seconds": wc_limit,
            "memory_input": args.mem_limit,
            "memory_bytes": mem_limit_bytes,
            "memory_runner_mib": mem_limit_mib,
        },
        "request": {
            "cpus_per_task": cpu_count,
            "memory_input": args.memory_request,
            "memory_slurm_mib": memory_request_mib,
            "worker_wall_seconds": worker_seconds,
        },
        "problem_count": len(problems),
        "solver_files": [os.fspath(absolute_path(path)) for path in args.solver],
        "archive_entries": [os.fspath(path) for path in entries],
        "result_columns": [
            "task_id", "complete", "status", "return_code", "wall_seconds",
            "cpu_seconds", "user_seconds", "system_seconds", "cpu_usage_percent",
            "max_virtual_memory_kib", "max_memory_kib", "timed_out", "memory_out",
            "task_key", "archive",
        ],
    }
    slurm_script = build_slurm_script(
        task_count=len(tasks),
        batch_size=args.batch_size,
        cpu_count=cpu_count,
        memory_request_mib=memory_request_mib,
        worker_seconds=worker_seconds,
        cpu_limit=cpu_limit,
        wc_limit=wc_limit,
        mem_limit_mib=mem_limit_mib,
        output_limit_mb=args.output_limit_mb,
        job_name=args.job_name,
        sbatch_options=sbatch_options,
    ).decode("utf-8")
    batches = [
        build_batch_file(tasks[first : first + args.batch_size])
        for first in range(0, len(tasks), args.batch_size)
    ]
    return {
        "tasks": tasks,
        "entries": entries,
        "metadata": metadata,
        "slurm_script": slurm_script,
        "batches": batches,
        "batch_count": batch_count,
        "max_parallel": min(args.max_parallel, batch_count),
    }


def build_submit_script(
    args: argparse.Namespace, *, asset_dir_name: str, task_count: int,
    batch_count: int, max_parallel: int
) -> str:
    host = shlex.quote(args.host)
    assets = shlex.quote(asset_dir_name)
    # unicode_escape produces a single line and doubles literal backslashes, making
    # user-controlled names safe to display inside generated shell comments.
    host_display = args.host.encode("unicode_escape").decode("ascii")
    assets_display = asset_dir_name.encode("unicode_escape").decode("ascii")
    script = rf"""
        #!/usr/bin/env bash
        # Generated by SluM {VERSION}; run this script on the laptop.
        #
        # Purpose
        # -------
        # Package the solver resources and problems selected when these files were
        # generated, copy them to {host_display}, and submit a batched Slurm job array.
        # This script returns after submission; it does not wait for jobs or download
        # results.
        #
        # Companion files
        # ---------------
        # Keep this script next to {assets_display}/. That directory contains:
        #   metadata.json       limits, requests, counts, and result columns
        #   archive-paths.txt   laptop files and directories to package
        #   batches/            generated solver commands for each array element
        #   remote_prepare.sh   creates the remote staging directory
        #   remote_submit.sh    extracts files and invokes sbatch
        #   slurm_job.sh        runs each batch under runsolver on a compute node
        #   runsolver           binary copied from the --runsolver path
        #
        # Generated configuration
        # -----------------------
        #   SSH host:             {host_display}
        #   Solver calls:         {task_count}
        #   Slurm array batches:  {batch_count}
        #   Max parallel batches: {max_parallel}
        #
        # Usage: execute this script with no arguments.
        set -euo pipefail

        # Locate companion files relative to this script so it can be run from any
        # working directory. Moving the script and its companion directory together
        # is safe only when the paths in archive-paths.txt are still valid.
        SCRIPT_DIR=$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)
        ASSET_DIR="$SCRIPT_DIR"/{assets}
        HOST={host}

        # Stage 1/5: fail early if the laptop lacks a required command or companion
        # directory. No remote state has been changed at this point.
        command -v ssh >/dev/null || {{ echo "SluM: ssh is required" >&2; exit 1; }}
        command -v scp >/dev/null || {{ echo "SluM: scp is required" >&2; exit 1; }}
        command -v tar >/dev/null || {{ echo "SluM: tar is required" >&2; exit 1; }}
        [[ -d "$ASSET_DIR" ]] || {{ echo "SluM: missing asset directory: $ASSET_DIR" >&2; exit 1; }}

        # Temporary archives exist only for this submission attempt and are removed
        # on normal exit or error. The original inputs are never modified.
        TEMP_DIR=$(mktemp -d "${{TMPDIR:-/tmp}}/slum-submit.XXXXXXXX")
        cleanup() {{ rm -rf -- "$TEMP_DIR"; }}
        trap cleanup EXIT

        # Stage 2/5: derive a unique remote job directory. `ssh -G` reads the local
        # SSH configuration; the first actual connection happens in Stage 4.
        REMOTE_USER=$(ssh -T -G -- "$HOST" | awk 'tolower($1) == "user" {{ print $2; exit }}')
        if [[ ! "$REMOTE_USER" =~ ^[A-Za-z0-9._-]+$ ]]; then
            echo "SluM: unable to obtain a safe remote username from ssh -G $HOST" >&2
            exit 1
        fi
        JOB_ID="${{REMOTE_USER}}_$(date +%s)_$$"
        REMOTE_REL="SluM/$JOB_ID"

        # Stage 3/5: create two archives:
        #   inputs.tar.gz     solver resources and problem files, mirrored from /
        #   job-files.tar.gz generated scripts, metadata, commands, and runsolver
        echo "SluM: packaging {task_count} calls in {batch_count} batches..." >&2
        tar --create --gzip --file "$TEMP_DIR/inputs.tar.gz" \
            --directory=/ --dereference --verbatim-files-from \
            --files-from="$ASSET_DIR/archive-paths.txt"
        tar --create --gzip --file "$TEMP_DIR/job-files.tar.gz" \
            --directory="$ASSET_DIR" .

        # Stage 4/5: create an empty $HOME/SluM/<job-id>/incoming directory, then
        # transfer both archives. Remote logic lives in named, readable helpers.
        ssh -T -- "$HOST" bash -s -- "$JOB_ID" \
            < "$ASSET_DIR/remote_prepare.sh"

        echo "SluM: transferring inputs and readable job files to $HOST:$REMOTE_REL..." >&2
        scp -- \
            "$TEMP_DIR/inputs.tar.gz" \
            "$TEMP_DIR/job-files.tar.gz" \
            "$HOST:$REMOTE_REL/incoming/"

        # Stage 5/5: install the uploaded files, validate runsolver, and invoke
        # sbatch. The helper prints the SluM ID, Slurm ID(s), and result directory.
        ssh -T -- "$HOST" bash -s -- "$JOB_ID" \
            < "$ASSET_DIR/remote_submit.sh"
        """
    return textwrap.dedent(script).lstrip("\n")


def build_remote_prepare_script() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        # Generated by SluM. Creates a fresh remote job staging directory.
        set -euo pipefail

        JOB_ID=$1
        JOB_DIR="$HOME/SluM/$JOB_ID"

        if [[ -e "$JOB_DIR" ]]; then
            echo "SluM: remote job directory already exists: $JOB_DIR" >&2
            exit 73
        fi
        mkdir -p "$JOB_DIR/incoming"
        """
    )


def build_remote_submit_script(*, batch_count: int, max_parallel: int) -> str:
    script = rf"""
        #!/usr/bin/env bash
        # Generated by SluM. Installs the uploaded files and submits Slurm arrays.
        set -euo pipefail

        JOB_ID=$1
        JOB_DIR="$HOME/SluM/$JOB_ID"
        INCOMING_DIR="$JOB_DIR/incoming"

        mkdir -p "$JOB_DIR/rootfs" "$JOB_DIR/results" "$JOB_DIR/logs"
        tar -xzf "$INCOMING_DIR/job-files.tar.gz" -C "$JOB_DIR"
        tar -xzf "$INCOMING_DIR/inputs.tar.gz" -C "$JOB_DIR/rootfs"
        chmod 0755 "$JOB_DIR/slurm_job.sh"

        RUNSOLVER="$JOB_DIR/runsolver"
        if [[ ! -x "$RUNSOLVER" ]]; then
            echo "SluM: included runsolver is missing or not executable: $RUNSOLVER" >&2
            exit 1
        fi
        RUNSOLVER_HELP=$("$RUNSOLVER" 2>&1 || true)
        if ! grep -q -- '--rss-swap-limit' <<< "$RUNSOLVER_HELP"; then
            echo "SluM: runsolver lacks --rss-swap-limit; version 3.4.1 is recommended" >&2
            exit 1
        fi
        printf '%s\n' "$RUNSOLVER" > "$JOB_DIR/runsolver.path"

        cd "$JOB_DIR"
        MAX_ARRAY_SIZE=$(scontrol show config 2>/dev/null \
            | awk -F= '$1 ~ /^[[:space:]]*MaxArraySize[[:space:]]*$/ {{gsub(/[[:space:]]/, "", $2); print $2; exit}}' \
            || true)
        if [[ ! "$MAX_ARRAY_SIZE" =~ ^[1-9][0-9]*$ ]]; then
            MAX_ARRAY_SIZE=1000
        fi

        BATCH_COUNT={batch_count}
        MAX_PARALLEL={max_parallel}

        # Preserve the array ID and its global batch offset for the dashboard.
        # This also makes submissions larger than Slurm's MaxArraySize traceable.
        SUBMISSION_FILE="$JOB_DIR/submission.tsv"
        printf 'slurm_job_id\toffset\tarray_size\tsubmitted_epoch\n' \
            > "$SUBMISSION_FILE"
        OFFSET=0
        PREVIOUS_JOB=
        SLURM_IDS=()

        while (( OFFSET < BATCH_COUNT )); do
            REMAINING=$((BATCH_COUNT - OFFSET))
            SLICE_SIZE=$((REMAINING < MAX_ARRAY_SIZE ? REMAINING : MAX_ARRAY_SIZE))
            ARRAY_SPEC="0-$((SLICE_SIZE - 1))%$MAX_PARALLEL"
            SUBMIT=(sbatch --parsable "--array=$ARRAY_SPEC" \
                "--export=ALL,SLUM_BATCH_OFFSET=$OFFSET")
            if [[ -n "$PREVIOUS_JOB" ]]; then
                SUBMIT+=("--dependency=afterany:$PREVIOUS_JOB")
            fi
            SUBMITTED=$("${{SUBMIT[@]}}" slurm_job.sh)
            SLURM_IDS+=("$SUBMITTED")
            PREVIOUS_JOB=${{SUBMITTED%%;*}}
            printf '%s\t%s\t%s\t%s\n' \
                "$PREVIOUS_JOB" "$OFFSET" "$SLICE_SIZE" "$(date +%s)" \
                >> "$SUBMISSION_FILE"
            OFFSET=$((OFFSET + SLICE_SIZE))
        done

        printf 'SluM job ID: %s\nSlurm job ID(s): %s\nRemote directory: %s\n' \
            "$JOB_ID" "${{SLURM_IDS[*]}}" "$JOB_DIR"
        """
    return textwrap.dedent(script).lstrip("\n")


def generate_submission(args: argparse.Namespace, output: Path) -> tuple[Path, Path]:
    prepared = prepare_submission(args)
    asset_dir = output.with_name(output.name + ".files")
    if output.exists() or asset_dir.exists():
        raise SlumError(
            f"refusing to overwrite existing output: {output if output.exists() else asset_dir}"
        )

    asset_dir.mkdir(parents=True)
    batches_dir = asset_dir / "batches"
    batches_dir.mkdir()
    try:
        (asset_dir / "metadata.json").write_text(
            json.dumps(prepared["metadata"], indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        (asset_dir / "archive-paths.txt").write_text(
            "".join(mirrored_relative(path) + "\n" for path in prepared["entries"]),
            encoding="utf-8",
        )
        slurm_path = asset_dir / "slurm_job.sh"
        slurm_path.write_text(prepared["slurm_script"], encoding="utf-8")
        slurm_path.chmod(0o755)
        prepare_path = asset_dir / "remote_prepare.sh"
        prepare_path.write_text(build_remote_prepare_script(), encoding="utf-8")
        prepare_path.chmod(0o755)
        submit_path = asset_dir / "remote_submit.sh"
        submit_path.write_text(
            build_remote_submit_script(
                batch_count=prepared["batch_count"],
                max_parallel=prepared["max_parallel"],
            ),
            encoding="utf-8",
        )
        submit_path.chmod(0o755)
        for batch_id, content in enumerate(prepared["batches"]):
            (batches_dir / f"batch_{batch_id:06d}.sh").write_text(
                content, encoding="utf-8"
            )

        source = absolute_path(args.runsolver)
        if not source.is_file() or not os.access(source, os.X_OK):
            raise SlumError(f"--runsolver path is not executable: {source}")
        shutil.copy2(source, asset_dir / "runsolver")
        (asset_dir / "runsolver").chmod(0o755)

        shell = build_submit_script(
            args,
            asset_dir_name=asset_dir.name,
            task_count=len(prepared["tasks"]),
            batch_count=prepared["batch_count"],
            max_parallel=prepared["max_parallel"],
        )
        output.write_text(shell, encoding="utf-8")
        output.chmod(0o755)
    except Exception:
        if output.exists():
            output.unlink()
        shutil.rmtree(asset_dir, ignore_errors=True)
        raise
    return output, asset_dir


def generator_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slum.py",
        description=(
            "Generate readable Bash submission files that package solver jobs, "
            "copy them over SSH, and submit a batched Slurm array."
        ),
    )
    parser.add_argument("--version", action="version", version=f"SluM {VERSION}")
    parser.add_argument("--cpu-limit", required=True, metavar="SECONDS")
    parser.add_argument("--wc-limit", required=True, metavar="SECONDS")
    parser.add_argument("--mem-limit", required=True, metavar="MEMORY")
    parser.add_argument("--cpu-request", required=True, metavar="CPUS")
    parser.add_argument("--memory-request", required=True, metavar="MEMORY")
    parser.add_argument(
        "--problems",
        required=True,
        nargs="+",
        action="append",
        metavar="GLOB",
        help="one or more problem globs; may be repeated",
    )
    parser.add_argument(
        "--solver",
        required=True,
        action="append",
        metavar="FILE",
        help="solver description file; may be repeated",
    )
    parser.add_argument("--host", default=DEFAULT_REMOTE_HOST, help="SSH host alias")
    parser.add_argument(
        "--runsolver",
        required=True,
        metavar="PATH",
        help="local runsolver executable to include in the submission files",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"solver calls per array element (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=DEFAULT_MAX_PARALLEL,
        help=(
            "maximum simultaneously running array elements "
            f"(default: {DEFAULT_MAX_PARALLEL})"
        ),
    )
    parser.add_argument(
        "--worker-overhead",
        type=int,
        default=300,
        metavar="SECONDS",
        help="extra Slurm wall time per batch for setup and cleanup (default: 300)",
    )
    parser.add_argument(
        "--output-limit-mb",
        type=int,
        default=0,
        metavar="MB",
        help="runsolver output cap per call; 0 is unlimited (default: 0)",
    )
    parser.add_argument("--job-name", default="SluM")
    parser.add_argument(
        "--sbatch-option",
        action="append",
        default=[],
        metavar="--OPTION=VALUE",
        help="extra #SBATCH option; use the = form and repeat as needed",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        required=True,
        help=(
            "write the laptop-side submit script here and job files to "
            "FILE.files"
        ),
    )
    return parser


def generator_main(argv: Sequence[str]) -> int:
    parser = generator_parser()
    args = parser.parse_args(argv)
    try:
        if args.worker_overhead < 0:
            raise SlumError("--worker-overhead cannot be negative")
        output = absolute_path(args.output)
        output, asset_dir = generate_submission(args, output)
    except (SlumError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    print(f"SluM: wrote {output}", file=sys.stderr)
    print(f"SluM: wrote {asset_dir}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return generator_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
