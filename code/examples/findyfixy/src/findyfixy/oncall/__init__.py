"""On call: FindyFixy operating simprod through the harness's loop, with no person approving each action.

    env.py      SimprodEnv: the harness's Environment port over simprod's edge
    decide.py   ModelDecider: the harness's Decider port, a model behind validation and one retry
    policy.py   FindyFixy's policy table, with the cap's precondition read from live metrics
    service.py  the agent and the loop as services, each in its own container
    cli.py      `make oncall`: seeded storms on the host, each incident handled by the loop container

The loop itself, its verdict and its safe state are the harness's
(code/src/wyld3rness/loop/). Nothing is imported here.
"""
