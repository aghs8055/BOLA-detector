"""Shared fixtures: a real petstore spec, a crafted recursive spec, and default settings."""

from pathlib import Path

import pytest

from bola.settings import Settings
from bola.spec import load_spec, load_spec_from_dict

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def petstore_spec():
    return load_spec(str(_FIXTURES / "petstore_3.0.json"))


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def recursive_spec():
    """A tiny spec with a self-referential Category schema + create/read operations.

    create (POST /categories) returns a Category whose `id` should flow into read
    (GET /categories/{categoryId}); `subcategories` is recursive via `$ref`.
    """
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "cats", "version": "1.0.0"},
        "paths": {
            "/categories": {
                "post": {
                    "operationId": "createCategory",
                    "summary": "Create a category",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Category"}
                                }
                            },
                        }
                    },
                }
            },
            "/categories/{categoryId}": {
                "get": {
                    "operationId": "getCategory",
                    "summary": "Read a category",
                    "parameters": [
                        {
                            "name": "categoryId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "format": "int64"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Category"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "Category": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "name": {"type": "string"},
                        "subcategories": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Category"},
                        },
                    },
                }
            }
        },
    }
    return load_spec_from_dict(doc, source="<recursive>")