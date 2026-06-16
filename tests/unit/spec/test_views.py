"""Unit tests for `bola.spec.views`."""

from bola.spec import views


# -- operations ------------------------------------------------------------------


def test_operations_keyed_by_operation_id(petstore_spec):
    keys = {o.key for o in views.operations(petstore_spec)}
    assert "addPet" in keys
    assert "getPetById" in keys


def test_operation_carries_method_and_path(petstore_spec):
    get = _op(petstore_spec, "getPetById")
    assert get.method == "GET"
    assert get.path == "/pet/{petId}"


def test_operation_key_falls_back_to_method_path():
    from bola.spec import load_spec_from_dict

    doc = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    spec = load_spec_from_dict(doc)
    assert views.operations(spec)[0].key == "GET /ping"


def test_duplicate_operation_id_disambiguated_to_method_path():
    """A reused operationId must not collapse distinct operations to one key (resume relies on it)."""
    from bola.spec import load_spec_from_dict

    resp = {"responses": {"200": {"description": "ok"}}}
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/projects": {"get": {"operationId": "getAll", **resp}},
            "/landing/project": {"get": {"operationId": "getAll", **resp}},
            "/health": {"get": {"operationId": "health", **resp}},
        },
    }
    spec = load_spec_from_dict(doc)
    keys = [o.key for o in views.operations(spec)]
    assert len(keys) == len(set(keys))  # every operation has a distinct key
    assert "health" in keys  # the unique operationId is preserved
    assert "getAll" not in keys  # the collided one falls back...
    assert {"GET /projects", "GET /landing/project"} <= set(keys)  # ...to METHOD /path for both


# -- leaf fields / json pointers -------------------------------------------------


def test_leaf_fields_build_property_pointers(recursive_spec):
    create = _op(recursive_spec, "createCategory")
    resp = _json_response(recursive_spec, create, "201")
    pointers = {f.json_pointer for f in resp.fields}
    assert "/properties/id" in pointers
    assert "/properties/name" in pointers


def test_leaf_fields_resolve_refs_with_type_and_format(recursive_spec):
    create = _op(recursive_spec, "createCategory")
    resp = _json_response(recursive_spec, create, "201")
    id_field = next(f for f in resp.fields if f.json_pointer == "/properties/id")
    assert id_field.type == "integer"
    assert id_field.format == "int64"


def test_recursive_ref_is_cut_after_one_level(recursive_spec):
    create = _op(recursive_spec, "createCategory")
    resp = _json_response(recursive_spec, create, "201")
    pointers = {f.json_pointer for f in resp.fields}
    # one level past the cut is emitted...
    assert "/properties/subcategories/items/properties/id" in pointers
    # ...but the recursion does not unroll further
    assert not any("subcategories/items/properties/subcategories" in p for p in pointers)


def test_additional_properties_pointer(petstore_spec):
    inv = _op(petstore_spec, "getInventory")
    resp = _json_response(petstore_spec, inv, "200")
    assert any(f.json_pointer == "/additionalProperties" for f in resp.fields)


def test_leaf_fields_of_none_is_empty(recursive_spec):
    assert views.leaf_fields(recursive_spec, None) == []


# -- parameters / request bodies -------------------------------------------------


def test_parameters_resolved_with_location_and_type(recursive_spec):
    get = _op(recursive_spec, "getCategory")
    params = views.parameters(recursive_spec, get)
    assert len(params) == 1
    p = params[0]
    assert (p.name, p.location, p.type, p.required) == ("categoryId", "path", "integer", True)


def test_request_body_fields(recursive_spec):
    create = _op(recursive_spec, "createCategory")
    bodies = views.request_bodies(recursive_spec, create)
    assert bodies[0].media_type == "application/json"
    assert {f.json_pointer for f in bodies[0].fields} == {"/properties/name"}


# -- constraints -----------------------------------------------------------------


def _constraints_spec():
    from bola.spec import load_spec_from_dict

    return load_spec_from_dict(
        {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/items": {
                    "post": {
                        "operationId": "createItem",
                        "parameters": [
                            {
                                "name": "status",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string", "enum": ["active", "archived"]},
                            }
                        ],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "code": {
                                                "type": "string",
                                                "pattern": "^[A-Z]{3}$",
                                                "minLength": 3,
                                                "maxLength": 3,
                                                "description": "three uppercase letters",
                                            },
                                            "qty": {"type": "integer", "minimum": 1, "maximum": 99},
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"201": {"description": "ok"}},
                    }
                }
            },
        }
    )


def test_parameter_carries_enum_constraint():
    spec = _constraints_spec()
    p = next(p for p in views.parameters(spec, _op(spec, "createItem")) if p.name == "status")
    assert p.constraints == {"enum": ["active", "archived"]}


def test_body_field_carries_value_constraints():
    spec = _constraints_spec()
    fields = views.request_bodies(spec, _op(spec, "createItem"))[0].fields
    code = next(f for f in fields if f.json_pointer == "/properties/code")
    assert code.constraints == {
        "pattern": "^[A-Z]{3}$",
        "minLength": 3,
        "maxLength": 3,
        "description": "three uppercase letters",
    }
    qty = next(f for f in fields if f.json_pointer == "/properties/qty")
    assert qty.constraints == {"minimum": 1, "maximum": 99}


def test_unconstrained_field_has_empty_constraints():
    spec = _constraints_spec()
    fields = views.request_bodies(spec, _op(spec, "createItem"))[0].fields
    name = next(f for f in fields if f.json_pointer == "/properties/name")
    assert name.constraints == {}


# -- spec hash -------------------------------------------------------------------


def test_spec_hash_is_stable(recursive_spec):
    assert views.spec_hash(recursive_spec) == views.spec_hash(recursive_spec)


def test_spec_hash_differs_by_content(recursive_spec, petstore_spec):
    assert views.spec_hash(recursive_spec) != views.spec_hash(petstore_spec)


# -- data_values (schema pointer -> real JSON) -----------------------------------


def test_data_values_drops_properties():
    assert views.data_values({"id": 7, "name": "x"}, "/properties/id") == [7]


def test_data_values_root_pointer_returns_whole_node():
    assert views.data_values({"id": 7}, "/") == [{"id": 7}]


def test_data_values_fans_out_array_items():
    data = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert views.data_values(data, "/items/properties/id") == [1, 2, 3]


def test_data_values_fans_out_additional_properties():
    data = {"a": {"id": 1}, "b": {"id": 2}}
    assert sorted(views.data_values(data, "/additionalProperties/properties/id")) == [1, 2]


def test_data_values_missing_key_yields_nothing():
    assert views.data_values({"name": "x"}, "/properties/id") == []


def test_data_values_type_mismatch_is_skipped():
    # pointer expects an array but data is a dict
    assert views.data_values({"id": 1}, "/items/properties/id") == []


def test_data_values_recurses_to_full_depth():
    data = {
        "id": 1,
        "subcategories": [
            {"id": 2, "subcategories": [{"id": 4, "subcategories": []}]},
            {"id": 3, "subcategories": []},
        ],
    }
    # one-level schema pointer, but every descendant id is harvested (not the root id=1)
    got = views.data_values(data, "/properties/subcategories/items/properties/id")
    assert sorted(got) == [2, 3, 4]


# -- helpers ---------------------------------------------------------------------


def _op(spec, key):
    return next(o for o in views.operations(spec) if o.key == key)


def _json_response(spec, opref, status):
    return next(
        r
        for r in views.responses(spec, opref)
        if r.status_code == status and r.media_type == "application/json"
    )