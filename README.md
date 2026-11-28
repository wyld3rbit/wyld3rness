# wyld3rness

A governance harness for autonomous on-call agents: the agent decides what is wrong and what to do, and code decides everything else, so no person has to approve each action.

![The console on a live eval run: golden cases move through the example agent while the harness scores them](docs/img/demo.gif)

## Why

On-call work that a machine could do still wakes people up. An LLM agent can take some of it, but it will sometimes be wrong with full confidence, and the text it reads can be hostile. wyld3rness makes being wrong safe and affordable instead of trying to make the agent never wrong: people set the rules and the budget before anything runs, and code decides every step after that.

## Vision

On-call where agents handle incidents end to end and people govern the system instead of approving each step. An agent detects a fault, fixes it, rolls back what does not work and writes the postmortem, inside rules and a budget that people set. A person approves each new kind of action once, and evidence keeps or removes it. The project tests whether that is reachable today with open-source parts, one level of autonomy at a time ([autonomy as policy](docs/design.md#autonomy-as-policy)).

## Philosophy

- **Two model decisions; code owns the rest.** An incident runs as ordinary code, after [12-Factor Agents](https://github.com/humanlayer/12-factor-agents). The model decides what is wrong and what to do; whether a change may run, whether it worked and when to stop are decided from facts the model cannot write.
- **Make being wrong safe and affordable.** Every change is limited before it runs, judged after, undone when it did not help and paid for from a budget.
- **Judge on what customers saw.** A fix counts only when customers recover and stay recovered, measured outside the agent's reach.
- **Least privilege.** The agent holds no keys; every change carries a one-time pass bound to exactly that change.
- **Autonomy is earned by evidence.** A bar is written before each run, a rejection is a result, and an agent keeps a kind of work only while it beats the simplest alternative, such as an automated runbook.
- **Adopt before building.** Established, open-source, pinned components first; custom code only for what they lack.

## How it works

| What happens in an incident | Who does it | Status |
|---|---|---|
| An alert starts the incident | code | planned |
| Gather what is needed first: metrics, past incidents, recent deploys | code | recall of past incidents built; the rest planned |
| Find the cause | model, one small step | triage built; judged on cause planned |
| Choose a fix, as a typed action | model, one small step | planned |
| Check, sign and apply the fix | harness | the one-time pass built in simprod; the policy planned |
| Watch customers, keep or undo | harness | the recovery rule built; the loop planned |
| If it failed, tell the model why and try again | code | planned |
| Record every step, so a crash can resume | harness | a trace per run built; the event log planned |
| Out of tries: call a person | harness | planned |

Before a change to the agent ships, the eval gate scores it on golden cases at every commit, and CI refuses an answer-affecting change that no passing run covers. [Design and strategy](docs/design.md) has the full picture.

## Quick start

You need [uv](https://docs.astral.sh/uv/), Docker Compose with a runtime (Colima or Docker) and [Ollama](https://ollama.com/): 16 GB of memory and about 8.5 GB of models.

```bash
cp code/examples/findyfixy/.env.example code/examples/findyfixy/.env    # model names
make setup        # once: packages, models, database, incidents
make up           # start the services
make console      # open http://127.0.0.1:8765
make help         # everything else
```

## Usage

```bash
make console      # run a triage or an eval and watch each step
make eval         # score FindyFixy on the golden cases through the harness (live models, minutes)
make sim-up       # start simprod, the simulated production
make test         # the harness and FindyFixy tests, with fakes, about a minute
```

To put your own agent behind the harness, implement `SystemUnderTest` in `code/src/wyld3rness/eval/sut.py`. The harness never imports the agent, and a test fails if it does. [The harness guide](docs/harness.md) shows each part and where FindyFixy plugs in.

## Documentation

- [Design and strategy](docs/design.md): the whole picture, the policies and what each control enforces.
- [Decision records](docs/adr/README.md): each choice with the options rejected.
- [FindyFixy](docs/findyfixy.md), the agent it is tested on, and [simprod](docs/simprod.md), the simulated production.
- [Harness](docs/harness.md) · [Testing](docs/testing.md) · [Evals](docs/evals.md) · [Data](docs/data.md) · [Infrastructure](docs/infra.md) · [Checks](docs/tools.md) · [Hooks](docs/hooks.md) · [Changelog](CHANGELOG.md)
- [AGENTS.md](AGENTS.md): for coding agents working in this repository.

## Status

A research prototype: one machine, local models, synthetic data and a simulated production. Nothing here is evidence of safety on real systems.

## Contributing and license

See [CONTRIBUTING](CONTRIBUTING.md). Apache-2.0: see `LICENSE` and `NOTICE`. Contributions are licensed under the same terms.

<details>
<summary>Disclaimer</summary>

This software and its documentation are provided "AS IS", without warranty of any kind, express or implied, including but not limited to warranties of merchantability, fitness for a particular purpose, accuracy and non-infringement. It is a research prototype and is not intended for production use or for decisions about real systems, incidents, safety or finances. Model outputs can be wrong, and the measurements here describe one setup on synthetic data.

To the maximum extent permitted by law, the author and contributors are not liable for any claim, damages or other liability, whether in contract, tort or otherwise, arising from or in connection with the software, its use or its documentation, including direct, indirect, incidental, special, consequential or other loss. You use it at your own risk and are responsible for reviewing, testing and securing anything you run. This section does not replace the warranty disclaimer and limitation of liability in sections 7 and 8 of `LICENSE`, which govern.

</details>

---

made with ❤️ in <img src="docs/img/space-needle.png" alt="Space Needle" height="20"> Seattle by [wyld3rbit.ai](https://wyld3rbit.ai)
