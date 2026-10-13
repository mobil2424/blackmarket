# TiTaN deployment guide

TiTaN runs Nginx, the FastAPI panel, Xray and a watchdog in one Docker container. The public HTTP listener is Nginx on `$PORT`; internal services are bound to loopback. `$PORT` must be a valid port and must not collide with `PANEL_PORT` or any Xray listener port; startup rejects collisions.

## Railway / managed Docker platform

1. Deploy this repository with the included `Dockerfile` and `entrypoint.sh`.
2. Attach persistent storage at **`/data`**. This is required to keep `titan.db`, backups, `paths.json`, and logs across replacement containers.
3. Let the platform provide `PORT`; do not point it at FastAPI directly. The container fixes FastAPI at `127.0.0.1:10000` and fronts it with Nginx.
4. Open the generated HTTPS domain at `/login`. Set a unique admin password using `TITAN_ADMIN_PASS` before first boot, or change the initial password after login.

`railway.json` uses the Dockerfile and checks `/healthz`. `render.yaml` is provided as an example Docker service; add a persistent disk mounted at `/data` if the plan supports one.

## Container layout

```text
Public $PORT
  └─ Nginx
      ├─ /                 → /app/static/landing/index.html
      ├─ /api/*            → FastAPI 127.0.0.1:10000 (10 requests/sec, burst 20)
      ├─ /sub/*, /s/*, /p/*→ FastAPI 127.0.0.1:10000 (5 requests/sec, burst 10)
      ├─ /dashboard, etc.  → FastAPI 127.0.0.1:10000
      └─ generated random routes → Xray loopback listeners

watchdog.py checks Nginx, FastAPI and Xray every 5 seconds.
```

`entrypoint.sh` creates or loads `/data/paths.json`, writes the matching Nginx include at `/data/nginx-paths.conf`, generates the initial Xray config and starts Nginx. `watchdog.py` supervises the three processes; Xray is restarted by `start_xray.sh`. FastAPI writes the config and signals the Xray child through `/data/xray.pid`; it does not start a second Xray process in the container. Watchdog output is written to `/data/watchdog.log` (rotated).

## Listener map

All Xray listeners, including its API, bind to `127.0.0.1`.

| Transport | Xray port | Public Nginx route |
|---|---:|---|
| VLESS WebSocket | 10001 | generated path under `/vl-ws/` |
| VMess WebSocket | 10002 | generated path under `/vm-ws/` |
| Trojan WebSocket | 10003 | generated path under `/tr-ws/` |
| VLESS XHTTP | 10004 | generated path under `/xhttp/` |
| VMess XHTTP | 10005 | generated path under `/xhttp/vmess/` |
| Xray API | 10085 | no public route |

The listener values are configurable with `XRAY_VLESS_WS_PORT`, `XRAY_VMESS_WS_PORT`, `XRAY_TROJAN_WS_PORT`, `XRAY_VLESS_XHTTP_PORT`, `XRAY_VMESS_XHTTP_PORT`, and `XRAY_API_PORT`. Nginx's generated include uses the same values.

## Supported combinations

- VLESS: WebSocket and XHTTP.
- VMess: WebSocket and XHTTP.
- Trojan: WebSocket only.

The paths are random per installation and remain stable while `/data/paths.json` is preserved. If that file is lost or invalid, new paths are generated; existing client links then need to be refreshed from the dashboard. Retired protocol and transport selections, including old HTTPUpgrade/gRPC and raw TCP records, are migrated to VLESS+WS/TLS. There is no public `/dns-query` endpoint.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `PORT` | provided by platform / `8080` | Nginx's public listener |
| `PANEL_HOST` | `127.0.0.1` in Docker | FastAPI bind address |
| `PANEL_PORT` | `10000` | Internal FastAPI port |
| `TITAN_DATA_DIR` | `/data` | Persistent app data |
| `TITAN_XRAY_CONFIG` | `/data/xray.json` | Generated config file |
| `TITAN_ADMIN_USER` | `TiTaN` | Initial admin name |
| `TITAN_ADMIN_PASS` | `TiTaN` | Initial password; set a unique value |
| `TITAN_NODE_SECRET` | empty | Optional multi-node sync secret |

## Run with Docker locally

```bash
docker build -t titan .
mkdir -p ./data
docker run --rm --name titan \
  -p 8080:8080 \
  -e TITAN_ADMIN_PASS='replace-this-password' \
  -v "$PWD/data:/data" \
  titan
```

Open `http://127.0.0.1:8080/login`. For live client traffic, put the deployment behind a TLS-enabled public hostname and set its advertised domain/port in the panel. A bare local HTTP test is intended for the dashboard and health endpoints.

## Health and troubleshooting

- `GET /healthz` is the container/platform healthcheck.
- `GET /health` is used for node discovery and synchronization.
- `/data/watchdog.log` records starts, health failures and restarts.
- Check that the persistent mount is writable and exactly `/data`.
- If the app is reachable but a transport is not, verify the path file and generated Nginx include match: `/data/paths.json` and `/data/nginx-paths.conf`.
- The panel is intentionally not exposed on `$PORT`; the only public listener is Nginx.
