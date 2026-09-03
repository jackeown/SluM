#!/usr/bin/env python
"""Incrementally download a SluM job and publish a watchable summary."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Sequence


VERSION = "0.1.0"
DEFAULT_HOST = "slurmy"
DEFAULT_INTERVAL = 5.0
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TASK_LOG_RE = re.compile(r"(?:^|/)task_(\d+)_[A-Fa-f0-9]+[.]solver[.]log$")
SZS_STATUS_RE = re.compile(rb"\bSZS\s+status\s+([A-Za-z][A-Za-z0-9_-]*)", re.IGNORECASE)
SOLVED_SZS_STATUSES = {
    "contradictoryaxioms",
    "countersatisfiable",
    "equivalent",
    "finitelysatisfiable",
    "satisfiable",
    "tautologousconclusion",
    "theorem",
    "unsatisfiable",
}
SUMMARY_NAME = "sync-metadata.json"


REMOTE_SELECT_SCRIPT = r"""
set -u

requested=${1:-}
base="$HOME/SluM"

if [[ ! -d "$base" ]]; then
    echo "No SluM jobs exist under $base" >&2
    exit 1
fi

if [[ -n "$requested" ]]; then
    if [[ ! "$requested" =~ ^[A-Za-z0-9._-]+$ ]]; then
        echo "Invalid SluM job ID: $requested" >&2
        exit 2
    fi
    if [[ ! -d "$base/$requested" ]]; then
        echo "SluM job does not exist: $requested" >&2
        exit 3
    fi
    printf '%s\n' "$requested"
    exit 0
fi

latest=$(
    for directory in "$base"/*; do
        [[ -d "$directory" ]] || continue
        job=${directory##*/}
        last=${job##*_}
        prefix=${job%_*}
        second_last=${prefix##*_}
        if [[ "$last" =~ ^[0-9]+$ && "$second_last" =~ ^[0-9]{9,}$ ]]; then
            submitted=$second_last
        elif [[ "$last" =~ ^[0-9]{9,}$ ]]; then
            submitted=$last
        else
            submitted=$(stat -c %Y "$directory" 2>/dev/null || printf '0')
        fi
        printf '%s\t%s\n' "$submitted" "$job"
    done | sort -t $'\t' -k1,1nr -k2,2r | awk -F '\t' 'NR == 1 {print $2}'
)
if [[ -z "$latest" ]]; then
    echo "No SluM jobs exist under $base" >&2
    exit 1
fi
printf '%s\n' "$latest"
"""


class SyncError(RuntimeError):
    """A user-facing synchronization failure."""


def resolve_job_id(host: str, requested: str | None, timeout: float) -> str:
    if shutil.which("ssh") is None:
        raise SyncError("ssh is not installed or not on PATH")
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout))}",
        "--",
        host,
        "bash",
        "-s",
        "--",
        requested or "",
    ]
    try:
        process = subprocess.run(
            command,
            input=REMOTE_SELECT_SCRIPT.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"SSH lookup timed out after {timeout + 10:g}s") from exc
    if process.returncode != 0:
        message = process.stderr.decode("utf-8", "replace").strip()
        raise SyncError(message or f"ssh exited with status {process.returncode}")
    job_id = process.stdout.decode("utf-8", "replace").strip()
    if not JOB_ID_RE.fullmatch(job_id):
        raise SyncError(f"cluster returned an invalid SluM job ID: {job_id!r}")
    return job_id


def rsync_job(host: str, job_id: str, destination: Path, timeout: float) -> int:
    if shutil.which("rsync") is None:
        raise SyncError("rsync is not installed or not on PATH")
    destination.mkdir(parents=True, exist_ok=True)
    remote_shell = (
        f"ssh -o BatchMode=yes -o ConnectTimeout={max(1, int(timeout))}"
    )
    command = [
        "rsync",
        "--archive",
        "--compress",
        "--partial",
        "--prune-empty-dirs",
        "--omit-dir-times",
        "--itemize-changes",
        "--out-format=%i\t%n%L",
        f"--timeout={max(1, int(timeout + 20))}",
        "--rsh",
        remote_shell,
        "--exclude=/results/.*.tmp",
        "--include=/metadata.json",
        "--include=/manifest.jsonl",
        "--include=/submission.tsv",
        "--include=/batches/",
        "--include=/batches/***",
        # Read-only compatibility for jobs submitted before the batch rename.
        "--include=/chunks/",
        "--include=/chunks/***",
        "--include=/progress/",
        "--include=/progress/***",
        "--include=/logs/",
        "--include=/logs/***",
        "--include=/results/",
        "--include=/results/***",
        "--exclude=*",
        "--",
        f"{host}:SluM/{job_id}/",
        os.fspath(destination) + "/",
    ]
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 120,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"rsync timed out after {timeout + 120:g}s") from exc
    if process.returncode != 0:
        message = process.stderr.strip()
        raise SyncError(message or f"rsync exited with status {process.returncode}")
    return sum(1 for line in process.stdout.splitlines() if line.strip())


def load_job_metadata(destination: Path) -> dict[str, Any]:
    path = destination / "metadata.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_result_records(destination: Path) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    results_dir = destination / "results"
    for path in sorted(results_dir.glob("*.tsv")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split("\t")
            if len(fields) < 15 or not fields[0].isdigit():
                continue
            task_id = int(fields[0])
            records[task_id] = {
                "task_id": task_id,
                "complete": fields[1].lower() == "true",
                "status": fields[2] or "unknown",
                "return_code": fields[3],
                "wall_seconds": as_float(fields[4]),
                "cpu_seconds": as_float(fields[5]),
                "max_memory_kib": as_float(fields[10]),
                "task_key": fields[13],
                "archive": fields[14],
            }
    for path in sorted(results_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
                task_id = int(item["task_id"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            records[task_id] = {
                "task_id": task_id,
                "complete": bool(item.get("complete")),
                "status": str(item.get("status", "unknown")),
                "return_code": str(item.get("return_code", "")),
                "wall_seconds": as_float(item.get("wall_seconds")),
                "cpu_seconds": as_float(item.get("cpu_seconds")),
                "max_memory_kib": as_float(item.get("max_memory_kib")),
                "task_key": str(item.get("task_key", "")),
                "archive": str(item.get("archive", "")),
            }
    return records


def read_member_edges(member: tarfile.ExFileObject, size: int) -> bytes:
    half = 512 * 1024
    if size <= half * 2:
        return member.read(half * 2)
    beginning = member.read(half)
    try:
        member.seek(max(0, size - half))
        ending = member.read(half)
    except (OSError, tarfile.TarError):
        ending = b""
    return beginning + b"\n" + ending


class ArchiveStatusCache:
    def __init__(self) -> None:
        self._entries: dict[Path, tuple[int, int, dict[int, str]]] = {}

    def scan(self, path: Path) -> tuple[dict[int, str], str | None]:
        try:
            stat = path.stat()
        except OSError as exc:
            return {}, f"{path.name}: {exc}"
        cached = self._entries.get(path)
        fingerprint = (stat.st_size, stat.st_mtime_ns)
        if cached and cached[:2] == fingerprint:
            return dict(cached[2]), None

        statuses: dict[int, str] = {}
        try:
            with tarfile.open(path, "r:gz") as archive:
                for info in archive:
                    if not info.isfile():
                        continue
                    match = TASK_LOG_RE.search(PurePosixPath(info.name).as_posix())
                    if not match:
                        continue
                    member = archive.extractfile(info)
                    if member is None:
                        continue
                    found = SZS_STATUS_RE.findall(read_member_edges(member, info.size))
                    if found:
                        statuses[int(match.group(1))] = found[-1].decode(
                            "ascii", "replace"
                        )
        except (OSError, tarfile.TarError) as exc:
            return {}, f"{path.name}: {exc}"
        self._entries[path] = (*fingerprint, statuses)
        return dict(statuses), None


def collect_szs_statuses(
    destination: Path,
    records: dict[int, dict[str, Any]],
    cache: ArchiveStatusCache,
) -> tuple[dict[int, str], list[str], int]:
    results_dir = destination / "results"
    by_archive: dict[str, dict[int, str]] = {}
    warnings: list[str] = []
    archives = sorted(results_dir.glob("*.tar.gz"))
    for path in archives:
        statuses, warning = cache.scan(path)
        by_archive[path.name] = statuses
        if warning:
            warnings.append(warning)

    selected: dict[int, str] = {}
    for task_id, record in records.items():
        archive_name = str(record.get("archive", ""))
        status = by_archive.get(archive_name, {}).get(task_id)
        if status:
            selected[task_id] = status
    return selected, warnings, len(archives)


def progress_counts(destination: Path) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for path in (destination / "progress").glob("*.tsv"):
        try:
            first = path.read_text(encoding="utf-8").split("\t", 1)[0].strip()
        except OSError:
            continue
        if first:
            counts[first] += 1
    return dict(sorted(counts.items()))


def directory_size(destination: Path) -> int:
    total = 0
    for path in destination.rglob("*"):
        if path.is_file() and path.name != SUMMARY_NAME:
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def percentage(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def build_summary(
    *,
    host: str,
    job_id: str,
    destination: Path,
    changed_paths: int,
    iteration: int,
    cache: ArchiveStatusCache,
) -> dict[str, Any]:
    job_metadata = load_job_metadata(destination)
    records = read_result_records(destination)
    task_count = int(job_metadata.get("task_count", 0) or 0)
    if not task_count and records:
        task_count = max(records) + 1
    completed = sum(bool(record["complete"]) for record in records.values())
    execution_counts = Counter(str(record["status"]) for record in records.values())
    szs_by_task, warnings, archive_count = collect_szs_statuses(
        destination, records, cache
    )
    szs_counts = Counter(szs_by_task.values())
    solved = sum(
        status.casefold() in SOLVED_SZS_STATUSES for status in szs_by_task.values()
    )
    wall_times = [
        value
        for record in records.values()
        if (value := record.get("wall_seconds")) is not None
    ]
    cpu_times = [
        value
        for record in records.values()
        if (value := record.get("cpu_seconds")) is not None
    ]
    memory_values = [
        value
        for record in records.values()
        if (value := record.get("max_memory_kib")) is not None
    ]
    now = datetime.now().astimezone()
    return {
        "schema_version": 1,
        "slum_job_id": job_id,
        "host": host,
        "remote_directory": f"$HOME/SluM/{job_id}",
        "local_directory": os.fspath(destination.resolve()),
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_epoch": now.timestamp(),
        "sync_iteration": iteration,
        "changed_paths_last_sync": changed_paths,
        "downloaded_bytes": directory_size(destination),
        "downloaded_archives": archive_count,
        "task_count": task_count,
        "recorded_tasks": len(records),
        "completed_tasks": completed,
        "remaining_tasks": max(0, task_count - completed),
        "percent_complete": percentage(completed, task_count),
        "solved_tasks": solved,
        "percent_solved": percentage(solved, task_count),
        "total_wall_seconds": round(sum(wall_times), 9),
        "total_cpu_seconds": round(sum(cpu_times), 9),
        "longest_wall_seconds": max(wall_times, default=0.0),
        "largest_memory_kib": max(memory_values, default=0.0),
        "execution_status_counts": dict(sorted(execution_counts.items())),
        "szs_status_counts": dict(sorted(szs_counts.items())),
        "progress_state_counts": progress_counts(destination),
        "all_tasks_complete": bool(task_count and completed >= task_count),
        "solved_szs_statuses": sorted(SOLVED_SZS_STATUSES),
        "warnings": warnings,
    }


def write_summary(destination: Path, summary: dict[str, Any]) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / SUMMARY_NAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{SUMMARY_NAME}.", suffix=".tmp", dir=destination
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(summary, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return target


def sync_loop(args: argparse.Namespace) -> int:
    job_id = resolve_job_id(args.host, args.job_id, args.ssh_timeout)
    destination = (
        Path(args.output).expanduser()
        if args.output
        else Path.cwd() / "slum-results" / job_id
    )
    if destination.exists() and not destination.is_dir():
        raise SyncError(f"output path is not a directory: {destination}")

    print(f"SluM job: {job_id}")
    print(f"Local directory: {destination.resolve()}")
    cache = ArchiveStatusCache()
    iteration = 0
    while True:
        iteration += 1
        changed_paths = rsync_job(args.host, job_id, destination, args.ssh_timeout)
        summary = build_summary(
            host=args.host,
            job_id=job_id,
            destination=destination,
            changed_paths=changed_paths,
            iteration=iteration,
            cache=cache,
        )
        summary_path = write_summary(destination, summary)
        print(
            f"[{summary['updated_at']}] "
            f"{summary['completed_tasks']}/{summary['task_count']} complete "
            f"({summary['percent_complete']:.2f}%), "
            f"{summary['solved_tasks']} solved "
            f"({summary['percent_solved']:.2f}%), "
            f"{changed_paths} paths updated"
        )
        if not args.follow or summary["all_tasks_complete"]:
            print(f"Metadata: {summary_path.resolve()}")
            return 0
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slum-sync.py",
        description=(
            "Incrementally download a SluM job's results and write live summary metadata."
        ),
    )
    parser.add_argument("--version", action="version", version=f"SluM sync {VERSION}")
    parser.add_argument(
        "job_id",
        nargs="?",
        metavar="SLUM_ID",
        help="SluM job ID; the latest remote job is used when omitted",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH host alias (default: slurmy)")
    parser.add_argument(
        "--output",
        metavar="DIRECTORY",
        help="local job directory (default: ./slum-results/<SluM-ID>)",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="keep syncing until every planned task has a complete result",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        metavar="SECONDS",
        help=f"seconds between --follow syncs (default: {DEFAULT_INTERVAL:g})",
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
    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")
    if args.ssh_timeout <= 0:
        raise SystemExit("--ssh-timeout must be greater than zero")
    try:
        return sync_loop(args)
    except KeyboardInterrupt:
        print("\nSluM sync stopped; already downloaded files were kept.", file=sys.stderr)
        return 130
    except (OSError, SyncError) as exc:
        print(f"SluM sync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
