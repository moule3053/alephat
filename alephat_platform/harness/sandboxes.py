"""Pluggable sandbox providers and Temporal DeepAgents backend factories."""

import asyncio
import logging
import os
import ssl
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import httpx
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from alephat_platform.common.config import get_settings
from alephat_platform.common.enums import SandboxProvider
from alephat_platform.harness.agent_sandbox_backend import AgentSandboxBackend

logger = logging.getLogger(__name__)


@dataclass
class SandboxRef:
    provider: str
    sandbox_id: str
    metadata: dict[str, object] = field(default_factory=dict)
    handle: object | None = None


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str = ""


class StubSandboxBackend(BaseSandbox):
    """In-memory stub BaseSandbox for explicit ALLOW_STUB_SANDBOXES dev testing."""

    def __init__(self, sandbox_id: str) -> None:
        self._id = sandbox_id
        self._files: dict[str, bytes] = {}

    @property
    def id(self) -> str:
        return self._id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del timeout
        return ExecuteResponse(
            output=f"[stub:{self._id}] ran: {command}\n",
            exit_code=0,
            truncated=False,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            self._files[path] = content
            responses.append(FileUploadResponse(path=path, error=None))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if path in self._files:
                responses.append(FileDownloadResponse(path=path, content=self._files[path]))
            else:
                responses.append(FileDownloadResponse(path=path, error="file_not_found"))
        return responses


class SandboxProviderBase(ABC):
    name: str

    @abstractmethod
    async def create(
        self, *, task_id: str, metadata: dict[str, object] | None = None
    ) -> SandboxRef: ...

    @abstractmethod
    async def connect(self, sandbox_id: str) -> SandboxRef: ...

    @abstractmethod
    async def exec(self, ref: SandboxRef, command: str, timeout: int = 120) -> ExecResult: ...

    @abstractmethod
    async def stop(self, ref: SandboxRef) -> None: ...

    @abstractmethod
    async def delete(self, ref: SandboxRef) -> None: ...


class DaytonaProvider(SandboxProviderBase):
    name = SandboxProvider.DAYTONA.value

    async def create(
        self, *, task_id: str, metadata: dict[str, object] | None = None
    ) -> SandboxRef:
        settings = get_settings()
        if not settings.daytona_api_key:
            if not settings.allow_stub_sandboxes:
                raise RuntimeError("DAYTONA_API_KEY is required for the Daytona provider")
            sid = f"daytona-stub-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
            logger.warning("Explicit local stub sandbox enabled", extra={"sandbox_id": sid})
            return SandboxRef(provider=self.name, sandbox_id=sid, metadata={"stub": True})
        try:
            from openswe.sandboxes.providers.daytona import create_daytona_sandbox

            backend = await asyncio.to_thread(create_daytona_sandbox, None)
            sid = (
                getattr(backend, "id", None)
                or getattr(backend, "sandbox_id", None)
                or str(uuid.uuid4())
            )
            return SandboxRef(
                provider=self.name,
                sandbox_id=str(sid),
                metadata=metadata or {},
                handle=backend,
            )
        except Exception:
            if not settings.allow_stub_sandboxes:
                raise
            logger.exception("Daytona create failed; explicit stub fallback enabled")
            return SandboxRef(
                provider=self.name,
                sandbox_id=f"daytona-stub-{uuid.uuid4().hex[:12]}",
                metadata={"stub": True},
            )

    async def connect(self, sandbox_id: str) -> SandboxRef:
        settings = get_settings()
        if not settings.daytona_api_key or sandbox_id.startswith("daytona-stub-"):
            return SandboxRef(provider=self.name, sandbox_id=sandbox_id, metadata={"stub": True})
        try:
            from openswe.sandboxes.providers.daytona import create_daytona_sandbox

            backend = await asyncio.to_thread(create_daytona_sandbox, sandbox_id)
            return SandboxRef(provider=self.name, sandbox_id=sandbox_id, handle=backend)
        except Exception:
            logger.exception("Daytona connect failed")
            raise

    async def exec(self, ref: SandboxRef, command: str, timeout: int = 120) -> ExecResult:
        if ref.metadata.get("stub") or ref.handle is None:
            return ExecResult(exit_code=0, stdout=f"[stub:{ref.sandbox_id}] ran: {command}\n")
        handle = ref.handle
        execute_fn = getattr(handle, "execute", None)
        if callable(execute_fn):
            result = await asyncio.to_thread(execute_fn, command, timeout=timeout)
            return ExecResult(
                exit_code=int(getattr(result, "exit_code", 0) or 0),
                stdout=str(getattr(result, "output", getattr(result, "stdout", result)) or ""),
                stderr=str(getattr(result, "stderr", "") or ""),
            )
        return ExecResult(exit_code=0, stdout=str(handle))

    async def stop(self, ref: SandboxRef) -> None:
        logger.info(
            "Stopping Daytona sandbox",
            extra={"provider": ref.provider, "sandbox_id": ref.sandbox_id},
        )

    async def delete(self, ref: SandboxRef) -> None:
        logger.info(
            "Deleting Daytona sandbox",
            extra={"provider": ref.provider, "sandbox_id": ref.sandbox_id},
        )


class AgentSandboxProvider(SandboxProviderBase):
    """Kubernetes-sigs/agent-sandbox provider using in-cluster Kubernetes REST."""

    name = SandboxProvider.AGENT_SANDBOX.value
    _CACHE_TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self._resolved_cache: dict[str, tuple[float, SandboxRef]] = {}

    def _kube_config(self) -> tuple[str, dict[str, str], ssl.SSLContext]:
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        if not os.path.exists(token_path):
            raise RuntimeError("agent_sandbox requires an in-cluster service account")
        with open(token_path, encoding="utf-8") as token_file:
            token = token_file.read().strip()
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise RuntimeError("KUBERNETES_SERVICE_HOST is not set")
        return (
            f"https://{host}:{port}",
            {"Authorization": f"Bearer {token}"},
            ssl.create_default_context(cafile=ca_path),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        base, auth_headers, verify = self._kube_config()
        request_headers = {**auth_headers, **(headers or {})}
        settings = get_settings()
        async with httpx.AsyncClient(
            base_url=base,
            headers=request_headers,
            verify=verify,
            timeout=settings.agent_sandbox_api_timeout_seconds,
        ) as client:
            response = await client.request(method, path, json=json)
        if response.is_error:
            raise RuntimeError(
                f"Kubernetes API {method} {path} failed ({response.status_code}): {response.text[:500]}"
            )
        parsed = response.json() if response.content else {}
        return parsed if isinstance(parsed, dict) else {}

    def _claim_path(self, namespace: str, name: str) -> str:
        return (
            f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxclaims/{name}"
        )

    def _sandbox_path(self, namespace: str, name: str) -> str:
        return f"/apis/agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxes/{name}"

    def _template_path(self, namespace: str, name: str) -> str:
        return f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxtemplates/{name}"

    def _service_path(self, namespace: str, name: str) -> str:
        return f"/api/v1/namespaces/{namespace}/services/{name}"

    async def _resolve_ref(self, ref: SandboxRef) -> SandboxRef:
        settings = get_settings()
        try:
            data = await self._request(
                "GET", self._sandbox_path(settings.agent_sandbox_namespace, ref.sandbox_id)
            )
            sandbox_name: object = ref.sandbox_id
        except RuntimeError:
            claim = await self._request(
                "GET", self._claim_path(settings.agent_sandbox_namespace, ref.sandbox_id)
            )
            claim_status = claim.get("status")
            sandbox = claim_status.get("sandbox") if isinstance(claim_status, dict) else None
            sandbox_dict = sandbox if isinstance(sandbox, dict) else {}
            sandbox_name = sandbox_dict.get("name") or sandbox_dict.get("sandboxName")
            if not sandbox_name:
                raise RuntimeError(
                    f"SandboxClaim {ref.sandbox_id} has no assigned sandbox"
                ) from None
            data = await self._request(
                "GET", self._sandbox_path(settings.agent_sandbox_namespace, str(sandbox_name))
            )
        status_raw = data.get("status")
        status = status_raw if isinstance(status_raw, dict) else {}
        conditions = status.get("conditions")
        ready = next(
            (
                c
                for c in (conditions if isinstance(conditions, list) else [])
                if isinstance(c, dict) and c.get("type") == "Ready"
            ),
            None,
        )
        if not ready or ready.get("status") != "True":
            raise RuntimeError(f"Sandbox {sandbox_name} is not ready: {ready or status}")
        host = status.get("serviceFQDN")
        if not isinstance(host, str) or not host:
            selector = str(status.get("selector") or "")
            key, separator, value = selector.partition("=")
            if not separator:
                raise RuntimeError(f"Sandbox {sandbox_name} has no service FQDN or selector")
            service_name = f"{ref.sandbox_id[:50]}".rstrip("-")
            service_body: dict[str, object] = {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": service_name,
                    "labels": {"app.kubernetes.io/part-of": "alephat"},
                },
                "spec": {
                    "selector": {key: value},
                    "ports": [{"name": "toolbox", "port": 8888, "targetPort": "toolbox"}],
                },
            }
            try:
                await self._request(
                    "POST",
                    f"/api/v1/namespaces/{settings.agent_sandbox_namespace}/services",
                    json=service_body,
                )
            except RuntimeError as exc:
                if "(409)" not in str(exc):
                    raise
            host = f"{service_name}.{settings.agent_sandbox_namespace}.svc.cluster.local"
        base_url = f"http://{host}:8888"
        pod_ips = status.get("podIPs")
        first_ip = (
            str(pod_ips[0]).strip()
            if isinstance(pod_ips, list) and pod_ips and isinstance(pod_ips[0], str)
            else ""
        )
        fallback_url = f"http://{first_ip}:8888" if first_ip and ":" not in first_ip else None
        return SandboxRef(
            provider=self.name,
            sandbox_id=ref.sandbox_id,
            metadata={
                **ref.metadata,
                "sandbox_name": str(sandbox_name),
                "base_url": base_url,
                **({"pod_ip": first_ip} if first_ip else {}),
            },
            handle=AgentSandboxBackend(ref.sandbox_id, base_url, fallback_url=fallback_url),
        )

    async def create(
        self, *, task_id: str, metadata: dict[str, object] | None = None
    ) -> SandboxRef:
        settings = get_settings()
        sid = f"asb-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
        template = await self._request(
            "GET",
            self._template_path(settings.agent_sandbox_namespace, settings.agent_sandbox_template),
        )
        template_spec = template.get("spec")
        pod_template = template_spec.get("podTemplate") if isinstance(template_spec, dict) else {}
        body: dict[str, object] = {
            "apiVersion": "agents.x-k8s.io/v1beta1",
            "kind": "Sandbox",
            "metadata": {"name": sid, "labels": {"app.kubernetes.io/part-of": "alephat"}},
            "spec": {
                "operatingMode": "Running",
                "podTemplate": pod_template if isinstance(pod_template, dict) else {},
                "service": True,
                "shutdownPolicy": "Delete",
            },
        }
        await self._request(
            "POST",
            f"/apis/agents.x-k8s.io/v1beta1/namespaces/{settings.agent_sandbox_namespace}/sandboxes",
            json=body,
        )
        ref = SandboxRef(
            provider=self.name,
            sandbox_id=sid,
            metadata={"namespace": settings.agent_sandbox_namespace, **(metadata or {})},
        )
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await self._resolve_ref(ref)
            except RuntimeError:
                await asyncio.sleep(2)
        raise TimeoutError(f"Timed out waiting for agent-sandbox claim {sid}")

    async def connect(self, sandbox_id: str) -> SandboxRef:
        now = asyncio.get_running_loop().time()
        cached = self._resolved_cache.get(sandbox_id)
        if cached is not None and (now - cached[0]) < self._CACHE_TTL_SECONDS:
            return cached[1]
        settings = get_settings()
        resolved = await self._resolve_ref(
            SandboxRef(
                provider=self.name,
                sandbox_id=sandbox_id,
                metadata={"namespace": settings.agent_sandbox_namespace},
            )
        )
        self._resolved_cache[sandbox_id] = (now, resolved)
        return resolved

    async def exec(self, ref: SandboxRef, command: str, timeout: int = 120) -> ExecResult:
        if not isinstance(ref.handle, AgentSandboxBackend):
            ref = await self._resolve_ref(ref)
        assert isinstance(ref.handle, AgentSandboxBackend)
        exit_code, stdout, stderr = await ref.handle.exec_result(command, timeout=timeout)
        return ExecResult(exit_code, stdout, stderr)

    async def stop(self, ref: SandboxRef) -> None:
        logger.info("Stopping agent_sandbox", extra={"sandbox_id": ref.sandbox_id})

    async def delete(self, ref: SandboxRef) -> None:
        self._resolved_cache.pop(ref.sandbox_id, None)
        settings = get_settings()
        try:
            await self._request(
                "DELETE", self._sandbox_path(settings.agent_sandbox_namespace, ref.sandbox_id)
            )
        except RuntimeError as exc:
            if "(404)" not in str(exc):
                try:
                    await self._request(
                        "DELETE", self._claim_path(settings.agent_sandbox_namespace, ref.sandbox_id)
                    )
                except RuntimeError as claim_exc:
                    if "(404)" not in str(claim_exc):
                        raise
        service_name = ref.sandbox_id[:50].rstrip("-")
        try:
            await self._request(
                "DELETE", self._service_path(settings.agent_sandbox_namespace, service_name)
            )
        except RuntimeError as exc:
            if "(404)" not in str(exc):
                raise


class OpenSandboxProvider(SandboxProviderBase):
    """OpenSandbox control-plane integration (stub-capable)."""

    name = SandboxProvider.OPENSANDBOX.value

    async def create(
        self, *, task_id: str, metadata: dict[str, object] | None = None
    ) -> SandboxRef:
        settings = get_settings()
        base = settings.opensandbox_base_url
        sid = f"osb-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
        if not base:
            if not settings.allow_stub_sandboxes:
                raise RuntimeError("OPENSANDBOX_BASE_URL is required for the OpenSandbox provider")
            logger.warning("Explicit local stub sandbox enabled", extra={"sandbox_id": sid})
            return SandboxRef(
                provider=self.name,
                sandbox_id=sid,
                metadata={"stub": True, **(metadata or {})},
            )
        raise RuntimeError("OpenSandbox API adapter is not configured for this deployment")

    async def connect(self, sandbox_id: str) -> SandboxRef:
        settings = get_settings()
        if not settings.allow_stub_sandboxes:
            raise RuntimeError("OpenSandbox reconnect is unavailable without an API adapter")
        return SandboxRef(
            provider=self.name,
            sandbox_id=sandbox_id,
            metadata={"base_url": settings.opensandbox_base_url, "stub": True},
        )

    async def exec(self, ref: SandboxRef, command: str, timeout: int = 120) -> ExecResult:
        del timeout
        return ExecResult(
            exit_code=0,
            stdout=f"[opensandbox:{ref.sandbox_id}] ran: {command}\n",
        )

    async def stop(self, ref: SandboxRef) -> None:
        logger.info("Stopping opensandbox", extra={"sandbox_id": ref.sandbox_id})

    async def delete(self, ref: SandboxRef) -> None:
        logger.info("Deleting opensandbox", extra={"sandbox_id": ref.sandbox_id})


class LocalProvider(SandboxProviderBase):
    """Local shell execution isolated under LOCAL_SANDBOX_ROOT_DIR."""

    name = SandboxProvider.LOCAL.value

    @staticmethod
    def root_dir(sandbox_id: str) -> str:
        if os.path.basename(sandbox_id) != sandbox_id:
            raise ValueError("Invalid local sandbox id")
        base = os.environ.get(
            "LOCAL_SANDBOX_ROOT_DIR",
            os.path.join(tempfile.gettempdir(), "alephat-sandboxes"),
        )
        return os.path.join(base, sandbox_id)

    async def create(
        self, *, task_id: str, metadata: dict[str, object] | None = None
    ) -> SandboxRef:
        from openswe.sandboxes.providers.local import create_local_sandbox

        sid = f"local-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
        root_dir = self.root_dir(sid)
        backend = await asyncio.to_thread(create_local_sandbox, sid, root_dir=root_dir)
        return SandboxRef(
            provider=self.name,
            sandbox_id=sid,
            metadata={"root_dir": root_dir, **(metadata or {})},
            handle=backend,
        )

    async def connect(self, sandbox_id: str) -> SandboxRef:
        from openswe.sandboxes.providers.local import create_local_sandbox

        root_dir = self.root_dir(sandbox_id)
        backend = await asyncio.to_thread(
            create_local_sandbox,
            sandbox_id,
            root_dir=root_dir,
        )
        return SandboxRef(
            provider=self.name,
            sandbox_id=sandbox_id,
            metadata={"root_dir": root_dir},
            handle=backend,
        )

    async def exec(self, ref: SandboxRef, command: str, timeout: int = 120) -> ExecResult:
        handle = ref.handle
        execute_fn = getattr(handle, "execute", None)
        if callable(execute_fn):
            result = await asyncio.to_thread(execute_fn, command, timeout=timeout)
            return ExecResult(
                exit_code=int(getattr(result, "exit_code", 0) or 0),
                stdout=str(getattr(result, "output", getattr(result, "stdout", result)) or ""),
                stderr=str(getattr(result, "stderr", "") or ""),
            )
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecResult(
                exit_code=proc.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
            )
        except TimeoutError:
            proc.kill()
            return ExecResult(exit_code=124, stdout="", stderr="Command timed out")

    async def stop(self, ref: SandboxRef) -> None:
        logger.info("Stopping local sandbox", extra={"sandbox_id": ref.sandbox_id})

    async def delete(self, ref: SandboxRef) -> None:
        logger.info("Deleting local sandbox", extra={"sandbox_id": ref.sandbox_id})


class SandboxRegistry:
    """Registry of pluggable sandbox providers and DeepAgentsPlugin backend factories."""

    def __init__(self) -> None:
        self._providers: dict[str, SandboxProviderBase] = {
            SandboxProvider.DAYTONA.value: DaytonaProvider(),
            SandboxProvider.AGENT_SANDBOX.value: AgentSandboxProvider(),
            SandboxProvider.OPENSANDBOX.value: OpenSandboxProvider(),
            SandboxProvider.LOCAL.value: LocalProvider(),
        }
        self._stub_cache: dict[str, StubSandboxBackend] = {}

    def get(self, name: str) -> SandboxProviderBase:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown sandbox provider: {name}") from exc

    def local_backend(self, sandbox_id: str) -> object:
        from openswe.sandboxes.providers.local import create_local_sandbox

        return create_local_sandbox(sandbox_id, root_dir=LocalProvider.root_dir(sandbox_id))

    async def agent_sandbox_backend(self, sandbox_id: str) -> object:
        ref = await self._providers[SandboxProvider.AGENT_SANDBOX.value].connect(sandbox_id)
        if ref.handle is None:
            raise RuntimeError(f"Agent sandbox {sandbox_id} returned no backend handle")
        return ref.handle

    async def daytona_backend(self, sandbox_id: str) -> object:
        ref = await self._providers[SandboxProvider.DAYTONA.value].connect(sandbox_id)
        if ref.handle is None:
            return self.stub_backend(sandbox_id)
        return ref.handle

    def stub_backend(self, sandbox_id: str) -> StubSandboxBackend:
        sid = str(sandbox_id or "stub")
        existing = self._stub_cache.get(sid)
        if existing is None:
            existing = StubSandboxBackend(sid)
            self._stub_cache[sid] = existing
        return existing

    def backend_factories(self) -> Mapping[str, Callable[..., object]]:
        return {
            SandboxProvider.LOCAL.value: self.local_backend,
            SandboxProvider.AGENT_SANDBOX.value: self.agent_sandbox_backend,
            SandboxProvider.DAYTONA.value: self.daytona_backend,
            "stub": self.stub_backend,
        }


SANDBOX_REGISTRY = SandboxRegistry()
get_sandbox_provider = SANDBOX_REGISTRY.get
