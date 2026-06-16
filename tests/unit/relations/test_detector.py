"""Unit tests for `bola.relations.detector`."""

import pytest

from bola.relations.detector import RelationDetector
from bola.relations.models import (
    EdgeSource,
    EdgeTargetParameter,
    GroupRelations,
    RelationEdge,
    TargetRelations,
)
from bola.spec import views


class FakeChain:
    """A chain stub returning successive payloads (last one repeats)."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def invoke(self, inputs, config=None):
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return payload


def _op(spec, key):
    return next(o for o in views.operations(spec) if o.key == key)


def _group(*edges, target="getCategory"):
    return GroupRelations(targets=[TargetRelations(target_key=target, edges=list(edges))])


def _edge(pointer="/properties/id", name="categoryId", location="path"):
    return RelationEdge(
        source=EdgeSource(status_code="201", media_type="application/json", json_pointer=pointer),
        target=EdgeTargetParameter(name=name, location=location),
        cast=None,
        rationale="x",
    )


def _detect(spec, settings, detect, revise):
    return RelationDetector(spec, settings, detect_chain=detect, revise_chain=revise)


def _edge_count(group):
    return sum(len(t.edges) for t in group.targets)


def test_clean_detection_skips_revise(recursive_spec, settings):
    detect = FakeChain(_group(_edge()))
    revise = FakeChain(GroupRelations())
    det = _detect(recursive_spec, settings, detect, revise)
    out = det.detect_group(_op(recursive_spec, "createCategory"), [_op(recursive_spec, "getCategory")])
    assert _edge_count(out) == 1
    assert revise.calls == 0


def test_invalid_edge_triggers_revise_and_is_dropped(recursive_spec, settings):
    good, bogus = _edge(), _edge(pointer="/properties/ghost")
    detect = FakeChain(_group(good, bogus))
    revise = FakeChain(_group(good))  # model repairs by dropping the bad edge
    det = _detect(recursive_spec, settings, detect, revise)
    out = det.detect_group(_op(recursive_spec, "createCategory"), [_op(recursive_spec, "getCategory")])
    assert revise.calls == 1
    assert _edge_count(out) == 1
    assert out.targets[0].edges[0].source.json_pointer == "/properties/id"


def test_regressing_revise_keeps_best(recursive_spec, settings):
    good, bogus = _edge(), _edge(pointer="/properties/ghost")
    detect = FakeChain(_group(good, bogus))
    revise = FakeChain(GroupRelations())  # revise wrongly empties everything
    det = _detect(recursive_spec, settings, detect, revise)
    out = det.detect_group(_op(recursive_spec, "createCategory"), [_op(recursive_spec, "getCategory")])
    assert _edge_count(out) == 1  # best-of protects the validated edge


def test_edge_with_has_relation_false_is_dropped(recursive_spec, settings):
    # Structurally valid edge, but the model's own verdict rejects it -> dropped, no revise.
    rejected = RelationEdge(
        source=EdgeSource(status_code="201", media_type="application/json",
                          json_pointer="/properties/id"),
        target=EdgeTargetParameter(name="categoryId", location="path"),
        cast=None,
        rationale="not actually the same identifier",
        has_relation=False,
    )
    detect = FakeChain(_group(rejected))
    revise = FakeChain(GroupRelations())
    det = _detect(recursive_spec, settings, detect, revise)
    out = det.detect_group(_op(recursive_spec, "createCategory"), [_op(recursive_spec, "getCategory")])
    assert _edge_count(out) == 0
    assert revise.calls == 0  # rejected edge produced no validation error, so no revise


def test_unknown_target_key_dropped(recursive_spec, settings):
    detect = FakeChain(_group(_edge(), target="ghostOp"))
    revise = FakeChain(GroupRelations())
    det = _detect(recursive_spec, settings, detect, revise)
    out = det.detect_group(_op(recursive_spec, "createCategory"), [_op(recursive_spec, "getCategory")])
    assert _edge_count(out) == 0


class SelectiveChain:
    """Returns a payload, but raises when the context's source is a chosen op (per-job failure)."""

    def __init__(self, payload, raise_for_source=None):
        self.payload = payload
        self.raise_for_source = raise_for_source

    def invoke(self, inputs, config=None):
        import json

        source_key = json.loads(inputs["context"]).get("source", {}).get("key")
        if self.raise_for_source and source_key == self.raise_for_source:
            raise RuntimeError("boom")
        return self.payload


def test_detect_groups_runs_batch_and_passes_through_exceptions(recursive_spec, settings):
    detect = SelectiveChain(_group(_edge()), raise_for_source="getCategory")
    det = _detect(recursive_spec, settings, detect, FakeChain(GroupRelations()))

    create = _op(recursive_spec, "createCategory")
    get = _op(recursive_spec, "getCategory")
    out = det.detect_groups([(create, [get]), (get, [create])])

    assert len(out) == 2
    assert _edge_count(out[0]) == 1  # createCategory job succeeded
    assert isinstance(out[1], BaseException)  # getCategory job's failure returned, not raised


def test_detect_groups_empty_jobs(recursive_spec, settings):
    det = _detect(recursive_spec, settings, FakeChain(GroupRelations()), FakeChain(GroupRelations()))
    assert det.detect_groups([]) == []


def test_body_edge_expands_across_media_types(petstore_spec, settings):
    # updatePet's request body declares the same Pet schema under 3 media types; a single
    # detected json body edge must be expanded to all of them by the deterministic backstop.
    from bola.relations.models import EdgeTargetRequestBody

    edge = RelationEdge(
        source=EdgeSource(status_code="200", media_type="application/json",
                          json_pointer="/properties/id"),
        target=EdgeTargetRequestBody(media_type="application/json", json_pointer="/properties/id"),
        cast=None,
        rationale="x",
    )
    detect = FakeChain(GroupRelations(targets=[TargetRelations(target_key="updatePet", edges=[edge])]))
    det = _detect(petstore_spec, settings, detect, FakeChain(GroupRelations()))
    out = det.detect_group(_op(petstore_spec, "addPet"), [_op(petstore_spec, "updatePet")])

    media_types = {e.target.media_type for t in out.targets for e in t.edges}
    assert media_types == {"application/json", "application/xml", "application/x-www-form-urlencoded"}


def test_revise_is_bounded_by_max_attempts(recursive_spec, settings):
    settings.relations.max_attempts = 3
    bogus = _group(_edge(pointer="/properties/ghost"))
    detect = FakeChain(bogus)
    revise = FakeChain(bogus)  # never repairs
    det = _detect(recursive_spec, settings, detect, revise)
    out = det.detect_group(_op(recursive_spec, "createCategory"), [_op(recursive_spec, "getCategory")])
    assert _edge_count(out) == 0
    assert revise.calls == 2  # detect(attempt1) + revise(2) + revise(3) -> stop