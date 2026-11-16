"""Temporal Worker entrypoint for Alephat DeepAgents execution."""

import asyncio
import logging
import signal
from collections.abc import Sequence

from temporalio.client import Client
from temporalio.deepagents import DeepAgentsPlugin
from temporalio.worker import Worker

from alephat_platform.common.config import Settings, get_settings
from alephat_platform.common.db import dispose_engine
from alephat_platform.common.temporal import (
    STREAMING_TOPIC,
    DataConverterFactory,
    TemporalTlsBuilder,
)
from alephat_platform.harness.activities import PlatformActivities, RepositoryProvisioner
from alephat_platform.harness.agents import MODEL_PROVIDER
from alephat_platform.harness.sandboxes import SANDBOX_REGISTRY
from alephat_platform.harness.workflows import ScheduledTaskWorkflow, TaskAgentWorkflow

logger = logging.getLogger(__name__)

WORKFLOW_PASSTHROUGH_MODULES: tuple[str, ...] = (
    "anyio",
    "fireworks",
    "langchain_fireworks",
    "langchain_google_genai",
    "langchain_openai",
    "openai",
    "alephat_platform.common.temporal",
    "alephat_platform.harness.agents",
    "alephat_platform.harness.agent_sandbox_backend",
    "alephat_platform.harness.contracts",
    "alephat_platform.harness.sandboxes",
    "openswe.sandboxes.providers.local",
)


class TemporalAgentWorker:
    """Self-hosted Temporal Worker hosting DeepAgentsPlugin workflows and activities."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()
        self.activities = PlatformActivities(self.settings)

    def request_stop(self) -> None:
        self._stop.set()

    def build_plugin(
        self,
        *,
        passthrough_modules: Sequence[str] = WORKFLOW_PASSTHROUGH_MODULES,
    ) -> DeepAgentsPlugin:
        return DeepAgentsPlugin(
            model_provider=MODEL_PROVIDER,
            backends=SANDBOX_REGISTRY.backend_factories(),
            streaming_topic=STREAMING_TOPIC,
            passthrough_modules=passthrough_modules,
        )

    async def connect_client(self) -> Client:
        converter = DataConverterFactory(self.settings.token_encryption_key).for_worker()
        tls_cfg = TemporalTlsBuilder(self.settings).build()
        plugin = self.build_plugin()
        while not self._stop.is_set():
            try:
                return await Client.connect(
                    self.settings.temporal_address,
                    namespace=self.settings.temporal_namespace,
                    data_converter=converter,
                    tls=tls_cfg,
                    plugins=[plugin],
                )
            except Exception:
                logger.warning(
                    "Waiting for Temporal service before starting worker",
                    extra={"temporal_address": self.settings.temporal_address},
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=3.0)
                except TimeoutError:
                    continue
        raise asyncio.CancelledError("Worker stopped before connecting to Temporal")

    async def start(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [temporal-worker] %(message)s",
        )
        client = await self.connect_client()
        worker = Worker(
            client,
            task_queue=self.settings.temporal_task_queue,
            workflows=[TaskAgentWorkflow, ScheduledTaskWorkflow],
            activities=self.activities.activity_list(),
            identity=self.settings.worker_id,
            max_concurrent_activities=16,
            max_concurrent_workflow_tasks=16,
        )
        logger.info(
            "Temporal DeepAgents worker started",
            extra={
                "worker_id": self.settings.worker_id,
                "temporal_address": self.settings.temporal_address,
                "task_queue": self.settings.temporal_task_queue,
            },
        )
        run_task = asyncio.create_task(worker.run())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                [run_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and not run_task.done():
                await worker.shutdown()
                await run_task
            elif run_task in done:
                run_task.result()
        finally:
            stop_task.cancel()
            await dispose_engine()


PROVISIONER = RepositoryProvisioner()


def main() -> None:
    worker = TemporalAgentWorker()
    loop = asyncio.new_event_loop()

    def handle_signal(_sig: int, _frame: object) -> None:
        worker.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    loop.run_until_complete(worker.start())


if __name__ == "__main__":
    main()
