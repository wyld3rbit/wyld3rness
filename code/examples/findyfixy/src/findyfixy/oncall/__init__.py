"""On call: FindyFixy operating simprod through the harness's loop, with no person approving each action.

    env.py         SimprodEnv: the harness's Environment port over simprod's edge
    decide.py      ModelDecider: the harness's Decider port, a model behind validation and one retry
    policy.py      FindyFixy's policy table, with the cap's precondition read from live metrics
    script.py      the script with no model: FindyFixy's three fix sets, in a given order
    service.py     the agent, the loop and the agent edge as services, each in its own container
    probe.py       FindyFixy's routes and ports for the harness's boundary probe
    cli.py         `make oncall`: seeded storms on the host, each incident handled by the loop container
    attacks.py     `make injection`: the attack suite against FindyFixy's policy and decider
    experiment.py  `make experiment`: every held-out storm through nobody, the script and the model

The loop itself, its verdict and its safe state are the harness's
(code/src/wyld3rness/loop/). Nothing is imported here.
"""
