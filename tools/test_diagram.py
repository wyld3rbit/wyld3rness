"""Tests for tools/diagram.py: every picture and every copy in the live page equals a fresh render of its spec.

Run from the training root: `make check`.
"""

import pytest

import diagram

PAGE = diagram.PAGE.read_text()
SPEC = """
caption = "A test"
width = 400
height = 200

[[zone]]
label = "Box & co"
x = 0
y = 0
w = 400
h = 200

[[box]]
id = "a"
name = "Policy"
icon = "Policy"
x = 10
y = 20

[[box]]
id = "b"
name = "Act"
icon = "Act"
x = 10
y = 120
status = "planned"

[[edge]]
from = "a"
to = "b"
label = "check"
status = "planned"
"""


@pytest.mark.parametrize("spec", diagram.specs(), ids=lambda p: p.stem)
def test_every_committed_picture_equals_a_fresh_render(spec) -> None:
    fresh = diagram.render(spec.read_text(), PAGE)
    assert (diagram.OUT / f"{spec.stem}.svg").read_text() == fresh, "run `make adr-snapshot` and commit the picture"
    assert diagram.page_copy(PAGE, spec.stem) == fresh, "the live page's copy differs: run `make adr-snapshot`"


@pytest.mark.parametrize("spec", diagram.doc_specs(), ids=lambda p: p.stem)
def test_every_docs_picture_equals_a_fresh_render(spec) -> None:
    fresh = diagram.render(spec.read_text(), PAGE)
    assert (diagram.DOC_OUT / f"{spec.stem}.svg").read_text() == fresh, "run `make adr-snapshot` and commit the picture"
    assert diagram.page_copy(PAGE, spec.stem) is None, "a docs picture has no copy in the live page"


def test_there_is_at_least_one_docs_spec() -> None:
    assert diagram.doc_specs(), "no docs spec found: the check above would pass with nothing to check"


def test_there_is_at_least_one_spec() -> None:
    assert diagram.specs(), "no spec found: the check above would pass with nothing to check"


def test_icons_come_from_the_live_page() -> None:
    svg = diagram.render(SPEC, PAGE)
    policy_icon = diagram.page_icons(PAGE)["Policy"]
    assert policy_icon and policy_icon in svg


def test_text_is_escaped_and_status_is_drawn() -> None:
    svg = diagram.render(SPEC, PAGE)
    assert "Box &amp; co" in svg and 'class="dg-box planned"' in svg and 'class="dg-edge planned"' in svg


def test_the_diagram_cannot_be_mistaken_for_a_page_node() -> None:
    """The page's truth test counts <g class="node"> blocks; a diagram uses its own classes."""
    assert 'class="node"' not in diagram.render(SPEC, PAGE)


def test_a_vertical_edge_goes_straight_with_its_label_beside_it() -> None:
    svg = diagram.render(SPEC, PAGE)
    assert 'd="M85,84 L85,120"' in svg and 'text-anchor="start"' in svg


def test_an_edge_bends_through_its_via_points() -> None:
    a = diagram.Box("a", "A", "", "Act", 0, 0, "built")
    b = diagram.Box("b", "B", "", "Act", 300, 200, "built")
    assert diagram.route(a, b, [(75, 150), (375, 150)]) == [(75, 64), (75, 150), (375, 150), (375, 200)]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('icon = "Act"', 'icon = "Nope"'), "no box named 'Nope'"),
        (('to = "b"', 'to = "c"'), "no box with id"),
        (('status = "planned"\n\n[[edge]]', 'status = "maybe"\n\n[[edge]]'), "status must be one of"),
    ],
)
def test_a_spec_naming_something_that_does_not_exist_fails(change, message) -> None:
    with pytest.raises(diagram.SpecError, match=message):
        diagram.render(SPEC.replace(*change), PAGE)


def test_a_page_without_the_region_is_refused() -> None:
    with pytest.raises(diagram.SpecError, match="no <!-- diagram missing --> region"):
        diagram.with_copy(PAGE, "missing", "<svg/>")
