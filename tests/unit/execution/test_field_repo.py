"""Unit tests for `bola.execution.field_repo`."""

from bola.execution.field_repo import (
    FieldRepo,
    candidates_for_body,
    candidates_for_parameter,
    harvest,
)


def _rel(source_key, target, *, pointer="/properties/id", status="200", media="application/json"):
    return {
        "source_key": source_key,
        "target_key": target["__op"],
        "edge": {
            "source": {"status_code": status, "media_type": media, "json_pointer": pointer},
            "target": target,
            "cast": None,
            "rationale": "x",
            "has_relation": True,
        },
    }


def _param_target(op, name, location):
    return {"__op": op, "type": "parameter", "name": name, "location": location}


def _body_target(op, media, pointer):
    return {"__op": op, "type": "request_body", "media_type": media, "json_pointer": pointer}


# -- FieldRepo -------------------------------------------------------------------


def test_add_and_get_dedups_preserving_order():
    repo = FieldRepo()
    repo.add("addPet", "/properties/id", 1)
    repo.add("addPet", "/properties/id", 1)
    repo.add("addPet", "/properties/id", 2)
    assert repo.get("addPet", "/properties/id") == [1, 2]


def test_get_unknown_coordinate_is_empty():
    assert FieldRepo().get("nope", "/properties/id") == []


def test_fork_is_a_deep_copy():
    repo = FieldRepo()
    repo.add("addPet", "/properties/id", 1)
    fork = repo.fork()
    fork.add("addPet", "/properties/id", 2)
    assert repo.get("addPet", "/properties/id") == [1]
    assert fork.get("addPet", "/properties/id") == [1, 2]


# -- harvest ---------------------------------------------------------------------


def test_harvest_stores_matching_source_values():
    repo = FieldRepo()
    relations = [_rel("addPet", _param_target("getPetById", "petId", "path"))]
    n = harvest(repo, "addPet", 200, "application/json", {"id": 42}, relations)
    assert n == 1
    assert repo.get("addPet", "/properties/id") == [42]


def test_harvest_ignores_status_or_media_mismatch():
    repo = FieldRepo()
    relations = [_rel("addPet", _param_target("getPetById", "petId", "path"))]
    assert harvest(repo, "addPet", 404, "application/json", {"id": 42}, relations) == 0
    assert harvest(repo, "addPet", 200, "application/xml", {"id": 42}, relations) == 0
    assert repo.items() == {}


def test_harvest_skips_non_scalar_and_bool():
    repo = FieldRepo()
    relations = [_rel("addPet", _param_target("getPetById", "petId", "path"))]
    # value is an object, not a scalar id
    assert harvest(repo, "addPet", 200, "application/json", {"id": {"nested": 1}}, relations) == 0
    # bool is not an id
    assert harvest(repo, "addPet", 200, "application/json", {"id": True}, relations) == 0


def test_harvest_fans_out_array_response():
    repo = FieldRepo()
    relations = [
        _rel("listPets", _param_target("getPetById", "petId", "path"), pointer="/items/properties/id")
    ]
    harvest(repo, "listPets", 200, "application/json", [{"id": 1}, {"id": 2}], relations)
    assert repo.get("listPets", "/items/properties/id") == [1, 2]


# -- candidates ------------------------------------------------------------------


def test_candidates_for_parameter():
    repo = FieldRepo()
    relations = [_rel("addPet", _param_target("getPetById", "petId", "path"))]
    harvest(repo, "addPet", 200, "application/json", {"id": 42}, relations)
    assert candidates_for_parameter(repo, relations, "getPetById", "petId", "path") == [42]
    # wrong location / name -> nothing
    assert candidates_for_parameter(repo, relations, "getPetById", "petId", "query") == []


def test_candidates_for_body():
    repo = FieldRepo()
    relations = [
        _rel("addPet", _body_target("updatePet", "application/json", "/properties/id"))
    ]
    harvest(repo, "addPet", 200, "application/json", {"id": 7}, relations)
    assert candidates_for_body(repo, relations, "updatePet", "application/json", "/properties/id") == [7]


def test_candidates_union_across_sources_dedup():
    repo = FieldRepo()
    relations = [
        _rel("addPet", _param_target("getPetById", "petId", "path")),
        _rel("listPets", _param_target("getPetById", "petId", "path"), pointer="/items/properties/id"),
    ]
    harvest(repo, "addPet", 200, "application/json", {"id": 1}, relations)
    harvest(repo, "listPets", 200, "application/json", [{"id": 1}, {"id": 2}], relations)
    assert candidates_for_parameter(repo, relations, "getPetById", "petId", "path") == [1, 2]

def test_owner_tracking_separates_victim_and_attacker_ids():
    from bola.execution.field_repo import FieldRepo

    repo = FieldRepo()
    repo.add("listAccounts", "/items/id", "acct-victim", owner="regular")
    repo.add("createAccount", "/id", "acct-attacker", owner="attacker")
    assert repo.values_owned_by("regular") == {"acct-victim"}
    assert repo.values_owned_by("attacker") == {"acct-attacker"}
    # fork carries ownership so the attacker phase still knows which ids are the victim's
    assert repo.fork().values_owned_by("regular") == {"acct-victim"}


def test_harvest_records_owner():
    from bola.execution.field_repo import FieldRepo, harvest

    rels = [{"source_key": "listAccounts", "target_key": "getAccount",
             "edge": {"source": {"status_code": "200", "media_type": "application/json",
                                 "json_pointer": "/properties/id"},
                      "target": {"type": "parameter", "name": "id", "location": "path"}}}]
    repo = FieldRepo()
    harvest(repo, "listAccounts", 200, "application/json", {"id": "v1"}, rels, owner="regular")
    harvest(repo, "listAccounts", 200, "application/json", {"id": "a1"}, rels, owner="attacker")
    assert repo.values_owned_by("regular") == {"v1"}
    assert repo.values_owned_by("attacker") == {"a1"}


def test_created_tier_separates_created_from_merely_listed():
    from bola.execution.field_repo import FieldRepo

    repo = FieldRepo()
    repo.add("listDocs", "/items/id", "doc-seen", owner="regular")               # merely listed
    repo.add("createDoc", "/id", "doc-made", owner="regular", created=True)       # provably owned
    assert repo.values_owned_by("regular") == {"doc-seen", "doc-made"}
    assert repo.values_created_by("regular") == {"doc-made"}     # only the POST-origin id
    # fork carries the created tier too
    assert repo.fork().values_created_by("regular") == {"doc-made"}


def test_harvest_marks_created_for_post_origin():
    from bola.execution.field_repo import FieldRepo, harvest

    rels = [{"source_key": "createDoc", "target_key": "getDoc",
             "edge": {"source": {"status_code": "201", "media_type": "application/json",
                                 "json_pointer": "/properties/id"},
                      "target": {"type": "parameter", "name": "id", "location": "path"}}}]
    repo = FieldRepo()
    harvest(repo, "createDoc", 201, "application/json", {"id": "d1"}, rels,
            owner="regular", created=True)
    assert repo.values_created_by("regular") == {"d1"}
