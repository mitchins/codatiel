# DASH-01 deployment (example, single-operator alpha)

The dashboard is a separate lightweight web service. It never touches
the cron poller, its schedule, or the worker's state beyond audited
`operator.json` saves from the settings form. Reads open
`events.sqlite` read-only (`mode=ro` + `query_only`).

## Install

```sh
pip install -e ".[dashboard]"
```

## Run (loopback only)

```sh
codatiel-i2 dashboard --port 8487
# state dir: --state-dir, else CODATIEL_DEV_STATE / CODATIEL_TEST_STATE
```

The server binds `127.0.0.1` by default and refuses any non-loopback
`--host`. Reach it over SSH (nothing listens beyond loopback):

```sh
ssh -L 8487:127.0.0.1:8487 operator@codatiel-host
# then open http://127.0.0.1:8487/
```

## systemd (example)

```ini
[Unit]
Description=Codatiel operator dashboard (loopback only)
After=network.target

[Service]
Type=simple
User=codatiel
EnvironmentFile=/data/coding/codatiel-i2/ops/codatiel-dev-semdup.env
ExecStart=/data/coding/codatiel-i2/.venv/bin/codatiel-i2 dashboard --port 8487
Restart=on-failure
# The dashboard needs no secrets and no network beyond loopback.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

## Reverse proxy (example, authenticated)

If the dashboard must be reachable beyond the host, keep it bound to
`127.0.0.1` and terminate access in the proxy — never expose the app
socket directly, and never add user management to the app itself:

```nginx
server {
    listen 443 ssl;
    server_name dashboard.example.internal;

    ssl_certificate     /etc/ssl/dashboard.crt;
    ssl_certificate_key /etc/ssl/dashboard.key;

    # Single-operator access: one basic-auth user in front of the
    # loopback-only dashboard.
    auth_basic "codatiel operator";
    auth_basic_user_file /etc/nginx/codatiel-dashboard.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8487/;
        proxy_set_header Host $host;
    }
}
```

API keys, tokens and secrets are never sent to the browser: the
dashboard only renders secret presence (`set`/`unset`).
