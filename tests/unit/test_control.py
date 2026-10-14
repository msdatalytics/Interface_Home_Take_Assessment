from __future__ import annotations

import pytest

from cua.hitl.control import ControlChannel, Controller, LeaseLostError


def test_lease_transfers_fence_stale_automation() -> None:
    channel = ControlChannel()
    epoch = channel.lease.epoch
    channel.assert_automation(epoch)

    channel.to_human("ops.jane", "stuck", "iv-1")
    with pytest.raises(LeaseLostError):
        channel.assert_automation(epoch)
    channel.assert_operator("ops.jane")
    with pytest.raises(LeaseLostError):
        channel.assert_operator("ops.mallory")

    lease = channel.to_automation("resolved")
    with pytest.raises(LeaseLostError, match="stale"):
        channel.assert_automation(epoch)  # the pre-handoff epoch stays invalid forever
    channel.assert_automation(lease.epoch)
    assert [entry.holder for entry in channel.history] == [
        Controller.AUTOMATION,
        Controller.HUMAN,
        Controller.AUTOMATION,
    ]


def test_a_second_operator_cannot_steal_the_session() -> None:
    channel = ControlChannel()
    channel.to_human("ops.jane", "approval", "iv-1")
    with pytest.raises(LeaseLostError, match=r"ops\.jane"):
        channel.to_human("ops.mallory", "approval", "iv-1")
