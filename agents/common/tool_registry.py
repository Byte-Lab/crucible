# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

import inspect
from typing import Any, Callable, get_type_hints

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json_schema(annotation: Any) -> dict[str, Any]:
    origin = getattr(annotation, "__origin__", None)
    if origin is type(int | str):  # types.UnionType
        args = annotation.__args__
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_json_schema(non_none[0])
    if origin is list:
        inner_args = getattr(annotation, "__args__", None)
        schema: dict[str, Any] = {"type": "array"}
        if inner_args:
            schema["items"] = _python_type_to_json_schema(inner_args[0])
        return schema
    if origin is dict:
        return {"type": "object"}
    if annotation in _TYPE_MAP:
        return {"type": _TYPE_MAP[annotation]}
    return {"type": "string"}


class ToolRegistry:
    def __init__(self) -> None:
        self._functions: dict[str, Callable] = {}
        self._schemas: list[dict[str, Any]] = []

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._schemas

    def tool(self, description: str) -> Callable:
        def decorator(func: Callable) -> Callable:
            hints = get_type_hints(func)
            sig = inspect.signature(func)
            properties: dict[str, Any] = {}
            required: list[str] = []
            for name, param in sig.parameters.items():
                if name == "self":
                    continue
                annotation = hints.get(name, str)
                properties[name] = _python_type_to_json_schema(annotation)
                if param.default is inspect.Parameter.empty:
                    required.append(name)
            schema: dict[str, Any] = {
                "name": func.__name__,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
            self._functions[func.__name__] = func
            self._schemas.append(schema)
            return func
        return decorator

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._functions:
            raise KeyError(f"unknown tool: {name}")
        return self._functions[name](**arguments)
