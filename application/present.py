"""What day it is, told to every model that is about to act on "latest".

A model's weights are a photograph of the past. Asked for "the latest AI news"
it answers with the latest it remembers, which is the month its training data
ends in, and it has no way to notice: nothing in a prompt contradicted it. A
run of this platform watched exactly that - a search for recent news came back
confidently about February 2025, with sources, a year and a half late.

The fix is not a tool. A clock the model has to think of calling is a clock it
does not call, because it does not know it is wrong. The date is a fact about
the situation, so it is stated in the same place the other facts about the
situation are - beside the prompt, as a system message, like the language the
user is answered in (`application/prometheus/language.py`).

It sits at the top of `application/` because both halves need it: the manager,
whose reading of a request decides what "recent" means, and the employee
runtime, whose searches have to be aimed at a year that exists.

The local timezone rather than UTC: "today" is a word about the person's day,
and a machine an hour ahead of midnight in UTC is not living in tomorrow.
"""

from __future__ import annotations

from datetime import datetime

from domain.llm.models import Message

#: Said once, in the terms a model can act on: the date, and what to do about
#: the fact that it ends before this one. The last sentence is the operative
#: one - without it a model reads the date, agrees with it, and goes on
#: searching for the year it remembers.
TEMPLATE = (
    "Today is {today}. Your training data ends before this date, so what you "
    'remember as "recent" or "the latest" is out of date. Read "latest", '
    '"current", "now", "this year" and any bare year against today\'s date, '
    "never against what you remember, and confirm anything time-sensitive "
    "with a tool rather than from memory."
)


def today(now: datetime | None = None) -> str:
    """The date twice: as a person writes it, and as a query is written.

    The long form so nothing depends on whether 09/20 is September or a day
    that does not exist, and the ISO form because the next thing a model does
    with it is put it in a search.
    """
    moment = now or datetime.now().astimezone()
    return f"{moment.strftime('%A, %d %B %Y')} ({moment.date().isoformat()})"


def situation(now: datetime | None = None) -> str:
    """The sentences themselves, for a caller already writing a system prompt."""
    return TEMPLATE.format(today=today(now))


def dated(now: datetime | None = None) -> tuple[Message, ...]:
    """The same thing as a system message, for a caller sending a prompt file.

    A tuple for the same reason `instruction` is one: the caller splices it in
    and never decides whether there is anything to splice.
    """
    return (Message.system(situation(now)),)
