"""Reference discovery plans for the mock core, played by :class:`ScriptedClient`.

They stand in for the LLM in tests, CI and the offline demo (``cua discover --provider
scripted``) so that the *rest* of the discovery pipeline - observation, locator synthesis and
verification, risk/approval, recording, leak checks - is exercised without an API key.
Elements are found by what they look like on the rendered screen, never by fixed refs.
Evidence of real model behaviour lives in ``evidence/`` (recorded with a real LLM).
"""

from __future__ import annotations

from cua.discovery.llm import ScreenRef, ToolCall

GET_SAVINGS_BALANCE = [
    ToolCall("click", {"ref": ScreenRef(r'link "Member Inquiry"'), "reason": "open member search"}),
    ToolCall("fill", {"ref": ScreenRef(r'textbox label="Member Number:"'), "input": "member_id", "reason": "search"}),
    ToolCall("click", {"ref": ScreenRef(r'button "Search"'), "reason": "run the search"}),
    ToolCall("click", {"ref": ScreenRef(r'link "View"'), "reason": "open the member record"}),
    ToolCall(
        "extract",
        {
            "ref": ScreenRef(r'"\$[\d,.]+"', line="Primary Savings"),
            "output": "savings_balance",
            "reason": "balance column of the Primary Savings row",
        },
    ),
    ToolCall("finish", {"success_text": "Member Account Summary", "summary": "balance read"}),
]

OPEN_SHARE_ACCOUNT = [
    *GET_SAVINGS_BALANCE[:4],
    ToolCall("click", {"ref": ScreenRef(r'button "Open Sub-Account"'), "reason": "start account opening"}),
    ToolCall(
        "select_option",
        {"ref": ScreenRef(r'combobox label="Share Type:"'), "input": "share_type", "reason": "product type"},
    ),
    ToolCall("fill", {"ref": ScreenRef(r'textbox label="Nickname:"'), "input": "nickname", "reason": "nickname"}),
    ToolCall(
        "fill",
        {"ref": ScreenRef(r'textbox label="Initial Deposit:"'), "input": "initial_deposit", "reason": "amount"},
    ),
    ToolCall(
        "select_option",
        {"ref": ScreenRef(r'combobox label="Funding Source:"'), "option": "Cash Deposit", "reason": "cash funding"},
    ),
    ToolCall("click", {"ref": ScreenRef(r'button "Continue"'), "reason": "go to the review page"}),
    ToolCall(
        "click",
        {"ref": ScreenRef(r'button "Submit"'), "commits_transaction": True, "reason": "submit the request"},
    ),
    ToolCall(
        "extract",
        {"ref": ScreenRef(r'"CB-\d+"'), "output": "confirmation_number", "reason": "confirmation number"},
    ),
    ToolCall("finish", {"success_text": "Sub-Account Opened Successfully", "summary": "account opened"}),
]

PLANS: dict[str, list[ToolCall]] = {
    "corebank.member.get_savings_balance": GET_SAVINGS_BALANCE,
    "corebank.member.open_share_account": OPEN_SHARE_ACCOUNT,
}
