"""Discriminated result states; these describe the existing wire dictionaries."""

from typing import Any, Literal, NotRequired, TypedDict

Event = dict[str, Any]


class ValidationRejected(TypedDict):
    valid: Literal[False]
    errors: list[str]


class ValidatedTimeline(TypedDict):
    valid: Literal[True]
    events: list[Event]


class SearchMatches(TypedDict):
    valid: Literal[True]
    events: list[Event]
    count: int


class PhaseEvidence(TypedDict):
    count: int
    events: list[Event]


class PhaseSummary(TypedDict):
    valid: Literal[True]
    phases: dict[str, PhaseEvidence]


class CsvCreated(TypedDict):
    ok: Literal[True]
    path: str
    event_count: int
    import_verified: Literal[False]
    warnings: NotRequired[list[str]]


class CsvRejected(TypedDict):
    ok: Literal[False]
    errors: list[str]
    warnings: NotRequired[list[str]]


TimelineResult = ValidatedTimeline | ValidationRejected
SearchResult = SearchMatches | ValidationRejected
SummaryResult = PhaseSummary | ValidationRejected
CsvResult = CsvCreated | CsvRejected
