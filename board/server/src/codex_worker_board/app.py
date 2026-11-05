"""REST, WebSocket polling, localhost protection and optional SPA serving."""

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse

from .accounts import Accounts
from .store import Store

REPOSITORY = Path(__file__).resolve().parents[4]
PING_SECONDS = 20
FILE_LIMIT = 256 * 1024
LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?", re.I)


@dataclass
class Settings:
    task_home: Path
    codex_task: Path
    poll_seconds: float = 1.0
    accounts_seconds: float = 300
    web_dist: Path = REPOSITORY / "board/web/dist"
    stale_days: float = 7

    @classmethod
    def from_env(cls):
        return cls(
            task_home=Path(os.environ.get("CODEX_TASK_HOME", "~/.codex")).expanduser(),
            codex_task=Path(os.environ.get(
                "BOARD_CODEX_TASK",
                str(REPOSITORY / "plugins/codex-worker/skills/codex-worker/scripts/codex_task.py"),
            )).expanduser(),
            poll_seconds=float(os.environ.get("BOARD_POLL_SECONDS", "1.0")),
            accounts_seconds=float(os.environ.get("BOARD_ACCOUNTS_SECONDS", "300")),
            stale_days=float(os.environ.get("BOARD_STALE_DAYS", "7")),
        )


def local_origin(value):
    try:
        origin = urlsplit(value)
        return (origin.scheme in ("http", "https") and not origin.path
                and not origin.query and not origin.fragment
                and LOCAL_HOST.fullmatch(origin.netloc) is not None)
    except ValueError:
        return False


class LocalOnly:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            hosts = [value.decode("latin-1") for key, value in scope["headers"] if key == b"host"]
            origins = [value.decode("latin-1") for key, value in scope["headers"] if key == b"origin"]
            allowed = len(hosts) == 1 and LOCAL_HOST.fullmatch(hosts[0]) is not None
            if scope["type"] == "websocket" and origins:
                allowed = allowed and len(origins) == 1 and local_origin(origins[0])
            if not allowed:
                if scope["type"] == "websocket":
                    socket = WebSocket(scope, receive, send)
                    await socket.send_denial_response(PlainTextResponse("Forbidden", status_code=403))
                else:
                    await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class Board:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.task_home / "claude-tasks", stale_days=settings.stale_days)
        self.accounts = Accounts(settings.codex_task, settings.accounts_seconds)
        self.lock = asyncio.Lock()
        self.clients = set()

    def publish(self, message):
        for queue in self.clients:
            queue.put_nowait(message)

    async def poll(self):
        async with self.lock:
            messages = await asyncio.to_thread(self.store.poll)
            for message in messages:
                self.publish(message)

    async def snapshot(self):
        async with self.lock:
            return {"generated_at": time.time(), "tasks": self.store.summaries(),
                    "accounts": self.accounts.value}

    async def poll_tasks(self):
        while True:
            await asyncio.sleep(self.settings.poll_seconds)
            await self.poll()

    async def poll_accounts(self):
        while True:
            started = time.monotonic()
            value = await self.accounts.refresh()
            if value is not None:
                self.publish({"type": "accounts", "accounts": value})
            await asyncio.sleep(max(0, self.settings.accounts_seconds - (time.monotonic() - started)))


def read_tail(path):
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - FILE_LIMIT))
        raw = stream.read(FILE_LIMIT)
    # Ignore a split multibyte character at the start of the byte-limited tail.
    return raw.decode("utf-8", "ignore")


def create_app(settings=None):
    board = Board(settings or Settings.from_env())

    @asynccontextmanager
    async def lifespan(app):
        await board.poll()
        jobs = [asyncio.create_task(board.poll_tasks()), asyncio.create_task(board.poll_accounts())]
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnly)
    app.state.board = board

    @app.get("/api/snapshot")
    async def snapshot():
        return await board.snapshot()

    @app.get("/api/tasks/{slug}")
    async def detail(slug: str):
        async with board.lock:
            task = await asyncio.to_thread(board.store.detail, slug)
        if task is None:
            raise HTTPException(404)
        return task

    @app.get("/api/tasks/{slug}/files/{name}")
    async def task_file(slug: str, name: str):
        async with board.lock:
            path = await asyncio.to_thread(board.store.file_path, slug, name)
            if path is None:
                raise HTTPException(404)
            try:
                content = await asyncio.to_thread(read_tail, path)
            except OSError:
                raise HTTPException(404)
        return PlainTextResponse(content)

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await socket.accept()
        queue = asyncio.Queue()
        board.clients.add(queue)

        async def send_updates():
            next_ping = time.monotonic() + PING_SECONDS
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), max(0, next_ping - time.monotonic()))
                except asyncio.TimeoutError:
                    message = {"type": "ping", "at": time.time()}
                    next_ping = time.monotonic() + PING_SECONDS
                await socket.send_json(message)

        async def ignore_messages():
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        try:
            await socket.send_json({"type": "snapshot", "snapshot": await board.snapshot()})
            async with anyio.create_task_group() as group:
                async def writer():
                    try:
                        await send_updates()
                    except WebSocketDisconnect:
                        group.cancel_scope.cancel()

                group.start_soon(writer)
                try:
                    await ignore_messages()
                finally:
                    group.cancel_scope.cancel()
        finally:
            board.clients.discard(queue)

    @app.get("/{path:path}")
    async def web(path: str):
        if path == "api" or path.startswith("api/") or path == "ws":
            raise HTTPException(404)
        dist = board.settings.web_dist.resolve()
        if not dist.is_dir():
            if path:
                raise HTTPException(404)
            return PlainTextResponse("Build the web app in board/web to serve the task board.\n")
        candidate = (dist / path).resolve()
        if not candidate.is_relative_to(dist):
            raise HTTPException(404)
        if candidate.is_file():
            return FileResponse(candidate)
        index = (dist / "index.html").resolve()
        if not index.is_relative_to(dist) or not index.is_file():
            raise HTTPException(404)
        return FileResponse(index)

    return app
