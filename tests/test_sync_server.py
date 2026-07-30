"""Tests for the safe, part-scoped server synchronization command."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "tools" / "sync_server.py"
SPEC = importlib.util.spec_from_file_location("sync_server", SCRIPT)
assert SPEC and SPEC.loader
sync_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_server)


def test_part_directory_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        sync_server._part_directory("../other")


def test_rsync_commands_preserve_part_scoping(tmp_path: Path) -> None:
    markdown = tmp_path / "MD"
    markdown.mkdir()
    corpus = tmp_path / "evidence" / "corpus.json"
    corpus.parent.mkdir()
    corpus.write_text("{}", encoding="utf-8")
    commands = sync_server._rsync_commands(
        [markdown, corpus], host="user@example", remote_part="/srv/datasheet/PART", dry_run=True
    )
    assert all("--dry-run" in command for command in commands)
    assert all("--delete" not in command for command in commands)
    assert commands[0][-1] == "user@example:/srv/datasheet/PART/"
    assert commands[1][-1] == "user@example:/srv/datasheet/PART/evidence/"


def test_remote_root_command_quotes_path() -> None:
    command = sync_server._remote_root_command("/srv/data sheet", "git status --short")
    assert command == "cd '/srv/data sheet' && git status --short"
