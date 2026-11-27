"""The harness: the product that measures an LLM system, gates its releases and runs its incidents.

It knows nothing about any one application. An application plugs in through
seams: `wyld3rness.eval.sut.SystemUnderTest` for evaluation, and
`wyld3rness.loop.env.Environment` with `wyld3rness.loop.run.Decider` for the
loop. The dependency rule is one way: an application may import the harness,
and nothing under `wyld3rness/` may import an application. The tests in
code/tests/test_harness_rules.py enforce it.
"""
