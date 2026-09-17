"""Asking a person who is not at the keyboard.

The local interface parks a question on a browser tab. That is the right answer
while somebody has the tab open, and no answer at all for the case this exists
for: a run that starts while the user is somewhere else and reaches something
irreversible ten minutes in. The choice then is between refusing the action and
reaching the person where they actually are.

It is one file and no new dependency - Telegram's bot API is HTTP and `httpx` is
already here - and it is wired only when a token is configured. The platform
does not gain a chat integration by being installed.

Three decisions worth stating:

**The message is the redacted request, and no more.** What is sent leaves the
machine and lands in somebody's chat history, on their phone, synced to
Telegram's servers. `ApprovalRequest.redacted()` is applied before anything is
formatted, and the payload is trimmed rather than dumped.

**Anything but a pressed Approve is a no.** A timeout, a network failure, a
malformed update, a reply from a chat that was not the one asked - all of them
answer the same way the closed browser tab does.

**The deadline is the point, not a safety net.** Nobody is watching a chat the
way they watch a prompt they just triggered, so a question here expires by
default and the run continues as refused rather than parking forever.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx

from domain.approvals.models import Approval, ApprovalRequest, ApprovalState
from domain.approvals.protocols import ApprovalRepository
from domain.errors import NotFoundError
from domain.secrets.protocols import SecretResolver
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

API = "https://api.telegram.org"

#: The credential names, resolved at the moment of the call and never stored.
TOKEN_SECRET = "TELEGRAM_BOT_TOKEN"
CHAT_SECRET = "TELEGRAM_CHAT_ID"

_APPROVE = "approve"
_REJECT = "reject"


def _keyboard(approval_id: UUID) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"{_APPROVE}:{approval_id}"},
                {"text": "Reject", "callback_data": f"{_REJECT}:{approval_id}"},
            ]
        ]
    }


def format_request(request: ApprovalRequest, *, width: int = 400) -> str:
    """What the person reads on their phone. Short enough to decide from."""
    action = request.action if len(request.action) <= width else request.action[:width] + "..."
    lines = [f"Approval needed: {action}"]
    if request.reason:
        lines.append(f"why: {request.reason}")
    lines.append(f"risk: {request.risk_level.value}")
    if request.scope:
        lines.append(f"scope: {request.scope.action} on {request.scope.resource}")
    return "\n".join(lines)


class TelegramApprovalService:
    """Implements `domain.approvals.protocols.ApprovalService`.

    Long-polls `getUpdates` rather than running a webhook: a webhook needs a
    public address, and this platform is deliberately reachable from nowhere.
    """

    def __init__(
        self,
        repository: ApprovalRepository,
        secrets: SecretResolver,
        *,
        ttl_seconds: float = 900.0,
        poll_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._repository = repository
        self._secrets = secrets
        self._ttl_seconds = ttl_seconds
        self._poll_seconds = poll_seconds
        self._client = client
        self._offset: int | None = None

    @classmethod
    def configured(cls, secrets: SecretResolver) -> bool:
        """Whether this machine has been told how to reach anyone."""
        return secrets.maybe(TOKEN_SECRET) is not None and secrets.maybe(CHAT_SECRET) is not None

    async def request(self, action: ApprovalRequest) -> ApprovalState:
        action = action.expiring_in(self._ttl_seconds)
        approval = Approval(request=action.redacted())
        await self._repository.save(approval)

        state, resolved_by = await self._ask(approval.request)
        approval = await self._repository.get(action.id) or approval
        await self._repository.save(
            approval.resolve(state, resolved_by=resolved_by, comment=action.reason)
        )
        log.info(
            "approval.resolved",
            approval_id=str(action.id),
            action=action.action,
            state=state.value,
            resolved_by=resolved_by,
        )
        return state

    async def _ask(self, request: ApprovalRequest) -> tuple[ApprovalState, str]:
        try:
            token = self._secrets.get(TOKEN_SECRET).reveal()
            chat_id = self._secrets.get(CHAT_SECRET).reveal()
        except Exception as error:
            log.warning("approval.telegram_unconfigured", error=str(error))
            return ApprovalState.REJECTED, "no-approver"

        try:
            await self._call(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": format_request(request),
                    "reply_markup": _keyboard(request.id),
                },
            )
        except Exception as error:
            # Unreachable is the same as unanswered. It is never an approval.
            log.warning("approval.telegram_unreachable", error=str(error))
            return ApprovalState.REJECTED, "no-approver"

        try:
            approved = await asyncio.wait_for(
                self._await_answer(token, request.id), timeout=self._ttl_seconds
            )
        except TimeoutError:
            log.info("approval.expired", approval_id=str(request.id))
            return ApprovalState.EXPIRED, "timeout"
        except Exception as error:
            log.warning("approval.telegram_failed", error=str(error))
            return ApprovalState.REJECTED, "no-approver"
        return (ApprovalState.APPROVED if approved else ApprovalState.REJECTED), "telegram"

    async def _await_answer(self, token: str, approval_id: UUID) -> bool:
        """Poll until this particular question is answered.

        Updates for other questions are consumed and skipped rather than left in
        the queue: leaving them would mean the next question read a stale answer
        as its own, which is the one bug in an approval flow that must not exist.
        """
        while True:
            payload: dict[str, Any] = {"timeout": int(self._poll_seconds)}
            if self._offset is not None:
                payload["offset"] = self._offset
            updates = await self._call(token, "getUpdates", payload)
            batch = updates.get("result", [])
            for update in batch:
                self._offset = int(update.get("update_id", 0)) + 1
                answer = self._answer_in(update, approval_id)
                if answer is not None:
                    return answer
            if not batch:
                # Telegram holds an empty long poll open for `timeout` seconds,
                # so this loop is normally paced by the server. A server that
                # answers immediately - misconfigured, proxied, or a test - would
                # otherwise turn it into a busy loop that never yields, which
                # starves the deadline that is supposed to end it.
                await asyncio.sleep(min(1.0, self._poll_seconds))

    @staticmethod
    def _answer_in(update: dict[str, Any], approval_id: UUID) -> bool | None:
        query = update.get("callback_query") or {}
        data = str(query.get("data", ""))
        decision, _, target = data.partition(":")
        if target != str(approval_id):
            return None
        return decision == _APPROVE

    async def _call(self, token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API}/bot{token}/{method}"
        timeout = self._poll_seconds + 10.0
        if self._client is not None:
            response = await self._client.post(url, json=payload, timeout=timeout)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def resolve(
        self,
        approval_id: UUID,
        decision: ApprovalState,
        *,
        resolved_by: str = "user",
        comment: str = "",
    ) -> None:
        approval = await self._repository.get(approval_id)
        if approval is None:
            raise NotFoundError(f"Unknown approval: {approval_id}")
        await self._repository.save(
            approval.resolve(decision, resolved_by=resolved_by, comment=comment)
        )
