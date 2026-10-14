"""A discovery request: *what* the capability must do (contract), written by its owner.

The split is deliberate - humans own the contract (name, inputs, outputs, sensitivity),
the model only discovers *how* to fulfil it on the live UI. A model never gets to invent a
caller-facing interface.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from cua.artifact.capability import CapabilityName, InputSpec, OutputSpec, SemVer
from cua.artifact.common import API_VERSION, Strict


class DiscoveryRequestMetadata(Strict):
    name: CapabilityName
    version: SemVer = "1.0.0"
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)


class DiscoveryRequestSpec(Strict):
    app: str
    goal: str = Field(description="Natural-language goal. '{input_name}' placeholders refer to inputs.")
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    values: dict[str, str] = Field(
        default_factory=dict,
        description="Synthetic input values used for this discovery run (never real customer data).",
    )
    max_steps: int = Field(default=30, ge=3, le=100)

    @model_validator(mode="after")
    def _values_cover_inputs(self) -> DiscoveryRequestSpec:
        missing = [i.name for i in self.inputs if i.required and i.name not in self.values]
        if missing:
            raise ValueError(f"discovery needs example values for required inputs: {missing}")
        return self


class DiscoveryRequest(Strict):
    api_version: Literal["cua.dev/v1"] = Field(default=API_VERSION, alias="apiVersion")
    kind: Literal["DiscoveryRequest"] = "DiscoveryRequest"
    metadata: DiscoveryRequestMetadata
    spec: DiscoveryRequestSpec
