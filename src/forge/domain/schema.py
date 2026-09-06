"""Shared validation and serialization rules for Forge domain contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypeAliasType

JsonScalar = str | int | float | bool | None
JsonValue = TypeAliasType("JsonValue", JsonScalar | list["JsonValue"] | dict[str, "JsonValue"])


class DomainModel(BaseModel):
    """Strict, immutable and JSON-safe base for durable domain messages."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VersionedDomainModel(DomainModel):
    """Base for the first version of Forge-owned runtime contracts."""

    schema_version: Literal["1.0"] = "1.0"
