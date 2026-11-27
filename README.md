# Autonomous on-call, safe and affordable to be wrong

wyld3rness is a harness for LLM agents that run on-call with no person approving each action. People set the rules and the budget. Code decides what may run, checks whether customers recovered, undoes what did not work, and stops when tries or money run out. It is tested on FindyFixy, an LLM agent that diagnoses and fixes incidents, acting only on [simprod](code/examples/simprod/README.md), a simulated payment system under real load.

![The console replaying a saved storm: customers' failures each second, every action and undo marked, the loop's steps, and the agent's three tries, each undone, ending in the safe state](code/docs/demo.gif)

## What is built

| Question | Control | Status |
|---|---|---|
| May this change ship? | Eval gate at every commit, checked again in CI | Partly built: it covers triage's answers, not the on-call proposer or its policy |
| What happened, at what cost? | A trace per run, with cost | Built |
| What may an incident spend? | A budget checked before each of the on-call agent's model calls | Partly built: one gateway for every model call is planned |
| May this action run? | A policy per kind of action, and a one-time signed pass | Built |
| Did it work? | A recovery check on what customers saw | Built |
| What if it was wrong? | Undo and retry, else a safe state | Built |
| Who is told when it stops? | A hand-off to the on-call person | Built |
| Is the model worth it? | The agent against a runbook script with no model, same boundary | Built: [the script won](code/docs/research-log.md) |
| How much may it do alone? | Approval once per kind of action, kept by evidence | Planned |

## The design

![Target design: what people set before runtime, the harness, the triage agent, the two proposers (a model that reads the metrics and a script with no model), the act, verify and undo loop, and the simulated production. Solid is built, dashed is planned.](code/docs/target-design.svg)

- [The harness](code/src/wyld3rness/README.md): its parts and what an app plugs in.
- [Design and strategy](code/docs/design.md): the whole picture, and how a proposer earns its place.
- [Decision records](code/docs/adr/README.md): one per phase, with the options rejected.
- [Research log](code/docs/research-log.md): the measurements; each protocol written before its run, exploratory runs marked, corrections appended.

## Run it

You need [uv](https://docs.astral.sh/uv/), Docker Compose with a runtime and [Ollama](https://ollama.com/): 16 GB of memory and about 8.5 GB of models.

```bash
cp code/examples/findyfixy/.env.example code/examples/findyfixy/.env    # model names
OLLAMA_NO_CLOUD=1 ollama serve   # in its own terminal, local models only
make setup        # once: packages, models, every service, incidents
make up           # start every service: database, Langfuse, simulated production, on-call containers
make console      # open http://127.0.0.1:8765
make help         # everything else
```

## Docs

[Design and strategy](code/docs/design.md) · [Decision records](code/docs/adr/README.md) · [Changelog](code/CHANGELOG.md) · [Harness](code/src/wyld3rness/README.md) · [FindyFixy](code/examples/findyfixy/README.md) · [Workspace](code/README.md) · [Infrastructure](code/infra/README.md) · [Checks](tools/README.md) · [Hooks](.githooks/README.md) · [Contributing](CONTRIBUTING.md)

## Limits

One machine, local models, synthetic data and a simulated production. Nothing here is evidence of safety on real systems.

<details>
<summary>Disclaimer</summary>

This software and its documentation are provided "AS IS", without warranty of any kind, express or implied, including but not limited to warranties of merchantability, fitness for a particular purpose, accuracy and non-infringement. It is a research prototype and is not intended for production use or for decisions about real systems, incidents, safety or finances. Model outputs can be wrong, and the measurements here describe one setup on synthetic data.

To the maximum extent permitted by law, the author and contributors are not liable for any claim, damages or other liability, whether in contract, tort or otherwise, arising from or in connection with the software, its use or its documentation, including direct, indirect, incidental, special, consequential or other loss. You use it at your own risk and are responsible for reviewing, testing and securing anything you run. This section does not replace the warranty disclaimer and limitation of liability in sections 7 and 8 of `LICENSE`, which govern.

</details>

## License

Apache-2.0. See `LICENSE` and `NOTICE`. Contributions are licensed under the same terms; see `CONTRIBUTING.md`.

---

made with ❤️ in <img src="code/docs/space-needle.png" alt="Space Needle" height="20"> Seattle by [wyld3rbit.ai](https://wyld3rbit.ai)
