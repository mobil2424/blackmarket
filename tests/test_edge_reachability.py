"""Acceptance tests for the loopback Xray + HTTP transport architecture."""
import base64
import json
from pathlib import Path

import pytest

from app import db, genpaths, xray

ORIGIN = {"Origin": "http://testserver"}
XRAY_INTERNAL_PORTS = set(range(10001, 10006)) | {10085}


@pytest.mark.parametrize(
    ("protocol", "transport", "security"),
    [
        ("vless", "ws", "tls"),
        ("vless", "xhttp", "tls"),
        ("vmess", "ws", "tls"),
        ("vmess", "xhttp", "tls"),
        ("trojan", "ws", "tls"),
    ],
)
def test_supported_protocol_transport_links_are_generated(admin, protocol, transport, security):
    response = admin.post("/api/users", headers=ORIGIN, json={
        "name": f"{protocol}-{transport}",
        "protocol": protocol,
        "transport": transport,
        "security": security,
    })
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert (user["protocol"], user["transport"], user["security"]) == (
        protocol, transport, security
    )
    assert user["main_link"].startswith(f"{protocol}://")
    if protocol == "vmess":
        encoded = user["main_link"].split("://", 1)[1]
        payload = json.loads(base64.b64decode(encoded + "=="))
        assert payload["net"] == transport and payload["tls"] == "tls"
        assert 1 <= int(payload["port"]) <= 65535
        assert int(payload["port"]) not in XRAY_INTERNAL_PORTS
    else:
        assert f"type={transport}" in user["main_link"]
        assert "security=tls" in user["main_link"]
        assert not any(f":{port}" in user["main_link"] for port in XRAY_INTERNAL_PORTS)
    admin.delete(f"/api/users/{user['uid']}", headers=ORIGIN)


@pytest.mark.parametrize("protocol", ["shadowsocks", "hysteria2", "wireguard"])
def test_retired_protocols_normalize_to_vless_websocket_tls(admin, protocol):
    response = admin.post("/api/users", headers=ORIGIN, json={
        "name": f"retired-{protocol}", "protocol": protocol,
        "transport": "xhttp", "security": "tls",
    })
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert (user["protocol"], user["transport"], user["security"]) == ("vless", "ws", "tls")
    assert user["main_link"].startswith("vless://")
    assert "type=ws" in user["main_link"]
    assert not user["main_link"].startswith(("wireguard://", "hysteria2://", "ss://"))
    admin.delete(f"/api/users/{user['uid']}", headers=ORIGIN)


@pytest.mark.parametrize("protocol", ["vless", "vmess", "trojan"])
def test_raw_tcp_and_reality_choices_remap_to_vless_websocket_tls(admin, protocol):
    response = admin.post("/api/users", headers=ORIGIN, json={
        "name": f"legacy-{protocol}", "protocol": protocol,
        "transport": "tcp", "security": "reality",
    })
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["protocol"] == "vless"
    assert user["transport"] == "ws" and user["security"] == "tls"
    assert user["main_link"].startswith("vless://")
    assert "type=tcp" not in user["main_link"] and "security=reality" not in user["main_link"]
    assert not any(f":{port}" in user["main_link"] for port in XRAY_INTERNAL_PORTS)
    admin.delete(f"/api/users/{user['uid']}", headers=ORIGIN)


def test_xray_inbounds_are_loopback_only_and_never_raw_tcp(monkeypatch):
    users = [
        {"uid": "a", "uuid": "00000000-0000-0000-0000-00000000000a", "enabled": 1,
         "protocol": "vless"},
        {"uid": "b", "uuid": "00000000-0000-0000-0000-00000000000b", "enabled": 1,
         "protocol": "vmess"},
        {"uid": "c", "uuid": "00000000-0000-0000-0000-00000000000c", "enabled": 1,
         "protocol": "trojan"},
    ]
    monkeypatch.setattr(xray, "_xray_users", lambda: users)
    monkeypatch.setattr(db, "get_settings", lambda: {})
    result = xray.generate_xray_config()
    inbounds = result["inbounds"]
    assert inbounds and all(item["listen"] == "127.0.0.1" for item in inbounds)
    assert {item["protocol"] for item in inbounds} == {"dokodemo-door", "vless", "vmess", "trojan"}
    networks = {
        item["streamSettings"]["network"]
        for item in inbounds if "streamSettings" in item
    }
    assert networks == {"ws", "xhttp"}
    assert "tcp" not in networks and "raw" not in networks and "reality" not in str(result).lower()
    assert all(item["listen"] != "0.0.0.0" for item in inbounds)


def test_generated_paths_persist_and_create_matching_nginx_routes(tmp_path, monkeypatch):
    paths_file = tmp_path / "paths.json"
    include_file = tmp_path / "nginx-paths.conf"
    monkeypatch.setattr(genpaths, "PATHS_FILE", str(paths_file))
    monkeypatch.setattr(genpaths, "NGINX_PATHS_FILE", str(include_file))
    first = genpaths.get_paths()
    second = genpaths.get_paths()
    assert first == second
    assert json.loads(paths_file.read_text(encoding="utf-8")) == first
    nginx = include_file.read_text(encoding="utf-8")
    assert f"location = {first['vless_ws']}" in nginx
    assert f"location = {first['vmess_ws']}" in nginx
    assert f"location = {first['trojan_ws']}" in nginx
    assert f"location ^~ {first['xhttp']}" in nginx
    assert f"location ^~ {first['vmess_xhttp']}" in nginx
    assert set(first) == {"vless_ws", "vmess_ws", "trojan_ws", "xhttp", "vmess_xhttp"}
    assert "127.0.0.1:10001" in nginx and "127.0.0.1:10005" in nginx
    assert "httpupgrade" not in nginx.lower() and "grpc" not in nginx.lower()
    assert "location = /dns-query" not in nginx


def test_path_migration_keeps_active_paths_and_drops_retired_keys(tmp_path, monkeypatch):
    paths_file = tmp_path / "paths.json"
    include_file = tmp_path / "nginx-paths.conf"
    monkeypatch.setattr(genpaths, "PATHS_FILE", str(paths_file))
    monkeypatch.setattr(genpaths, "NGINX_PATHS_FILE", str(include_file))
    legacy = {
        "vless_ws": "/vl-ws/" + "1" * 16,
        "vmess_ws": "/vm-ws/" + "2" * 16,
        "trojan_ws": "/tr-ws/" + "3" * 16,
        "xhttp": "/xhttp/" + "4" * 16,
        "vmess_xhttp": "/xhttp/vmess/" + "5" * 16,
        "httpupgrade": "/hup/" + "6" * 16,
        "grpc_service": "titan-" + "7" * 16,
    }
    paths_file.write_text(json.dumps(legacy), encoding="utf-8")

    paths = genpaths.get_paths()

    assert set(paths) == {"vless_ws", "vmess_ws", "trojan_ws", "xhttp", "vmess_xhttp"}
    assert all(paths[key] == legacy[key] for key in paths)
    assert json.loads(paths_file.read_text(encoding="utf-8")) == paths


def test_only_ws_xhttp_routes_and_xray_networks_are_generated(tmp_path, monkeypatch):
    paths_file = tmp_path / "paths.json"
    include_file = tmp_path / "nginx-paths.conf"
    monkeypatch.setattr(genpaths, "PATHS_FILE", str(paths_file))
    monkeypatch.setattr(genpaths, "NGINX_PATHS_FILE", str(include_file))
    genpaths.get_paths()
    generated = include_file.read_text(encoding="utf-8")
    assert "httpupgrade" not in generated.lower()
    assert "grpc" not in generated.lower()
    monkeypatch.setattr(xray, "_xray_users", lambda: [{
        "uid": "a", "uuid": "00000000-0000-0000-0000-00000000000a",
        "enabled": 1, "protocol": "vless",
    }])
    monkeypatch.setattr(db, "get_settings", lambda: {})
    networks = {
        item["streamSettings"]["network"]
        for item in xray.generate_xray_config()["inbounds"]
        if "streamSettings" in item
    }
    assert networks == {"ws", "xhttp"}


def test_subscription_status_marker_does_not_advertise_raw_tcp(admin):
    response = admin.post("/api/users", headers=ORIGIN, json={
        "name": "subscription-marker", "protocol": "vless", "transport": "ws"
    })
    assert response.status_code == 200, response.text
    uid = response.json()["user"]["uid"]
    body = base64.b64decode(admin.get(f"/sub/{uid}").text).decode()
    marker = next(line for line in body.splitlines() if "00000000-0000-0000-0000-000000000001" in line)
    assert "type=ws" in marker and "type=tcp" not in marker
    assert "headerType" not in marker
    admin.delete(f"/api/users/{uid}", headers=ORIGIN)


def test_public_nginx_front_door_keeps_dns_query_private(repo_root):
    nginx = (Path(repo_root) / "nginx.conf").read_text(encoding="utf-8")
    assert "listen ${PORT} default_server;" in nginx
    assert "server 127.0.0.1:${PANEL_PORT};" in nginx
    assert "limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;" in nginx
    assert "limit_req_zone $binary_remote_addr zone=sub:10m rate=5r/s;" in nginx
    assert "include /data/nginx-paths.conf;" in nginx
    assert "location = /dns-query" not in nginx
    assert "listen 10001" not in nginx and "listen 10085" not in nginx
