"""Disposable browser server: real Factory, MCP, Graph workers and public BFF."""
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fixtures.cancellation_stack import cancellation_stack


async def main():
    with TemporaryDirectory(prefix='igraph-cancel-e2e-') as directory:
        async with cancellation_stack(Path(directory), 'http://127.0.0.1:11129') as (app, _graph, _controller, _metrics):
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=11129, log_level='warning', lifespan='off'))
            await server.serve()


if __name__ == '__main__':
    asyncio.run(main())
