"""Generate the supported Xray configuration and manage the Xray process."""
import asyncio
import json
import logging
import os
import re
import signal
import subprocess

from . import config, db, genpaths
from . import nodes as nodesync

log = logging.getLogger("titan.xray")

_previous_stats: dict[str, dict] = {}
_xray_process: subprocess.Popen | None = None
_stats_lock = asyncio.Lock()


def xray_available() -> bool:
    return os.path.exists(config.XRAY_BIN)


def _external_pid_file() -> str:
    return os.path.join(config.DATA_DIR, "xray.pid")


def _external_pid() -> int | None:
    try:
        with open(_external_pid_file(), encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        return pid if pid > 1 else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def xray_running() -> bool:
    """Return whether the Xray child process is alive."""
    if config.XRAY_EXTERNAL_SUPERVISOR:
        return _pid_alive(_external_pid())
    return bool(_xray_process and _xray_process.poll() is None)


def _blocked_rules(settings: dict) -> list[dict]:
    rules = []
    if settings.get("block_ads"):
        rules.append({
            "type": "field",
            "domain": ["geosite:category-ads-all"],
            "outboundTag": "block-ads",
        })
    if settings.get("block_iran_sites"):
        rules.append({
            "type": "field",
            "domain": ["geosite:category-iran"],
            "outboundTag": "block-iran",
        })
    if settings.get("restrict_ips"):
        rules.append({
            "type": "field",
            "ip": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"],
            "outboundTag": "block",
        })
    return rules


def _xray_users() -> list[dict]:
    return nodesync.local_users()


def generate_xray_config() -> dict:
    """Build the active user inbounds and Xray stats services."""
    users = _xray_users()
    settings = db.get_settings()
    paths = genpaths.get_paths()

    def mk_client(user: dict) -> dict:
        return {"id": user["uuid"], "email": user["uid"]}

    vless_clients = [
        mk_client(user) for user in users
        if user.get("enabled") and user.get("protocol") == "vless"
    ]
    vmess_clients = [
        mk_client(user) for user in users
        if user.get("enabled") and user.get("protocol") == "vmess"
    ]
    trojan_clients = [
        {"password": user["uuid"], "email": user["uid"]}
        for user in users
        if user.get("enabled") and user.get("protocol") == "trojan"
    ]

    outbounds = [
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "blackhole", "tag": "block"},
        {"protocol": "blackhole", "tag": "block-ads"},
        {"protocol": "blackhole", "tag": "block-iran"},
        {"protocol": "blackhole", "tag": "block-adult"},
        {"protocol": "blackhole", "tag": "block-custom"},
    ]
    routing_rules = [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]
    routing_rules.extend(_blocked_rules(settings))

    inbounds = [{
        "listen": "127.0.0.1",
        "port": config.XRAY_API_PORT,
        "protocol": "dokodemo-door",
        "settings": {"address": "127.0.0.1"},
        "tag": "api",
    }]

    if vless_clients:
        inbounds.extend([
            {
                "listen": "127.0.0.1",
                "port": config.XRAY_VLESS_WS_PORT,
                "protocol": "vless",
                "settings": {"clients": vless_clients, "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "wsSettings": {"path": paths["vless_ws"]},
                },
                "tag": "in-vless-ws",
            },
            {
                "listen": "127.0.0.1",
                "port": config.XRAY_VLESS_XHTTP_PORT,
                "protocol": "vless",
                "settings": {"clients": vless_clients, "decryption": "none"},
                "streamSettings": {
                    "network": "xhttp",
                    "xhttpSettings": {"path": paths["xhttp"]},
                },
                "tag": "in-vless-xhttp",
            },
        ])
    if vmess_clients:
        inbounds.extend([
            {
                "listen": "127.0.0.1",
                "port": config.XRAY_VMESS_WS_PORT,
                "protocol": "vmess",
                "settings": {"clients": vmess_clients},
                "streamSettings": {
                    "network": "ws",
                    "wsSettings": {"path": paths["vmess_ws"]},
                },
                "tag": "in-vmess-ws",
            },
            {
                "listen": "127.0.0.1",
                "port": config.XRAY_VMESS_XHTTP_PORT,
                "protocol": "vmess",
                "settings": {"clients": vmess_clients},
                "streamSettings": {
                    "network": "xhttp",
                    "xhttpSettings": {"path": paths["vmess_xhttp"]},
                },
                "tag": "in-vmess-xhttp",
            },
        ])

    if trojan_clients:
        inbounds.append({
            "listen": "127.0.0.1",
            "port": config.XRAY_TROJAN_WS_PORT,
            "protocol": "trojan",
            "settings": {"clients": trojan_clients},
            "streamSettings": {
                "network": "ws",
                "wsSettings": {"path": paths["trojan_ws"]},
            },
            "tag": "in-trojan-ws",
        })

    return {
        "log": {"loglevel": "warning"},
        "dns": {
            "servers": [
                "https+local://1.1.1.1/dns-query",
                "https+local://8.8.8.8/dns-query",
                "1.1.1.1",
                "8.8.8.8",
                "localhost",
            ],
            "queryStrategy": "UseIPv4",
        },
        "api": {"tag": "api", "services": ["StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"rules": routing_rules},
    }


def write_xray_config() -> dict:
    cfg = generate_xray_config()
    directory = os.path.dirname(config.XRAY_CONFIG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = config.XRAY_CONFIG_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2)
    os.replace(temporary, config.XRAY_CONFIG_PATH)
    return cfg


def restart_xray() -> None:
    """Ask the configured process manager to restart the Xray child."""
    global _xray_process
    if config.XRAY_EXTERNAL_SUPERVISOR:
        pid = _external_pid()
        if pid and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("Xray restart requested (pid=%s)", pid)
            except OSError as exc:
                log.warning("could not request Xray restart: %s", exc)
        else:
            log.info("Xray supervisor will start the configured process")
        return

    if _xray_process and _xray_process.poll() is None:
        _xray_process.terminate()
        try:
            _xray_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _xray_process.kill()
    if xray_available():
        os.makedirs(os.path.dirname(config.XRAY_LOG_PATH), exist_ok=True)
        log_file = open(config.XRAY_LOG_PATH, "a", encoding="utf-8")
        _xray_process = subprocess.Popen(
            [config.XRAY_BIN, "run", "-c", config.XRAY_CONFIG_PATH],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        log.info("Xray restarted (pid=%s)", _xray_process.pid)
    else:
        log.warning("Xray binary not found — running in development mode")


async def get_xray_stats() -> dict:
    """Query Xray stats and return per-user traffic deltas."""
    global _previous_stats
    if not xray_available():
        return {}
    async with _stats_lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                config.XRAY_BIN,
                "api",
                "statsquery",
                f"--server=127.0.0.1:{config.XRAY_API_PORT}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore")
            if not output.strip():
                return {}
            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                matches = re.findall(r'name:\s*"([^"]+)"\s*value:\s*(\d+)', output)
                if not matches:
                    return {}
                data = {"stat": [{"name": name, "value": int(value)}
                                 for name, value in matches]}

            current: dict[str, dict] = {}
            for item in data.get("stat", []):
                name = item.get("name")
                value = item.get("value")
                if not name or value is None:
                    continue
                parts = name.split(">>>")
                if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
                    uid, direction, amount = parts[1], parts[3], int(value)
                    current.setdefault(uid, {"up": 0, "down": 0})
                    if direction == "uplink":
                        current[uid]["up"] += amount
                    elif direction == "downlink":
                        current[uid]["down"] += amount

            deltas: dict[str, dict] = {}
            for uid, stats in current.items():
                previous = _previous_stats.get(uid, {"up": 0, "down": 0})
                up = stats["up"] - previous["up"]
                down = stats["down"] - previous["down"]
                if up < 0:
                    up = stats["up"]
                if down < 0:
                    down = stats["down"]
                if up > 0 or down > 0:
                    deltas[uid] = {"up": up, "down": down}
            _previous_stats = current
            return deltas
        except Exception as exc:  # noqa: BLE001
            log.error("Error querying Xray stats: %s", exc)
            return {}
