"""FastAPI application implementing the mock "Meridian CoreBank" teller workstation."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from mock_corebank.data import SHARE_TYPES, Member, Share, TenantTheme, seed_members
from mock_corebank.faults import FaultInjector, FaultKind, FaultSpec

SESSION_COOKIE = "CBSESSID"


@dataclass
class AppState:
    theme: TenantTheme
    username: str
    password: str
    members: dict[str, Member]
    faults: FaultInjector = field(default_factory=FaultInjector)
    sessions: dict[str, str] = field(default_factory=dict)
    used_request_tokens: set[str] = field(default_factory=set)
    audit: list[dict[str, Any]] = field(default_factory=list)
    confirmation_seq: int = 48210


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def create_app(theme: TenantTheme, *, username: str, password: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    state = AppState(theme=theme, username=username, password=password, members=seed_members(theme))
    app.state.corebank = state

    env = Environment(
        loader=PackageLoader("mock_corebank", "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["money"] = _money

    def render(template: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        page = env.get_template(template).render(
            theme=theme,
            v=theme.vocabulary,
            p=theme.control_prefix,
            sid=secrets.token_hex(6),
            **ctx,
        )
        return HTMLResponse(page, status_code=status_code)

    def session_user(request: Request) -> str | None:
        sid = request.cookies.get(SESSION_COOKIE)
        return state.sessions.get(sid) if sid else None

    async def guard(request: Request) -> Response | dict[str, Any]:
        """Session check + fault injection for content pages rendered in the ``main`` frame."""
        if session_user(request) is None:
            return render("login.html", message="Your session has expired. Please sign on again.")
        fault = state.faults.take(request.url.path)
        if fault is None:
            return {}
        match fault.kind:
            case FaultKind.SLOW:
                await asyncio.sleep(fault.seconds)
                return {}
            case FaultKind.SESSION_EXPIRED:
                sid = request.cookies.get(SESSION_COOKIE)
                if sid:
                    state.sessions.pop(sid, None)
                return render("login.html", message="Your session has expired. Please sign on again.")
            case FaultKind.SERVER_ERROR:
                return render("server_error.html", status_code=500, path=request.url.path)
            case FaultKind.MAINTENANCE_NOTICE:
                return render("notice.html", continue_url=str(request.url))
            case FaultKind.UNKNOWN_DIALOG:
                return {"blocking_overlay": True}

    # ------------------------------------------------------------------ shell + auth

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/cb/login.asp", status_code=302)

    @app.get("/cb/login.asp", response_class=HTMLResponse)
    async def login_form(msg: str | None = None) -> HTMLResponse:
        return render("login.html", message=msg)

    @app.post("/cb/login.asp", response_model=None)
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        user = str(form.get(f"{theme.control_prefix}$txtUsr", "")).strip()
        pwd = str(form.get(f"{theme.control_prefix}$txtPwd", ""))
        if user != state.username or pwd != state.password:
            return render("login.html", message="Invalid User ID or Password.")
        sid = secrets.token_urlsafe(16)
        state.sessions[sid] = user
        response = RedirectResponse("/cb/main.asp", status_code=303)
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
        return response

    @app.get("/cb/logoff.asp", response_model=None)
    async def logoff(request: Request) -> Response:
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            state.sessions.pop(sid, None)
        return RedirectResponse("/cb/login.asp?msg=You+have+signed+off.", status_code=302)

    @app.get("/cb/main.asp", response_model=None)
    async def main(request: Request) -> Response:
        if session_user(request) is None:
            return RedirectResponse("/cb/login.asp", status_code=302)
        return render("frameset.html")

    @app.get("/cb/banner.asp", response_class=HTMLResponse)
    async def banner(request: Request) -> HTMLResponse:
        return render("banner.html", user=session_user(request) or "")

    @app.get("/cb/nav.asp", response_class=HTMLResponse)
    async def nav() -> HTMLResponse:
        return render("nav.html")

    @app.get("/cb/welcome.asp", response_model=None)
    async def welcome(request: Request) -> Response:
        guarded = await guard(request)
        if isinstance(guarded, Response):
            return guarded
        return render("welcome.html", **guarded)

    @app.get("/cb/reports.asp", response_model=None)
    async def reports(request: Request) -> Response:
        guarded = await guard(request)
        if isinstance(guarded, Response):
            return guarded
        return render("denied.html", **guarded)

    # ------------------------------------------------------------------ member inquiry

    @app.get("/cb/mbr_search.asp", response_model=None)
    async def member_search(request: Request) -> Response:
        guarded = await guard(request)
        if isinstance(guarded, Response):
            return guarded
        params = request.query_params
        if params.get("__EVENTTARGET") != "btnSearch":
            return render("search.html", **guarded)
        number = params.get(f"{theme.control_prefix}$txtMbrNo", "").strip()
        error: str | None = None
        results: list[Member] = []
        if not number:
            error = f"{theme.vocabulary.member_number.rstrip(':')} is required."
        elif not number.isdigit() or not 6 <= len(number) <= 10:
            error = f"{theme.vocabulary.member_number.rstrip(':')} must be 6 to 10 digits."
        elif number in state.members:
            results = [state.members[number]]
        if len(results) == 1 and theme.auto_open_single_result:
            member = results[0]
            if member.restricted:
                return render("denied.html", **guarded)
            return render("detail.html", member=member, **guarded)
        return render(
            "search.html",
            searched=True,
            query=number,
            error=error,
            results=results,
            **guarded,
        )

    @app.get("/cb/mbr_detail.asp", response_model=None)
    async def member_detail(request: Request, m: str = "") -> Response:
        guarded = await guard(request)
        if isinstance(guarded, Response):
            return guarded
        member = state.members.get(m)
        if member is None:
            return render("search.html", searched=True, query=m, results=[], **guarded)
        if member.restricted:
            return render("denied.html", **guarded)
        return render("detail.html", member=member, **guarded)

    # ------------------------------------------------------------------ sub-account opening

    def _funding_options(member: Member) -> list[tuple[str, str]]:
        options = [(s.suffix, f"Transfer from {s.suffix} - {s.description}") for s in member.shares]
        return [*options, ("CASH", "Cash Deposit")]

    def _validate_subaccount(member: Member, form: dict[str, str]) -> tuple[list[str], dict[str, Any]]:
        prefix = theme.control_prefix
        errors: list[str] = []
        share_type = form.get(f"{prefix}$ddlType", "")
        nickname = form.get(f"{prefix}$txtNick", "").strip()
        deposit_raw = form.get(f"{prefix}$txtDep", "").strip().replace("$", "").replace(",", "")
        funding = form.get(f"{prefix}$ddlFund", "")
        if share_type not in SHARE_TYPES:
            errors.append("Share Type is required.")
        if not nickname or len(nickname) > 20 or not all(c.isalnum() or c == " " for c in nickname):
            errors.append("Nickname is required (max 20 letters/digits).")
        deposit = Decimal("0")
        try:
            deposit = Decimal(deposit_raw)
            if deposit <= 0 or deposit.as_tuple().exponent < -2:  # type: ignore[operator]
                raise InvalidOperation
        except InvalidOperation:
            errors.append("Initial Deposit must be a positive dollar amount.")
        source = next((s for s in member.shares if s.suffix == funding), None)
        if funding != "CASH" and source is None:
            errors.append("Funding Source is required.")
        elif source is not None and deposit > source.available:
            errors.append(f"Insufficient available funds in {source.suffix}.")
        values = {
            "share_type": share_type,
            "share_type_label": SHARE_TYPES.get(share_type, ""),
            "nickname": nickname,
            "deposit": deposit,
            "funding": funding,
            "funding_label": dict(_funding_options(member)).get(funding, ""),
        }
        return errors, values

    async def _member_for_write(request: Request, m: str) -> Response | tuple[Member, dict[str, Any]]:
        guarded = await guard(request)
        if isinstance(guarded, Response):
            return guarded
        member = state.members.get(m)
        if member is None:
            return render("search.html", searched=True, query=m, results=[], **guarded)
        if member.restricted:
            return render("denied.html", **guarded)
        return member, guarded

    @app.get("/cb/subacct_new.asp", response_model=None)
    async def subaccount_form(request: Request, m: str = "") -> Response:
        resolved = await _member_for_write(request, m)
        if isinstance(resolved, Response):
            return resolved
        member, extra = resolved
        return render(
            "subacct_form.html",
            member=member,
            share_types=SHARE_TYPES,
            funding_options=_funding_options(member),
            errors=[],
            values={},
            **extra,
        )

    @app.post("/cb/subacct_new.asp", response_model=None)
    async def subaccount_review(request: Request, m: str = "") -> Response:
        resolved = await _member_for_write(request, m)
        if isinstance(resolved, Response):
            return resolved
        member, extra = resolved
        form = {k: str(v) for k, v in (await request.form()).items()}
        errors, values = _validate_subaccount(member, form)
        if errors:
            return render(
                "subacct_form.html",
                member=member,
                share_types=SHARE_TYPES,
                funding_options=_funding_options(member),
                errors=errors,
                values=values,
                **extra,
            )
        return render(
            "subacct_review.html",
            member=member,
            values=values,
            form=form,
            request_token=secrets.token_hex(8),
            **extra,
        )

    @app.post("/cb/subacct_submit.asp", response_model=None)
    async def subaccount_submit(request: Request, m: str = "") -> Response:
        resolved = await _member_for_write(request, m)
        if isinstance(resolved, Response):
            return resolved
        member, extra = resolved
        form = {k: str(v) for k, v in (await request.form()).items()}
        token = form.get("__REQTOKEN", "")
        if not token or token in state.used_request_tokens:
            return render(
                "message.html", title="Duplicate Request", message="This request has already been processed.", **extra
            )
        errors, values = _validate_subaccount(member, form)
        if errors:
            return render(
                "subacct_form.html",
                member=member,
                share_types=SHARE_TYPES,
                funding_options=_funding_options(member),
                errors=errors,
                values=values,
                **extra,
            )
        state.used_request_tokens.add(token)
        used = {s.suffix for s in member.shares}
        suffix = next(f"S{n:02d}" for n in range(11, 100) if f"S{n:02d}" not in used)
        deposit: Decimal = values["deposit"]
        source = next((s for s in member.shares if s.suffix == values["funding"]), None)
        if source is not None:
            source.balance -= deposit
            source.available -= deposit
        member.shares.append(Share(suffix, values["nickname"], values["share_type"], deposit, deposit))
        state.confirmation_seq += 1
        confirmation = f"CB-{state.confirmation_seq}"
        state.audit.append(
            {
                "event": "subaccount_opened",
                "member": member.member_number,
                "suffix": suffix,
                "confirmation": confirmation,
            }
        )
        return render(
            "subacct_confirm.html", member=member, values=values, suffix=suffix, confirmation=confirmation, **extra
        )

    # ------------------------------------------------------------------ test-harness admin API
    # Not part of the "vendor product"; deliberately outside the automation allowlist.

    @app.post("/__admin/faults")
    async def arm_fault(spec: FaultSpec) -> JSONResponse:
        state.faults.arm(spec)
        return JSONResponse({"armed": state.faults.snapshot()})

    @app.delete("/__admin/faults")
    async def clear_faults() -> JSONResponse:
        state.faults.clear()
        return JSONResponse({"armed": []})

    @app.post("/__admin/reset")
    async def reset() -> JSONResponse:
        state.members = seed_members(theme)
        state.faults.clear()
        state.audit.clear()
        state.used_request_tokens.clear()
        return JSONResponse({"ok": True})

    @app.get("/__admin/audit")
    async def audit() -> JSONResponse:
        return JSONResponse({"tenant": theme.tenant_id, "audit": state.audit})

    @app.get("/__admin/health")
    async def health() -> JSONResponse:
        return JSONResponse({"tenant": theme.tenant_id, "version": theme.product_version, "ok": True})

    return app
