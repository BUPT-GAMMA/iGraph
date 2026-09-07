"""Ordered resource admission for concurrent Graph Engine requests.

Reservations cover execution, including background children, rather than HTTP
response lifetime. Independent reads may overlap; conflicting requests retain
arrival order, so a queued writer cannot be starved by newer readers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from graph_engine.tool_execution import ToolExecutionPolicy, resource_scope, tools_conflict


@dataclass(eq=False)
class Reservation:
    owner: tuple[str, str]
    policy: ToolExecutionPolicy
    done: asyncio.Future


class ToolResourceScheduler:
    def __init__(self):
        self._pending: list[Reservation] = []

    @staticmethod
    def _conflicts(left: Reservation, right: Reservation) -> bool:
        if left.owner[0] != right.owner[0]:
            return False
        if left.policy.barrier or right.policy.barrier:
            # An unclassified operation may touch user-owned resources as
            # well as session state; do not narrow an unknown effect.
            return True
        if left.owner == right.owner:
            return tools_conflict(left.policy, right.policy)
        # Construction revisions and saved datasets are shared by a user's
        # sessions. Graph/model state remains isolated by session.
        def shared(policy):
            return ToolExecutionPolicy(
                reads=frozenset(r for r in policy.reads if resource_scope(r) == "user"),
                writes=frozenset(r for r in policy.writes if resource_scope(r) == "user"),
            )
        return tools_conflict(shared(left.policy), shared(right.policy))

    async def acquire(self, owner: tuple[str, str], policy: ToolExecutionPolicy) -> Reservation:
        reservation = Reservation(owner, policy, asyncio.get_running_loop().create_future())
        predecessors = [row.done for row in self._pending if self._conflicts(row, reservation)]
        self._pending.append(reservation)
        try:
            if predecessors:
                await asyncio.gather(*(asyncio.shield(done) for done in predecessors))
            return reservation
        except BaseException:
            self.release(reservation)
            raise

    def release(self, reservation: Reservation) -> None:
        if reservation in self._pending:
            self._pending.remove(reservation)
        if not reservation.done.done():
            reservation.done.set_result(None)
