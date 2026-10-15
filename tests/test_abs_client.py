"""ABS client: a slow Audiobookshelf gives a readable error, not an empty one."""
import asyncio
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotobridge.abs_client import ABSClient, ABSError  # noqa: E402


async def test_timeout_becomes_a_readable_abs_error():
    async def slow(request):
        await asyncio.sleep(2)
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/api/libraries", slow)
    srv = TestServer(app)
    await srv.start_server()
    async with aiohttp.ClientSession() as http:
        abs_ = ABSClient(http, str(srv.make_url("")).rstrip("/"), "k")
        with pytest.raises(ABSError) as e:
            await abs_._req("GET", "/api/libraries", timeout=0.2)
    await srv.close()
    assert e.value.status == 504 and "did not answer" in str(e.value)
