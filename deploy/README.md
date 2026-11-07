# WB OPS production deployment

This directory contains server-side templates only. Copy the application into `/opt/wb-ops`, keep secrets in `/etc/wb-ops/wb-ops.env`, and run Uvicorn only on loopback behind nginx.

## First install

```sh
sudo useradd --system --home /opt/wb-ops --shell /usr/sbin/nologin wbops || true
sudo install -d -o wbops -g wbops -m 0750 /opt/wb-ops /opt/wb-ops/releases /etc/wb-ops
sudo python3.11 -m venv /opt/wb-ops/venv
sudo /opt/wb-ops/venv/bin/pip install --upgrade pip
sudo /opt/wb-ops/venv/bin/pip install -e /opt/wb-ops/current
sudo install -o root -g wbops -m 0640 deploy/systemd/wb-ops.service /etc/systemd/system/wb-ops.service
sudo install -o root -g root -m 0644 deploy/nginx/wb-ops.conf /etc/nginx/sites-available/wb-ops
sudo ln -sfn /etc/nginx/sites-available/wb-ops /etc/nginx/sites-enabled/wb-ops
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now wb-ops
```

Create `/etc/wb-ops/wb-ops.env` from `.env.example`, set `WBOPS_ENVIRONMENT=production`, keep `WBOPS_DRY_RUN=true` until publishing is explicitly reviewed, and set `WBOPS_ALLOW_REMOTE=true` only when nginx authentication and TLS are ready. The file must be mode `0640`, owned by `root:wbops`.

## Release layout

Upload each release to `/opt/wb-ops/releases/<timestamp>`, install dependencies into the shared venv, smoke-test it with `curl http://127.0.0.1:8000/health`, then atomically point `/opt/wb-ops/current` to the new release and restart the unit. Keep at least one previous release for rollback. Never upload `.env`, `.git`, `.venv`, `node_modules`, backups, or local databases unless the data migration is intentional.

## TLS and access

The bundled nginx template listens on loopback only. Access it remotely with an SSH tunnel, for example `ssh -L 8080:127.0.0.1:80 ubuntu@<server-ip>` and open `http://127.0.0.1:8080`. Use a real DNS name, TLS, and an authentication layer before changing the `listen` directives to public interfaces. Keep port 8000 loopback-only. Validate `nginx -t`, `systemctl is-active wb-ops`, `curl -fsS http://127.0.0.1:8000/health`, and `curl -fsS http://127.0.0.1/health` after every release.
