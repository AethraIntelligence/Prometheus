"""Whether what one employee finished is something the next one can start from.

Checked before the downstream task is given to anybody, never after: a run that
starts from material it cannot use spends a whole budget finding that out, and
reports it as a failure of the wrong task.

What was *delivered* is read off the record rather than off the declaration
alone. A role that promises a file and wrote none handed over an answer, and the
next role is judged against that - which is the whole difference between a
contract and a description.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from domain.employees.contract import WorkContract, WorkProduct
from domain.tasks.task import Task
from domain.workforce.acceptance import written


@dataclass(frozen=True, slots=True)
class Delivery:
    """One finished upstream task, as the next one sees it."""

    employee: str
    products: frozenset[WorkProduct]


def delivered(task: Task, employee: str, contract: WorkContract) -> Delivery:
    """What this task actually handed on: its promise, less what the record lacks."""
    products = set(contract.produces)
    if WorkProduct.FILE in products and not written(task):
        products.discard(WorkProduct.FILE)
    return Delivery(employee=employee, products=frozenset(products))


def compatible(contract: WorkContract, upstream: Iterable[Delivery]) -> bool:
    """Whether a role with this contract can start from these deliveries.

    No upstream at all is always compatible: a first task starts from the
    request, and the contract speaks only about hand-offs.
    """
    deliveries = tuple(upstream)
    if not deliveries:
        return True
    return contract.can_start_from(
        frozenset().union(*(delivery.products for delivery in deliveries))
    )


def explain(contract: WorkContract, upstream: Iterable[Delivery]) -> str:
    """Why a hand-off does not fit, in a sentence a person reads."""
    deliveries = tuple(upstream)
    handed = sorted(
        {product.value for delivery in deliveries for product in delivery.products}
    )
    sources = ", ".join(sorted({delivery.employee for delivery in deliveries}))
    return (
        f"earlier work by {sources} delivered {', '.join(handed) or 'nothing'}; "
        f"this role starts only from {', '.join(sorted(p.value for p in contract.accepts))}"
    )
