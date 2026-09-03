#!/usr/bin/env python
"""Interactive laptop dashboard for current and historical SluM jobs."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Sequence

try:
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.css.query import NoMatches
    from textual.widgets import (
        DataTable,
        Footer,
        Header,
        Input,
        ProgressBar,
        RichLog,
        Static,
        TabbedContent,
        TabPane,
    )
except ModuleNotFoundError as exc:
    if exc.name == "textual" or (exc.name and exc.name.startswith("rich")):
        requirements = Path(__file__).resolve().with_name("requirements.txt")
        print(
            "SluM monitor needs its laptop dependencies. Install them with:\n"
            f"  python -m pip install -r {requirements}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    raise


VERSION = "0.1.0"
DEFAULT_HOST = "slurmy"
NORMAL_TASK_RESULTS = {"ok", "time-limit", "memory-limit"}
SLURM_ERROR_STATES = {
    "BOOT_FAIL",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "REVOKED",
    "TIMEOUT",
}
SLURM_INCOMPLETE_STATES = SLURM_ERROR_STATES | {"CANCELLED", "PREEMPTED"}


def slurm_state_code(state: str) -> str:
    return state.upper().split()[0].rstrip("+") if state else ""

# This script is sent to `ssh HOST bash -s`. It reads only the small monitoring
# files under $HOME/SluM and emits NUL-delimited records. Python is not needed on
# the cluster. Large solver inputs and result archives are never transferred.
REMOTE_SNAPSHOT_SCRIPT = r"""
set -u

DETAIL=${1:-}
BASE="$HOME/SluM"

printf 'REMOTE_HOME\0%s\0' "$BASE"
if [[ ! -d "$BASE" ]]; then
    printf 'DETAIL\0\0'
    printf 'SQUEUE\0\0SACCT\0\0'
    exit 0
fi

shopt -s nullglob
job_dirs=("$BASE"/*)
if [[ -z "$DETAIL" ]]; then
    for directory in "${job_dirs[@]}"; do
        [[ -d "$directory" ]] || continue
        candidate=${directory##*/}
        if [[ -z "$DETAIL" || "$candidate" > "$DETAIL" ]]; then
            DETAIL=$candidate
        fi
    done
fi
printf 'DETAIL\0%s\0' "$DETAIL"

oldest=
for directory in "${job_dirs[@]}"; do
    [[ -d "$directory" ]] || continue
    job=${directory##*/}
    created=$(stat -c %Y "$directory" 2>/dev/null || printf '0')
    printf 'JOB\0%s\0%s\0' "$job" "$created"

    if [[ -f "$directory/metadata.json" ]]; then
        printf 'METADATA\0%s\0' "$job"
        cat "$directory/metadata.json"
        printf '\0'
    fi

    # New jobs use batch_*; the second pattern keeps older SluM jobs readable.
    tsv_results=("$directory"/results/batch_*.tsv "$directory"/results/chunk_*.tsv)
    if (( ${#tsv_results[@]} )); then
        printf 'SUMMARY\0%s\0' "$job"
        awk -F '\t' '
            NF >= 3 && $1 ~ /^[0-9]+$/ {
                complete[$1] = $2
                result_status[$1] = $3
                wall[$1] = $5
            }
            END {
                for (id in result_status) {
                    if (complete[id] == "true") {
                        completed++
                        count[result_status[id]]++
                        if (wall[id] ~ /^[0-9]+([.][0-9]+)?$/) {
                            wall_sum += wall[id]
                            if (wall[id] > wall_max) wall_max = wall[id]
                        }
                    } else {
                        unresolved[result_status[id]]++
                    }
                }
                printf "completed\t%d\nwall_sum\t%.9f\nwall_max\t%.9f\n", completed, wall_sum, wall_max
                for (status in count) printf "status\t%s\t%d\n", status, count[status]
                for (status in unresolved) printf "unresolved\t%s\t%d\n", status, unresolved[status]
            }
        ' "${tsv_results[@]}"
        printf '\0'
    else
        legacy_results=("$directory"/results/chunk_*.jsonl)
        if (( ${#legacy_results[@]} )); then
            printf 'LEGACY_RESULTS\0%s\0' "$job"
            cat "${legacy_results[@]}"
            printf '\0'
        fi
    fi

    progress_files=("$directory"/progress/batch_*.tsv "$directory"/progress/chunk_*.tsv)
    if (( ${#progress_files[@]} )); then
        printf 'PROGRESS\0%s\0' "$job"
        for progress_file in "${progress_files[@]}"; do
            printf '%s\t' "${progress_file##*/}"
            cat "$progress_file"
        done
        printf '\0'
    fi

    if [[ -f "$directory/submission.tsv" ]]; then
        printf 'SUBMISSION\0%s\0' "$job"
        cat "$directory/submission.tsv"
        printf '\0'
    fi

    if [[ "$created" =~ ^[0-9]+$ ]] && { [[ -z "$oldest" ]] || (( created < oldest )); }; then
        oldest=$created
    fi

    if [[ "$job" == "$DETAIL" ]]; then
        if (( ${#tsv_results[@]} )); then
            printf 'RESULTS_TSV\0%s\0' "$job"
            cat "${tsv_results[@]}"
            printf '\0'
        fi
        legacy_results=("$directory"/results/chunk_*.jsonl)
        if (( ${#legacy_results[@]} )); then
            printf 'RESULTS_JSONL\0%s\0' "$job"
            cat "${legacy_results[@]}"
            printf '\0'
        fi
        if [[ -f "$directory/manifest.jsonl" ]]; then
            printf 'MANIFEST\0%s\0' "$job"
            cat "$directory/manifest.jsonl"
            printf '\0'
        fi

        batch_files=("$directory"/batches/batch_*.sh "$directory"/chunks/chunk_*.sh)
        for batch_file in "${batch_files[@]}"; do
            (
                set +u
                source "$batch_file"
                declare -p TASK_IDS TASK_KEYS TASK_PROBLEM_RELS TASK_COMMANDS TASK_SOLVER_ROOT_RELS >/dev/null 2>&1 || exit 0
                for index in "${!TASK_IDS[@]}"; do
                    system=
                    if declare -p TASK_SYSTEMS >/dev/null 2>&1; then
                        system=${TASK_SYSTEMS[$index]}
                    fi
                    printf 'TASKDEF\0%s\0%s\0%s\0%s\0%s\0%s\0%s\0' \
                        "$job" "${TASK_IDS[$index]}" "${TASK_KEYS[$index]}" \
                        "$system" "${TASK_PROBLEM_RELS[$index]}" "${TASK_COMMANDS[$index]}" \
                        "${TASK_SOLVER_ROOT_RELS[$index]}"
                done
            )
        done

        log_count=0
        log_files=("$directory"/logs/*.out)
        for log_file in "${log_files[@]}"; do
            (( log_count >= 100 )) && break
            printf 'LOG\0%s\0%s\0' "$job" "${log_file##*/}"
            tail -n 160 "$log_file" 2>/dev/null || true
            printf '\0'
            log_count=$((log_count + 1))
        done
    fi
done

printf 'SQUEUE\0'
squeue -r -h -u "$USER" -o '%i|%F|%K|%T|%M|%L|%j|%Z|%R' 2>/dev/null \
    | awk -F '|' -v prefix="$BASE/" 'index($8, prefix) == 1'
printf '\0'

if [[ -n "$oldest" ]]; then
    start=$(date -d "@$((oldest - 86400))" +%F 2>/dev/null || printf '1970-01-01')
else
    start=$(date +%F)
fi
printf 'SACCT\0'
sacct -X -S "$start" -n -P \
    -o JobIDRaw,JobID,JobName,State,Elapsed,Start,End,Submit,WorkDir,NodeList \
    2>/dev/null | awk -F '|' -v prefix="$BASE/" 'index($9, prefix) == 1'
printf '\0'
"""

# Task output is extracted only when the user asks for it. runsolver combines a
# solver's stdout and stderr in the .solver.log member. To keep a mistaken huge
# output from flooding SSH or the terminal, each displayed member is capped at
# 1 MiB, preserving both its beginning and end.
REMOTE_TASK_OUTPUT_SCRIPT = r"""
set -u

JOB=$1
ARCHIVE=$2
TASK_ID=$3
TASK_KEY=$4
LIMIT=1048576

if ! command -v tar >/dev/null || ! command -v base64 >/dev/null; then
    printf 'ERROR\0The cluster needs tar and base64 to display task output.\0'
    exit 0
fi

if [[ ! "$JOB" =~ ^[A-Za-z0-9._-]+$ ]] \
    || [[ ! "$ARCHIVE" =~ ^[A-Za-z0-9._-]+[.]tar[.]gz$ ]] \
    || [[ ! "$TASK_ID" =~ ^[0-9]+$ ]] \
    || [[ ! "$TASK_KEY" =~ ^[A-Fa-f0-9]+$ ]]; then
    printf 'ERROR\0Invalid task-output request.\0'
    exit 0
fi

archive_path="$HOME/SluM/$JOB/results/$ARCHIVE"
if [[ ! -f "$archive_path" ]]; then
    printf 'ERROR\0Result archive not found: %s\0' "$ARCHIVE"
    exit 0
fi

stem=$(printf 'task_%09d_%s' "$TASK_ID" "$TASK_KEY")
emit_member() {
    local kind=$1 suffix=$2 member="$stem.$2" size truncated half
    if ! tar -tzf "$archive_path" "$member" >/dev/null 2>&1; then
        member="./$member"
        if ! tar -tzf "$archive_path" "$member" >/dev/null 2>&1; then
            printf 'TASK_OUTPUT\0%s\00\0false\0\0' "$kind"
            return
        fi
    fi
    size=$(tar -tvzf "$archive_path" "$member" 2>/dev/null | awk 'NR == 1 {print $3}')
    [[ "$size" =~ ^[0-9]+$ ]] || size=0
    truncated=false
    printf 'TASK_OUTPUT\0%s\0%s\0' "$kind" "$size"
    if (( size > LIMIT )); then
        truncated=true
    fi
    printf '%s\0' "$truncated"
    if [[ "$truncated" == true ]]; then
        half=$((LIMIT / 2))
        {
            tar -xOzf "$archive_path" "$member" 2>/dev/null | head -c "$half" || true
            printf '\n\n--- middle omitted by SluM monitor ---\n\n'
            tar -xOzf "$archive_path" "$member" 2>/dev/null | tail -c "$half" || true
        } | base64 -w0
    else
        tar -xOzf "$archive_path" "$member" 2>/dev/null | base64 -w0
    fi
    printf '\0'
}

emit_member solver solver.log
emit_member controller controller.log
emit_member watcher watcher.log
emit_member variables var
"""


@dataclass
class ResultRecord:
    task_id: int
    complete: bool
    status: str
    return_code: str = ""
    wall_seconds: float | None = None
    cpu_seconds: float | None = None
    max_memory_kib: float | None = None
    task_key: str = ""
    archive: str = ""
    problem: str = ""
    command: str = ""


@dataclass
class TaskDefinition:
    task_id: int
    task_key: str = ""
    system: str = ""
    problem: str = ""
    command: str = ""
    solver_root: str = ""


@dataclass
class BatchProgress:
    batch_id: int
    state: str
    task_id: int | None
    started_epoch: float | None
    updated_epoch: float | None
    finished_count: int = 0


@dataclass
class SlurmRecord:
    display_id: str
    array_job_id: str
    array_task_id: str
    state: str
    elapsed: str
    time_left: str
    job_name: str
    workdir: str
    location: str
    source: str
    start: str = ""
    end: str = ""
    submitted: str = ""


@dataclass
class OutputStream:
    name: str
    content: str
    size: int
    truncated: bool


@dataclass
class TaskOutput:
    job_id: str
    task_id: int
    archive: str
    streams: dict[str, OutputStream]


@dataclass
class JobSnapshot:
    job_id: str
    remote_dir: str
    created_epoch: float
    metadata: dict[str, Any] = field(default_factory=dict)
    completed: int = 0
    wall_sum: float = 0.0
    wall_max: float = 0.0
    status_counts: dict[str, int] = field(default_factory=dict)
    unresolved_counts: dict[str, int] = field(default_factory=dict)
    results: dict[int, ResultRecord] = field(default_factory=dict)
    tasks: dict[int, TaskDefinition] = field(default_factory=dict)
    progress: dict[int, BatchProgress] = field(default_factory=dict)
    submission_offsets: dict[str, int] = field(default_factory=dict)
    slurm: list[SlurmRecord] = field(default_factory=list)
    logs: dict[str, str] = field(default_factory=dict)

    @property
    def task_count(self) -> int:
        return int(self.metadata.get("task_count", 0) or 0)

    @property
    def batch_count(self) -> int:
        # The fallback keeps jobs generated before the batch terminology change visible.
        return int(self.metadata.get("batch_count", self.metadata.get("chunk_count", 0)) or 0)

    @property
    def batch_size(self) -> int:
        return int(self.metadata.get("batch_size", self.metadata.get("chunk_size", 1)) or 1)

    @property
    def job_name(self) -> str:
        name = self.metadata.get("job_name")
        if name:
            return str(name)
        for record in self.slurm:
            if record.job_name:
                return record.job_name
        return "SluM"

    @property
    def issue_count(self) -> int:
        task_issues = sum(
            count
            for status, count in self.status_counts.items()
            if status.lower() not in NORMAL_TASK_RESULTS
        ) + sum(
            count
            for status, count in self.unresolved_counts.items()
            if status.lower() not in NORMAL_TASK_RESULTS
        )
        unresolved_slurm_failure = (
            self.completed < self.task_count
            and not self.active_records
            and any(
                slurm_state_code(record.state) in SLURM_ERROR_STATES
                for record in self.slurm
            )
        )
        return task_issues + int(unresolved_slurm_failure)

    @property
    def percent(self) -> float:
        return 100.0 * self.effective_completed / self.task_count if self.task_count else 0.0

    @property
    def active_batch_ids(self) -> set[int]:
        batches: set[int] = set()
        for record in self.active_records:
            if record.state.upper() not in {"RUNNING", "COMPLETING"}:
                continue
            if record.array_task_id.isdigit():
                batches.add(
                    self.submission_offsets.get(record.array_job_id, 0)
                    + int(record.array_task_id)
                )
        return batches

    @property
    def effective_completed(self) -> int:
        live = sum(
            progress.finished_count
            for batch_id, progress in self.progress.items()
            if batch_id in self.active_batch_ids
        )
        return min(self.task_count, self.completed + live)

    @property
    def active_records(self) -> list[SlurmRecord]:
        return [record for record in self.slurm if record.source == "queue"]

    @property
    def state(self) -> str:
        active_states = {record.state.upper() for record in self.active_records}
        if "RUNNING" in active_states or "COMPLETING" in active_states:
            return "RUNNING"
        if active_states:
            return "PENDING"
        if self.task_count and self.completed >= self.task_count:
            return "DONE" if not self.issue_count else "DONE · ERRORS"
        historical_states = {slurm_state_code(record.state) for record in self.slurm}
        if historical_states & SLURM_INCOMPLETE_STATES:
            return "INCOMPLETE"
        if self.completed:
            return "INCOMPLETE"
        return "SUBMITTED" if self.submission_offsets or self.slurm else "UNKNOWN"


@dataclass
class ClusterSnapshot:
    host: str
    remote_home: str
    detail_job: str
    fetched_epoch: float
    jobs: list[JobSnapshot]

    def find_job(self, job_id: str | None) -> JobSnapshot | None:
        return next((job for job in self.jobs if job.job_id == job_id), None)


PROTOCOL_ARITY = {
    "REMOTE_HOME": 1,
    "DETAIL": 1,
    "JOB": 2,
    "METADATA": 2,
    "SUMMARY": 2,
    "LEGACY_RESULTS": 2,
    "PROGRESS": 2,
    "SUBMISSION": 2,
    "RESULTS_TSV": 2,
    "RESULTS_JSONL": 2,
    "MANIFEST": 2,
    "TASKDEF": 7,
    "LOG": 3,
    "SQUEUE": 1,
    "SACCT": 1,
}


def decode_protocol(data: bytes) -> list[tuple[str, list[str]]]:
    parts = data.split(b"\0")
    records: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(parts) and parts[index]:
        kind = parts[index].decode("utf-8", "replace")
        index += 1
        if kind not in PROTOCOL_ARITY:
            raise RuntimeError(f"unknown response record from cluster: {kind!r}")
        arity = PROTOCOL_ARITY[kind]
        if index + arity > len(parts):
            raise RuntimeError(f"truncated {kind} record from cluster")
        fields = [part.decode("utf-8", "replace") for part in parts[index : index + arity]]
        index += arity
        records.append((kind, fields))
    return records


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_tsv_results(payload: str) -> dict[int, ResultRecord]:
    results: dict[int, ResultRecord] = {}
    for line in payload.splitlines():
        fields = line.split("\t")
        if len(fields) < 15 or not fields[0].isdigit():
            continue
        task_id = int(fields[0])
        results[task_id] = ResultRecord(
            task_id=task_id,
            complete=fields[1].lower() == "true",
            status=fields[2],
            return_code=fields[3],
            wall_seconds=as_float(fields[4]),
            cpu_seconds=as_float(fields[5]),
            max_memory_kib=as_float(fields[10]),
            task_key=fields[13],
            archive=fields[14],
        )
    return results


def parse_jsonl_results(payload: str) -> dict[int, ResultRecord]:
    results: dict[int, ResultRecord] = {}
    for line in payload.splitlines():
        try:
            item = json.loads(line)
            task_id = int(item["task_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        results[task_id] = ResultRecord(
            task_id=task_id,
            complete=bool(item.get("complete")),
            status=str(item.get("status", "unknown")),
            return_code=str(item.get("return_code", "")),
            wall_seconds=as_float(item.get("wall_seconds")),
            cpu_seconds=as_float(item.get("cpu_seconds")),
            max_memory_kib=as_float(item.get("max_memory_kib")),
            task_key=str(item.get("task_key", "")),
            archive=str(item.get("archive", "")),
            problem=str(item.get("problem", "")),
            command=str(item.get("command", "")),
        )
    return results


def summarize_results(job: JobSnapshot, results: dict[int, ResultRecord]) -> None:
    completed = [result for result in results.values() if result.complete]
    job.completed = len(completed)
    job.wall_sum = sum(result.wall_seconds or 0.0 for result in completed)
    job.wall_max = max((result.wall_seconds or 0.0 for result in completed), default=0.0)
    counts: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    for result in results.values():
        target = counts if result.complete else unresolved
        target[result.status] = target.get(result.status, 0) + 1
    job.status_counts = counts
    job.unresolved_counts = unresolved


def parse_summary(job: JobSnapshot, payload: str) -> None:
    for line in payload.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == "completed":
            job.completed = int(fields[1])
        elif len(fields) == 2 and fields[0] == "wall_sum":
            job.wall_sum = float(fields[1])
        elif len(fields) == 2 and fields[0] == "wall_max":
            job.wall_max = float(fields[1])
        elif len(fields) == 3 and fields[0] == "status":
            job.status_counts[fields[1]] = int(fields[2])
        elif len(fields) == 3 and fields[0] == "unresolved":
            job.unresolved_counts[fields[1]] = int(fields[2])


def parse_slurm_lines(payload: str, source: str) -> list[SlurmRecord]:
    records: list[SlurmRecord] = []
    for line in payload.splitlines():
        fields = line.rstrip("|").split("|")
        if source == "queue" and len(fields) >= 9:
            records.append(
                SlurmRecord(
                    display_id=fields[0],
                    array_job_id=fields[1],
                    array_task_id=fields[2],
                    state=fields[3],
                    elapsed=fields[4],
                    time_left=fields[5],
                    job_name=fields[6],
                    workdir=fields[7],
                    location="|".join(fields[8:]),
                    source=source,
                )
            )
        elif source == "accounting" and len(fields) >= 10:
            job_id = fields[1]
            array_job_id, _, array_task_id = job_id.partition("_")
            records.append(
                SlurmRecord(
                    display_id=job_id or fields[0],
                    array_job_id=array_job_id,
                    array_task_id=array_task_id or "N/A",
                    state=fields[3],
                    elapsed=fields[4],
                    time_left="—",
                    job_name=fields[2],
                    workdir=fields[8],
                    location=fields[9] or "—",
                    source=source,
                    start=fields[5],
                    end=fields[6],
                    submitted=fields[7],
                )
            )
    return records


def parse_snapshot(host: str, data: bytes) -> ClusterSnapshot:
    protocol = decode_protocol(data)
    remote_home = ""
    detail_job = ""
    jobs: dict[str, JobSnapshot] = {}
    pending_metadata: dict[str, str] = {}
    summaries: dict[str, str] = {}
    legacy_overviews: dict[str, str] = {}
    detail_tsv: dict[str, str] = {}
    detail_jsonl: dict[str, str] = {}
    manifests: dict[str, str] = {}
    taskdefs: dict[str, list[list[str]]] = {}
    progress_payloads: dict[str, str] = {}
    submissions: dict[str, str] = {}
    logs: dict[str, dict[str, str]] = {}
    queue_payload = ""
    accounting_payload = ""

    for kind, fields in protocol:
        if kind == "REMOTE_HOME":
            remote_home = fields[0]
        elif kind == "DETAIL":
            detail_job = fields[0]
        elif kind == "JOB":
            job_id, created = fields
            jobs[job_id] = JobSnapshot(
                job_id=job_id,
                remote_dir=f"{remote_home}/{job_id}" if remote_home else job_id,
                created_epoch=as_float(created) or 0.0,
            )
        elif kind == "METADATA":
            pending_metadata[fields[0]] = fields[1]
        elif kind == "SUMMARY":
            summaries[fields[0]] = fields[1]
        elif kind == "LEGACY_RESULTS":
            legacy_overviews[fields[0]] = fields[1]
        elif kind == "PROGRESS":
            progress_payloads[fields[0]] = fields[1]
        elif kind == "SUBMISSION":
            submissions[fields[0]] = fields[1]
        elif kind == "RESULTS_TSV":
            detail_tsv[fields[0]] = fields[1]
        elif kind == "RESULTS_JSONL":
            detail_jsonl[fields[0]] = fields[1]
        elif kind == "MANIFEST":
            manifests[fields[0]] = fields[1]
        elif kind == "TASKDEF":
            taskdefs.setdefault(fields[0], []).append(fields[1:])
        elif kind == "LOG":
            logs.setdefault(fields[0], {})[fields[1]] = fields[2]
        elif kind == "SQUEUE":
            queue_payload = fields[0]
        elif kind == "SACCT":
            accounting_payload = fields[0]

    for job_id, job in jobs.items():
        try:
            job.metadata = json.loads(pending_metadata.get(job_id, "{}"))
        except json.JSONDecodeError:
            job.metadata = {}
        if job_id in summaries:
            parse_summary(job, summaries[job_id])
        elif job_id in legacy_overviews:
            summarize_results(job, parse_jsonl_results(legacy_overviews[job_id]))

        for line in progress_payloads.get(job_id, "").splitlines():
            fields = line.split("\t")
            if len(fields) < 5:
                continue
            match = re.search(r"(?:batch|chunk)_(\d+)\.tsv$", fields[0])
            if not match:
                continue
            job.progress[int(match.group(1))] = BatchProgress(
                batch_id=int(match.group(1)),
                state=fields[1],
                task_id=int(fields[2]) if fields[2].isdigit() else None,
                started_epoch=as_float(fields[3]),
                updated_epoch=as_float(fields[4]),
                finished_count=int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 0,
            )

        for line in submissions.get(job_id, "").splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1].isdigit():
                job.submission_offsets[fields[0]] = int(fields[1])

        if job_id == detail_job:
            results = parse_tsv_results(detail_tsv.get(job_id, ""))
            results.update(parse_jsonl_results(detail_jsonl.get(job_id, "")))
            job.results = results
            if results:
                summarize_results(job, results)

            for values in taskdefs.get(job_id, []):
                task_id, key, system, problem, command, solver_root = values
                if task_id.isdigit():
                    job.tasks[int(task_id)] = TaskDefinition(
                        task_id=int(task_id),
                        task_key=key,
                        system=system,
                        problem=problem,
                        command=command,
                        solver_root=solver_root,
                    )
            for line in manifests.get(job_id, "").splitlines():
                try:
                    item = json.loads(line)
                    task_id = int(item["task_id"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                job.tasks[task_id] = TaskDefinition(
                    task_id=task_id,
                    task_key=str(item.get("task_key", "")),
                    system=str(item.get("system", "")),
                    problem=str(item.get("problem", "")),
                    command=str(item.get("command", "")),
                    solver_root=str(item.get("solver_root", "")),
                )
            for task_id, result in results.items():
                definition = job.tasks.setdefault(task_id, TaskDefinition(task_id))
                if not definition.problem and result.problem:
                    definition.problem = result.problem
                if not definition.command and result.command:
                    definition.command = result.command
                if not definition.task_key and result.task_key:
                    definition.task_key = result.task_key
            for definition in job.tasks.values():
                if not definition.system:
                    definition.system = inferred_system_name(definition.command)
            job.logs = logs.get(job_id, {})

    all_slurm = parse_slurm_lines(queue_payload, "queue") + parse_slurm_lines(
        accounting_payload, "accounting"
    )
    for record in all_slurm:
        job_id = PurePosixPath(record.workdir).name
        if job_id in jobs:
            jobs[job_id].slurm.append(record)

    return ClusterSnapshot(
        host=host,
        remote_home=remote_home,
        detail_job=detail_job,
        fetched_epoch=time.time(),
        jobs=sorted(jobs.values(), key=lambda item: item.created_epoch, reverse=True),
    )


def parse_task_output(job_id: str, task_id: int, archive: str, data: bytes) -> TaskOutput:
    parts = data.split(b"\0")
    streams: dict[str, OutputStream] = {}
    labels = {
        "solver": "Solver stdout + stderr",
        "controller": "Runsolver controller",
        "watcher": "Runsolver watcher",
        "variables": "Runsolver variables",
    }
    index = 0
    while index < len(parts) and parts[index]:
        kind = parts[index].decode("utf-8", "replace")
        index += 1
        if kind == "ERROR":
            if index >= len(parts):
                raise RuntimeError("truncated task-output error from cluster")
            raise RuntimeError(parts[index].decode("utf-8", "replace"))
        if kind != "TASK_OUTPUT" or index + 4 > len(parts):
            raise RuntimeError(f"invalid task-output response from cluster: {kind!r}")
        stream_kind = parts[index].decode("utf-8", "replace")
        size_text = parts[index + 1].decode("ascii", "replace")
        truncated_text = parts[index + 2].decode("ascii", "replace")
        encoded = parts[index + 3]
        index += 4
        try:
            raw = base64.b64decode(encoded, validate=True) if encoded else b""
        except ValueError as exc:
            raise RuntimeError(f"invalid base64 for {stream_kind} output") from exc
        streams[stream_kind] = OutputStream(
            name=labels.get(stream_kind, stream_kind),
            content=raw.decode("utf-8", "replace"),
            size=int(size_text) if size_text.isdigit() else len(raw),
            truncated=truncated_text == "true",
        )
    return TaskOutput(job_id=job_id, task_id=task_id, archive=archive, streams=streams)


class RemoteCollector:
    def __init__(self, host: str, timeout: float) -> None:
        self.host = host
        self.timeout = timeout

    def fetch(self, detail_job: str | None) -> ClusterSnapshot:
        if shutil.which("ssh") is None:
            raise RuntimeError("ssh is not installed or not on PATH")
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(self.timeout))}",
            "--",
            self.host,
            "bash",
            "-s",
            "--",
            detail_job or "",
        ]
        try:
            process = subprocess.run(
                command,
                input=REMOTE_SNAPSHOT_SCRIPT.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout + 20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"SSH refresh timed out after {self.timeout + 20:g}s") from exc
        if process.returncode != 0:
            message = process.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or f"ssh exited with status {process.returncode}")
        return parse_snapshot(self.host, process.stdout)

    def fetch_task_output(self, job_id: str, result: ResultRecord) -> TaskOutput:
        if not result.archive or not result.task_key:
            raise RuntimeError("this task has no saved result output yet")
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(self.timeout))}",
            "--",
            self.host,
            "bash",
            "-s",
            "--",
            job_id,
            result.archive,
            str(result.task_id),
            result.task_key,
        ]
        try:
            process = subprocess.run(
                command,
                input=REMOTE_TASK_OUTPUT_SCRIPT.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"task-output request timed out after {self.timeout + 30:g}s") from exc
        if process.returncode != 0:
            message = process.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or f"ssh exited with status {process.returncode}")
        return parse_task_output(job_id, result.task_id, result.archive, process.stdout)


def format_duration(seconds: float | None, *, precise: bool = False) -> str:
    if seconds is None:
        return "—"
    seconds = max(0.0, seconds)
    if precise and 0 < seconds < 0.001:
        return f"{seconds * 1_000_000:.0f} µs"
    if precise and seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    if precise and seconds < 10:
        return f"{seconds:.3f} s"
    if precise and seconds < 60:
        return f"{seconds:.1f} s"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_memory(kib: float | None) -> str:
    if kib is None:
        return "—"
    kib = max(0.0, kib)
    if kib < 1:
        byte_count = kib * 1024
        if byte_count.is_integer() or byte_count >= 10:
            return f"{byte_count:.0f} B"
        return f"{byte_count:.1f} B"
    if kib >= 1024 * 1024:
        return f"{kib / (1024 * 1024):.2f} GiB"
    if kib >= 1024:
        return f"{kib / 1024:.1f} MiB"
    return f"{kib:.0f} KiB"


def format_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def format_datetime(epoch: float) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def state_text(state: str) -> Text:
    upper = state.upper()
    if upper.startswith("DONE") or upper in {"OK", "COMPLETED", "FINISHED"}:
        style = "bold #52d6a3"
    elif upper in {"RUNNING", "SAVING", "COMPLETING"}:
        style = "bold #55c2ff"
    elif upper in {"PENDING", "QUEUED", "SUBMITTED", "STARTING", "TIME-LIMIT", "MEMORY-LIMIT"}:
        style = "bold #f4c95d"
    elif upper in {"UNKNOWN", "NOT RUN"}:
        style = "#8491aa"
    else:
        style = "bold #ff6b81"
    return Text(state, style=style)


def benchmark_name(path: str) -> str:
    return PurePosixPath(path).name if path else "—"


def inferred_system_name(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        return "—"
    while words and "=" in words[0] and not words[0].startswith(("/", "./")):
        words.pop(0)
    if words and words[0] == "env":
        words.pop(0)
        while words and "=" in words[0]:
            words.pop(0)
    return PurePosixPath(words[0]).name if words else "—"


def parse_iso_epoch(value: str) -> float | None:
    if not value or value in {"Unknown", "None", "N/A"}:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class SlumMonitorApp(App[None]):
    TITLE = "SluM Monitor"
    SUB_TITLE = "Slurm solver experiments"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: #09101f;
        color: #dce7f7;
    }
    Header {
        background: #101b33;
        color: #f2f7ff;
    }
    #main {
        padding: 0 1;
    }
    #connection {
        height: 1;
        color: #8fa5c7;
        margin: 0 1;
    }
    #cards {
        height: 5;
        layout: grid;
        grid-size: 4 1;
        grid-gutter: 1;
        margin: 0 0 1 0;
    }
    .card {
        height: 5;
        padding: 0 1;
        border: round #25395d;
        background: #101a30;
        content-align: center middle;
    }
    .card-primary { border: round #3d8bfd; }
    .card-good { border: round #36b987; }
    .card-warn { border: round #e3ae45; }
    TabbedContent {
        height: 1fr;
    }
    Tabs {
        background: #101a30;
        color: #91a4c3;
    }
    Tab.-active {
        color: #ffffff;
        text-style: bold;
    }
    TabPane {
        padding: 1;
        background: #0c1427;
    }
    Input {
        height: 3;
        margin-bottom: 1;
        border: tall #25395d;
        background: #0a1222;
    }
    Input:focus { border: tall #3d8bfd; }
    DataTable {
        height: 1fr;
        background: #0c1427;
        color: #dce7f7;
        border: round #25395d;
    }
    DataTable > .datatable--header {
        background: #182743;
        color: #ffffff;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #244876;
        color: #ffffff;
    }
    #job-summary {
        height: 3;
        padding: 0 1;
        color: #bad2f2;
    }
    #job-progress {
        height: 2;
        margin: 0 1 1 1;
    }
    #task-detail {
        height: 8;
        margin-top: 1;
        padding: 1;
        border: round #25395d;
        background: #0a1222;
        overflow: auto;
    }
    #log-layout, #output-layout {
        height: 1fr;
    }
    #log-files, #output-kinds {
        width: 34;
        margin-right: 1;
    }
    #output-summary {
        height: 4;
        padding: 0 1;
        color: #bad2f2;
    }
    #log-body, #output-body, #details-body {
        width: 1fr;
        height: 1fr;
        border: round #25395d;
        background: #070d19;
        padding: 0 1;
    }
    #slurm-summary {
        height: 3;
        padding: 0 1;
        color: #bad2f2;
    }
    Footer {
        background: #101b33;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("1", "show_tab('jobs-tab')", "Jobs"),
        ("2", "show_tab('tasks-tab')", "Tasks"),
        ("3", "show_tab('output-tab')", "Output"),
        ("4", "show_tab('slurm-tab')", "Slurm"),
        ("5", "show_tab('logs-tab')", "Logs"),
        ("6", "show_tab('details-tab')", "Details"),
        ("o", "open_output", "Task output"),
    ]

    def __init__(
        self,
        host: str,
        refresh_seconds: float,
        ssh_timeout: float,
        initial_job: str | None = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.refresh_seconds = refresh_seconds
        self.collector = RemoteCollector(host, ssh_timeout)
        self.selected_job_id = initial_job
        self.selected_task_id: int | None = None
        self.selected_log = ""
        self.selected_output_kind = "solver"
        self.current_output_key: tuple[str, int, str] | None = None
        self.output_cache: dict[tuple[str, int, str], TaskOutput] = {}
        self.snapshot: ClusterSnapshot | None = None
        self.refreshing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            yield Static(f"Connecting to {self.host}…", id="connection")
            with Horizontal(id="cards"):
                yield Static("[b]—[/b]\nSluM jobs", id="card-jobs", classes="card card-primary")
                yield Static("[b]—[/b]\nactive", id="card-active", classes="card")
                yield Static("[b]—[/b]\ntasks finished", id="card-tasks", classes="card card-good")
                yield Static("[b]—[/b]\nexecution errors", id="card-issues", classes="card card-warn")
            with TabbedContent(initial="jobs-tab", id="tabs"):
                with TabPane("1  Jobs", id="jobs-tab"):
                    yield Input(placeholder="Filter by job ID, name, or state…", id="job-filter")
                    yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
                with TabPane("2  Tasks", id="tasks-tab"):
                    yield Static("Select a job from the Jobs tab.", id="job-summary")
                    yield ProgressBar(total=100, show_eta=False, id="job-progress")
                    yield Input(placeholder="Filter tasks by ID, problem, command, or state…", id="task-filter")
                    yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
                    yield Static("Select a task to see its command and result.", id="task-detail")
                with TabPane("3  Output", id="output-tab"):
                    yield Static(
                        "Select a saved task, then press Enter or O to load its output.",
                        id="output-summary",
                    )
                    with Horizontal(id="output-layout"):
                        yield DataTable(id="output-kinds", cursor_type="row", zebra_stripes=True)
                        yield RichLog(
                            id="output-body",
                            wrap=False,
                            highlight=True,
                            markup=False,
                            auto_scroll=False,
                        )
                with TabPane("4  Slurm", id="slurm-tab"):
                    yield Static("Select a job from the Jobs tab.", id="slurm-summary")
                    yield DataTable(id="slurm", cursor_type="row", zebra_stripes=True)
                with TabPane("5  Logs", id="logs-tab"):
                    with Horizontal(id="log-layout"):
                        yield DataTable(id="log-files", cursor_type="row", zebra_stripes=True)
                        yield RichLog(id="log-body", wrap=True, highlight=True, markup=False)
                with TabPane("6  Details", id="details-tab"):
                    yield RichLog(id="details-body", wrap=True, highlight=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jobs", DataTable).add_columns(
            "Name", "State", "Progress", "Active", "Errors", "Elapsed", "Submitted", "SluM ID"
        )
        self.query_one("#tasks", DataTable).add_columns(
            "Task", "State", "System", "Benchmark", "Wall time", "CPU time", "Peak memory", "Exit"
        )
        self.query_one("#slurm", DataTable).add_columns(
            "Slurm ID", "Batch", "State", "Elapsed", "Left", "Node / reason", "Source"
        )
        self.query_one("#log-files", DataTable).add_columns("Slurm log", "Lines")
        self.query_one("#output-kinds", DataTable).add_columns("Task output", "Size")
        self.set_interval(self.refresh_seconds, self.request_refresh)
        self.request_refresh()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id
        if tab_id == "output-tab":
            self.request_task_output()

    def action_open_output(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "output-tab"
        self.request_task_output()

    def ui_available(self) -> bool:
        """Return false while Textual is dismantling the screen on exit."""

        try:
            self.query_one("#tabs", TabbedContent)
        except NoMatches:
            return False
        return True

    def action_refresh(self) -> None:
        self.request_refresh(force=True)

    def request_refresh(self, force: bool = False) -> None:
        if self.refreshing and not force:
            return
        self.refreshing = True
        try:
            self.query_one("#connection", Static).update(
                f"[bold #55c2ff]●[/] Refreshing from [b]{self.host}[/b]…"
            )
        except NoMatches:
            self.refreshing = False
            return
        self.run_worker(self._fetch_snapshot, thread=True, exclusive=force, group="ssh-refresh")

    def _fetch_snapshot(self) -> None:
        try:
            snapshot = self.collector.fetch(self.selected_job_id)
        except Exception as exc:  # surfaced in the dashboard, not a worker traceback
            self.call_from_thread(self._apply_error, str(exc))
        else:
            self.call_from_thread(self._apply_snapshot, snapshot)

    def _apply_error(self, message: str) -> None:
        self.refreshing = False
        try:
            self.query_one("#connection", Static).update(
                f"[bold #ff6b81]● Connection failed[/]  {message}  [dim]Press R to retry[/dim]"
            )
        except NoMatches:
            return
        self.notify(message, title="Could not refresh", severity="error", timeout=8)

    def _apply_snapshot(self, snapshot: ClusterSnapshot) -> None:
        self.refreshing = False
        self.snapshot = snapshot
        if not self.selected_job_id or not snapshot.find_job(self.selected_job_id):
            self.selected_job_id = snapshot.detail_job or (snapshot.jobs[0].job_id if snapshot.jobs else None)
        stamp = datetime.fromtimestamp(snapshot.fetched_epoch).strftime("%H:%M:%S")
        try:
            self.query_one("#connection", Static).update(
                f"[bold #52d6a3]● Connected[/] · {self.host} · [dim]Updated {stamp} · auto-refresh {self.refresh_seconds:g}s[/dim]"
            )
        except NoMatches:
            return
        try:
            self.render_all()
        except NoMatches:
            return
        if self.selected_job_id != snapshot.detail_job:
            self.request_refresh()

    def render_all(self) -> None:
        self.render_cards()
        self.render_jobs()
        self.render_selected_job()

    def render_cards(self) -> None:
        jobs = self.snapshot.jobs if self.snapshot else []
        active = sum(job.state in {"RUNNING", "PENDING", "SUBMITTED"} for job in jobs)
        finished = sum(job.effective_completed for job in jobs)
        total = sum(job.task_count for job in jobs)
        issues = sum(job.issue_count for job in jobs)
        self.query_one("#card-jobs", Static).update(f"[b #75a9ff]{len(jobs)}[/]\nSluM jobs")
        self.query_one("#card-active", Static).update(f"[b #55c2ff]{active}[/]\nactive")
        self.query_one("#card-tasks", Static).update(f"[b #52d6a3]{finished:,} / {total:,}[/]\ntasks finished")
        self.query_one("#card-issues", Static).update(f"[b #f4c95d]{issues:,}[/]\nexecution errors")

    def render_jobs(self) -> None:
        table = self.query_one("#jobs", DataTable)
        table.clear(columns=False)
        if not self.snapshot:
            return
        needle = self.query_one("#job-filter", Input).value.casefold().strip()
        selected_row = 0
        visible_index = 0
        now = time.time()
        for job in self.snapshot.jobs:
            haystack = f"{job.job_id} {job.job_name} {job.state}".casefold()
            if needle and needle not in haystack:
                continue
            if job.job_id == self.selected_job_id:
                selected_row = visible_index
            active = sum(record.state.upper() in {"RUNNING", "COMPLETING"} for record in job.active_records)
            end_times = [parse_iso_epoch(record.end) for record in job.slurm if record.end]
            end_epoch = max((value for value in end_times if value), default=None)
            elapsed = (end_epoch or now) - job.created_epoch if job.created_epoch else None
            table.add_row(
                job.job_name,
                state_text(job.state),
                f"{job.effective_completed:,}/{job.task_count:,} · {job.percent:.1f}%",
                str(active),
                str(job.issue_count),
                format_duration(elapsed),
                format_datetime(job.created_epoch),
                job.job_id,
                key=job.job_id,
            )
            visible_index += 1
        if table.row_count:
            table.move_cursor(row=min(selected_row, table.row_count - 1))

    def selected_job(self) -> JobSnapshot | None:
        return self.snapshot.find_job(self.selected_job_id) if self.snapshot else None

    def active_batch_ids(self, job: JobSnapshot) -> set[int]:
        return job.active_batch_ids

    def live_finished_task_ids(self, job: JobSnapshot) -> set[int]:
        batch_size = job.batch_size
        finished: set[int] = set()
        for batch_id in job.active_batch_ids:
            progress = job.progress.get(batch_id)
            if not progress or not progress.finished_count:
                continue
            first = batch_id * batch_size
            last = min(job.task_count, first + batch_size)
            candidates = [
                task_id
                for task_id in range(first, last)
                if not (job.results.get(task_id) and job.results[task_id].complete)
            ]
            finished.update(candidates[: progress.finished_count])
        return finished

    def task_state(self, job: JobSnapshot, task_id: int) -> tuple[str, float | None]:
        result = job.results.get(task_id)
        if result:
            return result.status, result.wall_seconds
        batch_id = task_id // job.batch_size
        progress = job.progress.get(batch_id)
        if progress and progress.task_id == task_id and batch_id in self.active_batch_ids(job):
            running_for = time.time() - progress.started_epoch if progress.started_epoch else None
            return progress.state, running_for
        if task_id in self.live_finished_task_ids(job):
            return "finished", None
        if job.state == "RUNNING":
            return "queued", None
        if job.state in {"PENDING", "SUBMITTED"}:
            return "pending", None
        return "not run", None

    def render_selected_job(self) -> None:
        job = self.selected_job()
        try:
            progress_bar = self.query_one("#job-progress", ProgressBar)
        except NoMatches:
            return
        task_table = self.query_one("#tasks", DataTable)
        slurm_table = self.query_one("#slurm", DataTable)
        log_table = self.query_one("#log-files", DataTable)
        output_table = self.query_one("#output-kinds", DataTable)
        task_table.clear(columns=False)
        slurm_table.clear(columns=False)
        log_table.clear(columns=False)
        output_table.clear(columns=False)
        if not job:
            self.query_one("#job-summary", Static).update("No SluM jobs found on this host.")
            progress_bar.update(total=100, progress=0)
            self.query_one("#slurm-summary", Static).update("No job selected.")
            self.query_one("#log-body", RichLog).clear()
            self.query_one("#output-summary", Static).update("No job selected.")
            self.query_one("#output-body", RichLog).clear()
            self.query_one("#details-body", RichLog).clear()
            return

        self.query_one("#job-summary", Static).update(
            f"[b]{job.job_name}[/b]  [dim]{job.job_id}[/dim]\n"
            f"{job.effective_completed:,} of {job.task_count:,} tasks finished · {job.percent:.1f}% · "
            f"{job.completed:,} saved · {job.issue_count:,} execution errors · "
            f"{job.batch_count:,} Slurm array elements"
        )
        progress_bar.update(total=max(1, job.task_count), progress=job.effective_completed)
        self.render_tasks(job)
        self.render_slurm(job)
        self.render_logs(job)
        self.render_output_panel(job)
        self.render_details(job)

    def render_tasks(self, job: JobSnapshot) -> None:
        table = self.query_one("#tasks", DataTable)
        needle = self.query_one("#task-filter", Input).value.casefold().strip()
        definitions = dict(job.tasks)
        for task_id in range(job.task_count):
            definitions.setdefault(task_id, TaskDefinition(task_id))
        selected_row = 0
        visible_index = 0
        for task_id in sorted(definitions):
            definition = definitions[task_id]
            result = job.results.get(task_id)
            state, duration = self.task_state(job, task_id)
            problem = definition.problem or (result.problem if result else "")
            command = definition.command or (result.command if result else "")
            system = definition.system or inferred_system_name(command)
            haystack = f"{task_id} {state} {system} {problem} {command}".casefold()
            if needle and needle not in haystack:
                continue
            if task_id == self.selected_task_id:
                selected_row = visible_index
            table.add_row(
                str(task_id),
                state_text(state),
                system,
                benchmark_name(problem),
                format_duration(duration, precise=True),
                format_duration(result.cpu_seconds, precise=True) if result else "—",
                format_memory(result.max_memory_kib) if result else "—",
                result.return_code if result and result.return_code else "—",
                key=str(task_id),
            )
            visible_index += 1
        if table.row_count:
            table.move_cursor(row=min(selected_row, table.row_count - 1))
            if self.selected_task_id is None:
                first_key = table.get_row_at(0)[0]
                try:
                    self.selected_task_id = int(str(first_key))
                except ValueError:
                    pass
        self.render_task_detail(job)

    def render_task_detail(self, job: JobSnapshot) -> None:
        task_id = self.selected_task_id
        if task_id is None:
            self.query_one("#task-detail", Static).update("Select a task to see its command and result.")
            return
        definition = job.tasks.get(task_id, TaskDefinition(task_id))
        result = job.results.get(task_id)
        state, duration = self.task_state(job, task_id)
        problem = definition.problem or (result.problem if result else "—")
        command = definition.command or (result.command if result else "—")
        system = definition.system or inferred_system_name(command)
        archive = result.archive if result and result.archive else "—"
        key = definition.task_key or (result.task_key if result else "—")
        detail = Text()
        detail.append(f"Task {task_id}", style="bold")
        detail.append("  ")
        detail.append_text(state_text(state))
        detail.append(f"  key {key}\n", style="dim")
        detail.append("System: ", style="bold")
        detail.append(f"{system}    ")
        detail.append("Benchmark: ", style="bold")
        detail.append(f"{problem}\n")
        detail.append("Command: ", style="bold")
        detail.append(f"{command}\n")
        detail.append("Wall time: ", style="bold")
        detail.append(format_duration(duration, precise=True))
        detail.append("    CPU time: ", style="bold")
        detail.append(format_duration(result.cpu_seconds, precise=True) if result else "—")
        detail.append("    Peak memory: ", style="bold")
        detail.append(format_memory(result.max_memory_kib) if result else "—")
        detail.append("\nResult archive: ", style="bold")
        detail.append(f"{archive}\n")
        if result and result.archive and result.task_key:
            detail.append("Press Enter or O to inspect solver stdout/stderr and runsolver diagnostics.", style="#75a9ff")
        else:
            detail.append("Output is available after this task's result archive is saved.", style="dim")
        self.query_one("#task-detail", Static).update(detail)

    def selected_output_request(
        self,
    ) -> tuple[tuple[str, int, str], JobSnapshot, ResultRecord] | None:
        job = self.selected_job()
        if not job or self.selected_task_id is None:
            return None
        result = job.results.get(self.selected_task_id)
        if not result or not result.archive or not result.task_key:
            return None
        key = (job.job_id, result.task_id, result.archive)
        return key, job, result

    def render_output_panel(self, job: JobSnapshot) -> None:
        table = self.query_one("#output-kinds", DataTable)
        body = self.query_one("#output-body", RichLog)
        table.clear(columns=False)
        request = self.selected_output_request()
        if not request:
            self.current_output_key = None
            self.query_one("#output-summary", Static).update(
                "Select a completed task with a saved result, then press Enter or O.\n"
                "Solver output appears after its result archive has been written."
            )
            body.clear()
            body.write(Text("No saved task output selected.", style="dim"))
            return

        key, _, result = request
        self.current_output_key = key
        output = self.output_cache.get(key)
        self.query_one("#output-summary", Static).update(
            f"[b]{job.job_name} · task {result.task_id}[/b]\n"
            "runsolver stores the solver's stdout and stderr together in Solver output."
        )
        if not output:
            body.clear()
            body.write(Text("Press Enter or O to load this task's archived output.", style="dim"))
            return

        kinds = ["solver", "controller", "watcher", "variables"]
        if self.selected_output_kind not in output.streams:
            self.selected_output_kind = "solver"
        selected_row = 0
        for index, kind in enumerate(kinds):
            stream = output.streams.get(kind)
            if not stream:
                continue
            if kind == self.selected_output_kind:
                selected_row = index
            size = format_bytes(stream.size)
            if stream.truncated:
                size += " · clipped"
            table.add_row(stream.name, size, key=kind)
        if table.row_count:
            table.move_cursor(row=min(selected_row, table.row_count - 1))
        self.render_output_body(output)

    def render_output_body(self, output: TaskOutput) -> None:
        body = self.query_one("#output-body", RichLog)
        body.clear()
        stream = output.streams.get(self.selected_output_kind)
        if not stream:
            body.write(Text("This output member is not present in the result archive.", style="dim"))
            return
        heading = f"{stream.name} · {format_bytes(stream.size)}"
        if stream.truncated:
            heading += " · first and last 512 KiB shown"
        body.write(Text(f"{heading}\n", style="bold #75a9ff"))
        body.write(Text(stream.content or "(empty)", style="dim" if not stream.content else ""))

    def request_task_output(self) -> None:
        if not self.ui_available():
            return
        request = self.selected_output_request()
        if not request:
            job = self.selected_job()
            if job:
                self.render_output_panel(job)
            return
        key, job, result = request
        self.current_output_key = key
        cached = self.output_cache.get(key)
        if cached:
            self.render_output_panel(job)
            return
        self.query_one("#output-summary", Static).update(
            f"[b]{job.job_name} · task {result.task_id}[/b]\n"
            f"[bold #55c2ff]Loading archived task output from {self.host}…[/]"
        )
        body = self.query_one("#output-body", RichLog)
        body.clear()
        body.write(Text("Extracting only this task's log members on the cluster…", style="dim"))
        self.run_worker(
            lambda: self._fetch_task_output(key, job.job_id, result),
            thread=True,
            exclusive=True,
            group="task-output",
        )

    def _fetch_task_output(
        self,
        key: tuple[str, int, str],
        job_id: str,
        result: ResultRecord,
    ) -> None:
        try:
            output = self.collector.fetch_task_output(job_id, result)
        except Exception as exc:  # surfaced in the output tab
            self.call_from_thread(self._apply_task_output_error, key, str(exc))
        else:
            self.call_from_thread(self._apply_task_output, key, output)

    def _apply_task_output(self, key: tuple[str, int, str], output: TaskOutput) -> None:
        self.output_cache[key] = output
        if not self.ui_available() or key != self.current_output_key:
            return
        job = self.selected_job()
        if job:
            self.render_output_panel(job)

    def _apply_task_output_error(self, key: tuple[str, int, str], message: str) -> None:
        if not self.ui_available() or key != self.current_output_key:
            return
        self.query_one("#output-summary", Static).update(
            "[bold #ff6b81]Could not load this task's output.[/]"
        )
        body = self.query_one("#output-body", RichLog)
        body.clear()
        body.write(Text(message, style="bold #ff6b81"))
        self.notify(message, title="Could not load task output", severity="error", timeout=8)

    def render_slurm(self, job: JobSnapshot) -> None:
        table = self.query_one("#slurm", DataTable)
        queued_ids = {record.display_id for record in job.active_records}
        records = job.active_records + [
            record for record in job.slurm if record.source != "queue" and record.display_id not in queued_ids
        ]
        for record in records:
            table.add_row(
                record.display_id,
                record.array_task_id,
                state_text(record.state),
                record.elapsed or "—",
                record.time_left or "—",
                record.location or "—",
                "live" if record.source == "queue" else "history",
                key=f"{record.source}:{record.display_id}",
            )
        running = sum(record.state.upper() == "RUNNING" for record in job.active_records)
        pending = sum(record.state.upper() == "PENDING" for record in job.active_records)
        self.query_one("#slurm-summary", Static).update(
            f"[b]{job.job_name}[/b] · {running} running · {pending} pending · "
            f"{len(records)} current/historical array records"
        )

    def render_logs(self, job: JobSnapshot) -> None:
        table = self.query_one("#log-files", DataTable)
        names = sorted(job.logs)
        if self.selected_log not in job.logs:
            self.selected_log = names[0] if names else ""
        selected_row = 0
        for index, name in enumerate(names):
            if name == self.selected_log:
                selected_row = index
            table.add_row(name, str(len(job.logs[name].splitlines())), key=name)
        if table.row_count:
            table.move_cursor(row=selected_row)
        self.render_log_body(job)

    def render_log_body(self, job: JobSnapshot) -> None:
        log = self.query_one("#log-body", RichLog)
        log.clear()
        if not self.selected_log:
            log.write(Text("No Slurm logs are available for this job yet.", style="dim"))
            return
        log.write(Text(f"{self.selected_log}\n", style="bold #75a9ff"))
        log.write(Text(job.logs.get(self.selected_log, "")))

    def render_details(self, job: JobSnapshot) -> None:
        details = self.query_one("#details-body", RichLog)
        details.clear()
        details.write(Text(f"{job.job_name}\n", style="bold #75a9ff"))
        details.write(Text(f"SluM ID       {job.job_id}\nRemote path   {job.remote_dir}\n"))
        details.write(Text(f"State         {job.state}\nProgress      {job.effective_completed}/{job.task_count} ({job.percent:.1f}%; {job.completed} saved)\n"))
        details.write(Text(f"Total solver wall time  {format_duration(job.wall_sum, precise=True)}\nLongest solver call     {format_duration(job.wall_max, precise=True)}\n"))
        if job.status_counts:
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(job.status_counts.items()))
            details.write(Text(f"Results       {summary}\n"))
        if job.unresolved_counts:
            summary = ", ".join(
                f"{name}: {count}" for name, count in sorted(job.unresolved_counts.items())
            )
            details.write(Text(f"Unresolved    {summary}\n"))
        task_issues = [
            result
            for result in job.results.values()
            if result.status.lower() not in NORMAL_TASK_RESULTS
        ]
        slurm_issues = [
            record
            for record in job.slurm
            if record.source != "queue"
            and slurm_state_code(record.state) in SLURM_ERROR_STATES
        ]
        if task_issues or (job.completed < job.task_count and slurm_issues):
            details.write(Text("\nExecution errors\n", style="bold #ff6b81"))
            for result in task_issues:
                details.write(
                    Text(
                        f"Task {result.task_id}: {result.status}"
                        f" (exit {result.return_code or 'unknown'})\n"
                    )
                )
            if job.completed < job.task_count:
                for record in slurm_issues:
                    details.write(
                        Text(
                            f"Slurm {record.display_id}: {record.state}"
                            f" ({record.location or 'no reason reported'})\n"
                        )
                    )
            details.write(
                Text(
                    "Select a failed task and press O for its output; use Slurm and Logs "
                    "for scheduler-level failures.\n",
                    style="#75a9ff",
                )
            )
        else:
            details.write(
                Text(
                    "\nNo execution errors. Solver time and memory limits are normal results.\n",
                    style="#52d6a3",
                )
            )
        details.write(Text("\nMetadata\n", style="bold #52d6a3"))
        details.write(Text(json.dumps(job.metadata, indent=2, sort_keys=True)))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.ui_available():
            return
        table_id = event.data_table.id
        key = str(event.row_key.value)
        if table_id == "jobs" and key != self.selected_job_id:
            self.selected_job_id = key
            self.selected_task_id = None
            self.selected_log = ""
            self.render_selected_job()
            self.request_refresh()
        elif table_id == "tasks" and key.isdigit():
            self.selected_task_id = int(key)
            job = self.selected_job()
            if job:
                self.render_task_detail(job)
                self.render_output_panel(job)
        elif table_id == "log-files":
            self.selected_log = key
            job = self.selected_job()
            if job:
                self.render_log_body(job)
        elif table_id == "output-kinds":
            self.selected_output_kind = key
            if self.current_output_key:
                output = self.output_cache.get(self.current_output_key)
                if output:
                    self.render_output_body(output)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self.ui_available():
            return
        if event.data_table.id == "jobs":
            self.query_one("#tabs", TabbedContent).active = "tasks-tab"
        elif event.data_table.id == "tasks":
            self.action_open_output()

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self.ui_available():
            return
        if event.input.id == "job-filter":
            self.render_jobs()
        elif event.input.id == "task-filter":
            job = self.selected_job()
            if job:
                self.query_one("#tasks", DataTable).clear(columns=False)
                self.render_tasks(job)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slum-monitor.py",
        description="Open an interactive dashboard for current and historical SluM jobs.",
    )
    parser.add_argument("--version", action="version", version=f"SluM monitor {VERSION}")
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH host alias (default: slurmy)")
    parser.add_argument("--job", metavar="SLUM_ID", help="initial SluM job to select")
    parser.add_argument(
        "--refresh",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="automatic refresh interval; minimum 1 second (default: 5)",
    )
    parser.add_argument(
        "--ssh-timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="SSH connection timeout (default: 10)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh < 1:
        raise SystemExit("--refresh must be at least 1 second")
    if args.ssh_timeout <= 0:
        raise SystemExit("--ssh-timeout must be greater than zero")
    SlumMonitorApp(args.host, args.refresh, args.ssh_timeout, args.job).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
