# WB Ops container: preparation and local rehearsal

This configuration runs the **exact verified production release** as a read-only
bind mount; the image contains Python 3.12.3 and pinned Python dependencies only.
It does not install Docker or change the host Nginx/SSH setup. The production
release observed for this migration is
`/opt/wb-ops/releases/20261127-wb-merges-import1`; verify its identity again at
cutover. Existing host Nginx still connects to `127.0.0.1:8000`. Production uses
host networking and Uvicorn binds that loopback address with one worker, trusting
forwarded headers only from `127.0.0.1`. Stop the old `wb-ops` service before
starting this container or the port will conflict.

## Build input and variables

Prepare a **new clean build context** with only `requirements-prod.txt` containing
the separately generated, reviewed, exact production Python versions. Do not use
the repository or server release as Docker build context. The source code, `.env`,
SQLite, `opiu` credentials, reports, and artifacts must never enter build context
or image layers. Set `WBOPS_BUILD_CONTEXT` to that directory and
`WBOPS_DOCKERFILE` to the absolute path of `wb-ops.Dockerfile`; set `WBOPS_IMAGE`
to a unique local tag. Build only after validating the dependency manifest against
the running release. FBS Excel export uses the Python `openpyxl` dependency.

Pass Compose substitutions through the operator shell or a **separate protected**
Compose env file outside the clean build context. `WBOPS_ENV_FILE` points to
the root-only `/etc/wb-ops/wb-ops.docker.env`, with one raw `KEY=value` line
per effective `WBOPS_*` variable. Use `docker compose config -q` so validation
does not print secrets. Recreate the container to load a changed runtime file.

Set all host path variables to **existing absolute directories** owned/readable as
needed by UID:GID `999:988`; Compose uses `create_host_path: false` throughout.
`WBOPS_RELEASE_DIR` is the resolved exact release directory; the other variables
are resolved real paths for its persistent directories:

| Variable | Container path | Access |
| --- | --- | --- |
| `WBOPS_DATA_DIR` | `/app/data` | write; all SQLite files and sidecars |
| `WBOPS_APP_DATA_DIR` | `/app/app/data` | write; chat state, separate from root data |
| `WBOPS_EXPORTS_DIR` | `/app/exports` | write |
| `WBOPS_FBS_REPORTS_DIR` | `/app/fbs_reports` | write |
| `WBOPS_OPIU_DIR` | `/app/opiu` | write; protect credentials |
| `WBOPS_KNOWLEDGE_DIR` | `/app/app/knowledge` | write |

Check the real paths of the release and each target, including symlinks, before
starting. The nested mounts must land on the intended `/app` paths and must not
mask live data. If a target resolves outside `/app` or a required directory is
missing, resolve the mapping before cutover. With `/app` mounted read-only, only
the listed subdirectories and the small `/tmp` tmpfs are writable.

FBS Excel export builds the workbook in memory with `openpyxl`; the image needs
no Node runtime or separately mounted tools directory.

## Isolated rehearsal

Use `wb-ops.rehearsal.yml` as its own project. Set
`WBOPS_REHEARSAL_RELEASE_DIR` to an isolated read-only copy of the exact code,
`WBOPS_REHEARSAL_ENV_FILE` to a fixture-only file, and each
`WBOPS_REHEARSAL_*_DIR` to an existing isolated test directory. The fixture must
disable real external integrations and credentials (`WBOPS_AUTO_SYNC_ENABLED=false`,
no production cabinet tokens); use only synthetic SQLite and files. The isolated
release copy must exclude `.env` and other embedded secrets: the app also reads a
relative `.env` if present. Other background loops still start but cannot reach
external services with `network_mode: none`. This Compose file uses no restart or
host-published port. Test from inside the container:

```sh
docker compose -f deploy/docker/wb-ops.rehearsal.yml -p wbops-rehearsal up -d --build
docker compose -f deploy/docker/wb-ops.rehearsal.yml -p wbops-rehearsal exec -T wb-ops python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status)"
docker compose -f deploy/docker/wb-ops.rehearsal.yml -p wbops-rehearsal down
```

No production env or data path belongs in a rehearsal variable. This runs the
same one-worker command and verifies the local health endpoint without network
access. Confirm other expected app behavior only against fixture data.

## Production gate

Follow the approved SQLite snapshot, frozen-source verification and rollback
plan. After stopping the old service and validating all data mounts, run only
`wb-ops.compose.yml`. The `wb-ops-docker.service` unit starts the container.
`WBOPS_GATE_DIR` points to a root-controlled host directory. With no `ready`
file, only `/health` responds and ordinary requests return 503. Create `ready`
after verifying both health routes and the correct mount for the live SQLite
directory. Confirm `docker compose ps`, container health, host
`127.0.0.1:8000/health`, existing Nginx route, and read/write behavior on the
correct SQLite and persistent paths. Ensure the old systemd unit cannot start a
second writer. Monitor RAM/OOM on the 2-vCPU host with about 1.2 GiB free before
cutover; this configuration deliberately starts one worker. Keep its production
env separate from the image and never publish port 8000 to a public interface.

To roll back, close the gate, stop the container, and enable and start the old
`wb-ops.service`. Both releases use the same six persistent paths, so the old
release sees writes made since the cutover. Keep the frozen final snapshot for
recovery and confirm the old release remains schema-compatible before rollback.
