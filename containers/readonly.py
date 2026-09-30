"""File inspection for review agents, without file mutation or shell execution."""

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult


class ReadOnlyFilesystemBackend(FilesystemBackend):
    """Retain file/search tools and reject every backend write path."""

    def write(self, file_path: str, content: str) -> WriteResult:
        del file_path, content  # Preserve the backend protocol keyword names.
        return WriteResult(error="This review stage is read-only.")

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        del file_path, old_string, new_string, replace_all
        return EditResult(error="This review stage is read-only.")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]
