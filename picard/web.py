"""Web UI — local dashboard for PICARD configuration and management.

Serves a single-page app at http://localhost:8471 that provides:
  - Adapter registry browser with credential configuration
  - RAID pool management
  - Mount-point (virtual path) overview
  - Live config.toml editing
"""

from __future__ import annotations

import asyncio
import inspect
import importlib
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from picard.adapters.base import Adapter
from picard.config import (
    DEFAULT_CONFIG_PATH,
    AdapterEntry,
    PicardConfig,
    init_config,
    load_config,
    mask_secrets,
)
from picard.index import Index
from picard.orchestrator import Orchestrator
from picard.raid import RaidLevel
from picard.registry import get_adapter_class, list_adapter_types

app = FastAPI(title="PICARD", docs_url=None, redoc_url=None)

_SAFE_ORIGINS = ("http://127.0.0.1", "http://localhost", "http://[::1]")


@app.middleware("http")
async def _security_middleware(request: Request, call_next):
    client = request.client
    if client and client.host not in ("127.0.0.1", "::1", "testclient"):
        return JSONResponse(status_code=403, content={"error": "local access only"})
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin", "")
        if origin and not any(origin.startswith(o) for o in _SAFE_ORIGINS):
            return JSONResponse(status_code=403, content={"error": "CSRF rejected"})
    return await call_next(request)


_UI_HTML = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")

_orch: Orchestrator | None = None
_index: Index | None = None


def _set_state(orch: Orchestrator | None, index: Index | None) -> None:
    global _orch, _index
    _orch = orch
    _index = index


def _ensure_index() -> Index:
    if _index is not None:
        return _index
    cfg = load_config()
    db_path = cfg.index_db
    if db_path.startswith("~"):
        db_path = str(Path(db_path).expanduser())
    pp = os.environ.get("PICARD_INDEX_PASSPHRASE") or None
    return Index(db_path, passphrase=pp)


# ── models ────────────────────────────────────────────────────────────────

class AdapterConfigRequest(BaseModel):
    name: str
    type: str
    enabled: bool = True
    params: dict[str, Any] = {}


class RaidPoolRequest(BaseModel):
    name: str
    level: str = "raid0"
    adapter_names: list[str] = []
    mirror_count: int = 2


class CredentialRequest(BaseModel):
    adapter_name: str
    param_name: str
    value: str


# ── API routes ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _UI_HTML


@app.get("/api/adapters/registry")
async def list_registry():
    """All known adapter types with their constructor signatures."""
    result = []
    for type_name in list_adapter_types():
        try:
            cls = get_adapter_class(type_name)
            sig = inspect.signature(cls.__init__)
            params = []
            for pname, p in sig.parameters.items():
                if pname == "self":
                    continue
                if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    continue
                annotation = ""
                if p.annotation is not inspect.Parameter.empty:
                    annotation = getattr(p.annotation, "__name__", str(p.annotation))
                params.append({
                    "name": pname,
                    "required": p.default is inspect.Parameter.empty,
                    "default": None if p.default is inspect.Parameter.empty else repr(p.default),
                    "annotation": annotation,
                    "is_credential": _is_credential_param(pname),
                })
            result.append({
                "type": type_name,
                "class": cls.__name__,
                "params": params,
                "category": _categorize_adapter(type_name),
            })
        except Exception:
            result.append({"type": type_name, "class": "?", "params": [], "category": "unknown"})
    return result


@app.get("/api/adapters/configured")
async def list_configured():
    """Adapters currently in config.toml."""
    cfg = load_config()
    return [
        {
            "name": e.name,
            "type": e.type,
            "enabled": e.enabled,
            "params": mask_secrets(e.params),
        }
        for e in cfg.adapters
    ]


@app.post("/api/adapters/configure")
async def configure_adapter(req: AdapterConfigRequest):
    """Add or update an adapter in config.toml."""
    cfg = load_config()
    existing = next((e for e in cfg.adapters if e.name == req.name), None)
    if existing:
        existing.type = req.type
        existing.enabled = req.enabled
        existing.params = req.params
    else:
        cfg.adapters.append(AdapterEntry(
            name=req.name,
            type=req.type,
            enabled=req.enabled,
            params=req.params,
        ))
    _save_config(cfg)
    return {"status": "ok", "adapter": req.name}


@app.delete("/api/adapters/{name}")
async def remove_adapter(name: str):
    """Remove an adapter from config.toml."""
    cfg = load_config()
    cfg.adapters = [e for e in cfg.adapters if e.name != name]
    _save_config(cfg)
    return {"status": "ok"}


@app.post("/api/adapters/{name}/toggle")
async def toggle_adapter(name: str):
    """Enable/disable an adapter."""
    cfg = load_config()
    entry = next((e for e in cfg.adapters if e.name == name), None)
    if not entry:
        raise HTTPException(404, f"adapter not found: {name}")
    entry.enabled = not entry.enabled
    _save_config(cfg)
    return {"status": "ok", "enabled": entry.enabled}


@app.post("/api/credentials")
async def set_credential(req: CredentialRequest):
    """Store a credential in the OS keyring."""
    try:
        import keyring
        service = f"picard/{req.adapter_name}"
        keyring.set_password(service, req.param_name, req.value)
        cfg = load_config()
        for entry in cfg.adapters:
            if entry.name == req.adapter_name:
                entry.params[req.param_name] = f"keyring:{service}/{req.param_name}"
                break
        _save_config(cfg)
        return {"status": "ok", "ref": f"keyring:{service}/{req.param_name}"}
    except ImportError:
        raise HTTPException(500, "keyring module not installed")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/config")
async def get_config():
    """Return the current configuration."""
    cfg = load_config()
    return {
        "index_db": cfg.index_db,
        "block_size": cfg.block_size,
        "adapters": [
            {
                "name": e.name,
                "type": e.type,
                "enabled": e.enabled,
                "params": mask_secrets(e.params),
            }
            for e in cfg.adapters
        ],
        "config_path": str(DEFAULT_CONFIG_PATH),
        "config_exists": DEFAULT_CONFIG_PATH.is_file(),
    }


@app.post("/api/config")
async def update_config(data: dict[str, Any]):
    """Update general configuration."""
    cfg = load_config()
    if "block_size" in data:
        cfg.block_size = int(data["block_size"])
    if "index_db" in data:
        cfg.index_db = data["index_db"]
    _save_config(cfg)
    return {"status": "ok"}


@app.post("/api/config/init")
async def init_config_endpoint():
    """Create default config.toml."""
    try:
        path = init_config()
        return {"status": "ok", "path": str(path)}
    except FileExistsError:
        raise HTTPException(409, "config already exists")


@app.get("/api/raid/levels")
async def raid_levels():
    """Available RAID levels and their requirements."""
    return [
        {"level": "raid0", "name": "RAID 0 — Striping", "min_adapters": 2,
         "description": "Round-robin striping. Maximum throughput, no redundancy."},
        {"level": "raid1", "name": "RAID 1 — Mirroring", "min_adapters": 2,
         "description": "Write to all adapters. Full redundancy, N× storage."},
        {"level": "raid5", "name": "RAID 5 — Parity", "min_adapters": 3,
         "description": "Striping with single parity. Survives one adapter failure."},
        {"level": "raid10", "name": "RAID 10 — Striped Mirrors", "min_adapters": 4,
         "description": "Mirror groups, striped across groups. Requires even count."},
    ]


@app.get("/api/scatter/profiles")
async def scatter_profiles():
    """Available scatter profiles (built-in + custom from config)."""
    from picard.scatter import list_profiles

    cfg = load_config()
    profiles = list_profiles(cfg.scatter_profiles)
    return [
        {
            "name": p.name,
            "raid_level": p.raid_level,
            "mirror_count": p.mirror_count,
            "adapter_tags": p.adapter_tags,
            "adapter_names": p.adapter_names,
            "min_adapters": p.min_adapters,
            "description": p.description,
        }
        for p in profiles
    ]


@app.get("/api/files")
async def list_files():
    """List all stored virtual paths with metadata."""
    cfg = load_config()
    db_path = cfg.index_db
    if db_path.startswith("~"):
        db_path = str(Path(db_path).expanduser())
    if not Path(db_path).is_file():
        return []
    try:
        idx = Index(db_path)
        paths = idx.list_paths()
        result = []
        for p in paths:
            manifest = idx.get_manifest_by_path(p)
            if manifest:
                adapters_used = set()
                for block in manifest.blocks:
                    for cr in block.chunks:
                        adapters_used.add(cr.adapter_name)
                result.append({
                    "path": manifest.path,
                    "manifest_id": manifest.manifest_id,
                    "original_size": manifest.original_size,
                    "block_size": manifest.block_size,
                    "total_blocks": manifest.total_blocks,
                    "total_chunks": manifest.total_chunks,
                    "created_at": manifest.created_at,
                    "adapters": sorted(adapters_used),
                })
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/stats")
async def system_stats():
    """System-wide stats."""
    types = list_adapter_types()
    cfg = load_config()
    configured = len(cfg.adapters)
    enabled = sum(1 for e in cfg.adapters if e.enabled)

    categories: dict[str, int] = {}
    for t in types:
        cat = _categorize_adapter(t)
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "total_adapters": len(types),
        "configured_adapters": configured,
        "enabled_adapters": enabled,
        "categories": categories,
        "block_size": cfg.block_size,
        "config_path": str(DEFAULT_CONFIG_PATH),
    }


# ── filesystem API ────────────────────────────────────────────────────────


@app.get("/api/fs/tree")
async def fs_tree(path: str = "/"):
    if _orch is None:
        raise HTTPException(503, "orchestrator not initialized")
    return await _orch.tree(path)


@app.get("/api/fs/ls")
async def fs_ls(path: str = ""):
    idx = _ensure_index()
    if path:
        children = idx.list_children(path)
    else:
        children = idx.list_paths()
    result = []
    for p in children:
        m = idx.get_manifest_by_path(p)
        if m:
            result.append({
                "path": p,
                "name": p.rsplit("/", 1)[-1],
                "type": m.file_type,
                "size": m.original_size,
                "version": m.version,
                "mtime": m.mtime,
                "mode": m.mode,
            })
        else:
            result.append({"path": p, "name": p.rsplit("/", 1)[-1]})
    return result


@app.get("/api/fs/stat")
async def fs_stat(path: str):
    idx = _ensure_index()
    m = idx.get_manifest_by_path(path)
    if m is None:
        raise HTTPException(404, f"not found: {path}")
    return {
        "path": m.path,
        "type": m.file_type,
        "size": m.original_size,
        "mode": m.mode,
        "mtime": m.mtime,
        "ctime": m.ctime,
        "atime": m.atime,
        "uid": m.uid,
        "gid": m.gid,
        "version": m.version,
        "nlink": m.nlink,
        "symlink_target": m.symlink_target,
        "manifest_id": m.manifest_id,
        "plaintext_hash": m.plaintext_hash,
        "block_size": m.block_size,
        "total_blocks": m.total_blocks,
        "parent_path": m.parent_path,
    }


@app.get("/api/fs/versions")
async def fs_versions(path: str):
    idx = _ensure_index()
    return idx.list_versions(path)


@app.post("/api/fs/mkdir")
async def fs_mkdir(body: dict):
    if _orch is None:
        raise HTTPException(503, "orchestrator not initialized")
    path = body.get("path", "")
    parents = body.get("parents", False)
    if not path:
        raise HTTPException(400, "path required")
    m = await _orch.mkdir(path, parents=parents)
    return {"path": m.path, "manifest_id": m.manifest_id}


@app.delete("/api/fs/rm")
async def fs_rm(path: str):
    if _orch is None:
        raise HTTPException(503, "orchestrator not initialized")
    result = await _orch.rm(path)
    return {"deleted": result, "path": path}


# ── peers API ─────────────────────────────────────────────────────────────


@app.get("/api/peers")
async def api_list_peers():
    idx = _ensure_index()
    from picard.peer import PeerRegistry
    registry = PeerRegistry(idx.connection)
    peers = registry.list_peers()
    return [
        {"url": p.url, "adapters": p.adapters, "status": p.status, "last_seen": p.last_seen}
        for p in peers
    ]


@app.post("/api/peers")
async def api_add_peer(body: dict):
    idx = _ensure_index()
    from picard.peer import PeerRegistry
    url = body.get("url", "")
    if not url:
        raise HTTPException(400, "url required")
    registry = PeerRegistry(idx.connection)
    registry.add(url, body.get("adapters", []))
    return {"added": url}


@app.delete("/api/peers")
async def api_remove_peer(url: str):
    idx = _ensure_index()
    from picard.peer import PeerRegistry
    registry = PeerRegistry(idx.connection)
    result = registry.remove(url)
    return {"removed": result, "url": url}


# ── journal API ───────────────────────────────────────────────────────────


@app.get("/api/journal")
async def journal_status():
    idx = _ensure_index()
    from picard.journal import Journal
    journal = Journal(idx.connection)
    entries = journal.get_incomplete()
    return [
        {
            "journal_id": e.journal_id,
            "operation": e.operation,
            "path": e.path,
            "state": e.state.value,
            "created_at": e.created_at,
        }
        for e in entries
    ]


@app.post("/api/recover")
async def run_recover():
    if _orch is None:
        raise HTTPException(503, "orchestrator not initialized")
    result = await _orch.recover()
    return result


# ── helpers ───────────────────────────────────────────────────────────────

def _is_credential_param(name: str) -> bool:
    cred_words = {"key", "token", "secret", "password", "credential", "api_key",
                  "app_password", "auth", "access_key", "secret_key", "private_key"}
    lower = name.lower()
    return any(w in lower for w in cred_words)


_CATEGORY_MAP = {
    "network": {"dns", "doh", "dot", "doq", "mqtt", "icmp", "ntp", "ntp-ext", "snmp",
                "snmp-trap", "sip", "coap", "amqp", "tftp", "ftp", "websocket",
                "grpc-metadata", "mdns", "ssdp", "udp", "tcp-options", "arp", "llmnr",
                "quic", "stun", "rtp", "bgp", "ospf", "vxlan", "gre", "netflow",
                "multicast", "proxy-protocol", "pop3", "smtp-imap", "irc", "finger",
                "gopher", "gemini", "ipv6-flowlabel", "lldp", "nfs", "rsync", "smb",
                "ssh", "stomp", "nats", "kafka", "zeromq", "syslog", "ipp",
                "dhcp", "radius", "http-headers", "wpad", "plan9"},
    "social": {"bluesky", "reddit", "mastodon", "discord", "telegram", "matrix",
               "slack", "nostr", "xmpp", "youtube-comments", "hackernews", "twitter",
               "stackoverflow", "spotify", "twitch", "tiktok", "linkedin", "tumblr",
               "wordpress", "medium", "flickr", "pinterest", "soundcloud", "imgur",
               "steam", "signal", "whatsapp"},
    "storage": {"s3", "redis", "memcached", "etcd", "consul", "couchdb", "sqlite-local",
                "sqlite-remote", "postgresql", "dynamodb", "mongodb", "cassandra",
                "neo4j", "clickhouse", "elasticsearch", "meilisearch", "gcs",
                "backblaze-b2", "wasabi", "minio", "do-spaces", "google-drive",
                "dropbox", "azure-blob", "cloudflare-kv", "cloudflare-r2", "vercel-kv",
                "firebase", "influxdb", "filesystem", "memory", "webdav",
                "caldav", "carddav", "ipfs"},
    "cloud": {"aws-sns", "aws-sqs", "google-pubsub", "discord-webhook",
              "slack-webhook", "teams-webhook", "pushover", "ntfy", "lambda",
              "gcp-functions", "webex"},
    "cicd": {"github", "github-actions", "github-gist", "gitlab", "gitlab-ci",
             "gitlab-snippet", "k8s-configmap", "k8s-secret", "docker-labels",
             "docker-registry", "nomad", "vault", "google-sheets",
             "confluence", "notion", "airtable", "trello"},
    "industrial": {"modbus", "opcua", "bacnet", "zigbee", "lorawan", "canbus",
                   "i2c", "spi", "uart"},
    "radio": {"aprs", "packet-radio", "iridium", "lora-direct", "wifi-beacon",
              "bluetooth-le", "nfc-ndef"},
    "blockchain": {"ethereum", "bitcoin", "arweave", "solana", "polygon", "tezos",
                   "cardano", "stellar", "near"},
    "steganography": {"audio-stego", "exif", "steganography", "qr-code",
                      "unicode-stego", "html-comments", "css-stego", "zip-comment",
                      "pdf-metadata", "docx-metadata", "xlsx-hidden",
                      "elf-section", "pe-resource", "macho-segment", "barcode"},
    "identity": {"ldap", "kerberos", "saml", "jwt", "acme", "ct-log", "ocsp",
                 "openvpn", "wireguard", "tor"},
    "packages": {"npm-registry", "pypi", "crates", "rubygems", "maven", "homebrew",
                 "npm-config", "pip-conf"},
    "os-level": {"windows-registry", "unix-xattr", "named-pipe", "proc", "env-vars",
                 "authorized-keys", "crontab", "bashrc", "hosts-file", "sysctl",
                 "gitconfig", "known-hosts", "clipboard", "usb-hid"},
    "enterprise": {"jira", "servicenow", "zendesk", "freshdesk", "pagerduty",
                   "opsgenie", "datadog", "grafana", "sentry", "splunk",
                   "newrelic", "loki", "prometheus", "intercom", "mediawiki",
                   "fandom", "wandb", "huggingface"},
    "misc": {"osm", "google-maps-reviews", "ical", "vcard", "atom", "rss",
             "toml", "yaml", "browser-bookmark", "at-jobs", "minecraft",
             "calendar", "pastebin", "dpaste", "ix", "nullpointer",
             "usenet", "whois", "cloudflare-dns"},
}

_TYPE_TO_CATEGORY: dict[str, str] = {}
for cat, types in _CATEGORY_MAP.items():
    for t in types:
        _TYPE_TO_CATEGORY[t] = cat


def _categorize_adapter(type_name: str) -> str:
    return _TYPE_TO_CATEGORY.get(type_name, "other")


def _escape_toml(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _format_toml_key(k: str) -> str:
    if all(c.isalnum() or c in "-_" for c in k) and k:
        return k
    return '"' + _escape_toml(k) + '"'


def _save_config(cfg: PicardConfig) -> None:
    """Write PicardConfig back to config.toml, preserving raid/scatter sections."""
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    preserved_sections: list[str] = []
    if DEFAULT_CONFIG_PATH.is_file():
        try:
            import tomllib
            existing = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
            if "raid" in existing:
                raw = existing["raid"]
                entries = raw if isinstance(raw, list) else [raw]
                for entry in entries:
                    section = ["[[raid]]\n"]
                    for rk, rv in entry.items():
                        if isinstance(rv, list):
                            section.append(f"{_format_toml_key(rk)} = {rv}\n")
                        elif isinstance(rv, str):
                            section.append(f'{_format_toml_key(rk)} = "{_escape_toml(rv)}"\n')
                        elif isinstance(rv, (int, float)):
                            section.append(f"{_format_toml_key(rk)} = {rv}\n")
                    preserved_sections.append("".join(section))
            if "scatter" in existing:
                for name, profile in existing["scatter"].items():
                    section = [f"[scatter.{_format_toml_key(name)}]\n"]
                    for sk, sv in profile.items():
                        if isinstance(sv, str):
                            section.append(f'{_format_toml_key(sk)} = "{_escape_toml(sv)}"\n')
                        elif isinstance(sv, list):
                            section.append(f"{_format_toml_key(sk)} = {sv}\n")
                        elif isinstance(sv, (int, float)):
                            section.append(f"{_format_toml_key(sk)} = {sv}\n")
                    preserved_sections.append("".join(section))
        except Exception:
            pass

    lines = ["[general]\n"]
    lines.append(f'index_db = "{_escape_toml(str(cfg.index_db))}"\n')
    lines.append(f"block_size = {cfg.block_size}\n")
    lines.append("\n")
    for entry in cfg.adapters:
        lines.append("[[adapters]]\n")
        lines.append(f'name = "{_escape_toml(entry.name)}"\n')
        lines.append(f'type = "{_escape_toml(entry.type)}"\n')
        lines.append(f"enabled = {'true' if entry.enabled else 'false'}\n")
        for k, v in entry.params.items():
            key = _format_toml_key(k)
            if isinstance(v, str):
                lines.append(f'{key} = "{_escape_toml(v)}"\n')
            elif isinstance(v, bool):
                lines.append(f"{key} = {'true' if v else 'false'}\n")
            elif isinstance(v, (int, float)):
                lines.append(f"{key} = {v}\n")
        lines.append("\n")
    for section in preserved_sections:
        lines.append(section)
        lines.append("\n")
    DEFAULT_CONFIG_PATH.write_text("".join(lines), encoding="utf-8")


def serve(host: str = "127.0.0.1", port: int = 8471) -> None:
    import uvicorn
    from picard.adapters.filesystem import FilesystemAdapter
    from picard.config import resolve_params
    from picard.journal import Journal
    from picard.raid import RaidLevel, RaidPool
    from picard.registry import instantiate

    cfg = load_config()
    db_path = cfg.index_db
    if db_path.startswith("~"):
        db_path = str(Path(db_path).expanduser())
    pp = os.environ.get("PICARD_INDEX_PASSPHRASE") or None
    index = Index(db_path, passphrase=pp)

    store_dir = Path("~/.picard/chunks").expanduser()
    by_name: dict[str, Adapter] = {}
    adapters: list[Adapter] = []
    for entry in cfg.adapters:
        if not entry.enabled:
            continue
        try:
            params = resolve_params(entry.params)
            adapter = instantiate(entry.type, **params)
            adapters.append(adapter)
            by_name[entry.name] = adapter
        except Exception:
            pass
    if cfg.raid:
        pooled_names: set[str] = set()
        pools: list[Adapter] = []
        for raid_entry in cfg.raid:
            pool_adapters = [by_name[n] for n in raid_entry.adapters if n in by_name]
            if len(pool_adapters) < 2:
                continue
            level = RaidLevel(raid_entry.level)
            pool = RaidPool(pool_adapters, level, mirror_count=raid_entry.mirror_count)
            pools.append(pool)
            pooled_names.update(raid_entry.adapters)
        remaining = [a for a in adapters if not any(
            name in pooled_names for name, obj in by_name.items() if obj is a
        )]
        adapters = pools + remaining
    if not adapters:
        store_dir.mkdir(parents=True, exist_ok=True)
        adapters = [FilesystemAdapter(store_dir)]

    journal = Journal(index.connection)
    orch = Orchestrator(adapters, index, journal=journal)
    _set_state(orch, index)
    uvicorn.run(app, host=host, port=port, log_level="info")
