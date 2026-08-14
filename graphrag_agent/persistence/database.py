"""Async SQLite engine with the required reliability pragmas."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from graphrag_agent.config.settings import APP_DATABASE_URL


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        return
    raw_path = database_url[len(prefix):]
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


class Database:
    def __init__(self, url: str = APP_DATABASE_URL, *, echo: bool = False):
        if not url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("本地 MVP 仅支持 sqlite+aiosqlite 数据库 URL")
        _ensure_sqlite_parent(url)
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, echo=echo)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._install_pragmas()

    def _install_pragmas(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            async with session.begin():
                yield session

    async def create_schema(self) -> None:
        """Create schema for tests/dev; deployed environments use Alembic."""
        from .models import Base
        from .fts import install_fts

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await install_fts(connection)

    async def foreign_key_violations(self) -> list[tuple]:
        async with self.engine.connect() as connection:
            result = await connection.execute(text("PRAGMA foreign_key_check"))
            return [tuple(row) for row in result.fetchall()]

    async def close(self) -> None:
        await self.engine.dispose()
