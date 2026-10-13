"""Build VLESS, VMess and Trojan connection links and subscriptions."""
import base64
import json
import time
from urllib.parse import quote

from . import genpaths

SERVED_TRANSPORTS = {
    "vless": {"ws", "xhttp"},
    "vmess": {"ws", "xhttp"},
    "trojan": {"ws"},
}


def volume_text(used_bytes: int, quota_bytes: int) -> str:
    quota = quota_bytes or 0
    if 0 < quota < 1024**3:
        return f"{used_bytes / 1024**2:.2f}/{quota / 1024**2:.0f}MB"
    return f"{used_bytes / 1024**3:.2f}/{quota / 1024**3:g}GB"


def _transport_for(user: dict, settings: dict) -> str:
    protocol = (user.get("protocol") or "vless").lower()
    if protocol not in SERVED_TRANSPORTS:
        protocol = "vless"
    requested = (user.get("transport")
                 or settings.get("default_transport", "ws") or "ws").lower()
    allowed = SERVED_TRANSPORTS[protocol]
    return requested if requested in allowed else "ws"


def _fragment_params(settings: dict) -> dict:
    if not settings.get("fragment_enabled"):
        return {}
    return {
        "fp_len": settings.get("fragment_length", "10-30"),
        "fp_int": settings.get("fragment_interval", "10-20"),
    }


def _host_params(host: str, path: str, sni: str, fp: str, alpn: str,
                 transport: str, settings: dict) -> str:
    parts = [
        f"type={quote(transport, safe='')}",
        f"host={quote(host, safe='')}",
        f"path={quote(path, safe='/')}",
        f"sni={quote(sni, safe='')}",
        f"fp={quote(fp, safe='')}",
    ]
    if alpn:
        parts.append(f"alpn={quote(alpn, safe=',/')}")
    for key, value in _fragment_params(settings).items():
        parts.append(f"{key}={quote(str(value), safe='')}")
    return "&".join(parts)


def _path_for(protocol: str, transport: str) -> str:
    paths = genpaths.get_paths()
    if transport == "ws":
        return {
            "vless": paths["vless_ws"],
            "vmess": paths["vmess_ws"],
            "trojan": paths["trojan_ws"],
        }.get(protocol, paths["vless_ws"])
    if transport == "xhttp":
        return paths["vmess_xhttp"] if protocol == "vmess" else paths["xhttp"]
    return paths["vless_ws"]


def build_vless_link(host: str, port: int, user: dict, settings: dict) -> str:
    uuid = user["uuid"]
    transport = _transport_for(user, settings)
    security = "tls"
    fingerprint = user.get("fingerprint") or settings.get("default_fingerprint", "chrome")
    alpn = user.get("alpn", settings.get("default_alpn", "http/1.1"))
    sni = settings.get("sni_override") or host
    name = quote(f"TiTaN-{user.get('name', 'User')}-VLESS-{transport.upper()}")

    params = _host_params(
        host, _path_for("vless", transport), sni, fingerprint, alpn,
        transport, settings,
    )
    return (
        f"vless://{uuid}@{host}:{port}?encryption=none&security={security}&"
        f"{params}#{name}"
    )


def build_vmess_link(host: str, port: int, user: dict, settings: dict) -> str:
    transport = _transport_for(user, settings)
    security = "tls"
    fingerprint = user.get("fingerprint") or settings.get("default_fingerprint", "chrome")
    alpn = user.get("alpn", settings.get("default_alpn", "http/1.1"))
    sni = settings.get("sni_override") or host
    config_data = {
        "v": "2",
        "ps": f"TiTaN-{user.get('name', 'User')}-VMess-{transport.upper()}",
        "add": host,
        "port": str(port),
        "id": user["uuid"],
        "aid": "0",
        "scy": "auto",
        "net": transport,
        "type": "none",
        "host": host,
        "path": _path_for("vmess", transport),
        "tls": security,
        "sni": sni,
        "alpn": alpn,
        "fp": fingerprint,
    }
    encoded = base64.b64encode(
        json.dumps(config_data, separators=(",", ":")).encode()
    ).decode()
    return "vmess://" + encoded


def build_trojan_link(host: str, port: int, user: dict, settings: dict) -> str:
    password = quote(user["uuid"], safe="")
    sni = quote(settings.get("sni_override") or host, safe="")
    alpn = quote(user.get("alpn", settings.get("default_alpn", "http/1.1")), safe=",/")
    fingerprint = quote(
        user.get("fingerprint") or settings.get("default_fingerprint", "chrome"), safe=""
    )
    path = quote(_path_for("trojan", "ws"), safe="/")
    name = quote(f"TiTaN-{user.get('name', 'User')}-Trojan-WS")
    return (
        f"trojan://{password}@{host}:{port}?security=tls&type=ws&"
        f"host={quote(host, safe='')}&path={path}&sni={sni}&alpn={alpn}&fp={fingerprint}#{name}"
    )


def build_links(host: str, port: int, user: dict, settings: dict,
                server_pub: str = "") -> dict:
    """Return the main link and the link collection for one supported user."""
    protocol = (user.get("protocol") or "vless").lower()
    if protocol not in SERVED_TRANSPORTS:
        user = {**user, "protocol": "vless", "transport": "ws", "security": "tls"}
        protocol = "vless"
    if protocol == "vless":
        main_link = build_vless_link(host, port, user, settings)
    elif protocol == "vmess":
        main_link = build_vmess_link(host, port, user, settings)
    else:
        main_link = build_trojan_link(host, port, user, settings)

    used = (user.get("used_up") or 0) + (user.get("used_down") or 0)
    expiry = ""
    if user.get("expire_at"):
        expiry = f"{max(0, int((user['expire_at'] - time.time()) // 86400))}d"
    remark = (
        f"TiTaN {user.get('name', 'User')} | "
        f"{volume_text(used, user.get('quota_bytes') or 0)} | {expiry or '∞'}"
    )
    dummy_path = quote(genpaths.get_paths()["vless_ws"], safe="/")
    dummy = (
        "vless://00000000-0000-0000-0000-000000000001@127.0.0.1:443?"
        f"encryption=none&security=none&type=ws&host=127.0.0.1&path={dummy_path}#{quote(remark)}"
    )
    info = [{"remark": remark, "link": dummy, "kind": "status"}]
    return {"main": main_link, "all": [main_link], "info": info}


def subscription_text(links: list[str]) -> str:
    return base64.b64encode("\n".join(links).encode()).decode()
