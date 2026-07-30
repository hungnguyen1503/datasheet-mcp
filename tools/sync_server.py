"""Deploy Datasheet MCP code or synchronize one evidence part to the lab host."""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "hungnguyen@192.168.2.8"
DEFAULT_REMOTE_ROOT = "/home/hungnguyen/datasheet-mcp"
PART_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
METADATA_FILES = ("catalog.json", "registers.json", "pins.json")
EXPECTED_TOOLS = {"ds_catalog", "ds_query", "ds_get"}


def _require(command: str) -> None:
    if not shutil.which(command):
        raise RuntimeError(f"'{command}' is required")


def _run(command: list[str], *, dry_run: bool = False) -> None:
    print("+", subprocess.list2cmdline(command))
    if not dry_run:
        subprocess.run(command, check=True)


def _output(command: list[str]) -> str:
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()


def _part_directory(part: str) -> Path:
    if not PART_PATTERN.fullmatch(part):
        raise ValueError("part must contain only letters, digits, '.', '_', or '-'")
    root = (REPOSITORY_ROOT / "datasheet").resolve()
    candidate = (root / part).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise FileNotFoundError(f"No local datasheet directory for '{part}': {candidate}")
    return candidate


def _payload(part_dir: Path, include_source_pdf: bool) -> list[Path]:
    paths: list[Path] = []
    if (markdown := part_dir / "MD").is_dir():
        paths.append(markdown)
    if (corpus := part_dir / "evidence" / "corpus.json").is_file():
        paths.append(corpus)
    paths.extend(path for name in METADATA_FILES if (path := part_dir / name).is_file())
    if include_source_pdf and (source := part_dir / "source.pdf").is_file():
        paths.append(source)
    if not paths:
        raise FileNotFoundError(f"No syncable evidence for '{part_dir.name}'. Run the extraction stage first.")
    return paths


def _remote(args: argparse.Namespace, command: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", args.host, command]


def _remote_root_command(root: str, command: str) -> str:
    return f"cd {shlex.quote(root)} && {command}"


def _rsync_commands(payload: Iterable[Path], *, host: str, remote_part: str, dry_run: bool) -> list[list[str]]:
    commands: list[list[str]] = []
    for path in payload:
        destination = f"{host}:{remote_part}/evidence/" if path.name == "corpus.json" else f"{host}:{remote_part}/"
        command = ["rsync", "--archive", "--itemize-changes", "--protect-args"]
        if dry_run:
            command.append("--dry-run")
        commands.append([*command, str(path), destination])
    return commands


def sync_part(args: argparse.Namespace, *, publish: bool = False) -> None:
    if not args.part:
        raise ValueError("--part is required for sync or publish")
    part = args.part.strip()
    part_dir = _part_directory(part)
    payload = _payload(part_dir, args.include_source_pdf)
    remote_root = args.remote_root.rstrip("/")
    remote_part = f"{remote_root}/datasheet/{part}"
    dry_run = not args.apply
    print(f"Target: {args.host}:{remote_part}")
    for path in payload:
        print(f"  - {path.relative_to(REPOSITORY_ROOT)}")
    if dry_run:
        print("Preview only. Add --apply to transfer or publish.")
    if not dry_run:
        _require("ssh")
        _require("rsync")
    _run(_remote(args, _remote_root_command(remote_root, f"mkdir -p {shlex.quote(remote_part)}/evidence")), dry_run=dry_run)
    for command in _rsync_commands(payload, host=args.host, remote_part=remote_part, dry_run=dry_run):
        _run(command, dry_run=dry_run)
    if publish:
        if dry_run:
            print("Publish preview: server-side Qdrant build would run after --apply.")
            return
        command = _remote_root_command(remote_root, f"test -f deploy/publish-part.sh && bash deploy/publish-part.sh --part {shlex.quote(part)}")
        _run(_remote(args, command))


def _branch(args: argparse.Namespace) -> str:
    branch = args.branch or _output(["git", "branch", "--show-current"])
    if not branch:
        raise RuntimeError("detached HEAD cannot be deployed; switch to a named branch")
    return branch


def deploy(args: argparse.Namespace) -> None:
    _require("git")
    _require("ssh")
    if _output(["git", "status", "--porcelain"]):
        raise RuntimeError("local worktree is dirty; commit or stash changes before deploy")
    branch = _branch(args)
    _run(["git", "fetch", "origin", branch])
    if subprocess.run(["git", "merge-base", "--is-ancestor", f"origin/{branch}", "HEAD"], check=False).returncode:
        raise RuntimeError(f"origin/{branch} is ahead or diverged; integrate it locally before deploy")
    _run(["git", "push", "origin", branch])
    command = _remote_root_command(args.remote_root.rstrip("/"), f"git fetch origin {shlex.quote(branch)} && git pull --ff-only origin {shlex.quote(branch)}")
    _run(_remote(args, command))
    print("Code updated. Restart datasheetmcp interactively if the running process must reload it:")
    print(f"  ssh -t {args.host} 'sudo systemctl restart datasheetmcp'")


def status(args: argparse.Namespace) -> None:
    command = _remote_root_command(args.remote_root.rstrip("/"), "printf 'branch='; git branch --show-current; printf '\\nrevision='; git rev-parse --short HEAD; printf '\\nchanges='; git status --short; printf '\\nservice='; systemctl is-active datasheetmcp || true")
    _run(_remote(args, command))


def tools(args: argparse.Namespace) -> None:
    script = "from ds.mcp_server import mcp; names=set(mcp._tool_manager._tools); expected=" + repr(EXPECTED_TOOLS) + "; print('tools=' + ','.join(sorted(names))); raise SystemExit(0 if names == expected else 1)"
    command = _remote_root_command(args.remote_root.rstrip("/"), f"cd mcp && ../.venv/bin/python -c {shlex.quote(script)}")
    _run(_remote(args, command))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="sync", choices=("sync", "publish", "deploy", "status", "tools"))
    parser.add_argument("--part", help="Single datasheet part for sync or publish")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"SSH target (default: {DEFAULT_HOST})")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT, help="Remote repository root")
    parser.add_argument("--branch", help="Git branch for deploy (default: current branch)")
    parser.add_argument("--include-source-pdf", action="store_true", help="Also transfer source.pdf when it exists")
    parser.add_argument("--apply", action="store_true", help="Execute sync or publish; otherwise print a dry-run plan")
    parser.add_argument("--publish", action="store_true", help="Legacy: publish after a sync")
    args = parser.parse_args()
    try:
        if args.command == "deploy":
            deploy(args)
        elif args.command == "status":
            status(args)
        elif args.command == "tools":
            tools(args)
        else:
            sync_part(args, publish=args.command == "publish" or args.publish)
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
