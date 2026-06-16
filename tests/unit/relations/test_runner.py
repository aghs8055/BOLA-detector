"""Unit tests for `bola.relations.runner`."""

import pytest

from bola.db.store import Store
from bola.relations.models import (
    EdgeSource,
    EdgeTargetParameter,
    GroupRelations,
    RelationEdge,
    TargetRelations,
)
from bola.relations.runner import detect_relations
from bola.spec import views


class FakeDetector:
    """Stands in for RelationDetector: returns one edge per group, can fail chosen sources."""

    def __init__(self, fail_sources=()):
        self.fail_sources = set(fail_sources)
        self.calls = 0
        self.max_batch = 0  # largest batch the runner asked for

    def _one(self, source, targets):
        self.calls += 1
        if source.key in self.fail_sources:
            raise RuntimeError("boom")
        if not targets:
            return GroupRelations()
        edge = RelationEdge(
            source=EdgeSource(status_code="200", media_type="application/json",
                              json_pointer="/properties/id"),
            target=EdgeTargetParameter(name="x", location="path"),
            cast=None,
            rationale="x",
        )
        return GroupRelations(targets=[TargetRelations(target_key=targets[0].key, edges=[edge])])

    def detect_groups(self, jobs):
        self.max_batch = max(self.max_batch, len(jobs))
        out = []
        for source, targets in jobs:
            try:
                out.append(self._one(source, targets))
            except Exception as exc:  # return per-job, mirroring graph.batch(return_exceptions=True)
                out.append(exc)
        return out


class InterruptingDetector(FakeDetector):
    """Succeeds on the first batch, raises KeyboardInterrupt on the second (mid-detection Ctrl-C)."""

    def __init__(self):
        super().__init__()
        self._batches = 0

    def detect_groups(self, jobs):
        self._batches += 1
        if self._batches == 2:
            raise KeyboardInterrupt
        return super().detect_groups(jobs)


@pytest.fixture
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"))


def _run(spec, settings, store, detector):
    return detect_relations(spec, settings, store, detector=detector, show_progress=False)


def test_detect_reraises_interrupt_by_default_after_checkpointing(recursive_spec, settings, store):
    from bola.spec.views import spec_hash
    with pytest.raises(KeyboardInterrupt):
        detect_relations(recursive_spec, settings, store, detector=InterruptingDetector(),
                         show_progress=False)
    # the first unit was checkpointed before the interrupt → a rerun resumes from there
    done = store.get_group_results(spec_hash(recursive_spec), settings.llm.model)
    assert sum(1 for cp in done.values() if cp["status"] == "done") == 1


def test_detect_swallows_interrupt_when_reraise_disabled(recursive_spec, settings, store):
    res = detect_relations(recursive_spec, settings, store, detector=InterruptingDetector(),
                           show_progress=False, reraise_interrupt=False)
    assert not res.completed


def test_unit_count_follows_group_size(petstore_spec, settings, store):
    n = len(views.operations(petstore_spec))  # 19
    settings.relations.group_size = 10  # ceil(18/10) = 2 groups per source
    res = _run(petstore_spec, settings, store, FakeDetector())
    assert res.units_total == n * 2


def test_smaller_group_size_makes_more_units(petstore_spec, settings, store):
    n = len(views.operations(petstore_spec))
    settings.relations.group_size = 5  # ceil(18/5) = 4 groups per source
    res = _run(petstore_spec, settings, store, FakeDetector())
    assert res.units_total == n * 4


def test_completed_run_assembles_cache(recursive_spec, settings, store):
    res = _run(recursive_spec, settings, store, FakeDetector())
    assert res.completed
    assert res.units_done == res.units_total
    from bola.spec.views import spec_hash

    cached = store.get_relations(spec_hash(recursive_spec), settings.llm.model)
    assert cached == res.relations
    assert len(res.relations) > 0


def test_resume_skips_completed_units(recursive_spec, settings, store):
    _run(recursive_spec, settings, store, FakeDetector())
    second = FakeDetector()
    res = _run(recursive_spec, settings, store, second)
    assert res.units_skipped == res.units_total
    assert res.units_done == 0
    assert second.calls == 0  # nothing re-detected
    assert res.completed


def test_result_records_elapsed_time(recursive_spec, settings, store):
    res = _run(recursive_spec, settings, store, FakeDetector())
    assert res.completed
    assert res.elapsed_s >= 0.0


def test_on_error_stop_halts(petstore_spec, settings, store):
    settings.relations.on_error = "stop"
    res = _run(petstore_spec, settings, store, FakeDetector(fail_sources={"addPet"}))
    assert not res.completed
    assert res.units_failed == 1
    assert res.units_done < res.units_total  # stopped early


def test_on_error_continue_processes_rest(petstore_spec, settings, store):
    settings.relations.on_error = "continue"
    res = _run(petstore_spec, settings, store, FakeDetector(fail_sources={"addPet"}))
    assert not res.completed  # a failed unit means not fully done
    assert res.units_failed >= 1
    assert res.units_done > 0


def test_batch_size_groups_units_into_batches(petstore_spec, settings, store):
    settings.relations.group_size = 10  # 19 ops × 2 groups = 38 units
    settings.relations.batch_size = 8
    det = FakeDetector()
    res = detect_relations(petstore_spec, settings, store, detector=det, show_progress=False)
    assert res.completed
    assert res.units_done == res.units_total
    assert det.max_batch == 8  # runner actually batched


def test_batch_size_one_is_sequential(recursive_spec, settings, store):
    settings.relations.batch_size = 1
    det = FakeDetector()
    res = detect_relations(recursive_spec, settings, store, detector=det, show_progress=False)
    assert res.completed
    assert det.max_batch == 1


def test_failure_inside_batch_still_checkpoints_survivors_continue(petstore_spec, settings, store):
    # One whole batch covers every unit; a failing source must not lose its batch-mates.
    settings.relations.group_size = 10
    settings.relations.batch_size = 100  # all 38 units in a single batch
    settings.relations.on_error = "continue"
    res = _run(petstore_spec, settings, store, FakeDetector(fail_sources={"addPet"}))
    assert not res.completed
    assert res.units_failed == 2  # addPet -> 2 groups
    assert res.units_done == res.units_total - res.units_failed
    # every survivor in that batch was persisted as done
    from bola.spec.views import spec_hash

    done = [v for v in store.get_group_results(spec_hash(petstore_spec), settings.llm.model).values()
            if v["status"] == "done"]
    assert len(done) == res.units_done


def test_failure_inside_batch_then_stop(petstore_spec, settings, store):
    settings.relations.group_size = 10
    settings.relations.batch_size = 100
    settings.relations.on_error = "stop"
    res = _run(petstore_spec, settings, store, FakeDetector(fail_sources={"addPet"}))
    assert not res.completed
    assert res.units_failed == 2
    # the batch's outcomes are all processed before stopping, so survivors are still saved
    assert res.units_done == res.units_total - res.units_failed


def test_failed_unit_is_retried_on_resume(recursive_spec, settings, store):
    settings.relations.on_error = "continue"
    first = _run(recursive_spec, settings, store, FakeDetector(fail_sources={"createCategory"}))
    assert first.units_failed >= 1
    # rerun with a healthy detector: previously-failed units are not "done", so retried
    res = _run(recursive_spec, settings, store, FakeDetector())
    assert res.completed
    assert res.units_done >= 1