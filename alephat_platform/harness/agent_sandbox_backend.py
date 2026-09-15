"""Deep Agents backend for the Kubernetes Agent Sandbox Python runtime."""

import posixpath
import secrets
import shlex
import urllib.parse
from typing import Any

import httpx
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    PERMISSION_DENIED,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import (
    BaseSandbox,
    _build_edit_tmpfile_cmd,  # noqa: PLC2701 — pinned SDK helpers preserve edit semantics
    _parse_edit_output,  # noqa: PLC2701
)


class AgentSandboxBackend(BaseSandbox):
    """Async Deep Agents backend backed by a Python runtime Sandbox service."""

    runtime_root = "/app"
    workspace = "/app/repo"

    def __init__(
        self,
        sandbox_id: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._id = sandbox_id
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def id(self) -> str:
        return self._id

    @staticmethod
    def _relative_path(path: str) -> str:
        if not path or "\x00" in path:
            raise ValueError("path must be a non-empty absolute path")
        if not path.startswith("/"):
            raise ValueError("path must be absolute")

        normalized = posixpath.normpath(path)
        if normalized in {AgentSandboxBackend.runtime_root, AgentSandboxBackend.workspace}:
            return ""
        workspace_prefix = f"{AgentSandboxBackend.workspace}/"
        runtime_prefix = f"{AgentSandboxBackend.runtime_root}/"
        if normalized.startswith(workspace_prefix):
            normalized = normalized[len(AgentSandboxBackend.workspace) :]
        elif normalized.startswith(runtime_prefix):
            normalized = normalized[len(AgentSandboxBackend.runtime_root) :]
        relative = normalized.lstrip("/")
        if any(part == ".." for part in relative.split("/")):
            raise ValueError("path traversal is not allowed")
        return relative

    @classmethod
    def _runtime_path(cls, path: str) -> str:
        relative = cls._relative_path(path)
        return cls.workspace if not relative else f"{cls.workspace}/{relative}"

    @classmethod
    def _transfer_path(cls, path: str) -> str:
        return posixpath.relpath(cls._runtime_path(path), cls.runtime_root)

    @staticmethod
    def _path_error(path: str, exc: ValueError) -> str:
        return f"Error: invalid path '{path}': {exc}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: int | float,
        **kwargs: Any,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=self._transport,
        ) as client:
            return await client.request(method, path, **kwargs)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise NotImplementedError

    async def exec_result(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        request_timeout = timeout if timeout is not None else 120
        workspace = shlex.quote(self.workspace)
        scoped_command = f"mkdir -p -- {workspace} && cd -- {workspace} && {command}"
        shell_command = f"/bin/sh -lc {shlex.quote(scoped_command)}"
        response = await self._request(
            "POST",
            "/execute",
            timeout=request_timeout,
            json={"command": shell_command},
        )
        response.raise_for_status()
        payload = response.json()
        return (
            int(payload.get("exit_code", 1)),
            str(payload.get("stdout") or ""),
            str(payload.get("stderr") or ""),
        )

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        exit_code, stdout, stderr = await self.exec_result(command, timeout=timeout)
        output = stdout
        if stderr:
            output = f"{output}{'' if not output or output.endswith(chr(10)) else chr(10)}{stderr}"
        return ExecuteResponse(
            output=output,
            exit_code=exit_code,
            truncated=False,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        raise NotImplementedError

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                if not self._relative_path(path):
                    raise ValueError("workspace root is a directory")
                runtime_path = self._runtime_path(path)
                transfer_path = self._transfer_path(path)
                parent = posixpath.dirname(runtime_path)
                mkdir = await self.aexecute(f"mkdir -p -- {shlex.quote(parent)}")
                if mkdir.exit_code != 0:
                    responses.append(
                        FileUploadResponse(
                            path=path,
                            error=mkdir.output.strip() or "could not create parent directory",
                        )
                    )
                    continue
                response = await self._request(
                    "POST",
                    "/upload",
                    timeout=120,
                    files={"file": (transfer_path, content, "application/octet-stream")},
                )
                if response.status_code == 403:
                    responses.append(FileUploadResponse(path=path, error=PERMISSION_DENIED))
                    continue
                response.raise_for_status()
                responses.append(FileUploadResponse(path=path))
            except ValueError:
                responses.append(FileUploadResponse(path=path, error=INVALID_PATH))
            except httpx.HTTPError as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        raise NotImplementedError

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                if not self._relative_path(path):
                    raise ValueError("workspace root is a directory")
                encoded = urllib.parse.quote(self._transfer_path(path), safe="")
                response = await self._request(
                    "GET",
                    f"/download/{encoded}",
                    timeout=120,
                )
                if response.status_code == 404:
                    responses.append(FileDownloadResponse(path=path, error=FILE_NOT_FOUND))
                    continue
                if response.status_code == 403:
                    responses.append(FileDownloadResponse(path=path, error=PERMISSION_DENIED))
                    continue
                response.raise_for_status()
                responses.append(FileDownloadResponse(path=path, content=response.content))
            except ValueError:
                responses.append(FileDownloadResponse(path=path, error=INVALID_PATH))
            except httpx.HTTPError as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
        return responses

    async def als(self, path: str) -> LsResult:
        try:
            return await super().als(self._runtime_path(path))
        except ValueError as exc:
            return LsResult(error=self._path_error(path, exc))

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        try:
            return await super().aread(self._runtime_path(file_path), offset, limit)
        except ValueError as exc:
            return ReadResult(error=self._path_error(file_path, exc))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        try:
            result = await super().awrite(self._runtime_path(file_path), content)
        except ValueError as exc:
            return WriteResult(error=self._path_error(file_path, exc))
        if result.path is not None:
            result.path = file_path
        return result

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        try:
            result = await super().aedit(
                self._runtime_path(file_path),
                old_string,
                new_string,
                replace_all,
            )
        except ValueError as exc:
            return EditResult(error=self._path_error(file_path, exc))
        if result.path is not None:
            result.path = file_path
        return result

    async def _aedit_via_upload(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> EditResult:
        token = secrets.token_hex(10)
        old_tmp = f"{self.workspace}/.deepagents-tmp/edit-{token}-old"
        new_tmp = f"{self.workspace}/.deepagents-tmp/edit-{token}-new"
        responses = await self.aupload_files(
            [
                (old_tmp, old_string.encode()),
                (new_tmp, new_string.encode()),
            ]
        )
        if len(responses) != 2:
            return EditResult(error=f"Error editing file '{file_path}': upload failed")
        upload_error = next((response.error for response in responses if response.error), None)
        if upload_error:
            await self.aexecute(f"rm -f -- {shlex.quote(old_tmp)} {shlex.quote(new_tmp)}")
            return EditResult(error=f"Error editing file '{file_path}': {upload_error}")

        command = _build_edit_tmpfile_cmd(
            file_path,
            old_tmp,
            new_tmp,
            replace_all=replace_all,
        )
        result = await self.aexecute(command)
        return _parse_edit_output(result.output, file_path, old_string)

    async def adelete(self, file_path: str) -> DeleteResult:
        try:
            runtime_path = self._runtime_path(file_path)
        except ValueError as exc:
            return DeleteResult(error=self._path_error(file_path, exc))
        quoted = shlex.quote(runtime_path)
        exists = await self.aexecute(f"test -e {quoted} || test -L {quoted}")
        if exists.exit_code is not None and exists.exit_code != 0:
            return DeleteResult(error=f"Error: '{file_path}' not found")
        deleted = await self.aexecute(f"rm -rf -- {quoted}")
        if deleted.exit_code == 0:
            return DeleteResult(path=file_path)
        return DeleteResult(
            error=f"Error deleting file '{file_path}': {deleted.output.strip() or 'unknown error'}"
        )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        try:
            runtime_path = self._runtime_path(path or "/")
        except ValueError as exc:
            return GrepResult(error=self._path_error(path or "/", exc))
        return await super().agrep(pattern, runtime_path, glob, max_count=max_count)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        try:
            runtime_path = self._runtime_path(path or "/")
        except ValueError as exc:
            return GlobResult(error=self._path_error(path or "/", exc))
        return await super().aglob(pattern, runtime_path)
