"""Generate and persist private transport paths for this TiTaN install."""
import json
import os
import re
import secrets
import tempfile

from . import config

PATHS_FILE = os.path.join(config.DATA_DIR, "paths.json")
NGINX_PATHS_FILE = os.path.join(config.DATA_DIR, "nginx-paths.conf")

_PATH_PREFIXES = {
    "vless_ws": "/vl-ws/",
    "vmess_ws": "/vm-ws/",
    "trojan_ws": "/tr-ws/",
    "xhttp": "/xhttp/",
    "vmess_xhttp": "/xhttp/vmess/",
}


def _new_paths() -> dict[str, str]:
    return {
        key: prefix + secrets.token_hex(8)
        for key, prefix in _PATH_PREFIXES.items()
    }


def _valid_paths(data) -> bool:
    if not isinstance(data, dict):
        return False
    for key, prefix in _PATH_PREFIXES.items():
        value = data.get(key)
        if not isinstance(value, str) or not re.fullmatch(
            re.escape(prefix) + r"[0-9a-f]{16}", value
        ):
            return False
    return True


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".titan-paths-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_or_create() -> dict[str, str]:
    try:
        with open(PATHS_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
        if _valid_paths(saved):
            paths = {key: saved[key] for key in _PATH_PREFIXES}
            if paths != saved:
                _atomic_write(PATHS_FILE, json.dumps(paths, indent=2, sort_keys=True) + "\n")
            return paths
    except (OSError, ValueError, TypeError):
        pass
    paths = _new_paths()
    _atomic_write(PATHS_FILE, json.dumps(paths, indent=2, sort_keys=True) + "\n")
    return paths


def _http_location(
    path: str, port: int, websocket: bool = False, prefix: bool = False
) -> str:
    matcher = "^~ " if prefix else "= "
    if websocket:
        return f"""location {matcher}{path} {{
    proxy_pass http://127.0.0.1:{port};
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection \"upgrade\";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}}"""
    return f"""location {matcher}{path} {{
    proxy_pass http://127.0.0.1:{port};
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}}"""


def _write_nginx_include(paths: dict[str, str]) -> None:
    locations = [
        _http_location(paths["vless_ws"], config.XRAY_VLESS_WS_PORT, websocket=True),
        _http_location(paths["vmess_ws"], config.XRAY_VMESS_WS_PORT, websocket=True),
        _http_location(paths["trojan_ws"], config.XRAY_TROJAN_WS_PORT, websocket=True),
        _http_location(paths["xhttp"], config.XRAY_VLESS_XHTTP_PORT, prefix=True),
        _http_location(paths["vmess_xhttp"], config.XRAY_VMESS_XHTTP_PORT, prefix=True),
    ]
    _atomic_write(NGINX_PATHS_FILE, "\n\n".join(locations) + "\n")


def get_paths() -> dict[str, str]:
    """Return the persistent path map and keep its Nginx include in sync."""
    paths = _load_or_create()
    _write_nginx_include(paths)
    return paths


def main() -> None:
    get_paths()
    print(f"Saved transport paths to {PATHS_FILE}")
    print(f"Generated Nginx locations in {NGINX_PATHS_FILE}")


if __name__ == "__main__":
    main()
