"""The loop's building blocks: what an agent's action is, how it is signed, and how recovery is judged.

Modules, from the bottom of the dependency graph up:
    verdict.py     the good-second rule: verified, recovered and held, judged from
                   the user stream only. Standard library only.
    env.py         Action, Applied and the Environment port an app implements for
                   the system it operates.
    credential.py  a one-time pass for exactly one action, signed by a vault the
                   agent cannot reach.

Why these are the harness's and not the app's
    What counts as recovered, and what proves an action was allowed, must be the
    same for every app and must not be something a model can talk its way
    around. The harness never imports an app (code/tests/test_harness_rules.py).

Nothing is imported here, so importing the package costs nothing.
"""
