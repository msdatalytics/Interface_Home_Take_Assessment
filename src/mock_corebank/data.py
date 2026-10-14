"""Synthetic tenant configuration and member data for the mock core banking app."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Vocabulary:
    """UI labels that differ between tenants running the same vendor product."""

    member_number: str
    search_button: str
    balance_column: str
    primary_savings: str
    open_subaccount_button: str


@dataclass(frozen=True)
class TenantTheme:
    tenant_id: str
    institution_name: str
    product_version: str
    banner_color: str
    control_prefix: str
    vocabulary: Vocabulary
    auto_open_single_result: bool = False
    """Tenant configuration flag: a search with exactly one hit opens the member directly."""


@dataclass
class Share:
    suffix: str
    description: str
    share_type: str
    balance: Decimal
    available: Decimal


@dataclass
class Member:
    member_number: str
    name: str
    ssn: str
    dob: str
    status: str
    restricted: bool = False
    shares: list[Share] = field(default_factory=list)


ALPHA = TenantTheme(
    tenant_id="cu-alpha",
    institution_name="Alpha Community Credit Union",
    product_version="4.2.1",
    banner_color="#1f3a5f",
    control_prefix="ctl00$cph",
    vocabulary=Vocabulary(
        member_number="Member Number:",
        search_button="Search",
        balance_column="Balance",
        primary_savings="Primary Savings",
        open_subaccount_button="Open Sub-Account",
    ),
)

BETA = TenantTheme(
    tenant_id="cu-beta",
    institution_name="Lakeshore Federal Credit Union",
    product_version="4.3.0",
    banner_color="#5f1f3a",
    control_prefix="ctl01$MainContent",
    vocabulary=Vocabulary(
        member_number="Account Number:",
        search_button="Find Member",
        balance_column="Current Balance",
        primary_savings="Share Savings",
        open_subaccount_button="Open Sub-Account",
    ),
    auto_open_single_result=True,
)

TENANTS: dict[str, TenantTheme] = {t.tenant_id: t for t in (ALPHA, BETA)}

SHARE_TYPES: dict[str, str] = {
    "RS": "Regular Savings",
    "HC": "Holiday Club",
    "VC": "Vacation Club",
    "MM": "Money Market",
}


def seed_members(theme: TenantTheme) -> dict[str, Member]:
    """Return a fresh, mutable member store for a tenant."""
    savings = theme.vocabulary.primary_savings
    if theme.tenant_id == "cu-alpha":
        members = [
            Member(
                "100234",
                "Jordan A. Rivera",
                "123-45-6789",
                "1984-03-14",
                "Active",
                shares=[
                    Share("S00", savings, "SAV", Decimal("2481.37"), Decimal("2476.37")),
                    Share("S10", "Holiday Club", "CLB", Decimal("150.00"), Decimal("150.00")),
                    Share("D20", "Share Draft Checking", "CHK", Decimal("613.02"), Decimal("613.02")),
                ],
            ),
            Member(
                "100591",
                "Priya N. Shah",
                "987-65-4321",
                "1991-11-02",
                "Active",
                shares=[
                    Share("S00", savings, "SAV", Decimal("18240.00"), Decimal("18235.00")),
                ],
            ),
            Member("100777", "Employee Record", "555-12-3456", "1979-07-21", "Active", restricted=True),
        ]
    else:
        members = [
            Member(
                "200145",
                "Morgan T. Ellis",
                "321-54-9876",
                "1976-05-30",
                "Active",
                shares=[
                    Share("S01", savings, "SAV", Decimal("932.10"), Decimal("927.10")),
                    Share("D05", "Checking Plus", "CHK", Decimal("2210.55"), Decimal("2210.55")),
                ],
            ),
        ]
    return {m.member_number: m for m in members}
