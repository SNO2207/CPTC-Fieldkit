"""Explicit module registration keeps startup deterministic and easy to audit."""

from fieldkit.core.module import Module
from fieldkit.modules.web_artifacts import WebArtifacts


def modules() -> dict[str, Module]:
    builtins: tuple[Module, ...] = (WebArtifacts(),)
    return {module.name: module for module in builtins}
