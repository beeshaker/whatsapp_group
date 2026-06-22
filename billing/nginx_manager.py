import logging
import os
import re
from pathlib import Path

import docker
import docker.errors

_log = logging.getLogger(__name__)

NGINX_CONF_DIR = os.getenv("NGINX_CONF_DIR", "/nginx-conf")
NGINX_CONTAINER_NAME = os.getenv("NGINX_CONTAINER_NAME", "billing-nginx-1")
_PORTS_FILE = "00-client-ports.conf"


def _ports_path() -> Path:
    return Path(NGINX_CONF_DIR) / _PORTS_FILE


def _read_entries() -> dict[str, int]:
    """Parse existing subdomain→port entries from the conf file."""
    path = _ports_path()
    entries: dict[str, int] = {}
    if not path.exists():
        return entries
    for line in path.read_text().splitlines():
        m = re.match(r"\s+([\w-]+)\s+(\d+);", line)
        if m and m.group(1) != "default":
            entries[m.group(1)] = int(m.group(2))
    return entries


def _write_entries(entries: dict[str, int]) -> None:
    path = _ports_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["map $client $backend_port {"]
    for name in sorted(entries):
        lines.append(f"    {name}  {entries[name]};")
    lines.append("    default   8001;")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n")


def _reload_nginx() -> None:
    try:
        dc = docker.from_env()
        container = dc.containers.get(NGINX_CONTAINER_NAME)
        result = container.exec_run("nginx -s reload")
        if result.exit_code != 0:
            _log.error("nginx reload failed (exit %d): %s", result.exit_code, result.output)
        else:
            _log.info("nginx reloaded after conf update")
    except docker.errors.NotFound:
        _log.warning("nginx container %r not found — skipping reload", NGINX_CONTAINER_NAME)
    except Exception as exc:
        _log.error("Failed to reload nginx: %s", exc)


def add_client_port(subdomain: str, port: int) -> None:
    """Register or update subdomain→port and reload nginx."""
    entries = _read_entries()
    entries[subdomain] = port
    _write_entries(entries)
    _reload_nginx()


def remove_client_port(subdomain: str) -> None:
    """Remove a subdomain entry and reload nginx."""
    entries = _read_entries()
    if subdomain not in entries:
        return
    del entries[subdomain]
    _write_entries(entries)
    _reload_nginx()
