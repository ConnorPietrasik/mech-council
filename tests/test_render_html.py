"""The HTML presenter: escaping, the badge, the anchors, and one page per report."""

from __future__ import annotations

from mech_council.deliberation import state
from mech_council.render import report_review
from mech_council.render.context import build_minimal
from mech_council.render.html import OUTCOME_CLASS, render_html
from tests.test_render import build, context, item, stance


def test_model_text_can_carry_no_markup_into_the_page():
    docket = build()
    item(docket, "F1", "<script>alert(1)</script> & <b>bold</b>", evidence="a.py:1")
    for agent in docket.roster:
        stance(docket, "F1", agent, state.AGREE)
    page = render_html(report_review.build(docket, context(outcome="consensus")), "review · /srv/app")
    assert "<script>" not in page, "a seat's claim is data, never markup"
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in page
    assert "<b>bold</b>" not in page


def test_the_outcome_wears_its_colour_and_the_sections_get_anchors():
    docket = build()
    item(docket, "F1", "the cache ignores tenants")
    for agent in docket.roster:
        stance(docket, "F1", agent, state.AGREE)
    page = render_html(report_review.build(docket, context(outcome="consensus")), "review · /srv/app")
    assert f'<span class="badge {OUTCOME_CLASS["consensus"]}">consensus</span>' in page
    assert '<dl class="meta">' in page, "the header card is the styled metadata list, not a bare dl"
    assert '<h2 id="roster">Roster</h2>' in page
    assert 'href="#roster"' in page, "the contents list reaches the heading"
    assert page.startswith("<!doctype html>")
    assert page.count("<html") == 1, "one page, not a fragment"


def test_an_unlisted_outcome_reads_muted():
    docket = build()
    page = render_html(report_review.build(docket, context(outcome="something_new")), "review · /srv/app")
    assert '<span class="badge muted">something_new</span>' in page


def test_the_minimal_report_is_one_page_too():
    doc = build_minimal(reason="interrupted by SIGINT 41.2 s in (phase 0)", context=context(outcome="aborted"))
    page = render_html(doc, "review · /srv/app")
    assert "Why nothing ran" in page
    assert "interrupted by SIGINT 41.2 s in (phase 0)" in page
    assert f'<span class="badge {OUTCOME_CLASS["aborted"]}">aborted</span>' in page
