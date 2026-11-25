"""On call: FindyFixy operating simprod through the harness's loop, with no person approving each action.

    env.py      SimprodEnv: the harness's Environment port over simprod's edge
    decide.py   ModelDecider: the harness's Decider port, a model behind validation and one retry
    cli.py      `make oncall`: seeded storms through the loop, one line each, a results file

The loop itself, its verdict and its safe state are the harness's
(code/src/wyld3rness/loop/). Nothing is imported here.
"""
