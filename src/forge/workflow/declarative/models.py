"""Strict public models for Forge's declarative workflow format."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORKFLOW_PROPERTY_PREFIX = "forge.workflow."
WORKFLOW_LABEL_PREFIX = "forge:workflow:"
MAX_PROPERTY_BYTES = 32_768
MAX_STEPS = 64
MAX_BRANCHES = 32
MAX_TRANSITIONS = 500
WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowMetadata(StrictModel):
    name: str
    revision: int = Field(ge=1)
    description: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not WORKFLOW_NAME_RE.fullmatch(value):
            raise ValueError("must match [a-z][a-z0-9_-]{0,62}")
        return value


class WorkflowStep(StrictModel):
    next: str | None = None
    route: str | None = None
    branches: dict[str, str] = Field(default_factory=dict)
    dynamic_route: bool = Field(default=False, alias="dynamicRoute")
    # Legacy router capability metadata. New definitions omit it; the trusted
    # router catalog owns the possible destinations.
    dynamic_targets: tuple[str, ...] = Field(
        default=(), alias="dynamicTargets", exclude_if=lambda value: not value
    )
    # Legacy catalog metadata remains readable for pinned definitions. New
    # definitions omit it and the compiler derives it from the state profile.
    kind: Literal["station", "gate", "operation"] | None = None
    station_contract: str | None = Field(default=None, alias="stationContract")
    station_contract_version: str | None = Field(default=None, alias="stationContractVersion")
    required_policies: tuple[str, ...] = Field(
        default=(), alias="requiredPolicies", exclude_if=lambda value: not value
    )
    # Legacy effect metadata. Authority belongs to the trusted node catalog;
    # this remains readable so old pinned definitions preserve their identity.
    allowed_effects: tuple[str, ...] | None = Field(default=None, alias="allowedEffects")
    join: Literal["all", "any"] | None = None
    max_concurrency: int | None = Field(default=None, alias="maxConcurrency", ge=1, le=64)
    retry_bound: int | None = Field(default=None, alias="retryBound", ge=1, le=100)
    external_entry: bool = Field(
        default=False,
        alias="externalEntry",
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_transition(self) -> WorkflowStep:
        if bool(self.next) == bool(self.route):
            raise ValueError("exactly one of 'next' or 'route' is required")
        if self.next and self.branches:
            raise ValueError("branches are only valid with 'route'")
        if self.route and not self.branches and not self.dynamic_route:
            raise ValueError("a routed step requires non-empty branches")
        if self.dynamic_route and (not self.route or self.branches):
            raise ValueError("dynamicRoute requires a route and cannot declare static branches")
        if not self.dynamic_route and self.dynamic_targets:
            raise ValueError("dynamicTargets are only valid with dynamicRoute")
        if len(self.branches) > MAX_BRANCHES:
            raise ValueError(f"a routed step may have at most {MAX_BRANCHES} branches")
        if self.kind == "station" and not (self.station_contract and self.station_contract_version):
            raise ValueError("station steps require stationContract and stationContractVersion")
        if self.kind not in {None, "station", "gate"} and (
            self.station_contract or self.station_contract_version
        ):
            raise ValueError("station contract fields are only valid for station steps")
        if bool(self.station_contract) != bool(self.station_contract_version):
            raise ValueError("stationContract and stationContractVersion must be declared together")
        if self.max_concurrency is not None and not self.dynamic_route:
            raise ValueError("maxConcurrency is only valid for dynamic routing")
        if self.dynamic_route and self.max_concurrency is None:
            raise ValueError("dynamicRoute requires an explicit maxConcurrency")
        if self.join is not None and self.dynamic_route:
            raise ValueError("a fan-out step cannot also be a join")
        return self


class WorkflowResume(StrictModel):
    from_revisions: dict[int, dict[str, str]] = Field(
        default_factory=dict,
        alias="fromRevisions",
    )


class WorkflowInclude(StrictModel):
    source: Literal["builtin", "project"]
    name: str
    exits: dict[str, str] = Field(default_factory=dict)
    return_to: str | None = Field(default=None, alias="returnTo")
    # Full workflow includes must identify which terminal edges return to the
    # caller. Other __end__ edges may represent waits or blocked termination.
    return_from: tuple[str, ...] = Field(
        default=(), alias="returnFrom", exclude_if=lambda value: not value
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not WORKFLOW_NAME_RE.fullmatch(value):
            raise ValueError("invalid included definition name")
        return value


class WorkflowSpec(StrictModel):
    state: Literal["feature", "bug", "task_takeover"]
    entry: str
    steps: dict[str, WorkflowStep] = Field(default_factory=dict)
    includes: tuple[WorkflowInclude, ...] = Field(default=(), exclude_if=lambda value: not value)
    resolved_dependencies: tuple[str, ...] = Field(
        default=(), alias="resolvedDependencies", exclude_if=lambda value: not value
    )
    # Subworkflows can be checked against more than one trusted state profile.
    compatible_states: tuple[Literal["feature", "bug", "task_takeover"], ...] = Field(
        default=(), alias="compatibleStates", exclude_if=lambda value: not value
    )
    # The provider-neutral policy used to apply external observations to this
    # workflow instance.  Policies are versioned, allowlisted runtime
    # capabilities; arbitrary import paths are deliberately not supported.
    # Definitions which do not accept external observation transitions may
    # leave this unset (for example, small local test workflows).
    # Legacy derived/governance fields. They are accepted so an old pinned
    # artifact keeps its identity, but omitted from newly-authored definitions.
    observation_policy: str | None = Field(default=None, alias="observationPolicy")
    resume: WorkflowResume = Field(default_factory=WorkflowResume)
    mandatory_policies: tuple[str, ...] = Field(
        default=(), alias="mandatoryPolicies", exclude_if=lambda value: not value
    )
    extension_points: tuple[str, ...] = Field(
        default=(), alias="extensionPoints", exclude_if=lambda value: not value
    )

    @field_validator("entry")
    @classmethod
    def validate_entry(cls, value: str) -> str:
        if not NODE_NAME_RE.fullmatch(value):
            raise ValueError("entry must be a canonical Forge node name")
        return value

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: dict[str, WorkflowStep]) -> dict[str, WorkflowStep]:
        if len(value) > MAX_STEPS:
            raise ValueError(f"a workflow may have at most {MAX_STEPS} steps")
        invalid = [name for name in value if not NODE_NAME_RE.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid canonical node name: {invalid[0]}")
        return value

    @model_validator(mode="after")
    def validate_body(self) -> WorkflowSpec:
        if not self.steps and not self.includes:
            raise ValueError("at least one step or include is required")
        return self


class WorkflowDefinition(StrictModel):
    api_version: Literal["forge/v1"] = Field(alias="apiVersion")
    kind: Literal["Workflow", "Subworkflow"]
    metadata: WorkflowMetadata
    spec: WorkflowSpec

    @model_validator(mode="after")
    def validate_kind(self) -> WorkflowDefinition:
        if self.kind == "Workflow" and self.spec.compatible_states:
            raise ValueError("compatibleStates is only valid for subworkflows")
        if self.kind == "Subworkflow" and self.spec.state not in (
            self.spec.compatible_states or (self.spec.state,)
        ):
            raise ValueError("subworkflow state must be compatible")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def property_key(self) -> str:
        return f"{WORKFLOW_PROPERTY_PREFIX}{self.metadata.name}"

    def validate_property_size(self) -> None:
        size = len(self.canonical_json().encode("utf-8"))
        if size > MAX_PROPERTY_BYTES:
            raise ValueError(
                f"canonical workflow is {size} bytes; Jira project properties allow "
                f"at most {MAX_PROPERTY_BYTES} bytes"
            )
