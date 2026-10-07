"""WebDAV server — mount PICARD's encrypted filesystem as a network drive.

Exposes the orchestrator's virtual filesystem over WebDAV so that OS
file managers (Explorer, Finder, Nautilus) can browse, drag-and-drop,
and edit files with native tooling.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.dav_error import (
    DAVError,
    HTTP_FORBIDDEN,
    HTTP_NOT_FOUND,
    HTTP_CONFLICT,
    HTTP_METHOD_NOT_ALLOWED,
)

from picard.index import Index
from picard.manifest import Manifest
from picard.orchestrator import Orchestrator

logger = logging.getLogger("picard.webdav")


def _run(coro):
    """Run an async coroutine synchronously, reusing the current event loop if possible."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _parse_iso(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


class PicardFile(DAVNonCollection):
    """A file resource backed by the PICARD orchestrator."""

    def __init__(self, path: str, environ: dict, manifest: Manifest,
                 orch: Orchestrator, passphrase: str, read_only: bool):
        super().__init__(path, environ)
        self._manifest = manifest
        self._orch = orch
        self._passphrase = passphrase
        self._read_only = read_only

    def get_content_length(self) -> int:
        return self._manifest.original_size

    def get_content_type(self) -> str | None:
        import mimetypes
        mt, _ = mimetypes.guess_type(self._manifest.path)
        return mt or "application/octet-stream"

    def get_creation_date(self) -> datetime:
        return _parse_iso(self._manifest.ctime or self._manifest.created_at)

    def get_last_modified(self) -> datetime:
        return _parse_iso(self._manifest.mtime or self._manifest.created_at)

    def get_display_name(self) -> str:
        return self._manifest.path.rstrip("/").rsplit("/", 1)[-1]

    def get_etag(self) -> str | None:
        return f'"{self._manifest.manifest_id}"'

    def support_etag(self) -> bool:
        return True

    def get_content(self) -> io.BytesIO:
        data = _run(self._orch.get(self._manifest.path, self._passphrase))
        return io.BytesIO(data)

    def begin_write(self, *, content_type: str | None = None) -> io.BytesIO:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        return io.BytesIO()

    def end_write(self, *, with_errors: bool = False) -> None:
        pass

    def handle_copy(self, dest_path: str, *, depth_infinity: bool = True) -> bool:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_dest = dest_path.rstrip("/")
        if not virt_dest.startswith("/"):
            virt_dest = "/" + virt_dest
        _run(self._orch.cp(self._manifest.path, virt_dest))
        return True

    def handle_move(self, dest_path: str) -> bool:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_dest = dest_path.rstrip("/")
        if not virt_dest.startswith("/"):
            virt_dest = "/" + virt_dest
        _run(self._orch.mv(self._manifest.path, virt_dest))
        return True

    def handle_delete(self) -> bool:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        _run(self._orch.rm(self._manifest.path))
        return True

    def support_ranges(self) -> bool:
        return False


class PicardDirectory(DAVCollection):
    """A directory resource backed by the PICARD orchestrator."""

    def __init__(self, path: str, environ: dict, manifest: Manifest | None,
                 orch: Orchestrator, index: Index, passphrase: str, read_only: bool):
        super().__init__(path, environ)
        self._manifest = manifest
        self._orch = orch
        self._index = index
        self._passphrase = passphrase
        self._read_only = read_only

    def get_creation_date(self) -> datetime:
        if self._manifest:
            return _parse_iso(self._manifest.ctime or self._manifest.created_at)
        return datetime.now(timezone.utc)

    def get_last_modified(self) -> datetime:
        if self._manifest:
            return _parse_iso(self._manifest.mtime or self._manifest.created_at)
        return datetime.now(timezone.utc)

    def get_display_name(self) -> str:
        p = self.path.rstrip("/")
        if not p:
            return "/"
        return p.rsplit("/", 1)[-1]

    def get_member_names(self) -> list[str]:
        virt_path = self.path.rstrip("/") or "/"
        children = self._index.list_children(virt_path)
        return [c.rstrip("/").rsplit("/", 1)[-1] for c in children]

    def get_member(self, name: str) -> PicardFile | PicardDirectory | None:
        virt_path = self.path.rstrip("/") or ""
        child_path = f"{virt_path}/{name}"

        m = self._index.get_manifest_by_path(child_path)
        if m is None:
            return None

        if m.file_type == "directory":
            return PicardDirectory(
                child_path + "/", self.environ, m,
                self._orch, self._index, self._passphrase, self._read_only,
            )
        return PicardFile(
            child_path, self.environ, m,
            self._orch, self._passphrase, self._read_only,
        )

    def create_empty_resource(self, name: str) -> PicardFile:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_path = self.path.rstrip("/") or ""
        child_path = f"{virt_path}/{name}"
        m = _run(self._orch.put(child_path, b"", self._passphrase))
        return PicardFile(
            child_path, self.environ, m,
            self._orch, self._passphrase, self._read_only,
        )

    def create_collection(self, name: str) -> PicardDirectory:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_path = self.path.rstrip("/") or ""
        child_path = f"{virt_path}/{name}"
        m = _run(self._orch.mkdir(child_path, parents=True))
        return PicardDirectory(
            child_path + "/", self.environ, m,
            self._orch, self._index, self._passphrase, self._read_only,
        )

    def handle_delete(self) -> bool:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_path = self.path.rstrip("/")
        if not virt_path or virt_path == "/":
            raise DAVError(HTTP_FORBIDDEN, "cannot delete root")
        _run(self._orch.rm(virt_path))
        return True

    def handle_move(self, dest_path: str) -> bool:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_path = self.path.rstrip("/")
        virt_dest = dest_path.rstrip("/")
        if not virt_dest.startswith("/"):
            virt_dest = "/" + virt_dest
        _run(self._orch.mv(virt_path, virt_dest))
        return True

    def handle_copy(self, dest_path: str, *, depth_infinity: bool = True) -> bool:
        if self._read_only:
            raise DAVError(HTTP_FORBIDDEN, "read-only mode")
        virt_path = self.path.rstrip("/")
        virt_dest = dest_path.rstrip("/")
        if not virt_dest.startswith("/"):
            virt_dest = "/" + virt_dest
        _run(self._orch.cp(virt_path, virt_dest))
        return True


class PicardProvider(DAVProvider):
    """WsgiDAV provider mapping WebDAV operations to PICARD's orchestrator."""

    def __init__(self, orch: Orchestrator, index: Index,
                 passphrase: str, read_only: bool = False):
        super().__init__()
        self._orch = orch
        self._index = index
        self._passphrase = passphrase
        self._read_only = read_only

    def get_resource_inst(self, path: str, environ: dict) -> PicardFile | PicardDirectory | None:
        virt_path = path.rstrip("/") or "/"

        if virt_path == "/":
            return PicardDirectory(
                "/", environ, None,
                self._orch, self._index, self._passphrase, self._read_only,
            )

        m = self._index.get_manifest_by_path(virt_path)
        if m is None:
            return None

        if m.file_type == "directory":
            return PicardDirectory(
                path if path.endswith("/") else path + "/",
                environ, m,
                self._orch, self._index, self._passphrase, self._read_only,
            )
        return PicardFile(
            path, environ, m,
            self._orch, self._passphrase, self._read_only,
        )

    def is_readonly(self) -> bool:
        return self._read_only


class _PutInterceptor:
    """WSGI middleware that intercepts PUT requests to write file data through the orchestrator."""

    def __init__(self, app, orch: Orchestrator, passphrase: str, read_only: bool):
        self._app = app
        self._orch = orch
        self._passphrase = passphrase
        self._read_only = read_only

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD") == "PUT" and not self._read_only:
            path = environ.get("PATH_INFO", "/").rstrip("/")
            if not path:
                path = "/"
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
            except (ValueError, TypeError):
                content_length = 0

            body = environ["wsgi.input"].read(content_length) if content_length else b""
            try:
                _run(self._orch.put(path, body, self._passphrase))
                start_response("201 Created", [("Content-Length", "0")])
                return [b""]
            except Exception as e:
                logger.error("PUT %s failed: %s", path, e)
                msg = str(e).encode()
                start_response("500 Internal Server Error", [
                    ("Content-Length", str(len(msg))),
                    ("Content-Type", "text/plain"),
                ])
                return [msg]

        return self._app(environ, start_response)


def serve_webdav(
    *,
    host: str = "127.0.0.1",
    port: int = 8472,
    passphrase: str = "",
    read_only: bool = False,
    config_path: Path | None = None,
    index_passphrase: str | None = None,
) -> None:
    """Start a WsgiDAV server exposing PICARD's filesystem."""
    import os
    from cheroot.wsgi import Server as CherootServer
    from wsgidav.wsgidav_app import WsgiDAVApp

    from picard.adapters.base import Adapter
    from picard.adapters.filesystem import FilesystemAdapter
    from picard.config import load_config, resolve_params, DEFAULT_CONFIG_PATH
    from picard.journal import Journal
    from picard.registry import instantiate

    cfg = load_config(config_path or DEFAULT_CONFIG_PATH)
    db_path = cfg.index_db
    if db_path.startswith("~"):
        db_path = str(Path(db_path).expanduser())

    idx_pp = index_passphrase or os.environ.get("PICARD_INDEX_PASSPHRASE") or None
    index = Index(db_path, passphrase=idx_pp)

    store_dir = Path("~/.picard/chunks").expanduser()
    adapters: list[Adapter] = []
    for entry in cfg.adapters:
        if not entry.enabled:
            continue
        try:
            params = resolve_params(entry.params)
            adapter = instantiate(entry.type, **params)
            adapters.append(adapter)
        except Exception:
            pass

    if not adapters:
        store_dir.mkdir(parents=True, exist_ok=True)
        adapters = [FilesystemAdapter(store_dir)]

    journal = Journal(index.connection)
    orch = Orchestrator(adapters, index, journal=journal)

    pp = passphrase or os.environ.get("PICARD_PASSPHRASE", "")

    provider = PicardProvider(orch, index, pp, read_only=read_only)

    dav_config: dict[str, Any] = {
        "provider_mapping": {"/": provider},
        "verbose": 1,
        "logging": {"enable": True, "enable_loggers": []},
        "http_authenticator": {"domain_controller": None},
        "simple_dc": {"user_mapping": {"*": True}},
    }

    dav_app = WsgiDAVApp(dav_config)
    wrapped = _PutInterceptor(dav_app, orch, pp, read_only)

    server = CherootServer((host, port), wrapped)
    logger.info("PICARD WebDAV → http://%s:%d%s", host, port,
                " (read-only)" if read_only else "")
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    finally:
        _run(orch.close())
