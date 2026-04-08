import aiohttp
import asyncio
import os
import hashlib
from typing import Callable

class AsyncDownloader:
    def __init__(self, max_concurrent: int = 10):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        # FIX: share one session for the lifetime of the downloader instead of
        # creating a new ClientSession per request (causes ResourceWarning and
        # wastes TCP connection setup time).
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "gnome-mc-launcher"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_json(self, url: str) -> dict:
        session = await self._get_session()
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def download_file(self, url: str, dest: str, sha1: str = None, callback: Callable = None):
        async with self.semaphore:
            if os.path.exists(dest):
                if sha1 and self._check_hash(dest, sha1):
                    return  # Файл уже существует и цел

            # FIX: dirname can be "" if dest is a bare filename → makedirs("") raises FileNotFoundError
            dest_dir = os.path.dirname(dest)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)

            session = await self._get_session()
            async with session.get(url) as response:
                response.raise_for_status()
                with open(dest, "wb") as f:
                    while chunk := await response.content.read(8192):
                        f.write(chunk)
                        if callback:
                            callback(len(chunk))

    def _check_hash(self, filepath: str, expected_sha1: str) -> bool:
        sha1 = hashlib.sha1()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                sha1.update(chunk)
        return sha1.hexdigest() == expected_sha1

