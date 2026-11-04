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
    info = {"title": app.title, "version": app.version}
    if app.description is not None:
        info["description"] = app.description
    document: dict[str, Any] = {"openapi": "3.1.0", "info": info, "paths": {}}
    schemas: dict[str, Any] = {}
    operation_ids = set()
    for route, plan in zip(routes, compiled, strict=True):
        if not route.include_in_schema:
            continue
        handler_name = getattr(route.handler, "__name__", route.kind)
        operation_id = route.operation_id or re.sub(
            r"[^a-zA-Z0-9_]", "_", f"{handler_name}_{route.method.lower()}_{route.path}"
        ).strip("_")
        if operation_id in operation_ids:
            raise ValueError(f"duplicate OpenAPI operation_id: {operation_id}")
        operation_ids.add(operation_id)
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "responses": {
                str(route.status_code): {"description": "Successful Response"}
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
        for parameter in plan.declaration["parameters"]:
            schema = _register_schema(parameter.get("schema", {}), schemas)
            if parameter["source"] == "body":
                operation["requestBody"] = {
                    "required": parameter["required"],
                    "content": {"application/json": {"schema": schema}},
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
            identity = (public["in"], public["name"])
            if identity in seen_parameters:
                if seen_parameters[identity] != public:
                    raise ValueError(
                        f"{route.path}: conflicting declarations for {identity[0]} parameter {identity[1]!r}"
                    )
                continue
            seen_parameters[identity] = public
            parameters.append(public)
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
            if isinstance(model, type) and issubclass(model, Response):
                media_type = model.media_type
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
        api_key = settings.get("api_key")
        if api_key is not None:
            operation["security"] = (
                []
                if route.path in api_key.get("exclude_paths", ())
                else [{"APIKey": []}]
            )
        document["paths"].setdefault(route.path, {})[route.method.lower()] = operation
    components: dict[str, Any] = {}
    if schemas:
        components["schemas"] = schemas
    if settings.get("api_key") is not None:
        components["securitySchemes"] = {
            "APIKey": {
                "type": "apiKey",
                "in": "header",
                "name": settings["api_key"].get("header", "X-API-Key"),
            }
        }
    if components:
        document["components"] = components
    return document


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
