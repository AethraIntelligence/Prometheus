"""Phase 13: the parsers and rules that guard installations, held to properties.

Each property is something a hand-written example cannot exhaust: no archive path
escapes the directory it is restored into, no stop record reads as released, no
policy declaration can lower the risk an effect implies, a schedule's next run is
always after the moment asked about. Hypothesis looks for the counterexample.

The time spent is bounded: `PROMETHEUS_PROPERTY_EXAMPLES` sets how many examples
each property gets (CI keeps the default), and no single example may take long.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime, time, timedelta
from pathlib import PurePosixPath

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domain.errors import ConfigurationError
from domain.policies.engine import PolicyRequest
from domain.policies.models import ActorKind, Decision, RiskLevel, SimpleActor
from domain.policies.risk import Effect, at_least, risk_of
from domain.policies.rules import CATALOG, RuleBasedPolicyEngine
from domain.safety import emergency
from domain.safety.backup import BackupError, parse_manifest, safe_entry_path
from domain.safety.platform import check
from domain.safety.schema import SchemaVerdict, judge
from domain.scheduling.models import MIN_INTERVAL_SECONDS, Recurrence
from domain.tools.models import ToolSpec

EXAMPLES = int(os.environ.get("PROMETHEUS_PROPERTY_EXAMPLES", "200"))
bounded = settings(
    max_examples=EXAMPLES,
    deadline=timedelta(milliseconds=500),
    suppress_health_check=[HealthCheck.too_slow],
)

segment = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/"),
    min_size=0,
    max_size=12,
)


# --- Backup manifests ------------------------------------------------------------------


@bounded
@given(st.lists(segment, min_size=1, max_size=6).map("/".join))
def test_an_accepted_entry_path_never_leaves_the_restore_directory(path: str) -> None:
    try:
        accepted = safe_entry_path(path)
    except BackupError:
        return
    parts = PurePosixPath(accepted).parts
    assert ".." not in parts and "." not in parts
    assert not accepted.startswith("/") and "\\" not in accepted
    depth = 0
    for part in parts:
        depth += -1 if part == ".." else 1
        assert depth > 0


@bounded
@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(max_size=20),
        lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=10), inner),
        max_leaves=20,
    )
)
def test_any_json_is_either_a_manifest_or_a_backup_error(document) -> None:
    with contextlib.suppress(BackupError):
        parse_manifest(json.dumps(document))


@bounded
@given(st.text(max_size=200))
def test_any_text_is_either_a_manifest_or_a_backup_error(text: str) -> None:
    with contextlib.suppress(BackupError):
        parse_manifest(text)


digest = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
entry_name = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_.", min_size=1, max_size=10).filter(
        lambda part: part not in {".", ".."}
    ),
    min_size=1,
    max_size=4,
).map(lambda parts: "files/" + "/".join(parts))


@bounded
@given(
    st.lists(
        st.tuples(entry_name, st.integers(min_value=0, max_value=2**40), digest),
        max_size=8,
        unique_by=lambda item: item[0],
    ),
    st.text(alphabet="0123456789", min_size=3, max_size=3),
)
def test_a_manifest_round_trips(entries, revision: str) -> None:
    from domain.safety.backup import BackupEntry, BackupManifest, EntryKind

    manifest = BackupManifest(
        created_at=datetime(2026, 9, 17, tzinfo=UTC),
        app_version="0.1.0",
        schema_revision=revision,
        entries=(
            BackupEntry("data/prometheus.db", EntryKind.DATABASE, 1, "0" * 64),
            *(
                BackupEntry(path, EntryKind.ARTIFACT, size, sha, "files")
                for path, size, sha in entries
            ),
        ),
        roots={"files": "/somewhere"},
    )

    assert parse_manifest(manifest.to_json()) == manifest


# --- The stop record ------------------------------------------------------------------


@bounded
@given(st.text(max_size=300))
def test_no_stop_record_ever_reads_as_released(text: str) -> None:
    assert emergency.parse(text).engaged


@bounded
@given(st.text(max_size=80), st.text(max_size=20))
def test_a_written_stop_reads_back_as_written(reason: str, by: str) -> None:
    written = emergency.engaged(reason, by, datetime(2026, 9, 17, tzinfo=UTC))
    read = emergency.parse(emergency.render(written))

    assert read.engaged and not read.unreadable
    assert read.reason == written.reason and read.engaged_by == written.engaged_by


# --- Schema compatibility -------------------------------------------------------------


revisions = st.lists(
    st.text(alphabet="0123456789abcdef", min_size=3, max_size=6),
    min_size=1,
    max_size=20,
    unique=True,
).map(tuple)


@bounded
@given(revisions, st.data())
def test_only_known_revisions_may_open_and_the_pending_ones_are_exactly_what_follows(
    history, data
) -> None:
    current = data.draw(st.none() | st.sampled_from(history) | st.text(min_size=1, max_size=6))
    decision = judge(current, history, has_tables=current is not None)

    if current is None:
        assert decision.verdict is SchemaVerdict.FRESH and decision.pending == history
    elif current not in history:
        assert decision.verdict is SchemaVerdict.NEWER and not decision.verdict.may_open
    else:
        assert decision.pending == history[history.index(current) + 1 :]
        assert decision.verdict.may_open


# --- Policies and effects -------------------------------------------------------------


@bounded
@given(st.sampled_from(list(Effect)), st.sampled_from(list(RiskLevel)), st.booleans())
def test_a_declared_risk_can_raise_what_an_effect_implies_and_never_lower_it(
    effect: Effect, declared: RiskLevel, reversible: bool
) -> None:
    spec = ToolSpec.of(
        "some.tool", "A tool.", effect=effect, risk_level=declared, reversible=reversible
    )

    assert at_least(spec.risk_level, risk_of(effect))
    assert at_least(spec.risk_level, declared)


@bounded
@given(
    st.sampled_from(list(Effect)),
    st.sampled_from(list(RiskLevel)),
    st.booleans(),
    st.frozensets(st.sampled_from(sorted(CATALOG))),
)
def test_a_policy_decision_is_deterministic_and_a_declaration_only_narrows(
    effect: Effect, level: RiskLevel, reversible: bool, policies: frozenset[str]
) -> None:
    engine = RuleBasedPolicyEngine()
    actor = SimpleActor("worker", ActorKind.EMPLOYEE, frozenset({"some.tool"}))

    def decide(declared: frozenset[str]) -> Decision:
        request = PolicyRequest(
            actor=actor,
            action="some.tool()",
            tool="some.tool",
            effect=effect,
            risk_level=level,
            reversible=reversible,
            policies=declared,
        )
        return engine.evaluate(request).decision

    severity = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}
    assert decide(policies) == decide(policies)
    assert severity[decide(policies)] >= severity[decide(frozenset())]


# --- Schedules ------------------------------------------------------------------------


moments = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
)


@bounded
@given(st.integers(min_value=MIN_INTERVAL_SECONDS, max_value=90 * 86400), moments)
def test_an_interval_schedule_next_runs_after_the_moment_asked_about(seconds: int, moment) -> None:
    next_run = Recurrence(every_seconds=seconds).next_after(moment)

    assert moment < next_run <= moment + timedelta(seconds=seconds)


@bounded
@given(
    st.times(),
    st.sampled_from(
        ["", "UTC", "Europe/Rome", "America/New_York", "Asia/Tokyo", "Australia/Lord_Howe"]
    ),
    moments,
)
def test_a_daily_schedule_next_runs_after_the_moment_and_within_a_day_and_an_hour(
    at: time, zone: str, moment
) -> None:
    next_run = Recurrence(daily_at=at, timezone=zone).next_after(moment)

    assert next_run > moment
    # A daylight-saving jump can move a wall-clock time by the length of the jump.
    assert next_run - moment <= timedelta(days=1, hours=1)


@bounded
@given(st.integers(max_value=MIN_INTERVAL_SECONDS - 1))
def test_an_interval_below_the_minimum_is_refused(seconds: int) -> None:
    with pytest.raises(ValueError):
        Recurrence(every_seconds=seconds)


# --- URLs and platforms ---------------------------------------------------------------


@bounded
@given(st.text(max_size=60))
def test_a_setup_link_that_is_not_https_is_refused(url: str) -> None:
    from pathlib import Path

    from infrastructure.integrations.yaml_catalog import _step

    try:
        step = _step(Path("plugin.yaml"), {"text": "Open this", "url": url})
    except ConfigurationError:
        assert url.strip() and not url.strip().startswith("https://")
        return
    assert step.url == "" or step.url.startswith("https://")


@bounded
@given(st.text(max_size=60))
def test_a_database_url_is_normalised_idempotently(url: str) -> None:
    from app.config.settings import normalise_database_url

    once = normalise_database_url(url)

    assert normalise_database_url(once) == once


@bounded
@given(
    st.sampled_from(["Darwin", "Windows", "Linux", "FreeBSD", ""]),
    st.text(alphabet="0123456789.-abc", max_size=12),
    st.sampled_from(["arm64", "x86_64", "AMD64", "aarch64", "i386", ""]),
)
def test_a_platform_check_never_raises_and_always_explains_a_refusal(
    system, release, machine
) -> None:
    verdict = check(system, release, machine)

    assert verdict.supported or verdict.message
