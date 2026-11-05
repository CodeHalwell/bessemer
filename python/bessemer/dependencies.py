"""Execution of dependency graphs compiled during application startup."""

from __future__ import annotations

from typing import Any


class DependencyResolver:
    """Resolve a compiled graph with independent request and application caches."""

    def __init__(
        self,
        route,
        arguments,
        *,
        state,
        request,
        background,
        principal=None,
        invoke,
        request_stack,
        app_stack,
        app_cache,
    ) -> None:
        self.route = route
        self.arguments = arguments
        self.state = state
        self.request = request
        self.background = background
        self.principal = principal
        self.websocket = None
        self.invoke = invoke
        self.request_stack = request_stack
        self.app_stack = app_stack
        self.app_cache = app_cache
        self.cache: dict[int, Any] = {}
        self.resolving: set[int] = set()

    async def binding(self, binding):
        if binding.kind == "input":
            return self.arguments[binding.key]
        if binding.kind == "state":
            return self.state
        if binding.kind == "request":
            return self.request
        if binding.kind == "principal":
            if self.principal is None:
                from .application import HTTPException

                # Only reachable on a jwt exclude_paths route.
                raise HTTPException(401, "Not authenticated")
            return self.principal
        if binding.kind == "websocket":
            return self.websocket
        if binding.kind == "background_tasks":
            return self.background
        if binding.kind == "security_scopes":
            from .security import SecurityScopes

            return SecurityScopes(list(binding.scopes))
        if binding.kind == "dependency":
            return await self.resolve(binding.dependency, use_cache=binding.use_cache)
        raise RuntimeError("unknown compiled argument binding")

    async def arguments_for(self, plan) -> dict[str, Any]:
        return {binding.name: await self.binding(binding) for binding in plan.bindings}

    async def resolve(self, dependency_id: int, *, use_cache: bool = True):
        plan = self.route.dependencies[dependency_id]
        application = plan.scope == "app"
        cache = self.app_cache if application else self.cache
        key = id(plan.call) if application else dependency_id
        if use_cache and key in cache:
            return cache[key]
        if dependency_id in self.resolving:
            raise RuntimeError("compiled dependency graph contains a cycle")
        self.resolving.add(dependency_id)
        try:
            arguments = await self.arguments_for(plan)
            stack = self.app_stack if application else self.request_stack
            value = await self.invoke(plan, arguments, stack)
            if use_cache:
                cache[key] = value
            return value
        finally:
            self.resolving.remove(dependency_id)
