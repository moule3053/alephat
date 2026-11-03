"""Pluggable sandbox providers: daytona, agent_sandbox, opensandbox."""

import asyncio
import logging
import os
import ssl
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from alephat_platform.common.config import get_settings
from alephat_platform.common.enums import SandboxProvider
from alephat_platform.harness.agent_sandbox_backend import AgentSandboxBackend

logger = logging.getLogger(__name__)


@dataclass
class SandboxRef:
    provider: str
    sandbox_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # optional live handle for providers that keep client objects
    handle: Any = None


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str = ""


class SandboxProviderBase(ABC):
    name: str

    @abstractmethod
    async def create(
        self, *, task_id: str, metadata: dict[str, Any] | None = None
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

    async def create(self, *, task_id: str, metadata: dict[str, Any] | None = None) -> SandboxRef:
        settings = get_settings()
        if not settings.daytona_api_key:
            if not settings.allow_stub_sandboxes:
                raise RuntimeError("DAYTONA_API_KEY is required for the Daytona provider")
            sid = f"daytona-stub-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
            logger.warning("Explicit local stub sandbox enabled: %s", sid)
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
        # DaytonaSandbox typically exposes execute / run
        handle = ref.handle
        if hasattr(handle, "execute"):
            result = await asyncio.to_thread(handle.execute, command, timeout=timeout)
            return ExecResult(
                exit_code=getattr(result, "exit_code", 0) or 0,
                stdout=str(getattr(result, "output", getattr(result, "stdout", result)) or ""),
                stderr=str(getattr(result, "stderr", "") or ""),
            )
        return ExecResult(exit_code=0, stdout=str(handle))

    async def stop(self, ref: SandboxRef) -> None:
        logger.info("stop sandbox %s/%s", ref.provider, ref.sandbox_id)

    async def delete(self, ref: SandboxRef) -> None:
        logger.info("delete sandbox %s/%s", ref.provider, ref.sandbox_id)


class AgentSandboxProvider(SandboxProviderBase):
    """Kubernetes-sigs/agent-sandbox provider using in-cluster Kubernetes REST."""

    name = SandboxProvider.AGENT_SANDBOX.value

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        base, headers, verify = self._kube_config()
        request_headers = {**headers, **(kwargs.pop("headers", {}) or {})}
        settings = get_settings()
        async with httpx.AsyncClient(
            base_url=base,
            headers=request_headers,
            verify=verify,
            timeout=settings.agent_sandbox_api_timeout_seconds,
        ) as client:
            response = await client.request(method, path, **kwargs)
        if response.is_error:
            raise RuntimeError(
                f"Kubernetes API {method} {path} failed ({response.status_code}): {response.text[:500]}"
            )
        return response.json() if response.content else {}

    @staticmethod
    def _claim_path(namespace: str, name: str) -> str:
        return (
            f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxclaims/{name}"
        )

    @staticmethod
    def _sandbox_path(namespace: str, name: str) -> str:
        return f"/apis/agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxes/{name}"

    @staticmethod
    def _template_path(namespace: str, name: str) -> str:
        return f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxtemplates/{name}"

    @staticmethod
    def _service_path(namespace: str, name: str) -> str:
        return f"/api/v1/namespaces/{namespace}/services/{name}"

    async def _resolve_ref(self, ref: SandboxRef) -> SandboxRef:
        settings = get_settings()
        try:
            data = await self._request(
                "GET", self._sandbox_path(settings.agent_sandbox_namespace, ref.sandbox_id)
            )
            sandbox_name = ref.sandbox_id
        except RuntimeError:
            claim = await self._request(
                "GET", self._claim_path(settings.agent_sandbox_namespace, ref.sandbox_id)
            )
            sandbox = (claim.get("status") or {}).get("sandbox") or {}
            sandbox_name = sandbox.get("name") or sandbox.get("sandboxName")
            if not sandbox_name:
                raise RuntimeError(
                    f"SandboxClaim {ref.sandbox_id} has no assigned sandbox"
                ) from None
            data = await self._request(
                "GET", self._sandbox_path(settings.agent_sandbox_namespace, str(sandbox_name))
            )
        status = data.get("status") or {}
        ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), None)
        if not ready or ready.get("status") != "True":
            raise RuntimeError(f"Sandbox {sandbox_name} is not ready: {ready or status}")
        host = status.get("serviceFQDN")
        if not host:
            selector = status.get("selector") or ""
            key, separator, value = selector.partition("=")
            if not separator:
                raise RuntimeError(f"Sandbox {sandbox_name} has no service FQDN or selector")
            service_name = f"{ref.sandbox_id[:50]}".rstrip("-")
            service_body = {
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
        return SandboxRef(
            provider=self.name,
            sandbox_id=ref.sandbox_id,
            metadata={**ref.metadata, "sandbox_name": str(sandbox_name), "base_url": base_url},
            handle=AgentSandboxBackend(ref.sandbox_id, base_url),
        )

    async def create(self, *, task_id: str, metadata: dict[str, Any] | None = None) -> SandboxRef:
        settings = get_settings()
        sid = f"asb-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
        body = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {"name": sid, "labels": {"app.kubernetes.io/part-of": "alephat"}},
            "spec": {
                "warmPoolRef": {"name": settings.agent_sandbox_template},
                "lifecycle": {"shutdownPolicy": "Delete"},
            },
        }
        template = await self._request(
            "GET",
            self._template_path(settings.agent_sandbox_namespace, settings.agent_sandbox_template),
        )
        body = {
            "apiVersion": "agents.x-k8s.io/v1beta1",
            "kind": "Sandbox",
            "metadata": {"name": sid, "labels": {"app.kubernetes.io/part-of": "alephat"}},
            "spec": {
                "operatingMode": "Running",
                "podTemplate": (template.get("spec") or {}).get("podTemplate") or {},
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
        settings = get_settings()
        return await self._resolve_ref(
            SandboxRef(
                provider=self.name,
                sandbox_id=sandbox_id,
                metadata={"namespace": settings.agent_sandbox_namespace},
            )
        )

    async def exec(self, ref: SandboxRef, command: str, timeout: int = 120) -> ExecResult:
        if not isinstance(ref.handle, AgentSandboxBackend):
            ref = await self._resolve_ref(ref)
        assert isinstance(ref.handle, AgentSandboxBackend)
        exit_code, stdout, stderr = await ref.handle.exec_result(command, timeout=timeout)
        return ExecResult(exit_code, stdout, stderr)

    async def stop(self, ref: SandboxRef) -> None:
        logger.info("agent_sandbox stop %s", ref.sandbox_id)

    async def delete(self, ref: SandboxRef) -> None:
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

    async def create(self, *, task_id: str, metadata: dict[str, Any] | None = None) -> SandboxRef:
        settings = get_settings()
        base = settings.opensandbox_base_url
        sid = f"osb-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
        if not base:
            if not settings.allow_stub_sandboxes:
                raise RuntimeError("OPENSANDBOX_BASE_URL is required for the OpenSandbox provider")
            logger.warning("Explicit local stub sandbox enabled: %s", sid)
            return SandboxRef(
                provider=self.name, sandbox_id=sid, metadata={"stub": True, **(metadata or {})}
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
        return ExecResult(
            exit_code=0,
            stdout=f"[opensandbox:{ref.sandbox_id}] ran: {command}\n",
        )

    async def stop(self, ref: SandboxRef) -> None:
        logger.info("opensandbox stop %s", ref.sandbox_id)

    async def delete(self, ref: SandboxRef) -> None:
        logger.info("opensandbox delete %s", ref.sandbox_id)


class LocalProvider(SandboxProviderBase):
    """Local shell execution (host-direct)."""

    name = SandboxProvider.LOCAL.value

    @staticmethod
    def _root_dir(sandbox_id: str) -> str:
        if os.path.basename(sandbox_id) != sandbox_id:
            raise ValueError("Invalid local sandbox id")
        base = os.environ.get(
            "LOCAL_SANDBOX_ROOT_DIR",
            os.path.join(tempfile.gettempdir(), "alephat-sandboxes"),
        )
        return os.path.join(base, sandbox_id)

    async def create(self, *, task_id: str, metadata: dict[str, Any] | None = None) -> SandboxRef:
        from openswe.sandboxes.providers.local import create_local_sandbox

        sid = f"local-{task_id[:8]}-{uuid.uuid4().hex[:8]}"
        root_dir = self._root_dir(sid)
        backend = await asyncio.to_thread(create_local_sandbox, sid, root_dir=root_dir)
        return SandboxRef(
            provider=self.name,
            sandbox_id=sid,
            metadata={"root_dir": root_dir, **(metadata or {})},
            handle=backend,
        )

    async def connect(self, sandbox_id: str) -> SandboxRef:
        from openswe.sandboxes.providers.local import create_local_sandbox

        root_dir = self._root_dir(sandbox_id)
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
        if handle and hasattr(handle, "execute"):
            result = await asyncio.to_thread(handle.execute, command, timeout=timeout)
            return ExecResult(
                exit_code=getattr(result, "exit_code", 0) or 0,
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
        logger.info("local sandbox stop %s", ref.sandbox_id)

    async def delete(self, ref: SandboxRef) -> None:
        logger.info("local sandbox delete %s", ref.sandbox_id)


_REGISTRY: dict[str, SandboxProviderBase] = {
    SandboxProvider.DAYTONA.value: DaytonaProvider(),
    SandboxProvider.AGENT_SANDBOX.value: AgentSandboxProvider(),
    SandboxProvider.OPENSANDBOX.value: OpenSandboxProvider(),
    SandboxProvider.LOCAL.value: LocalProvider(),
}


def get_sandbox_provider(name: str) -> SandboxProviderBase:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown sandbox provider: {name}") from exc
