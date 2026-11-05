"""Startup-built OpenAPI 3.1 documents and a self-contained API reference."""

from __future__ import annotations

import copy
import html
import inspect
import json
import re
from typing import Any

from .responses import Response


def _register_schema(
    schema: dict[str, Any], components: dict[str, Any], *, suffix: str = ""
) -> dict[str, Any]:
    schema_maps = {"properties", "patternProperties", "dependentSchemas"}
    schema_values = {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "additionalItems",
        "unevaluatedItems",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
    schema_arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}

    def register(value):
        if not isinstance(value, dict):
            return value
        # Nullable wrappers and container schemas can nest Pydantic $defs.
        # All generated refs use the document-wide components namespace.
        definitions = value.get("$defs", {})
        for name, definition in definitions.items():
            name += suffix
            normalized = register(definition)
            if name in components and components[name] != normalized:
                raise ValueError(
                    f"OpenAPI model name {name!r} has conflicting definitions; use distinct model names"
                )
            components[name] = normalized
        result = {}
        for key, item in value.items():
            if key == "$defs":
                continue
            if key in schema_maps and isinstance(item, dict):
                result[key] = {name: register(child) for name, child in item.items()}
            elif key in schema_values:
                result[key] = register(item)
            elif key in schema_arrays and isinstance(item, list):
                result[key] = [register(child) for child in item]
            else:
                # Defaults/examples and property names are application data;
                # "$defs" and "$ref" inside them have no schema semantics.
                result[key] = item
        return result

    return register(copy.deepcopy(schema))


def build_document(app, routes, compiled, settings: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {"title": app.title}
    if getattr(app, "summary", None) is not None:
        info["summary"] = app.summary
    if app.description is not None:
        info["description"] = app.description
    info["version"] = app.version
    for key, attribute in (
        ("termsOfService", "terms_of_service"),
        ("contact", "contact"),
        ("license", "license_info"),
    ):
        value = getattr(app, attribute, None)
        if value is not None:
            info[key] = copy.deepcopy(value)
    document: dict[str, Any] = {"openapi": "3.1.0", "info": info}
    if getattr(app, "servers", None):
        document["servers"] = copy.deepcopy(app.servers)
    document["paths"] = {}
    schemas: dict[str, Any] = {}
    schemes: dict[str, Any] = {}
    operation_ids = set()
    for route, plan in zip(routes, compiled, strict=True):
        if not route.include_in_schema:
            continue
        handler_name = getattr(route.handler, "__name__", route.kind)
        unique_id = getattr(route, "generate_unique_id_function", None)
        if route.operation_id is None and unique_id is not None:
            from .routing import APIRoute

            operation_id = unique_id(APIRoute._of(route))
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(
                    "generate_unique_id_function must return a nonempty string"
                )
        else:
            operation_id = route.operation_id or re.sub(
                r"[^a-zA-Z0-9_]",
                "_",
                f"{handler_name}_{route.method.lower()}_{route.path}",
            ).strip("_")
        if operation_id in operation_ids:
            raise ValueError(f"duplicate OpenAPI operation_id: {operation_id}")
        operation_ids.add(operation_id)
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "responses": {
                str(route.status_code): {
                    "description": getattr(
                        route, "response_description", "Successful Response"
                    )
                }
            },
        }
        if route.tags:
            operation["tags"] = list(route.tags)
        if route.summary is not None:
            operation["summary"] = route.summary
        description = route.description or (
            inspect.getdoc(route.handler) if route.handler is not None else None
        )
        if description:
            operation["description"] = description
        parameters = []
        seen_parameters = {}
        form: dict[str, Any] = {"type": "object", "properties": {}}
        form_required: list[str] = []
        multipart = False
        for parameter in plan.declaration["parameters"]:
            metadata = parameter.get("openapi", {})
            if not metadata.get("include_in_schema", True):
                continue
            schema = _register_schema(parameter.get("schema", {}), schemas)
            _schema_metadata(schema, metadata)
            if parameter["source"] in ("form", "file"):
                alias = parameter.get("alias", parameter["name"])
                field = dict(schema)
                if not parameter["required"] and parameter["source"] == "form":
                    field["default"] = parameter["default"]
                if parameter.get("description"):
                    field["description"] = parameter["description"]
                form["properties"][alias] = field
                # Bindings in a dependency and its handler may share an alias.
                if parameter["required"] and alias not in form_required:
                    form_required.append(alias)
                multipart |= parameter["source"] == "file"
                continue
            if parameter["source"] == "body":
                content: dict[str, Any] = {"schema": schema}
                if "openapi_examples" in metadata:
                    content["examples"] = copy.deepcopy(metadata["openapi_examples"])
                operation["requestBody"] = {
                    "required": parameter["required"],
                    "content": {
                        metadata.get("media_type", "application/json"): content
                    },
                }
                if parameter.get("description"):
                    operation["requestBody"]["description"] = parameter["description"]
                continue
            public = {
                "name": parameter.get("alias", parameter["name"]),
                "in": parameter["source"],
                "required": parameter["required"],
                "schema": schema,
            }
            if not parameter["required"]:
                public["schema"]["default"] = parameter["default"]
            for name in ("description", "deprecated"):
                if name in parameter:
                    public[name] = parameter[name]
            if "openapi_examples" in metadata:
                public["examples"] = copy.deepcopy(metadata["openapi_examples"])
            identity = (public["in"], public["name"])
            if identity in seen_parameters:
                if seen_parameters[identity] != public:
                    raise ValueError(
                        f"{route.path}: conflicting declarations for {identity[0]} parameter {identity[1]!r}"
                    )
                continue
            seen_parameters[identity] = public
            parameters.append(public)
        if form["properties"]:
            if form_required:
                form["required"] = form_required
            media = (
                "multipart/form-data"
                if multipart
                else "application/x-www-form-urlencoded"
            )
            operation["requestBody"] = {
                "required": bool(form_required),
                "content": {media: {"schema": form}},
            }
        if parameters:
            operation["parameters"] = parameters
        if route.status_code not in (204, 205, 304) and route.method != "HEAD":
            response_schema = (
                plan.response_adapter.json_schema(
                    ref_template="#/components/schemas/{model}-Output",
                    mode="serialization",
                )
                if plan.response_adapter is not None
                else {}
            )
            media_type = "application/json"
            model = plan.response_model
            response_class = getattr(route, "response_class", None)
            if isinstance(model, type) and issubclass(model, Response):
                response_class = model
            if response_class is not None:
                media_type = response_class.media_type
                if media_type is not None and media_type.startswith("text/"):
                    response_schema = {"type": "string"}
            if media_type is not None:
                operation["responses"][str(route.status_code)]["content"] = {
                    media_type: {
                        "schema": _register_schema(
                            response_schema, schemas, suffix="-Output"
                        )
                    }
                }
        if plan.declaration["parameters"]:
            operation["responses"].setdefault(
                "422",
                {
                    "description": "Validation Error",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "detail": {
                                        "type": "array",
                                        "items": {"type": "object"},
                                    }
                                },
                                "required": ["detail"],
                            }
                        }
                    },
                },
            )
        security: dict[str, list] = {}
        api_key = settings.get("api_key")
        if api_key is not None and route.path not in api_key.get("exclude_paths", ()):
            security["APIKey"] = []
        jwt = settings.get("jwt")
        if jwt is not None and route.path not in jwt.get("exclude_paths", ()):
            security["BearerJWT"] = []
        for scheme, scopes in plan.security:
            name = scheme.scheme_name
            if (name == "APIKey" and api_key is not None) or (
                name == "BearerJWT" and jwt is not None
            ):
                raise ValueError(
                    f"security scheme name {name!r} is reserved for App settings"
                )
            if name in schemes and schemes[name] != scheme.model:
                raise ValueError(
                    f"security scheme name {name!r} has conflicting definitions;"
                    " pass distinct scheme_name values"
                )
            schemes[name] = copy.deepcopy(scheme.model)
            # Only OAuth2 and OpenID Connect requirements carry scopes.
            oauth = scheme.model.get("type") in {"oauth2", "openIdConnect"}
            required = security.get(name, []) + list(scopes) if oauth else []
            security[name] = list(dict.fromkeys(required))
        if api_key is not None or jwt is not None or plan.security:
            # Every configured scheme and dependency is required together.
            operation["security"] = [security] if security else []
        if getattr(route, "deprecated", False):
            operation["deprecated"] = True
        for status, response in (getattr(route, "responses", None) or {}).items():
            key = str(status)
            existing = operation["responses"].get(key, {})
            merged = {**existing, **copy.deepcopy(response)}
            merged.pop("model", None)
            model = response.get("model")
            if model is not None:
                merged.setdefault("content", {}).setdefault("application/json", {})[
                    "schema"
                ] = _model_schema(model, schemas)
            merged.setdefault(
                "description",
                _status_phrase(key),
            )
            operation["responses"][key] = merged
        if getattr(route, "openapi_extra", None):
            _deep_update(operation, copy.deepcopy(route.openapi_extra))
        document["paths"].setdefault(route.path, {})[route.method.lower()] = operation
    components: dict[str, Any] = {}
    if schemas:
        components["schemas"] = schemas
    if settings.get("api_key") is not None:
        schemes["APIKey"] = {
            "type": "apiKey",
            "in": "header",
            "name": settings["api_key"].get("header", "X-API-Key"),
        }
    if settings.get("jwt") is not None:
        schemes["BearerJWT"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    if schemes:
        components["securitySchemes"] = schemes
    if components:
        document["components"] = components
    if getattr(app, "openapi_tags", None):
        document["tags"] = copy.deepcopy(app.openapi_tags)
    if getattr(app, "openapi_external_docs", None):
        document["externalDocs"] = copy.deepcopy(app.openapi_external_docs)
    return document


def _schema_metadata(schema: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Apply a marker's JSON Schema metadata (title, examples, extras)."""
    if "title" in metadata:
        schema["title"] = metadata["title"]
    if "examples" in metadata:
        schema["examples"] = copy.deepcopy(metadata["examples"])
    if "json_schema_extra" in metadata:
        schema.update(copy.deepcopy(metadata["json_schema_extra"]))


def _model_schema(model: Any, schemas: dict[str, Any]) -> dict[str, Any]:
    """The schema of a ``responses={...: {"model": Model}}`` entry."""
    from .schema import response_adapter

    adapter = response_adapter(model)
    if adapter is None or not hasattr(adapter, "json_schema"):
        raise ValueError(
            "responses models need the optional Pydantic dependency or a JSON type"
        )
    schema = adapter.json_schema(
        ref_template="#/components/schemas/{model}-Output", mode="serialization"
    )
    return _register_schema(schema, schemas, suffix="-Output")


def _status_phrase(status: str) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(int(status)).phrase
    except ValueError:
        return "Additional Response"


def _deep_update(target: dict[str, Any], extra: dict[str, Any]) -> None:
    """Merge ``openapi_extra`` into an operation, as FastAPI does."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def encode_document(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def documentation_page(title: str, openapi_url: str) -> bytes:
    source = json.dumps(openapi_url).replace("<", "\\u003c")
    page = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__ — API reference</title>
<style>body{font:16px system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;color:#192331}h1{font-size:2rem}article{border:1px solid #dbe1e8;border-radius:8px;margin:16px 0;padding:16px}code,pre{font-family:ui-monospace,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:12px;border-radius:6px}.method{font-weight:bold;color:#165d9c}a{color:#165d9c}small{color:#536176}button{padding:8px 12px;border:1px solid #cbd3dc;border-radius:4px;background:white;cursor:pointer}</style>
<h1>__TITLE__</h1><p>API reference generated from the application’s OpenAPI 3.1 document. All assets on this page are bundled; no external scripts or styles are loaded.</p><p><a id="schema">Download OpenAPI JSON</a></p><main id="operations"><p>Loading the schema…</p></main>
<script>const url=__SOURCE__;document.querySelector('#schema').href=url;const root=document.querySelector('#operations');function node(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.append(n);return n;}fetch(url,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('Schema request returned HTTP '+r.status);return r.json();}).then(doc=>{root.replaceChildren();for(const[path,methods]of Object.entries(doc.paths)){for(const[method,operation]of Object.entries(methods)){const section=node('article',undefined,root);const heading=node('h2',undefined,section);node('span',method.toUpperCase()+' ',heading).className='method';node('code',path,heading);if(operation.summary)node('p',operation.summary,section);if(operation.description)node('p',operation.description,section);if(operation.parameters?.length){node('h3','Parameters',section);for(const p of operation.parameters)node('p',p.name+' ('+p.in+(p.required?', required':'')+') — '+(p.description||JSON.stringify(p.schema)),section);}if(operation.requestBody){node('h3','Request body',section);node('pre',JSON.stringify(operation.requestBody,null,2),section);}const details=node('details',undefined,section);node('summary','Response schemas',details);node('pre',JSON.stringify(operation.responses,null,2),details);}}}).catch(error=>{root.replaceChildren();node('p',error.message,root);});</script></html>"""
    replacements = {"__TITLE__": html.escape(title), "__SOURCE__": source}
    return re.sub(
        r"__TITLE__|__SOURCE__", lambda match: replacements[match[0]], page
    ).encode("utf-8")
