"""Entry point: python -m yotobridge"""
import asyncio
import logging
import os
import shutil
import signal

import aiohttp
from aiohttp import web

from . import config
from .app import Bridge, make_app


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("yotobridge")
    cfg = config.load()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH")
    if not cfg.abs_api_key:
        raise SystemExit("abs_api_key is not set (config.yaml or ABS_API_KEY)")
    async with aiohttp.ClientSession() as http:
        br = Bridge(cfg, http)
        try:
            libs = await br.abs.libraries()
            log.info("ABS reachable at %s, %d book librar%s visible", cfg.abs_url, len(libs), "y" if len(libs) == 1 else "ies")
        except Exception as e:
            log.error("Cannot reach ABS at %s: %s", cfg.abs_url, e)
        # listen first: connecting to Yoto (and refreshing playlists) takes a few seconds
        runner = web.AppRunner(make_app(br))
        await runner.setup()
        await web.TCPSite(runner, cfg.listen_host, cfg.listen_port).start()
        log.info("Bridge listening on %s:%d; players will fetch %s/s/<card>.mp3",
                 cfg.listen_host, cfg.listen_port, cfg.public_url)
        br.collection_index.refresh()      # warm the book picker's collection lookup
        if br.auth.logged_in:
            await br.connect_yoto()
        else:
            log.warning("Not logged in to Yoto yet: open the bridge page (this host, port %s) and press Connect Yoto. "
                        "Yoto must list %s as an Allowed Callback URL.", cfg.listen_port, br.auth.redirect_uri)
        sync_task = asyncio.create_task(br.pm.run())
        alert_task = asyncio.create_task(br.alerts.run())
        if not br.alerts.enabled:
            log.info("Alerts are off (set ntfy_url or discord_webhook_url in config.yaml to get a push when something breaks)")
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await stop.wait()   # docker stop sends SIGTERM: close ABS sessions with the final position
        finally:
            sync_task.cancel()
            alert_task.cancel()
            await br.pm.shutdown()
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
