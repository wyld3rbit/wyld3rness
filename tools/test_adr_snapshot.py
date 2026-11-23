"""Tests for tools/adr_snapshot.py.

The live page is the source of the ADR pictures from phase 5 on, and phases 1
to 4 are frozen files. These tests keep both true: the latest phase must match
what the page marks built, a frozen picture must keep its recorded hash, a
drawn picture must equal a fresh render, and a phase draws only what existed then.

Run from the training root: `make check`.
"""

import hashlib
import xml.dom.minidom

import pytest

import adr_snapshot as snap

PAGE = snap.PAGE.read_text()
DRAWN = [r for r in snap.RECORDS if r not in snap.FROZEN]


def as_built(page: str, name: str) -> str:
    """The page with one planned node, found by its title, flipped to built."""
    block = next(b for b in snap.page_parts(page)[0] if snap.node_name(b) == name)
    return page.replace(block, block.replace('data-status="planned"', 'data-status="built"', 1))


def test_the_latest_phase_matches_the_live_page() -> None:
    assert snap.built_in_phase(max(snap.PHASES)) == snap.built_on_page(PAGE)


def test_flipping_a_node_on_the_page_without_a_new_phase_is_caught() -> None:
    flipped = as_built(PAGE, "Policy")
    assert flipped != PAGE
    assert snap.built_in_phase(max(snap.PHASES)) != snap.built_on_page(flipped)


def test_the_frozen_pictures_keep_their_recorded_hashes() -> None:
    """Every frozen ADR picture still has the hash it was accepted with."""
    for record, digest in snap.FROZEN.items():
        assert hashlib.sha256(snap.image_path(record).read_bytes()).hexdigest() == digest, record


def test_a_changed_frozen_picture_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(snap, "OUT", tmp_path)
    snap.image_path(1).write_text("<svg/>")
    with pytest.raises(snap.FrozenImageChanged):
        snap.check_frozen(1)


def test_a_frozen_record_is_never_drawn_from_the_page() -> None:
    """render() refuses every frozen ADR, including 0005, whose phase is drawn again for 0006."""
    for record in snap.FROZEN:
        with pytest.raises(ValueError, match="frozen"):
            snap.render(record, PAGE)


def test_every_record_maps_to_a_known_phase() -> None:
    """Each ADR picture shows a phase that exists, and the newest ADR shows the latest phase."""
    assert set(snap.RECORDS.values()) <= set(snap.PHASES)
    assert snap.RECORDS[max(snap.RECORDS)] == max(snap.PHASES)


def test_phase_five_draws_both_lanes_and_the_loop_between_them() -> None:
    svg = snap.snapshot(5, PAGE)
    for needle in (">Harness<", ">FindyFixy<", ">cases in<", ">answers scored<",
                   'id="e_eval_q"', 'id="e_rec_eval"'):
        assert needle in svg, needle
    for name in ("Question", "Recall", "Investigate", "Deploy tool", "Small model", "Validate", "Route",
                 "Large model", "Triage record", "Eval gate", "CI gate"):
        block = svg.split(f">{name}<")[0].rsplit("<g ", 1)[1]
        assert 'data-status="built"' in block, name
    for name in ("Tracing", "Policy", "Gateway", "Budget", "Verify", "Safe state"):
        assert f">{name}<" in svg, name
        block = svg.split(f">{name}<")[0].rsplit("<g ", 1)[1]
        assert 'data-status="planned"' in block, name
    assert "data-code" not in svg and "<title>" not in svg and "tool step off" not in svg


def test_a_node_built_later_is_drawn_planned_with_its_day(monkeypatch) -> None:
    # Pretend Policy was built on Day 7: the Day 6 picture must still show it planned.
    monkeypatch.setitem(snap.FIRST_BUILT, "Policy", 7)
    page = as_built(PAGE, "Policy")
    svg = snap.snapshot(6, page)
    block = svg.split(">Policy<")[0].rsplit("<g ", 1)[1]
    assert 'data-status="planned"' in block
    assert ">Day 7<" in svg.split(">Policy<")[1].split("</g>")[0]


def test_every_drawn_snapshot_is_well_formed() -> None:
    for record in DRAWN:
        xml.dom.minidom.parseString(snap.render(record, PAGE))


def test_the_committed_drawn_images_equal_a_fresh_render() -> None:
    for record in DRAWN:
        path = snap.image_path(record)
        assert path.read_text() == snap.render(record, PAGE), f"{path.name} is stale: run make adr-snapshot"


def test_the_page_draws_the_whole_loop_with_cost_and_no_human_review() -> None:
    """The plan on the page: act, verify, undo or stop, inside a budget, with nobody approving each action."""
    names = {snap.node_name(b) for b in snap.page_parts(PAGE)[0]}
    assert {"Policy", "Identity", "Act", "Verify", "Undo", "Safe state", "Kill switch", "Budget", "Scorecard"} <= names
    assert "Human review" not in names


def test_the_target_design_is_drawn_from_the_page() -> None:
    """One source: the design figure is a render of the page, with its planned paths."""
    svg = snap.target(PAGE)
    xml.dom.minidom.parseString(svg)
    assert snap.TARGET.read_text() == svg, "target-design.svg is stale: run make adr-snapshot"
    assert 'id="p_verify_undo"' in svg


def test_adr_0008_keeps_the_plan_it_was_accepted_with() -> None:
    """A direction record: frozen, with its planned paths, and not captioned as an earlier phase."""
    svg = snap.image_path(8).read_text()
    assert 8 in snap.FROZEN
    assert 'id="p_verify_undo"' in svg and 'id="p_verify_safe"' in svg
    assert "close of Day 6" not in svg


def test_the_target_keeps_its_plan_when_a_phase_is_added(monkeypatch) -> None:
    """A new phase must not empty the planned loop out of the target design."""
    monkeypatch.setattr(snap, "PHASES", range(1, max(snap.PHASES) + 2))
    assert 'id="p_verify_undo"' in snap.target(PAGE)


def test_every_phase_drawn_from_the_page_is_well_formed() -> None:
    """Not left to DRAWN, which is empty while every record is frozen."""
    for phase in (5, max(snap.PHASES)):
        xml.dom.minidom.parseString(snap.snapshot(phase, PAGE))
