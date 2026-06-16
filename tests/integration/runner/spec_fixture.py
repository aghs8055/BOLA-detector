"""The OpenAPI spec the runner explores — mirrors `stub_app` exactly, with rich input constraints.

The `createEntry` body carries the full spread of input types/constraints (enum, pattern, int range,
number, boolean, date/date-time/email/uuid/uri formats, array, nested object) so the run exercises
the AI strategy's field-filling. Response schemas expose ids so real relation detection can find the
data-flow edges (`createEntry.id → getEntry.id`, `createEntry.id → createOrder.entryId`,
`createOrder.id → getOrder.id`, `createRecord.id → getRecord.id`).
"""

from __future__ import annotations

from bola.spec import load_spec_from_dict

_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "format": "int64"},
        "title": {"type": "string", "minLength": 1},
        "code": {"type": "string", "pattern": "^[A-Z]{3}-\\d{4}$", "description": "three caps, dash, four digits"},
        "category": {"type": "string", "enum": ["alpha", "beta", "gamma"]},
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "amount": {"type": "number", "minimum": 0},
        "active": {"type": "boolean"},
        "dueDate": {"type": "string", "format": "date"},
        "scheduledAt": {"type": "string", "format": "date-time"},
        "contact": {"type": "string", "format": "email"},
        "externalRef": {"type": "string", "format": "uuid"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "meta": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "enum": ["na", "eu", "apac"]},
                "weight": {"type": "number"},
            },
        },
        "link": {"type": "string", "format": "uri"},
    },
}

_ENTRY_CREATE = {
    "type": "object",
    "required": ["title", "code", "category", "priority"],
    "properties": {k: v for k, v in _ENTRY_SCHEMA["properties"].items() if k != "id"},
}

_ORDER_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "integer", "format": "int64"}, "entryId": {"type": "integer", "format": "int64"}}}

_RECORD_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "integer", "format": "int64"}, "label": {"type": "string"}}}


def _json(schema):
    return {"content": {"application/json": {"schema": schema}}}


def _ref(name):
    return {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}


def _id_param():
    return [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer", "format": "int64"}}]


def build_spec():
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "workspace", "version": "1.0.0"},
        "paths": {
            "/entries": {
                "post": {"operationId": "createEntry", "summary": "Create an entry",
                         "requestBody": _json(_ENTRY_CREATE),
                         "responses": {"201": {"description": "created", **_ref("Entry")}}},
                "get": {"operationId": "listEntries", "summary": "List my entries",
                        "parameters": [
                            {"name": "category", "in": "query", "required": False,
                             "schema": {"type": "string", "enum": ["alpha", "beta", "gamma"]}},
                            {"name": "limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                        ],
                        "responses": {"200": {"description": "ok",
                                              **_json({"type": "array", "items": {"$ref": "#/components/schemas/Entry"}})}}},
            },
            "/entries/{id}": {
                "parameters": _id_param(),
                "get": {"operationId": "getEntry", "summary": "Read an entry",
                        "responses": {"200": {"description": "ok", **_ref("Entry")}}},
                "put": {"operationId": "updateEntry", "summary": "Update an entry",
                        "requestBody": _json(_ENTRY_CREATE),
                        "responses": {"200": {"description": "ok", **_ref("Entry")}}},
                "delete": {"operationId": "deleteEntry", "summary": "Delete an entry",
                           "responses": {"204": {"description": "deleted"}}},
            },
            "/entries/{id}/status": {
                "parameters": _id_param(),
                "put": {"operationId": "setEntryStatus", "summary": "Set entry status",
                        "requestBody": _json({"type": "object", "required": ["status"], "properties": {
                            "status": {"type": "string", "enum": ["open", "closed", "pending"]}}}),
                        "responses": {"200": {"description": "ok",
                                              **_json({"type": "object", "properties": {"result": {"type": "string"}}})}}},
            },
            "/entries/{id}/flag": {
                "parameters": _id_param(),
                "put": {"operationId": "flagEntry", "summary": "Flag an entry",
                        "requestBody": _json({"type": "object", "properties": {"reason": {"type": "string"}}}),
                        "responses": {"200": {"description": "ok",
                                              **_json({"type": "object", "properties": {"result": {"type": "string"}}})}}},
            },
            "/entries/{id}/summary": {
                "parameters": _id_param(),
                "get": {"operationId": "getEntrySummary", "summary": "Read an entry summary",
                        "responses": {"200": {"description": "ok",
                                              **_json({"type": "object", "properties": {
                                                  "title": {"type": "string"}, "amount": {"type": "number"}}})}}},
            },
            "/orders": {
                "post": {"operationId": "createOrder", "summary": "Create an order",
                         "requestBody": _json({"type": "object", "required": ["entryId"], "properties": {
                             "entryId": {"type": "integer", "format": "int64"}}}),
                         "responses": {"201": {"description": "created", **_ref("Order")}}},
            },
            "/orders/{id}": {
                "parameters": _id_param(),
                "get": {"operationId": "getOrder", "summary": "Read an order",
                        "responses": {"200": {"description": "ok", **_ref("Order")}}},
            },
            "/records": {
                "post": {"operationId": "createRecord", "summary": "Create a record",
                         "requestBody": _json({"type": "object", "properties": {"label": {"type": "string"}}}),
                         "responses": {"201": {"description": "created", **_ref("Record")}}},
            },
            "/records/{id}": {
                "parameters": _id_param(),
                "get": {"operationId": "getRecord", "summary": "Read a record",
                        "responses": {"200": {"description": "ok", **_ref("Record")}}},
                "put": {"operationId": "updateRecord", "summary": "Update a record",
                        "requestBody": _json({"type": "object", "properties": {"label": {"type": "string"}}}),
                        "responses": {"200": {"description": "ok", **_ref("Record")}}},
            },
        },
        "components": {"schemas": {"Entry": _ENTRY_SCHEMA, "Order": _ORDER_SCHEMA, "Record": _RECORD_SCHEMA}},
    }
    return load_spec_from_dict(doc, source="<workspace>")


def build_manifest_dict(base_url: str) -> dict:
    return {
        "version": 1,
        "base_url": base_url,
        "spec": {"kind": "file", "value": "spec.json"},
        "auth": {
            "login": {"method": "POST", "path": "/login",
                      "headers": {"Content-Type": "application/json"},
                      "body": '{"username": "{{username}}", "password": "{{password}}"}'},
            "extract": {"from": "body", "path": "token"},
            "inject": {"into": "header", "name": "Authorization", "value": "Bearer {{credential}}"},
        },
        "users": {
            "regular": {"vars": {"username": "alice", "password": "pw-alice-123"}},
            "attacker": {"vars": {"username": "bob", "password": "pw-bob-456"}},
        },
        "access": ("Every entry, order, and record belongs to the user who created it. A user may "
                   "only read or modify their own objects; accessing another user's object is "
                   "unauthorized."),
    }