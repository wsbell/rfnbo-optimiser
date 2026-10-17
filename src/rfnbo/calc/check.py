"""Every rule as a named check with a regulation or scope reference.

Checks never change numbers; they report whether a calculated decision set obeys the rules.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from rfnbo.calc.ledger import priority_key
from rfnbo.calc.tolerance import ABS_KG, at_most, close, meets_threshold
from rfnbo.config.schema import InputBlock, ProcessingUnit
from rfnbo.data import calendar as cal

if TYPE_CHECKING:
    from rfnbo.calc.calculator import RunResult

TEMPORAL = "Delegated Regulation (EU) 2023/1184, Art. 6 (temporal correlation)"
THRESHOLD = (
    "Directive (EU) 2018/2001, Art. 25(2); Delegated Regulation (EU) 2023/1185 "
    "(70% saving against 94 gCO2e/MJ)"
)
MASS_BALANCE = "Directive (EU) 2018/2001, Art. 30(1) (mass balance); certification scheme rules"
SCOPE_BATCH = "Scope document: batching (batches cover every interval once)"
SCOPE_MATCHING = "Scope document: renewable power matching (shared pool)"
SCOPE_WRITE_OFF = "Scope document: quarter-end write-off (highest GHG first, newest on ties)"
SCOPE_CARRY = (
    "Scope document decisions log: non-compliant RFNBO cannot be carried between quarters "
    "(2026-10-17)"
)
SCOPE_AGGREGATION = "Scope document: consignment aggregation (one production month)"
SCOPE_CONSIGNMENT = "Scope document: consignment requirements"


@dataclass(frozen=True)
class CheckResult:
    name: str
    reference: str
    subject: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"{status} {self.name} [{self.subject}]: {self.detail}"


Check = Callable[["RunResult"], Iterator[CheckResult]]
CHECKS: list[Check] = []


def check(fn: Check) -> Check:
    CHECKS.append(fn)
    return fn


def _r(name: str, ref: str, subject: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(name, ref, subject, bool(ok), detail)


@check
def batch_cover(run: RunResult) -> Iterator[CheckResult]:
    """Each unit's batches tile the period: contiguous, no overlap, no gap."""
    n = len(run.frame)
    for unit in run.config.blocks_of(ProcessingUnit):
        spans = sorted(
            (b.start_index, b.end_index, b.id) for b in run.batches.values() if b.unit == unit.id
        )
        pos, problems = 0, []
        for s, e, bid in spans:
            if s != pos:
                problems.append(f"{'overlap' if s < pos else 'gap'} before {bid}")
            pos = max(pos, e)
        if pos != n:
            problems.append(f"intervals {pos}..{n - 1} not in any batch")
        yield _r(
            "batch_cover",
            SCOPE_BATCH,
            unit.id,
            not problems,
            "; ".join(problems[:5]) or f"{len(spans)} batches cover {n} intervals",
        )


@check
def batch_within_month(run: RunResult) -> Iterator[CheckResult]:
    tz = run.frame.timezone
    for b in run.batches.values():
        last = cal.month_key(run.frame.index[b.end_index - 1], tz)
        yield _r(
            "batch_within_month", TEMPORAL, b.id, last == b.month, f"{b.month} to {last} ({tz})"
        )


@check
def batch_max_length(run: RunResult) -> Iterator[CheckResult]:
    """From the switch date, units where electricity adds to LHV batch within a clock hour."""
    tz, switch = run.frame.timezone, run.config.settings.hourly_correlation_from
    for b in run.batches.values():
        unit = run.config.block(b.unit)
        assert isinstance(unit, ProcessingUnit)
        stamps = run.frame.index[b.start_index : b.end_index]
        if not unit.electricity_adds_to_lhv or not any(
            cal.hourly_correlation_applies(t, tz, switch) for t in stamps
        ):
            continue
        hours = {cal.hour_key(t, tz) for t in stamps}
        yield _r(
            "batch_max_length",
            TEMPORAL,
            b.id,
            len(hours) == 1,
            f"{len(stamps)} intervals over {len(hours)} clock hour(s)",
        )


@check
def renewable_pool(run: RunResult) -> Iterator[CheckResult]:
    """Renewable kWh allocated to units never exceeds what was available in an interval."""
    if not run.decisions.renewable_allocations:
        return
    given = {r.unit: np.asarray(r.kWh) for r in run.decisions.renewable_allocations}
    for src in run.config.blocks_of(InputBlock):
        if not src.renewable:
            continue
        units = [f.target for f in run.config.flows_out_of(src.id) if f.target in given]
        if not units:
            continue
        total = sum(given[u] for u in units)
        available = run.frame.series[src.series or ""]
        over = np.flatnonzero(total > available + ABS_KG)
        neg = any((given[u] < 0).any() for u in units)
        yield _r(
            "renewable_pool",
            SCOPE_MATCHING,
            src.id,
            over.size == 0 and not neg,
            f"{over.size} interval(s) over-allocated" + ("; negative allocation" if neg else ""),
        )


@check
def feed_source_rfnbo(run: RunResult) -> Iterator[CheckResult]:
    """Feed comes from an RFNBO batch held in a storage that feeds the target unit."""
    for a in run.decisions.feed:
        src, tgt = run.batches[a.source], run.batches[a.target]
        store = run.storage_of[a.source]
        feeds = {f.target for f in run.config.flows_out_of(store)}
        ok = src.is_rfnbo and tgt.unit in feeds
        yield _r(
            "feed_source_rfnbo",
            MASS_BALANCE,
            f"{a.source}->{a.target}",
            ok,
            f"{a.kg} kg; source RFNBO {src.rfnbo_kg:.6g} kg in {store}",
        )


@check
def feed_within_consumption(run: RunResult) -> Iterator[CheckResult]:
    for b in run.batches.values():
        if b.kind != "downstream":
            continue
        yield _r(
            "feed_within_consumption",
            MASS_BALANCE,
            b.id,
            at_most(b.rfnbo_share, 1.0),
            f"RFNBO feed {b.rfnbo_feed_kg:.6f} of {b.feed_consumed_kg:.6f} kg consumed",
        )


@check
def batch_not_overdrawn(run: RunResult) -> Iterator[CheckResult]:
    for ledger in run.ledgers:
        for x in ledger.lines:
            used = x.out_feed_kg + x.out_consignment_kg + x.write_off_kg
            yield _r(
                "batch_not_overdrawn",
                MASS_BALANCE,
                x.batch,
                at_most(used, x.in_kg),
                f"{used:.6f} kg used of {x.in_kg:.6f} kg",
            )


@check
def ledger_non_negative(run: RunResult) -> Iterator[CheckResult]:
    for ledger in run.ledgers:
        yield _r(
            "ledger_non_negative",
            MASS_BALANCE,
            f"{ledger.storage} {ledger.quarter}",
            ledger.carried_kg >= -ABS_KG,
            f"closing ledger {ledger.carried_kg:.6f} kg",
        )


@check
def ledger_within_physical(run: RunResult) -> Iterator[CheckResult]:
    for ledger in run.ledgers:
        yield _r(
            "ledger_within_physical",
            MASS_BALANCE,
            f"{ledger.storage} {ledger.quarter}",
            at_most(ledger.carried_kg, ledger.physical_kg),
            f"closing ledger {ledger.carried_kg:.6f} kg, measured {ledger.physical_kg:.6f} kg",
        )


@check
def noncompliant_not_carried(run: RunResult) -> Iterator[CheckResult]:
    """No non-compliant RFNBO is carried into the next quarter."""
    for ledger in run.ledgers:
        carried = sum(x.carried_kg for x in ledger.lines if not x.compliant)
        yield _r(
            "noncompliant_not_carried",
            SCOPE_CARRY,
            f"{ledger.storage} {ledger.quarter}",
            carried <= ABS_KG,
            f"non-compliant RFNBO carried {carried:.6f} kg",
        )


@check
def write_off_amount(run: RunResult) -> Iterator[CheckResult]:
    """Exactly what the rules require is written off: all non-compliant RFNBO, and at least the
    excess over physical inventory. No more, no less."""
    for ledger in run.ledgers:
        yield _r(
            "write_off_amount",
            SCOPE_WRITE_OFF,
            f"{ledger.storage} {ledger.quarter}",
            close(ledger.write_off_kg, ledger.required_write_off_kg),
            f"written off {ledger.write_off_kg:.6f} kg, required {ledger.required_write_off_kg:.6f}"
            f" kg (excess {ledger.excess_kg:.6f}, non-compliant "
            f"{ledger.noncompliant_closing_kg:.6f})",
        )


@check
def write_off_order(run: RunResult) -> Iterator[CheckResult]:
    """Write-off takes non-compliant batches first, then the highest GHG; equal GHG, newest
    month first."""
    for ledger in run.ledgers:
        groups: dict[tuple[bool, float, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for x in ledger.lines:
            g = groups[priority_key(x.ghg_gCO2e_per_MJ, x.month, x.compliant)]
            g[0] += x.closing_before_kg
            g[1] += x.write_off_kg
        problem = ""
        partial_seen = None
        for key in sorted(groups, reverse=True):
            closing, written = groups[key]
            if partial_seen is not None and written > ABS_KG:
                problem = (
                    f"{written:.6f} kg written off at GHG {key[1]} ({key[2]}) while "
                    f"higher-priority GHG {partial_seen[1]} ({partial_seen[2]}) is carried"
                )
                break
            if not close(written, closing) and partial_seen is None:
                partial_seen = key
        yield _r(
            "write_off_order",
            SCOPE_WRITE_OFF,
            f"{ledger.storage} {ledger.quarter}",
            not problem,
            problem or "highest GHG written off first",
        )


@check
def aggregated_single_month(run: RunResult) -> Iterator[CheckResult]:
    for c in run.consignments:
        for a in c.aggregated:
            yield _r(
                "aggregated_single_month",
                SCOPE_AGGREGATION,
                a.id,
                len(a.months) == 1,
                f"production month(s) {', '.join(a.months)}",
            )


@check
def aggregated_compliant_only(run: RunResult) -> Iterator[CheckResult]:
    """Non-compliant RFNBO sub-batches can never be aggregated into a consignment."""
    for c in run.consignments:
        for a in c.aggregated:
            bad = [p.batch for p in a.parts if not p.compliant]
            yield _r(
                "aggregated_compliant_only",
                THRESHOLD,
                a.id,
                not bad,
                f"non-compliant: {bad}" if bad else "all sub-batches ≤ threshold",
            )


@check
def consignment_source(run: RunResult) -> Iterator[CheckResult]:
    for c in run.consignments:
        wrong = sorted(
            {p.batch for a in c.aggregated for p in a.parts if p.storage != c.source_storage}
        )
        yield _r(
            "consignment_source",
            MASS_BALANCE,
            c.id,
            not wrong,
            f"not in {c.source_storage}: {wrong}" if wrong else f"from {c.source_storage}",
        )


@check
def consignment_quarter(run: RunResult) -> Iterator[CheckResult]:
    for c in run.consignments:
        q = f"{c.ship_date.year:04d}-Q{(c.ship_date.month - 1) // 3 + 1}"
        yield _r(
            "consignment_quarter",
            MASS_BALANCE,
            c.id,
            q == run.frame.quarter,
            f"ships {c.ship_date} ({q}); ledger {run.frame.quarter}",
        )


@check
def consignment_volume(run: RunResult) -> Iterator[CheckResult]:
    for c in run.consignments:
        yield _r(
            "consignment_volume",
            SCOPE_CONSIGNMENT,
            c.id,
            close(c.allocated_kg, c.volume_kg),
            f"allocated {c.allocated_kg:.6f} kg of {c.volume_kg:.6f} kg",
        )


@check
def consignment_target(run: RunResult) -> Iterator[CheckResult]:
    for c in run.consignments:
        if c.target_gCO2e_per_MJ is None:
            continue
        ghg = c.shipped_ghg_gCO2e_per_MJ
        ok = ghg is not None and meets_threshold(ghg, c.target_gCO2e_per_MJ)
        yield _r(
            "consignment_target",
            SCOPE_CONSIGNMENT,
            c.id,
            ok,
            f"shipped {ghg} vs target {c.target_gCO2e_per_MJ} gCO2e/MJ",
        )


@check
def consignment_threshold(run: RunResult) -> Iterator[CheckResult]:
    for c in run.consignments:
        ghg = c.shipped_ghg_gCO2e_per_MJ
        ok = ghg is not None and meets_threshold(ghg, c.threshold_gCO2e_per_MJ)
        yield _r(
            "consignment_threshold",
            THRESHOLD,
            c.id,
            ok,
            f"shipped {ghg} vs {c.threshold_gCO2e_per_MJ:.6g} gCO2e/MJ",
        )


def run_checks(run: RunResult) -> list[CheckResult]:
    return [r for fn in CHECKS for r in fn(run)]
