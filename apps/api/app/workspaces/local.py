import uuid
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a workspace path would leave the configured root."""


class LocalWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def prepare(self, task_id: str) -> Path:
        directory = self.task_directory(task_id)
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    def task_directory(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        directory = (self.root / task_id).resolve()
        if not directory.is_relative_to(self.root):
            raise WorkspacePathError("workspace path escaped the root")
        return directory

    def resolve(self, task_id: str, relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(part == ".." for part in relative_path.parts):
            raise WorkspacePathError("path traversal is not allowed")
        directory = self.task_directory(task_id)
        candidate = (directory / relative_path).resolve()
        if not candidate.is_relative_to(directory):
            raise WorkspacePathError("resolved path is outside the task workspace")
        return candidate

    def write_text(self, task_id: str, relative: str, content: str) -> Path:
        path = self.resolve(task_id, relative)
        if path == self.task_directory(task_id):
            raise WorkspacePathError("refusing to write the task directory as a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _validate_task_id(self, task_id: str) -> None:
        try:
            parsed = uuid.UUID(task_id)
        except ValueError as exc:
            raise WorkspacePathError("invalid task id") from exc
        if str(parsed) != task_id:
            raise WorkspacePathError("invalid task id")
