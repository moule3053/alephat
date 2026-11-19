"""Agent Substrate (ate.dev) control-plane gRPC client and Deep Agents sandbox backend."""

import asyncio
import base64
import logging
import os
import posixpath
import shlex
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import grpc
import httpx
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    FileDownloadResponse,
    FileUploadResponse,
)

from alephat_platform.common.config import Settings, get_settings
from alephat_platform.harness.agent_sandbox_backend import AgentSandboxBackend

logger = logging.getLogger(__name__)

TARGET_ACTOR_HEADER = "ate-target-actor"

ACTOR_STATE_UNSPECIFIED = 0
ACTOR_STATE_RESUMING = 1
ACTOR_STATE_RUNNING = 2
ACTOR_STATE_SUSPENDING = 3
ACTOR_STATE_SUSPENDED = 4
ACTOR_STATE_CRASHED = 5
ACTOR_STATE_DELETING = 6
ACTOR_STATE_REVERTING = 7


@dataclass(frozen=True)
class SubstrateActorRef:
    """Canonical Agent Substrate actor identity (<atespace>/<name>)."""

    atespace: str
    name: str

    def header_value(self) -> str:
        return f"{self.atespace}/{self.name}"

    @classmethod
    def parse(cls, value: str, *, default_atespace: str) -> SubstrateActorRef:
        raw = value.strip()
        if "/" in raw:
            atespace, _, name = raw.partition("/")
            if atespace and name:
                return cls(atespace=atespace, name=name)
        return cls(atespace=default_atespace, name=raw)


@dataclass(frozen=True)
class SubstrateActorInfo:
    """Subset of substrate.ateapi.v1.Actor needed for sandbox lifecycle."""

    atespace: str
    name: str
    uid: str
    state: int


class PemBundleSplitter:
    """Split a combined PEM credential bundle (private key + cert chain) for TLS/gRPC."""

    def split_pem(self, bundle_pem: bytes) -> tuple[bytes, bytes]:
        text = bundle_pem.decode("utf-8")
        certs: list[str] = []
        keys: list[str] = []
        current: list[str] = []
        kind: str | None = None
        for line in text.splitlines(keepends=True):
            if line.startswith("-----BEGIN "):
                current = [line]
                kind = "key" if "PRIVATE KEY" in line else "cert"
            elif line.startswith("-----END ") and kind is not None:
                current.append(line)
                block = "".join(current)
                if kind == "key":
                    keys.append(block)
                else:
                    certs.append(block)
                current = []
                kind = None
            elif kind is not None:
                current.append(line)
        if not certs or not keys:
            raise ValueError(
                "PEM credential bundle must contain both a certificate and private key"
            )
        return "".join(certs).encode("utf-8"), "".join(keys).encode("utf-8")


class SubstrateProtoCodec:
    """Wire-exact protobuf3 encoder/decoder for substrate.ateapi.v1.Control messages."""

    def _encode_varint(self, value: int) -> bytes:
        out = bytearray()
        n = value
        while True:
            chunk = n & 0x7F
            n >>= 7
            if n:
                out.append(chunk | 0x80)
            else:
                out.append(chunk)
                break
        return bytes(out)

    def _decode_varint(self, data: bytes, pos: int) -> tuple[int, int]:
        shift = 0
        result = 0
        while pos < len(data):
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return result, pos
            shift += 7
        raise ValueError("Truncated protobuf varint")

    def _encode_tag(self, field_number: int, wire_type: int) -> bytes:
        return self._encode_varint((field_number << 3) | wire_type)

    def _encode_bytes(self, field_number: int, payload: bytes) -> bytes:
        if not payload:
            return b""
        return self._encode_tag(field_number, 2) + self._encode_varint(len(payload)) + payload

    def _encode_string(self, field_number: int, value: str) -> bytes:
        return self._encode_bytes(field_number, value.encode("utf-8"))

    def _encode_varint_field(self, field_number: int, value: int) -> bytes:
        if value == 0:
            return b""
        return self._encode_tag(field_number, 0) + self._encode_varint(value)

    def _iter_fields(self, data: bytes) -> list[tuple[int, int, int | bytes]]:
        pos = 0
        fields: list[tuple[int, int, int | bytes]] = []
        while pos < len(data):
            tag, pos = self._decode_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 0:
                val, pos = self._decode_varint(data, pos)
                fields.append((field_number, wire_type, val))
            elif wire_type == 2:
                length, pos = self._decode_varint(data, pos)
                chunk = data[pos : pos + length]
                pos += length
                fields.append((field_number, wire_type, chunk))
            elif wire_type == 1:
                fields.append((field_number, wire_type, data[pos : pos + 8]))
                pos += 8
            elif wire_type == 5:
                fields.append((field_number, wire_type, data[pos : pos + 4]))
                pos += 4
            else:
                raise ValueError(f"Unsupported protobuf wire type: {wire_type}")
        return fields

    def encode_object_ref(self, ref: SubstrateActorRef) -> bytes:
        return self._encode_string(1, ref.atespace) + self._encode_string(2, ref.name)

    def encode_resource_metadata(self, atespace: str, name: str) -> bytes:
        return self._encode_string(1, atespace) + self._encode_string(2, name)

    def encode_create_actor_request(
        self,
        actor: SubstrateActorRef,
        template: SubstrateActorRef,
    ) -> bytes:
        actor_msg = self._encode_bytes(
            1, self.encode_resource_metadata(actor.atespace, actor.name)
        ) + self._encode_bytes(2, self.encode_object_ref(template))
        return self._encode_bytes(1, actor_msg)

    def encode_actor_ref_request(self, actor: SubstrateActorRef) -> bytes:
        """Encode GetActorRequest, ResumeActorRequest, SuspendActorRequest, or DeleteActorRequest."""
        return self._encode_bytes(1, self.encode_object_ref(actor))

    def encode_create_egress_policy_request(
        self,
        actor: SubstrateActorRef,
        *,
        allowed_hosts: tuple[str, ...],
        ports: tuple[int, ...] = (443,),
    ) -> bytes:
        ports_payload = b"".join(self._encode_varint_field(2, port) for port in ports)
        tls_rule = b"".join(self._encode_string(1, host) for host in allowed_hosts)
        tls_rule += self._encode_bytes(2, ports_payload)
        egress_rule = self._encode_bytes(3, tls_rule)
        egress_policy = self._encode_bytes(
            1, self.encode_resource_metadata(actor.atespace, "default")
        ) + self._encode_bytes(2, egress_rule)
        return self._encode_bytes(1, self.encode_object_ref(actor)) + self._encode_bytes(
            2, egress_policy
        )

    def decode_actor(self, payload: bytes) -> SubstrateActorInfo:
        atespace = ""
        name = ""
        uid = ""
        state = ACTOR_STATE_UNSPECIFIED
        for field_no, wire_type, val in self._iter_fields(payload):
            if field_no == 1 and wire_type == 2 and isinstance(val, bytes):
                for m_no, m_wire, m_val in self._iter_fields(val):
                    if m_wire == 2 and isinstance(m_val, bytes):
                        if m_no == 1:
                            atespace = m_val.decode("utf-8", errors="replace")
                        elif m_no == 2:
                            name = m_val.decode("utf-8", errors="replace")
                        elif m_no == 3:
                            uid = m_val.decode("utf-8", errors="replace")
            elif field_no == 5 and wire_type == 2 and isinstance(val, bytes):
                for s_no, s_wire, s_val in self._iter_fields(val):
                    if s_no == 1 and s_wire == 0 and isinstance(s_val, int):
                        state = s_val
        return SubstrateActorInfo(atespace=atespace, name=name, uid=uid, state=state)

    def decode_wrapped_actor(self, payload: bytes) -> SubstrateActorInfo:
        """Decode ResumeActorResponse or SuspendActorResponse where field 1 is Actor."""
        for field_no, wire_type, val in self._iter_fields(payload):
            if field_no == 1 and wire_type == 2 and isinstance(val, bytes):
                return self.decode_actor(val)
        return SubstrateActorInfo(atespace="", name="", uid="", state=ACTOR_STATE_UNSPECIFIED)


class SubstrateControlProtocol(Protocol):
    """Minimal async interface for Agent Substrate's Control gRPC API."""

    async def create_actor(
        self, actor: SubstrateActorRef, template: SubstrateActorRef
    ) -> SubstrateActorInfo: ...

    async def get_actor(self, actor: SubstrateActorRef) -> SubstrateActorInfo: ...

    async def resume_actor(self, actor: SubstrateActorRef) -> SubstrateActorInfo: ...

    async def suspend_actor(self, actor: SubstrateActorRef) -> SubstrateActorInfo: ...

    async def delete_actor(self, actor: SubstrateActorRef) -> None: ...

    async def create_egress_policy(
        self, actor: SubstrateActorRef, allowed_hosts: tuple[str, ...]
    ) -> None: ...


class SubstrateControlClient:
    """Async gRPC client for Agent Substrate's `substrate.ateapi.v1.Control` service."""

    _SERVICE_PREFIX = "/substrate.ateapi.v1.Control"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._codec = SubstrateProtoCodec()
        self._splitter = PemBundleSplitter()

    def _endpoint(self) -> str:
        endpoint = (self._settings.substrate_api_endpoint or "").strip()
        if not endpoint:
            raise RuntimeError("SUBSTRATE_API_ENDPOINT is required for Substrate control API calls")
        return endpoint

    def _metadata(self) -> list[tuple[str, str]]:
        token = self._settings.substrate_api_token
        if not token and self._settings.substrate_api_token_path:
            token_path = Path(self._settings.substrate_api_token_path)
            if token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
        if not token and Path("/var/run/ate/token").exists():
            token = Path("/var/run/ate/token").read_text(encoding="utf-8").strip()
        if token:
            return [("authorization", f"Bearer {token}")]
        return []

    def _open_channel(self) -> grpc.aio.Channel:
        endpoint = self._endpoint()
        if not self._settings.substrate_api_tls:
            return grpc.aio.insecure_channel(endpoint)

        ca_bytes: bytes | None = None
        ca_path = self._settings.substrate_api_ca_cert_path or (
            "/var/run/ate/ca.crt" if Path("/var/run/ate/ca.crt").exists() else None
        )
        if ca_path and Path(ca_path).exists():
            ca_bytes = Path(ca_path).read_bytes()

        cert_bytes: bytes | None = None
        key_bytes: bytes | None = None
        bundle_path = self._settings.substrate_api_credential_bundle_path
        if bundle_path and Path(bundle_path).exists():
            cert_bytes, key_bytes = self._splitter.split_pem(Path(bundle_path).read_bytes())

        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca_bytes,
            private_key=key_bytes,
            certificate_chain=cert_bytes,
        )
        options: list[tuple[str, str]] = [
            ("grpc.service_config", '{"loadBalancingConfig":[{"round_robin":{}}]}'),
        ]
        if self._settings.substrate_api_server_name:
            options.append(
                ("grpc.ssl_target_name_override", self._settings.substrate_api_server_name)
            )
        return grpc.aio.secure_channel(endpoint, credentials, options=options)

    async def _unary_call(self, method: str, request_bytes: bytes) -> bytes:
        timeout = float(self._settings.substrate_api_timeout_seconds)
        async with self._open_channel() as channel:
            caller = channel.unary_unary(
                f"{self._SERVICE_PREFIX}/{method}",
                request_serializer=lambda b: b,
                response_deserializer=lambda b: b,
            )
            return await caller(request_bytes, timeout=timeout, metadata=self._metadata())

    async def create_actor(
        self, actor: SubstrateActorRef, template: SubstrateActorRef
    ) -> SubstrateActorInfo:
        raw = await self._unary_call(
            "CreateActor",
            self._codec.encode_create_actor_request(actor, template),
        )
        return self._codec.decode_actor(raw)

    async def get_actor(self, actor: SubstrateActorRef) -> SubstrateActorInfo:
        raw = await self._unary_call("GetActor", self._codec.encode_actor_ref_request(actor))
        return self._codec.decode_actor(raw)

    async def resume_actor(self, actor: SubstrateActorRef) -> SubstrateActorInfo:
        raw = await self._unary_call("ResumeActor", self._codec.encode_actor_ref_request(actor))
        return self._codec.decode_wrapped_actor(raw)

    async def suspend_actor(self, actor: SubstrateActorRef) -> SubstrateActorInfo:
        raw = await self._unary_call("SuspendActor", self._codec.encode_actor_ref_request(actor))
        return self._codec.decode_wrapped_actor(raw)

    async def delete_actor(self, actor: SubstrateActorRef) -> None:
        try:
            await self._unary_call("DeleteActor", self._codec.encode_actor_ref_request(actor))
        except grpc.aio.AioRpcError as exc:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                raise

    async def create_egress_policy(
        self, actor: SubstrateActorRef, allowed_hosts: tuple[str, ...]
    ) -> None:
        if not allowed_hosts:
            return
        try:
            await self._unary_call(
                "CreateActorEgressPolicy",
                self._codec.encode_create_egress_policy_request(
                    actor,
                    allowed_hosts=allowed_hosts,
                    ports=(443,),
                ),
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise


class SubstrateSandboxBackend(AgentSandboxBackend):
    """Deep Agents sandbox backend communicating with an Agent Substrate actor via atenet-router.

    Supports both:
    - ``protocol="toolbox"`` (default): routes ``/execute``, ``/upload``, ``/download/{path}``
      requests to ``alephat-sandbox`` with ``ate-target-actor: <atespace>/<actor>``.
    - ``protocol="process"``: routes ``POST /process`` commands to Substrate's reference
      sandbox HTTP server and transfers files via base64 shell commands.
    """

    def __init__(
        self,
        actor: SubstrateActorRef,
        router_url: str,
        *,
        protocol: str = "toolbox",
        ssl_context: ssl.SSLContext | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(actor.header_value(), router_url, transport=transport)
        self.actor = actor
        self.protocol = protocol.strip().lower() or "toolbox"
        self._ssl_context = ssl_context

    def _default_headers(self) -> dict[str, str]:
        return {TARGET_ACTOR_HEADER: self.actor.header_value()}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: int | float,
        json: dict[str, object] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        merged_headers = self._default_headers()
        if headers:
            merged_headers.update(headers)

        verify: ssl.SSLContext | bool = self._ssl_context if self._ssl_context is not None else True
        last_exc: httpx.ConnectError | None = None
        for attempt in range(8):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=merged_headers,
                    verify=verify,
                    timeout=timeout,
                    transport=self._transport,
                ) as client:
                    return await client.request(
                        method,
                        path,
                        json=json,
                        files=files,
                    )
            except httpx.ConnectError as exc:
                last_exc = exc
            if self._transport is not None or attempt == 7:
                break
            await asyncio.sleep(min(0.5 * (attempt + 1), 2.0))
        assert last_exc is not None
        raise last_exc

    async def exec_result(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        if self.protocol != "process":
            return await super().exec_result(command, timeout=timeout)

        request_timeout = timeout if timeout is not None else 120
        workspace = shlex.quote(self.workspace)
        scoped_command = f"mkdir -p -- {workspace} && cd -- {workspace} && {command}"
        response = await self._request(
            "POST",
            "/process",
            timeout=request_timeout + 5,
            json={
                "command": ["sh", "-c", scoped_command],
                "timeout": f"{request_timeout}s",
            },
        )
        response.raise_for_status()
        payload = response.json()
        exit_code = int(payload.get("exitCode", payload.get("exit_code", 0)))
        stdout = str(payload.get("stdout") or "")
        stderr = str(payload.get("stderr") or "")
        err_text = str(payload.get("error") or "")
        if exit_code == 0 and err_text:
            exit_code = 1
        if err_text and err_text not in stderr:
            stderr = f"{stderr}\n{err_text}".strip() if stderr else err_text
        return exit_code, stdout, stderr

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        if self.protocol != "process":
            return await super().aupload_files(files)

        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                if not self._relative_path(path):
                    raise ValueError("workspace root is a directory")
                runtime_path = self._runtime_path(path)
                parent = posixpath.dirname(runtime_path)
                b64 = base64.b64encode(content).decode("ascii")
                cmd = (
                    f"mkdir -p -- {shlex.quote(parent)} && "
                    f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(runtime_path)}"
                )
                code, _out, err = await self.exec_result(cmd, timeout=120)
                if code != 0:
                    responses.append(
                        FileUploadResponse(path=path, error=err.strip() or "upload failed")
                    )
                else:
                    responses.append(FileUploadResponse(path=path))
            except ValueError:
                responses.append(FileUploadResponse(path=path, error=INVALID_PATH))
            except httpx.HTTPError as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        if self.protocol != "process":
            return await super().adownload_files(paths)

        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                if not self._relative_path(path):
                    raise ValueError("workspace root is a directory")
                runtime_path = self._runtime_path(path)
                cmd = (
                    f"if [ ! -f {shlex.quote(runtime_path)} ]; then exit 44; fi; "
                    f"base64 < {shlex.quote(runtime_path)} | tr -d '\\n'"
                )
                code, out, err = await self.exec_result(cmd, timeout=120)
                if code == 44:
                    responses.append(FileDownloadResponse(path=path, error=FILE_NOT_FOUND))
                elif code != 0:
                    responses.append(
                        FileDownloadResponse(path=path, error=err.strip() or "download failed")
                    )
                else:
                    responses.append(
                        FileDownloadResponse(path=path, content=base64.b64decode(out.strip()))
                    )
            except ValueError:
                responses.append(FileDownloadResponse(path=path, error=INVALID_PATH))
            except httpx.HTTPError as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
        return responses


class SubstrateRouterTlsBuilder:
    """Build an SSLContext for mTLS connections from Alephat Harness to atenet-router."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._splitter = PemBundleSplitter()

    def build(self, router_url: str) -> ssl.SSLContext | None:
        if not router_url.lower().startswith("https://"):
            return None
        ca_path = self._settings.substrate_router_ca_cert_path or (
            self._settings.substrate_api_ca_cert_path
            or ("/var/run/ate/ca.crt" if Path("/var/run/ate/ca.crt").exists() else None)
        )
        ctx = (
            ssl.create_default_context(cafile=ca_path)
            if ca_path and Path(ca_path).exists()
            else ssl.create_default_context()
        )
        cert_path = self._settings.substrate_router_client_cert_path
        key_path = self._settings.substrate_router_client_key_path
        bundle_path = self._settings.substrate_router_credential_bundle_path or (
            "/var/run/ate/credential-bundle.pem"
            if Path("/var/run/ate/credential-bundle.pem").exists()
            else None
        )
        if cert_path and key_path and Path(cert_path).exists() and Path(key_path).exists():
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        elif bundle_path and Path(bundle_path).exists():
            cert_bytes, key_bytes = self._splitter.split_pem(Path(bundle_path).read_bytes())
            with (
                tempfile.NamedTemporaryFile(delete=False) as cert_f,
                tempfile.NamedTemporaryFile(delete=False) as key_f,
            ):
                cert_f.write(cert_bytes)
                key_f.write(key_bytes)
                cert_tmp, key_tmp = cert_f.name, key_f.name
            try:
                ctx.load_cert_chain(certfile=cert_tmp, keyfile=key_tmp)
            finally:
                for p in (cert_tmp, key_tmp):
                    if os.path.exists(p):
                        os.unlink(p)
        return ctx
