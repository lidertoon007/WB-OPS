# Netdata in Docker: staged migration

This is a deployment plan, not an automatic cutover. The existing
`netdata-auth-proxy.service` still requires `netdata.service` and proxies to
`127.0.0.1:19999`; keep its Nginx configuration and the live agent unchanged
through the rehearsal. Use the same `netdata.service` name only at cutover.

The image is the official `netdata/netdata:v2.13.0` release, not a moving tag.
Compose requests only `127.0.0.1:${NETDATA_HOST_PORT}:19999`. Bridge networking
is intentional: host networking cannot enforce a loopback listener in Compose,
and a copied `netdata.conf` could accidentally bind `0.0.0.0`. The agent runs
as `nobody` (UID 65534) with all Linux capabilities dropped. Host `/proc` and
`/sys` are read-only. `/host/root` is an empty directory on the host root
filesystem, so Netdata can measure capacity without reading the filesystem.
Some per-process I/O, Docker discovery and journal charts may be unavailable.
The agent receives systemd status through a separate bus rather than the host
D-Bus socket.
The production-only `netdata.listeners.compose.yml` adds loopback StatsD
8125/TCP+UDP and OTLP/gRPC 4317/TCP when explicitly passed as a second Compose
file. It is never used for the stage.

## Preflight on the Ubuntu host

Confirm the active install and paths, disk free space, architecture, Docker
Engine/Compose availability, and the current listener before copying anything:

```sh
systemctl cat netdata.service netdata-auth-proxy.service
systemctl show -p FragmentPath -p DropInPaths netdata.service
ss -ltnp '( sport = :19999 or sport = :19997 )'
docker version --format '{{.Server.Version}}'
docker compose version
curl -fsS http://127.0.0.1:19999/api/v1/info >/dev/null
```

Record whether the active install uses `/etc/netdata`, `/var/lib/netdata`, and
`/var/cache/netdata` (a static install usually uses `/opt/netdata/...`). The
three **source** paths below are examples for a package install; substitute
the verified actual paths. Check actual `netdata.conf` `[directories]` and any
`stream.conf`, `exporting.conf`, `claim.conf`, `cloud.d/`, and
`health_alarm_notify.conf` by **filename and effective setting only**. Do not
print claim tokens, keys, notifier destinations, or whole secret files. Confirm
the live `health.d/wb-ops-resources.conf` and historical database files exist.
The stage needs enough disk for copies of both `lib` and `cache`, and roughly
another agent's memory on a host with only about 1.2 GiB free; stop the stage
if memory pressure or OOM events appear.

## Isolated rehearsal

Use a local filesystem (not NFS). Keep the live source untouched. The sample
commands intentionally exclude installation metadata and Cloud credentials,
but preserve the copied historical metrics and `wb_ops_*` rule. `rsync` during
a live run is only a rehearsal snapshot; a final, consistent copy is made after
stopping the old agent at cutover.

```sh
sudo install -d -m 0700 /var/tmp/wb-ops-netdata-stage-20261127/{etc,lib,cache}
sudo rsync -a --exclude=.environment --exclude=.install-type \
  --exclude=claim.conf --exclude=cloud.d/ --exclude=stream.conf \
  --exclude=exporting.conf /etc/netdata/ /var/tmp/wb-ops-netdata-stage-20261127/etc/
sudo rsync -a --exclude=cloud.d/ /var/lib/netdata/ /var/tmp/wb-ops-netdata-stage-20261127/lib/
sudo rsync -a /var/cache/netdata/ /var/tmp/wb-ops-netdata-stage-20261127/cache/
```

On the **stage copy only**, set `[web] bind to = 0.0.0.0:19999` (container
side), `[health] enabled = no`, and verify the effective settings; this stops
notifier callbacks while preserving the `health.d/wb-ops-resources.conf` file.
Ensure the copied config has no
claiming setting and the copied lib has no `cloud.d` credentials; inspect names
and status, never values. Do not point the stage at a production parent or
Cloud. If these checks cannot be confirmed, do not start the stage.

```sh
sudo docker network create --internal --driver bridge wb-ops-netdata-stage-20261127
sudo env NETDATA_ROOT=/var/tmp/wb-ops-netdata-stage-20261127 \
  NETDATA_HOST_PORT=19997 NETDATA_NETWORK=wb-ops-netdata-stage-20261127 \
  NETDATA_MEMORY_LIMIT=512m NETDATA_DBUS_GID=987 docker compose \
  -f /var/tmp/wb-ops-netdata-stage-20261127/netdata.compose.yml \
  -p wb-ops-netdata-stage-20261127 config -q
```

Upload this Compose file to the root-owned stage directory, pull the pinned
image, and start only `-p wb-ops-netdata-stage-20261127` with the same `sudo
env` variables. On Docker 29.1.3, an `--internal` bridge does **not** publish
the requested host port: `docker inspect` reports `19999/tcp:null`, so
`127.0.0.1:19997` cannot be used in this rehearsal. Inspect the API with
`docker compose exec -T netdata curl http://127.0.0.1:19999/...` instead.
Do not switch to an egress-capable bridge until copied collector configurations
have been audited for outbound targets. The production bridge must be an
ordinary, non-internal bridge for the loopback publication to work.

While stage health is still disabled, use its own API token inside the
container to call `/api/v1/manage/health?cmd=SILENCE%20ALL`, then verify
`LIST` reports `all:true,type:SILENCE`. A prewritten
`health.silencers.json` was **not loaded** in this rehearsal, so the API step
is necessary. In the stage copy set `[health] script to execute on alarm =
/bin/true`, remove the copied `health_alarm_notify.conf`, set `enabled = yes`,
and restart **only** stage. Verify `LIST` again, then check `/api/v1/alarms?all`
for all four `wb_ops_*` alarms, chart counts, and historical `first_entry`.
Recreate stage with `up -d --force-recreate --no-build` and recheck. Before
stopping, confirm the Compose project label is the stage name; use `down`
without `-v`, retaining the copied data and internal network.

Observed on 2026-11-27: rsync took about 30 s; first start 2 s; recreate 3 s.
History starts at `1794901920` for CPU/RAM and `1794902100` for load before
and after recreation; recent samples and all four `wb_ops_*` alarms (`CLEAR`)
were present. `SILENCE ALL` persisted across restart/recreation, OOM was false.
MemAvailable was ~1,047,748 kB before stage start, ~875,416 kB during the
comparison, ~1,095,048 kB after stage stop. The live agent/proxy remained
active. Active chart counts differed (~1969 live, ~1698 stage): the largest
gap was 159 `systemd.service_unit_state` charts because host D-Bus is not
mounted. The read-only mount flag on a D-Bus socket does not make D-Bus calls
read-only; enabling it needs an explicit security decision. Core `net.*` and
`cgroup.*` active context counts matched (38 and 50). No direct
`127.0.0.1:19997` access test was possible on the internal bridge.
On a separate ordinary Docker bridge, an Alpine probe with no host mounts or
credentials confirmed Docker 29.1.3 published TCP `127.0.0.1:19997` to the
host: localhost connected, the host's public IP refused, and an external
connection timed out. The probe container and bridge were removed.

The follow-up rehearsal recovered 161 `systemd.service_unit_state` charts.
`netdata-dbus-bus.service` listens under `/run/wb-ops-netdata-dbus`, accessible
to the host Netdata group. `netdata-dbus-status.service` reads host D-Bus as
the unprivileged `netdata` user and mirrors only unit lists and the systemd
version. Install its Python file and the private bus XML in the root-owned
`/etc/wb-ops-netdata` directory with group `netdata` and mode `0640` on the
files. The private bus applies its method allowlist. AppArmor mediation is
disabled only on this private bus so the confined Docker client can connect.
Verify the client cannot see the host bus, and that commands to start, stop,
or restart a fixture unit and requests for other properties are rejected.

## Cutover and rollback

First save the packaged unit's path and contents outside `/etc/systemd/system`
and take independent backups of the three source trees. Keep the original
package installed. Stop/remove the stage project. Stop the old
`netdata.service`, then make a **fresh** copy of config/lib/cache into
`/var/lib/wb-ops-netdata/current/{etc,lib,cache}`. Exclude `.environment` and
`.install-type` from config, but preserve production `claim.conf`, `cloud.d/`,
health rules, notification config, history, and machine identity in this final
copy. Do not reuse the stage tree with `[health] enabled = no`. Inspect copied
ownership and adjust only the copy if container startup needs it.

Create the external Compose network
`wb-ops-netdata-prod` as an **ordinary bridge**, and verify `Internal=false`.
The packaged v2.13.0 currently binds `[web]` to `127.0.0.1:19999`; in the
**copied production config only**, change it to `0.0.0.0:19999` for bridge
DNAT. StatsD currently uses its default `udp:localhost tcp:localhost` 8125;
change copied `[statsd] bind to = udp:0.0.0.0:8125 tcp:0.0.0.0:8125` to
accept host loopback forwards. Copy the packaged `otel.yaml` and change only
its gRPC bind to `0.0.0.0:4317`. Copy the stopped agent's `/var/log/netdata/otel`
to `$NETDATA_ROOT/otel` and set its owner to `65534:65534`; the container needs
that persistent directory for the OTLP ledger. The listeners override publishes **only**
`127.0.0.1:8125` TCP+UDP and `127.0.0.1:4317` TCP. Verify these three
listeners before declaring collector parity. The live Cloud API reports
`agent-claimed=false, online=false` even though `lib/cloud.d` exists; preserve
identity files in the final copy and verify the same effective state.

After checking ports 19999, 8125, and 4317 are free, install
`deploy/docker/netdata.service` as
`/etc/systemd/system/netdata.service`, run `systemctl daemon-reload`, then
`systemctl start netdata.service`. The wrapper starts the named Compose project
`wb-ops-netdata` from the checked-out release at `/opt/wb-ops`. It does not
delete volumes on stop. Confirm `systemctl status netdata`, `docker compose ps`,
`curl http://127.0.0.1:19999/api/v1/info`, the existing authenticated proxy,
and `ss` showing only loopback listeners. Check historical charts and four
`wb_ops_*` alarms via `/api/v1/alarms?all`, with statuses other than
`UNDEFINED`/`UNINITIALIZED`. Confirm Cloud/notification behavior against the
recorded preflight state. Do not alter Nginx, SSH, firewall, or other services.

If any check fails, stop the Docker-backed `netdata.service`, remove only the
override `/etc/systemd/system/netdata.service`, `systemctl daemon-reload`, and
start the original packaged `netdata.service`; verify 19999 and the auth proxy.
The original package data directories remain untouched and are the rollback
source. Do not run `docker compose down -v` or delete the saved copies.

Sources: [official Docker install](https://learn.netdata.cloud/docs/netdata-agent/installation/docker),
[install-type data backup](https://learn.netdata.cloud/docs/netdata-agent/installation/linux/switch-install-types-and-release-channels),
[Netdata Cloud identity files](https://learn.netdata.cloud/docs/netdata-cloud/connect-agent),
[health controls](https://learn.netdata.cloud/docs/alerts-%26-notifications/health-api-calls),
[StatsD bind syntax](https://learn.netdata.cloud/docs/collecting-metrics/statsd),
[OTLP endpoint syntax](https://learn.netdata.cloud/docs/opentelemetry/opentelemetry-plugin-reference).
