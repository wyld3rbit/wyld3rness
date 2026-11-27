# FindyFixy

The agent the harness is tested on. It triages incidents (`make triage`) and, on call, proposes fixes that the harness's loop applies to [simprod](../simprod/README.md) (`make oncall`). Settings go in `code/examples/findyfixy/.env`, copied from `code/examples/findyfixy/.env.example`.

## Where things are

| Folder in `code/examples/findyfixy/src/findyfixy/` | Holds |
|---|---|
| oncall/ | the harness's ports for on-call: environment, decider (the model), policy table, script, attacks, experiment, and the agent and loop services |
| triage/, tools/, retrieval/ | the triage graph, its tools and its incident memory |
| evals/ | the golden set and the adapter the eval gate drives |
| console/ | the console: the storm page (home) and the architecture page |
| wiring.py, settings.py | the one composition root and the configuration |

Beside the package: `code/examples/findyfixy/tests/`, `code/examples/findyfixy/data/` (synthetic corpus and golden set), `code/examples/findyfixy/evals/` (baseline and attestation).

## Common changes

| To | Change | Then run |
|---|---|---|
| Allow a new kind of action | the action and its undo in `code/examples/findyfixy/src/findyfixy/oncall/env.py`, its rule in `code/examples/findyfixy/src/findyfixy/oncall/policy.py`, the endpoint in simprod | `make test`, then `make oncall` |
| Change how the model proposes | `code/examples/findyfixy/src/findyfixy/oncall/decide.py` | `make test`, then `make experiment` |
| Add an attack | `code/examples/findyfixy/src/findyfixy/oncall/attacks.py` | `make injection` |
| Change a triage answer | anything on the eval path, or `code/examples/findyfixy/data/` | the commit runs the eval gate |

## Rules the tests enforce

- Only entry points import the composition root (`code/examples/findyfixy/tests/test_architecture.py`).
- Each box on the architecture page names the code behind it, and a box marked built must exist (`code/examples/findyfixy/tests/test_page_truth.py`).
- The agent reaches only the loop and its model: its network is internal, the loop answers it 401 without the host's token, and the agent edge forwards one model route to Ollama and nothing else; every published port binds to 127.0.0.1 (`code/examples/findyfixy/tests/test_loop_token.py`, `code/tests/test_edge.py`). The boundary probe runs inside the agent's container at every `make oncall-up` and fails it if any way out is open.
