# simprod: a small simulated production

Three services from the incident corpus, under real load, with faults that hurt them and typed actions that fix them. It is what FindyFixy operates. Everything here can be checked by standard means, trusting neither the agent, the harness nor whoever built it.

```
 host 127.0.0.1:8100 ─► edge ─┬─ prod network (internal: no route out) ─┬─ gateway ─► ledger (4 shards)
                              │                                          │     └────► notifications
                              └─ outside network (publishes the port)    └─ loadgen (customers, open loop)
```

| Service | Does | Code |
|---|---|---|
| gateway | Takes a payment, writes it to the ledger with retries, notifies. Holds two of the actions | [gateway.py](src/simprod/gateway.py) |
| ledger | Writes entries into 4 shards of fixed capacity; sheds when a shard's line is full | [ledger.py](src/simprod/ledger.py) |
| notifications | One notification per payment, best effort | [notifications.py](src/simprod/notifications.py) |
| edge | The only door from the host: an allow-list | [edge.py](src/simprod/edge.py) |
| loadgen | Customers paying at a fixed average rate, whatever the gateway does | [loadgen.py](src/simprod/loadgen.py) |

Standard library only, one image ([Dockerfile](Dockerfile)), hardened in [docker-compose.yml](docker-compose.yml): read-only filesystem, no capabilities, non-root.

## Inspect it yourself

Start it with `make sim-up` from `code/` (after `make up`). Then:

| Check | Command | What you should see |
|---|---|---|
| Alive | `curl 127.0.0.1:8100/gateway/healthz` | `{"status": "ok", "service": "gateway"}` |
| Pay once | `curl -X POST 127.0.0.1:8100/gateway/pay -H 'Content-Type: application/json' -d '{"key":"acct-0001","amount_cents":1999}'` | `{"id": ..., "shard": ..., "attempts": 1}` |
| Metrics | `curl 127.0.0.1:8100/ledger/metrics` | Prometheus text: `ledger_writes_total`, `ledger_queue_depth` per shard |
| Postman | Import `http://127.0.0.1:8100/openapi.json` | Every allowed endpoint, grouped by service |
| Logs | `docker compose -p simprod logs -f gateway` | One JSON line per failure, action and fault |
| Actions taken | `docker compose -p simprod logs gateway \| grep '"event": "action"'` | Each action with its actor, written by the service that executed it |
| Sealed faults | `curl -i 127.0.0.1:8100/ledger/fault` | `404`: the edge never forwards a fault control |
| No route out | `docker compose -p simprod exec ledger python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"` | `OSError: [Errno 101] Network is unreachable` |
| Fault in force | `docker compose -p simprod exec gateway python -c "import urllib.request; print(urllib.request.urlopen('http://ledger:9000/fault').read())"` | The injected fault, visible to you from inside the network, never through the edge |

## Actions

| Action | Request (through the edge) | Undo |
|---|---|---|
| Retry backoff | `POST /gateway/actions/backoff {"enabled": true}` | `{"enabled": false}` |
| Cap a key's rate | `POST /gateway/actions/rate-cap {"key": "acct-0042", "rps": 5}` | `{"key": "acct-0042", "rps": null}` |
| Restart a service | `POST /<service>/actions/restart {}` | none |

Send `X-Actor: <who>` with an action and the service logs it.

## Load test

`make load-test` runs the protocol in research log entry 006: a ramp to find what the VM can serve, then three soaks at the operating rate. Results land in `results/` (git-ignored) and the verdict is printed.
