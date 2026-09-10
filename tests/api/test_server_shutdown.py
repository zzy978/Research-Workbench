import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager, nullcontext

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from backend.server import create_config


def test_real_app_sigint_exits_process_and_closes_database(tmp_path):
    # An isolated database and injected workflow avoid resuming user jobs or
    # making paid model calls. Exercise the actual app lifespan and SIGINT path.
    code = '''
import torch
import asyncio, signal, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
from backend.app.main import create_app
from backend.server import create_config
import uvicorn, httpx
root = Path(sys.argv[1])
app = create_app(database_url='sqlite+aiosqlite:///' + (root/'app.db').as_posix(),
    workflow_factory=lambda *args: None, artifact_root=root/'artifacts',
    skills_root=root/'skills', auto_resume=False)
server = uvicorn.Server(create_config(app=app, port=0))
async def interrupt():
    while not server.started:
        await asyncio.sleep(.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient() as client:
        for _ in range(3):
            response = await client.get(f'http://127.0.0.1:{port}/api/v1/cache/stats')
            assert response.status_code == 200
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(10)
async def main():
    task = asyncio.create_task(interrupt())
    try:
        await server.serve()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
print('SIGINT_EXIT_COMPLETE', flush=True)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'Application shutdown complete' in result.stderr
    assert 'SIGINT_EXIT_COMPLETE' in result.stdout


def test_default_shutdown_has_a_finite_deadline():
    config = create_config()
    assert 0 < config.timeout_graceful_shutdown <= 10
    assert config.workers == 1


@pytest.mark.asyncio
async def test_stuck_connection_wait_still_reaches_lifespan_shutdown():
    closed = asyncio.Event()

    class Listener:
        def close(self):
            pass

        async def wait_closed(self):
            await asyncio.Event().wait()

    class Lifespan:
        async def shutdown(self):
            closed.set()

    server = uvicorn.Server(create_config())
    server.config.timeout_graceful_shutdown = .05
    server.servers = [Listener()]
    server.lifespan = Lifespan()
    await asyncio.wait_for(server.shutdown(), timeout=1)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_active_http_stream_cannot_block_exit():
    cleaned = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        yield
        cleaned.set()

    app = FastAPI(lifespan=lifespan)

    @app.get('/stream')
    async def stream():
        async def body():
            yield b'ready\n'
            await asyncio.Event().wait()
        return StreamingResponse(body())

    config = create_config(app=app, port=0)
    config.timeout_graceful_shutdown = .1
    server = uvicorn.Server(config)
    server.capture_signals = nullcontext
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            async with client.stream('GET', f'http://127.0.0.1:{port}/stream') as response:
                assert await anext(response.aiter_lines()) == 'ready'
                server.should_exit = True
                await asyncio.wait_for(asyncio.shield(task), 2)
                assert cleaned.is_set()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
