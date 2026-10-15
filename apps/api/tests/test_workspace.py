import uuid
from pathlib import Path

import pytest

from app.workspaces.local import LocalWorkspace, WorkspacePathError


@pytest.fixture
def workspace(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "workspaces"
    root.mkdir()
    return LocalWorkspace(root)


def test_write_stays_inside_workspace(workspace: LocalWorkspace, tmp_path: Path) -> None:
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    path = workspace.write_text(task_id, "result.txt", "ok")
    assert path.is_file()
    assert path.is_relative_to(workspace.root)
    outside_files = [p for p in tmp_path.rglob("*") if p.is_file() and not p.is_relative_to(workspace.root)]
    assert outside_files == []


def test_rejects_parent_traversal(workspace: LocalWorkspace) -> None:
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    with pytest.raises(WorkspacePathError):
        workspace.resolve(task_id, "../escape.txt")


def test_rejects_absolute_path(workspace: LocalWorkspace) -> None:
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    with pytest.raises(WorkspacePathError):
        workspace.resolve(task_id, "/tmp/escape.txt")


def test_rejects_symlink_escape(workspace: LocalWorkspace, tmp_path: Path) -> None:
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace.task_directory(task_id) / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(WorkspacePathError):
        workspace.resolve(task_id, "link.txt")
    assert outside.read_text(encoding="utf-8") == "secret"


def test_rejects_intermediate_symlink_directory_escape(
    workspace: LocalWorkspace, tmp_path: Path
) -> None:
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    link_dir = workspace.task_directory(task_id) / "nested"
    link_dir.symlink_to(outside_dir)
    with pytest.raises(WorkspacePathError):
        workspace.write_text(task_id, "nested/escape.txt", "nope")
    assert list(outside_dir.iterdir()) == []


def test_write_does_not_create_outside_files(workspace: LocalWorkspace, tmp_path: Path) -> None:
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    before = {p for p in tmp_path.rglob("*")}
    with pytest.raises(WorkspacePathError):
        workspace.write_text(task_id, "../../escape.txt", "nope")
    after = {p for p in tmp_path.rglob("*")}
    assert after == before
    assert not (tmp_path / "escape.txt").exists()
