"""Who gets asked, and what the answer is when nobody is there."""

from __future__ import annotations

import pytest

from domain.approvals.models import ApprovalRequest, ApprovalState
from domain.tasks.task import Task
from infrastructure.approvals.service import ApprovalMode, LocalApprovalService
from infrastructure.persistence.approval_repository import InMemoryApprovalRepository


def request(action: str = "fs.write(path='notes.txt')") -> ApprovalRequest:
    return ApprovalRequest.create(Task.create("Tidy up").id, action, reason="Overwrite notes.txt")


@pytest.fixture
def repository() -> InMemoryApprovalRepository:
    return InMemoryApprovalRepository()


async def test_a_yes_at_the_terminal_approves(repository) -> None:
    service = LocalApprovalService(
        repository, confirmer=lambda _: True, is_interactive=lambda: True
    )

    assert await service.request(request()) is ApprovalState.APPROVED


async def test_anything_but_a_yes_rejects(repository) -> None:
    service = LocalApprovalService(
        repository, confirmer=lambda _: False, is_interactive=lambda: True
    )

    assert await service.request(request()) is ApprovalState.REJECTED


async def test_with_nobody_at_the_terminal_the_answer_is_no(repository) -> None:
    """A scheduled run cannot consent on the user's behalf by being silent."""
    service = LocalApprovalService(
        repository,
        confirmer=lambda _: True,  # would say yes, but is never called
        is_interactive=lambda: False,
    )

    assert await service.request(request()) is ApprovalState.REJECTED


async def test_deny_mode_does_not_even_ask(repository) -> None:
    asked = []
    service = LocalApprovalService(
        repository,
        mode=ApprovalMode.DENY,
        confirmer=lambda r: asked.append(r) or True,
        is_interactive=lambda: True,
    )

    assert await service.request(request()) is ApprovalState.REJECTED
    assert asked == []


async def test_allow_mode_is_still_recorded(repository) -> None:
    """Turning the prompt off is a choice about being interrupted, not about the record."""
    service = LocalApprovalService(repository, mode=ApprovalMode.ALLOW)
    action = request()

    assert await service.request(action) is ApprovalState.APPROVED

    stored = await repository.get(action.id)
    assert stored.state is ApprovalState.APPROVED
    assert stored.resolved_by == "configuration"


async def test_the_question_is_written_down_before_it_is_answered(repository) -> None:
    """A process killed mid-question leaves a pending row, not a lost decision."""
    seen: list[ApprovalState] = []

    async def watching_save(approval):
        seen.append(approval.state)
        await InMemoryApprovalRepository.save(repository, approval)

    repository.save = watching_save  # type: ignore[method-assign]
    service = LocalApprovalService(repository, mode=ApprovalMode.ALLOW)

    await service.request(request())

    assert seen == [ApprovalState.PENDING, ApprovalState.APPROVED]


async def test_a_pending_decision_can_be_resolved_later(repository) -> None:
    from domain.approvals.models import Approval

    action = request()
    await repository.save(Approval(request=action))
    service = LocalApprovalService(repository, mode=ApprovalMode.DENY)

    await service.resolve(action.id, ApprovalState.APPROVED, comment="checked it")

    stored = await repository.get(action.id)
    assert stored.state is ApprovalState.APPROVED
    assert stored.comment == "checked it"


async def test_resolving_something_that_does_not_exist_says_so(repository) -> None:
    from uuid import uuid4

    from domain.errors import NotFoundError

    service = LocalApprovalService(repository, mode=ApprovalMode.DENY)

    with pytest.raises(NotFoundError):
        await service.resolve(uuid4(), ApprovalState.APPROVED)


async def test_only_pending_approvals_are_listed(repository) -> None:
    service = LocalApprovalService(repository, mode=ApprovalMode.ALLOW)
    await service.request(request())

    assert await repository.list_pending() == []


async def test_a_request_that_said_do_not_ask_is_approved_without_asking(repository) -> None:
    from domain.workforce.directions import ApprovalChoice, Directions, given

    asked = []
    service = LocalApprovalService(
        repository, confirmer=lambda r: asked.append(r) or False, is_interactive=lambda: True
    )
    action = request()

    with given(Directions(approvals=ApprovalChoice.AUTO)):
        assert await service.request(action) is ApprovalState.APPROVED

    assert asked == []
    stored = await repository.get(action.id)
    assert stored.resolved_by == "request", "done under a standing yes, and recorded as such"


async def test_a_request_cannot_talk_a_refusing_machine_into_it(repository) -> None:
    """Not asking widens only what this machine would have asked a person about."""
    from domain.workforce.directions import ApprovalChoice, Directions, given

    service = LocalApprovalService(repository, mode=ApprovalMode.DENY, is_interactive=lambda: True)

    with given(Directions(approvals=ApprovalChoice.AUTO)):
        assert await service.request(request()) is ApprovalState.REJECTED
