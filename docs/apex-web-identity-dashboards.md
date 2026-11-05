# Identity-era Module-3 and Module-6 dashboards on apex-web

This runbook starts the dashboards of the identity release (contracts of 2026-11-03) on the host `apex-web`. They run next to the legacy dashboards. The runbook does not stop, change or rebuild a legacy container.

The activity-api of the Vector Agent Activity Dashboard reads the new dashboards on the docker network `umami_default` (repo `vector-agent-dashboard`, `deploy/docker-compose.yml`):

| activity-api variable | Value |
|---|---|
| `M3_IDENTITY_REPUTATION_API` | `http://module3-dashboard-identity:8000` |
| `M6_IDENTITY_DASHBOARD_URL` | `http://module6-dashboard-identity:8000` |

## 1. What runs

| Container | Compose file (in this repo) | Compose project | Volume | Deployment data in the image |
|---|---|---|---|---|
| `module3-indexer-identity` | `Module-3/dashboard/docker-compose.apex-web-identity.yml` | `module3-identity` | `module3-identity_indexer-data` | `/app/deploy/identity/mainnet/deploy_state.json` (`--deploy-state`) |
| `module3-dashboard-identity` | same file | `module3-identity` | `module3-identity_indexer-data` | same file (`MODULE3_DEPLOY_STATE`) |
| `module6-dashboard-identity` | `Module-6/dashboard/docker-compose.apex-web-identity.yml` | `module6-identity` | none | `/app/deploy/identity/mainnet/deployment.json` (`MODULE6_DEPLOYMENT_FILE`) |

- Each compose file sets its project name (`name:`). Do not add `-p`.
- The images are `module3-identity-indexer`, `module3-identity-dashboard` and `module6-identity-dashboard`.
- The new containers have no public route. The Caddy configuration does not change.

The legacy dashboards stay as they are. Do not touch them:

| Copy on the host | Compose project | Containers | Volume |
|---|---|---|---|
| `/opt/vector-agent-modules/Module-3` | `module3-mainnet` | `module3-indexer`, `module3-dashboard` | `module3-mainnet_indexer-data` |
| `/opt/vector-agent-modules/Module-6` | `module6` | `module6-dashboard-mainnet`, `module6-dashboard-testnet` | none |

> **Caution:** Do not rebuild the legacy Module-3 images. A build today installs cbor2 6, and `import pycardano` then fails (cbor2 6 removed `CBORDecodeValueError`, which cbor2pure 5.8 imports). This branch pins `cbor2>=5.6.0,<6` in `Module-3/dashboard/requirements.txt`. The legacy copy on the host probably has the old unpinned line (not checked on the host).

## 2. Deployment data

| File | Source | sha256 |
|---|---|---|
| `Module-3/deploy/identity/mainnet/deploy_state.json` | Byte copy of the git-ignored `Module-3/deploy/identity/state/mainnet/deploy_state.json` that the deploy tool wrote on 2026-11-03 | `112993ef2c20215aa6a361c9fdb6a04cdbe9d3e38bb054064296c993aca41a72` |
| `Module-6/deploy/identity/mainnet/deployment.json` | Made from the git-ignored `Module-6/deploy/identity/state/mainnet/deploy_state.json` (sha256 `3ddbba29f7f746b48733b6a83a77cc10a7cc732cc1eb6bec41b18bbfb0d7f7a0`) in the `deploy/<network>/deployment.json` format of the Module-6 dashboard | `b853a08ae897eb91186941393d33b7942556e031d613aa988576be74333b2913` |

Both files hold only public chain data. The Module-6 file has no compiled code. The addresses of its three spend validators are the enterprise script addresses of the hashes (header `0x71`).

| Value | Module-3 | Module-6 |
|---|---|---|
| Identity registry (`identity_hash`) | `d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762` | `d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762` |
| Validators | reputation `fad945ab20f61a58fbdf6a33a8f491899b7dd4167fb64200bc060424`, endorsement `21ac1502ba7bab74e98ef3ad256bba1622a1351934ac19d0432126fe` | proposal_spend `0dc0e8b60c6a3f4d16b43bf7b6dc9df4977337fafa561d08b5ce8768`, critique_spend `92f65ad8a23396d864c1fc571ff48c0b25008fc3d5f5d7ab3fd568c5`, endorsement_spend `2e7e2a48850adb3cf78d088fdbcfcc7a91f5c50f960d966dde083550` |
| Addresses | reputation `addr1w8adj3dtyrmp5k8mma4r8285jxyeklw5zelmvssqhsrqgfqc9uavs`, endorsement `addr1wys6c9gzhfa6ka8f3me66ftthgtz9gf4ry62cxwsgvsjdlspch2kv` | treasury `addr1w83wjku6nd9uw90nmmzarrtgn0mdzxt2xx7kam4d5eq7a2s262ejl` |

The tests `Module-3/python/tests/test_deployment.py::test_published_identity_mainnet_state_loads` and `Module-6/tests/test_dashboard_identity.py::test_the_published_identity_mainnet_deployment_is_a_new_deployment` check these files.

## 3. Before you start

- You have the commit of this branch that you deploy. All commands below use it.
- `ssh apex-web` opens a root shell on the host.
- The activity-api container `activity-api-mainnet` runs. The health checks use its `wget`.

## 4. Copy the subtrees to the host

Do this on your workstation, in the root of a `vector-agent-modules` checkout at the release commit. The copy holds only the files of the commit (`git archive`). The command puts the new copy in place and keeps the previous copy as `/opt/vector-agent-modules-identity.prev` for a rollback.

```sh
git status --short          # must print nothing
REV=$(git rev-parse HEAD)
git archive --format=tar "$REV" Module-3 Module-6 \
  | ssh apex-web "set -e
      rm -rf /opt/vector-agent-modules-identity.new
      mkdir -p /opt/vector-agent-modules-identity.new
      tar -x -C /opt/vector-agent-modules-identity.new
      echo $REV > /opt/vector-agent-modules-identity.new/REVISION"
ssh apex-web 'set -e
  cd /opt
  if [ -d vector-agent-modules-identity ]; then
    rm -rf vector-agent-modules-identity.prev
    mv vector-agent-modules-identity vector-agent-modules-identity.prev
  fi
  mv vector-agent-modules-identity.new vector-agent-modules-identity'
```

## 5. Check the copy on the host

```sh
ssh apex-web
cd /opt/vector-agent-modules-identity
cat REVISION
sha256sum Module-3/deploy/identity/mainnet/deploy_state.json Module-6/deploy/identity/mainnet/deployment.json
grep -o '"identity_hash": "[0-9a-f]*"' Module-3/deploy/identity/mainnet/deploy_state.json Module-6/deploy/identity/mainnet/deployment.json
docker network inspect umami_default --format '{{.Name}}'
df -h /var/lib/docker
```

Expected:
- The two sha256 values of section 2.
- `identity_hash` `d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762` in both files.
- `umami_default`.
- Free space for three images of approximately 280 MB each (the two Module-3 images share their layers).

## 6. Build and start

```sh
cd /opt/vector-agent-modules-identity/Module-3/dashboard
docker compose -f docker-compose.apex-web-identity.yml build
docker compose -f docker-compose.apex-web-identity.yml up -d

cd /opt/vector-agent-modules-identity/Module-6/dashboard
docker compose -f docker-compose.apex-web-identity.yml build
docker compose -f docker-compose.apex-web-identity.yml up -d
```

> **Caution:** Do not use these compose files with `-p module3-mainnet` or `-p module6`, and do not use `--remove-orphans` in the legacy projects. Both actions can stop or remove legacy containers.

## 7. Health checks

Containers:

```sh
docker ps --filter name=identity --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
```

Expected: `module3-indexer-identity`, `module3-dashboard-identity` and `module6-dashboard-identity` are `Up`.

Logs:

```sh
docker logs module3-indexer-identity 2>&1 | grep -E 'Deploy state|Identity hash|Indexed'
docker logs module3-dashboard-identity 2>&1 | grep 'Loaded deploy state'
docker logs module6-dashboard-identity 2>&1 | grep -E '\[OK\]|Cache refreshed'
```

Expected:
- `Deploy state: /app/deploy/identity/mainnet/deploy_state.json` and `Identity hash: d958b823…`. Then one `Indexed N agents at slot S` line every 60 s. The indexer reads the current outputs at each poll, so the first poll fills a new volume.
- `Loaded deploy state for mainnet from /app/deploy/identity/mainnet/deploy_state.json (identity_hash d958b823…)`.
- `[OK] Dashboard connected to https://ogmios.vector.mainnet.apexfusion.org` and `Cache refreshed: N proposals`.

HTTP on the `umami_default` network (from the activity-api container):

```sh
docker exec activity-api-mainnet wget -qO- http://module3-dashboard-identity:8000/health; echo
docker exec activity-api-mainnet wget -qO- http://module3-dashboard-identity:8000/api/config; echo
docker exec activity-api-mainnet wget -qO- http://module6-dashboard-identity:8000/api/health; echo
docker exec activity-api-mainnet wget -qO- http://module6-dashboard-identity:8000/api/config; echo
docker exec activity-api-mainnet wget -qO- http://module6-dashboard-identity:8000/api/treasury; echo
```

Expected:
- Module-3 `/health`: `"status":"ok"`, `"identity_hash":"d958b823…"`. `last_poll_slot` increases every 60 s.
- Module-3 `/api/config`: `"reputation_validator_hash":"fad945ab…"`, `"endorsement_validator_hash":"21ac1502…"`.
- Module-6 `/api/health`: `"status":"ok"` and the current slot.
- Module-6 `/api/config`: `"identity_hash":"d958b823…"`, `"deployment_note":null`.
- Module-6 `/api/treasury`: on 2026-11-05 `{"total_lovelace":90000000,"total_apex":90.0,"utxo_count":3}` (3 × 30 AP3X of the deployment). Later governance actions can change it.

The legacy dashboards still answer:

```sh
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E '^(module3-indexer|module3-dashboard|module6-dashboard-mainnet|module6-dashboard-testnet)\s'
docker exec activity-api-mainnet wget -qO- http://module3-dashboard:8000/health; echo
docker exec activity-api-mainnet wget -qO- http://module6-dashboard-mainnet:8000/api/health; echo
```

## 8. Activity-api

Start the identity dashboards first. Then deploy the activity-api build that sets `M3_IDENTITY_REPUTATION_API` and `M6_IDENTITY_DASHBOARD_URL` (`vector-agent-dashboard`, `deploy/docker-compose.yml`). If the activity-api starts first, `modules.m3.identity` and `modules.m6.identity` show `sync.status` `syncing` with a `last_error` until the containers answer. The legacy blocks do not change.

Checks after the activity-api deploy:

```sh
docker exec activity-api-mainnet wget -qO- http://127.0.0.1:3001/api/state \
  | python3 -c 'import json,sys; m=json.load(sys.stdin)["modules"]; print(m["m3"]["identity"]["sync"]); print(m["m6"]["identity"]["sync"])'
docker exec activity-api-mainnet wget -qO- 'http://127.0.0.1:3001/api/m3/state?deployment=identity'; echo
docker exec activity-api-mainnet wget -qO- 'http://127.0.0.1:3001/api/m6/state?deployment=identity'; echo
docker exec activity-api-mainnet wget -qO- http://127.0.0.1:3001/healthz; echo
```

Expected:
- Both `sync` blocks have `"status": "live"`.
- The two `deployment=identity` snapshots give the data of the identity dashboards (not a 404).
- `/healthz` lists `m3_identity_rest` and `m6_identity_dashboard` in `upstream`. These two sources do not change the `/healthz` status.

## 9. Rollback

The activity-api continues to work when the identity dashboards stop: the identity blocks show a sync error, and the legacy blocks do not change. To remove the identity blocks too, remove the two variables from `deploy/docker-compose.yml` of `vector-agent-dashboard` and deploy the activity-api again. Then `modules.m3.identity` and `modules.m6.identity` are `null`.

Stop and remove the identity containers (the volume stays):

```sh
cd /opt/vector-agent-modules-identity/Module-6/dashboard
docker compose -f docker-compose.apex-web-identity.yml down
cd /opt/vector-agent-modules-identity/Module-3/dashboard
docker compose -f docker-compose.apex-web-identity.yml down
```

Go back to the previous revision after an update:

```sh
cd /opt
mv vector-agent-modules-identity vector-agent-modules-identity.bad
mv vector-agent-modules-identity.prev vector-agent-modules-identity
cat vector-agent-modules-identity/REVISION
```

Then do sections 6 and 7 again.

Remove all identity items (optional). The index volume holds no unique data: the next start fills it again in one poll.

```sh
docker volume rm module3-identity_indexer-data
docker image rm module3-identity-indexer module3-identity-dashboard module6-identity-dashboard
rm -rf /opt/vector-agent-modules-identity /opt/vector-agent-modules-identity.prev /opt/vector-agent-modules-identity.bad
```

Check that the legacy containers still run (section 7, last commands).
