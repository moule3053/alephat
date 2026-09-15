"""Apply the idempotent platform schema before services start."""

import asyncio
import uuid
from pathlib import Path

import asyncpg

from alephat_platform.common.config import Settings, get_settings


async def _sync_development_defaults(connection: asyncpg.Connection, settings: Settings) -> None:
    if not settings.bootstrap_dev_defaults:
        return

    await connection.execute(
        """
        UPDATE org_settings
        SET default_model = $2,
            default_sandbox_provider = $3,
            enabled_sandbox_providers = CASE
                WHEN $3 = ANY(enabled_sandbox_providers) THEN enabled_sandbox_providers
                ELSE array_append(enabled_sandbox_providers, $3)
            END,
            updated_at = now()
        WHERE org_id = $1
        """,
        uuid.UUID(settings.default_org_id),
        settings.default_model,
        settings.default_sandbox_provider,
    )


async def migrate() -> None:
    settings = get_settings()
    database_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    schema = Path(__file__).parents[1] / "migrations" / "001_init.sql"
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute("SELECT pg_advisory_lock(hashtext('alephat-platform-migration'))")
        await connection.execute(schema.read_text())
        await _sync_development_defaults(connection, settings)
    finally:
        await connection.close()


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
