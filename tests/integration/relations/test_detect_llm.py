"""Real-LLM relation detection — costs money, excluded by default.

Run explicitly with:  pytest -m llm tests/integration/relations
Requires OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway) in the environment / .env.

The fixture is a deliberately tiny two-resource spec (projects + tasks) rather than a real-world
spec: it keeps prompt/token cost low and the ground truth small enough to be reproduced reliably,
while still exercising both relation kinds the detector exists to find —
- **identity carry-over**: an object's `id` in a create/read response flows into a `{...Id}` path
  parameter of a read/delete op,
- **foreign key**: a task carries a `projectId`, so a project's id flows into the task create body
  and a task's `projectId` selects a project.
"""

import pytest
from pydantic import BaseModel, Field

from bola.llm import build_chat_model
from bola.relations.detector import RelationDetector
from bola.settings import load_settings
from bola.spec import load_spec_from_dict
from bola.spec import views

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def settings():
    s = load_settings()
    s.llm.model = "anthropic/claude-haiku-4.5"  # integration tests run on the cheap model
    s.llm.analysis_model = ""
    return s


@pytest.fixture(scope="module")
def spec():
    """Projects + tasks: a task references a project via `projectId` (a foreign key).

    Operations: createProject / getProject / deleteProject and createTask / getTask. `Project.id`
    selects a project (path param `projectId`); `Task.id` selects a task (path param `taskId`);
    `Task.projectId` is the foreign key tying a task to its project.
    """
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "projects", "version": "1.0.0"},
        "paths": {
            "/projects": {
                "post": {
                    "operationId": "createProject",
                    "summary": "Create a project",
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"name": {"type": "string"}}}}}},
                    "responses": {"201": {"description": "created", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Project"}}}}},
                }
            },
            "/projects/{projectId}": {
                "get": {
                    "operationId": "getProject",
                    "summary": "Read a project",
                    "parameters": [{"name": "projectId", "in": "path", "required": True,
                                    "schema": {"type": "integer", "format": "int64"}}],
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Project"}}}}},
                },
                "delete": {
                    "operationId": "deleteProject",
                    "summary": "Delete a project",
                    "parameters": [{"name": "projectId", "in": "path", "required": True,
                                    "schema": {"type": "integer", "format": "int64"}}],
                    "responses": {"204": {"description": "deleted"}},
                },
            },
            "/tasks": {
                "post": {
                    "operationId": "createTask",
                    "summary": "Create a task under a project",
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"projectId": {"type": "integer", "format": "int64"},
                                       "title": {"type": "string"}}}}}},
                    "responses": {"201": {"description": "created", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}}},
                }
            },
            "/tasks/{taskId}": {
                "get": {
                    "operationId": "getTask",
                    "summary": "Read a task",
                    "parameters": [{"name": "taskId", "in": "path", "required": True,
                                    "schema": {"type": "integer", "format": "int64"}}],
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}}},
                }
            },
        },
        "components": {"schemas": {
            "Project": {"type": "object", "properties": {
                "id": {"type": "integer", "format": "int64"}, "name": {"type": "string"}}},
            "Task": {"type": "object", "properties": {
                "id": {"type": "integer", "format": "int64"},
                "projectId": {"type": "integer", "format": "int64"},
                "title": {"type": "string"}}},
        }},
    }
    return load_spec_from_dict(doc, source="<projects>")


@pytest.fixture(autouse=True)
def _require_key(settings):
    if not settings.openai_api_key:
        pytest.skip("no OPENAI_API_KEY configured")


def _op(spec, key):
    return next(o for o in views.operations(spec) if o.key == key)


def test_detects_create_to_read_id_carryover(spec, settings):
    source = _op(spec, "createProject")
    targets = [_op(spec, k) for k in ("getProject", "deleteProject")]
    det = RelationDetector(spec, settings)

    result = det.detect_group(source, targets)

    edges = [(t.target_key, e) for t in result.targets for e in t.edges]
    assert edges, "expected at least one relation from createProject"
    # the project id returned by createProject should map into a path parameter of a read/del op
    assert any(
        e.source.json_pointer.endswith("/id")
        and getattr(e.target, "location", None) == "path"
        for _, e in edges
    ), f"expected an id->path-param carry-over, got {[(k, e.model_dump()) for k, e in edges]}"


def _expected_relations() -> set[tuple[str, str, str, str]]:
    """The identity carry-overs in the projects+tasks spec — the robust ground truth.

    Tuple = (source_key, source_pointer, target_key, target_descriptor). These are the
    consistently-detected `id`->path-param mappings: a project/task id in a create or read response
    selecting that object in a read/delete op. (The foreign-key edges are checked separately in
    `test_detects_project_foreign_key`, kept out of this strict set because foreign keys are emitted
    less symmetrically and are the most likely to need a rerun on a model revision.)
    """
    return {
        ("createProject", "/properties/id", "getProject", "param:projectId/path"),
        ("createProject", "/properties/id", "deleteProject", "param:projectId/path"),
        ("getProject", "/properties/id", "deleteProject", "param:projectId/path"),
        ("createTask", "/properties/id", "getTask", "param:taskId/path"),
    }


def _descriptor(edge) -> str:
    t = edge.target
    if t.type == "parameter":
        return f"param:{t.name}/{t.location}"
    return f"body:{t.media_type}{t.json_pointer}"


def _detect_all(spec, settings, sources) -> set[tuple[str, str, str, str]]:
    """Run the detector with each source against all other ops; flatten to descriptor tuples."""
    all_ops = views.operations(spec)
    by_key = {o.key: o for o in all_ops}
    det = RelationDetector(spec, settings)

    detected: set[tuple[str, str, str, str]] = set()
    for src_key in sources:
        source = by_key[src_key]
        targets = [o for o in all_ops if o.key != src_key]
        group = det.detect_group(source, targets)
        for tr in group.targets:
            for e in tr.edges:
                detected.add((src_key, e.source.json_pointer, tr.target_key, _descriptor(e)))
    return detected


def test_all_relations_detected(spec, settings):
    """100% recall: every identity carry-over in the spec must be detected (extra FPs allowed)."""
    core = _expected_relations()
    sources = {s for (s, _, _, _) in core}

    detected = _detect_all(spec, settings, sources)

    missing = core - detected
    assert not missing, "missing core relations:\n" + "\n".join(
        "  " + " | ".join(m) for m in sorted(missing)
    )


def test_detects_project_foreign_key(spec, settings):
    """The `Task.projectId` foreign key must be detected in at least one direction.

    Either a project's id flows into the task-create body, or a task's `projectId` selects a project
    by path param. Foreign keys are less consistently emitted than identities, so this asserts the
    relation exists without pinning the exact source/target pair.
    """
    detected = _detect_all(spec, settings, {"createProject", "getProject", "createTask", "getTask"})

    fk_found = any(
        # project id -> task-create body.projectId
        (desc == "body:application/json/properties/projectId" and ptr.endswith("/id"))
        # task.projectId -> project path param
        or (ptr.endswith("/projectId") and desc == "param:projectId/path")
        for (_, ptr, _, desc) in detected
    )
    assert fk_found, f"projectId foreign key not detected; got:\n" + "\n".join(
        "  " + " | ".join(d) for d in sorted(detected)
    )


class Judgement(BaseModel):
    plausible: bool = Field(description="Are the proposed relations semantically reasonable?")
    reason: str


def test_llm_judge_rates_relations_plausible(spec, settings):
    source = _op(spec, "createProject")
    targets = [_op(spec, k) for k in ("getProject", "deleteProject", "createTask")]
    det = RelationDetector(spec, settings)
    result = det.detect_group(source, targets)

    judge = build_chat_model(settings).with_structured_output(Judgement)
    verdict = judge.invoke(
        "You audit API data-flow analysis. Source operation: createProject (creates a project, "
        "returns it with an id). Proposed relations (source field -> target input):\n"
        + "\n".join(
            f"- {t.target_key}: {e.source.json_pointer} -> {e.target.model_dump()}"
            for t in result.targets
            for e in t.edges
        )
        + "\nAre these plausible identifier carry-overs? Reject any obviously wrong mapping."
    )
    assert verdict.plausible, verdict.reason