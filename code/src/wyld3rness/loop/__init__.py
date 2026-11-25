"""The loop: the harness component that runs one incident with no person approving each action.

Modules, from the bottom of the dependency graph up:
    verdict.py     the good-second rule: verified, recovered and held, from the user
                   stream only. Standard library only.
    env.py         Action, Applied and the Environment port an app implements for
                   the system it operates.
    run.py         run_incident: decide, act, wait, verify, hold, then keep, undo and
                   retry, or the safe state. The Decider port the model sits behind.

Why this is the harness's and not the app's
    What happens after a model proposes an action decides whether autonomy is
    safe: how long to wait, what counts as recovered, when to undo, when to stop.
    That is the same for every app and must not be something a model can talk
    its way around. The app supplies the environment and the decider
    (FindyFixy: findyfixy/oncall/). The harness never imports an app
    (code/tests/test_harness_rules.py).

Nothing is imported here, so importing the package costs nothing.
"""
