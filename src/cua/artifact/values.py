"""Value expressions: where a typed/selected value comes from at replay time.

Literal values captured during discovery are never written for parameters or secrets:
the agent references inputs by *name* (``{param: member_id}``) and credentials by vault
key (``{secret: corebank.password}``), so artifacts stay free of PII and credentials by
construction, not by after-the-fact scrubbing.
"""

from __future__ import annotations

from pydantic import Field

from cua.artifact.common import Strict


class ParamValue(Strict):
    param: str = Field(description="Name of a declared input parameter.")

    def describe(self) -> str:
        return f"{{{{{self.param}}}}}"


class SecretValue(Strict):
    secret: str = Field(description="Vault key; resolved inside the surface boundary only.")

    def describe(self) -> str:
        return f"<secret:{self.secret}>"


class LiteralValue(Strict):
    literal: str = Field(description="Constant, non-sensitive value (e.g. a fixed dropdown choice).")

    def describe(self) -> str:
        return repr(self.literal)


ValueExpr = ParamValue | SecretValue | LiteralValue
