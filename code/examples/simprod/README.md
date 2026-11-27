# simprod: a small simulated production

Three services from the incident corpus, under real load, with faults that hurt them and typed actions that fix them. It is what FindyFixy operates. Everything here can be checked by standard means, trusting neither the agent, the harness nor whoever built it.

```
 host 127.0.0.1:8100 ─► edge ─────┐  edge and openbao join both networks; the outside one publishes their ports
 host 127.0.0.1:8200 ─► openbao ──┤
                                  └─ prod network (internal: no route out) ─┬─ gateway ─► ledger (4 shards)
                                                                            │     └────► notifications
                                                                            ├─ customers and chaos (storms only)
                                                                            └─ loadgen (load tests only)
```

| Service | Does | Code |
|---|---|---|
| gateway | Takes a payment, writes it to the ledger with retries, notifies. Holds two of the actions | [gateway.py](src/simprod/gateway.py) |
| ledger | Writes entries into 4 shards of fixed capacity; sheds when a shard's line is full | [ledger.py](src/simprod/ledger.py) |
| notifications | One notification per payment, best effort | [notifications.py](src/simprod/notifications.py) |
| edge | The only door from the host: an allow-list | [edge.py](src/simprod/edge.py) |
| loadgen | Customers paying at a fixed average rate, whatever the gateway does, for one load-test phase | [loadgen.py](src/simprod/loadgen.py) |
| customers | The same customers all the time, for storms, plus the hot shard's runaway client | [customers.py](src/simprod/customers.py) |
| chaos | Injects a seeded fault from inside the network, where the fault ports are | [faults.py](src/simprod/faults.py) |
| openbao | Holds the actions' signing key and verifies credentials; in-memory storage, a fresh key at each start | [vault.py](src/simprod/vault.py), [openbao/Dockerfile](openbao/Dockerfile) |

The services: standard library only, one image ([Dockerfile](Dockerfile)). OpenBao: its own pinned image ([openbao/Dockerfile](openbao/Dockerfile)). Both hardened in [docker-compose.yml](docker-compose.yml): read-only filesystem, no capabilities, non-root.

## Inspect it yourself

`make up` from `code/` starts it (`make sim-up` starts it alone, rebuilds what changed and mints fresh vault passes; containers whose settings did not change keep running). Every allowed endpoint is in `http://127.0.0.1:8100/openapi.json` (Postman can import it).

| Check | Command |
|---|---|
| Alive | `curl 127.0.0.1:8100/gateway/healthz` |
| Metrics | `curl 127.0.0.1:8100/ledger/metrics` |
| What customers see (during a storm: the customers service runs only then) | `curl 127.0.0.1:8100/customers/metrics` |
| Actions taken, with actor | `docker compose -p simprod logs gateway \| grep '"event": "action"'` |
| No route out | `docker compose -p simprod exec ledger python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"` |

## Actions

| Action | Request (through the edge) | Undo |
|---|---|---|
| Retry backoff | `POST /gateway/actions/backoff {"enabled": true}` | `{"enabled": false}` |
| Cap a key's rate | `POST /gateway/actions/rate-cap {"key": "acct-0042", "rps": 5}` | `{"key": "acct-0042", "rps": null}` |
| Restart a service | `POST /<service>/actions/restart {}` | none |

Every action needs `X-Action-Token`, a pass for exactly that action, signed by OpenBao ([credentials.py](src/simprod/credentials.py)). The harness signs it for 60 s, and a service refuses any pass valid for more than 120 s. It is usable once per service process: used ids live in memory, so a restarted service accepts the restart's own pass once more until it expires. The service refuses anything missing, forged, expired, reused or meant for another action, and logs the reason.

## Faults

A storm is a fault plus the gateway it meets: as shipped (retries at once) or well configured (`GATEWAY_BACKOFF=1`). Each kind has one correct fix, a set of actions applied together.

| Kind | Gateway | Injected | Why it lasts | Fix |
|---|---|---|---|---|
| Retry storm | As shipped | Every ledger write slower for 5 to 15 s | Immediate retries keep the ledger over capacity after the slowdown ends | Backoff on |
| Hot shard | Backs off | One key hammered by a runaway client | It never stops by itself | Cap that key |
| Compound | As shipped | The same runaway | The runaway, and that shard's own customers retrying at once | Cap that key and backoff on |

Each storm is drawn from a seed, so a storm set is reproducible ([faults.py](src/simprod/faults.py)). Recovery is judged only from what customers saw ([checker](src/simprod/checker.py), the harness's [verdict](../../src/wyld3rness/loop/verdict.py)).

To add a fault, add it to [faults.py](src/simprod/faults.py) with its fix; to add an action, add the endpoint and its undo to the service, its entry to the edge's allow-list ([edge.py](src/simprod/edge.py)) and its name to `ACTION_NAMES` ([credentials.py](src/simprod/credentials.py)), then to FindyFixy's environment, decider and policy.

## Tests of simprod itself

`make load-test` finds what the VM can serve (research log entry 006). `make controls-pilot`, then `make controls`, show that doing nothing fails, the scripted fix recovers, and a wrong fix is caught and undone (entries 008, 011 and 012).
