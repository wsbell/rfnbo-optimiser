"""The calculator: config + data + decisions -> every reported number, plus rule checks.

Pure and deterministic. The optimiser only proposes decision sets; everything reported comes
from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rfnbo.calc.batch import BatchResult, compute_batches
from rfnbo.calc.check import CheckResult, run_checks
from rfnbo.calc.consign import ConsignmentResult, compute_consignments
from rfnbo.calc.ledger import (
    LedgerLine,
    LedgerResult,
    Reconciliation,
    batch_storages,
    compute_ledgers,
    compute_reconciliation,
    feed_storage_for,
)
from rfnbo.calc.weights import StorageWeight, storage_weights
from rfnbo.config.schema import PlantConfig
from rfnbo.data.quality import Frame
from rfnbo.model.entities import Decisions


@dataclass(frozen=True)
class RunResult:
    config: PlantConfig
    frame: Frame
    decisions: Decisions
    batches: dict[str, BatchResult]
    storage_of: dict[str, str]  # a batch's (first) storage
    ledgers: list[LedgerResult]
    reconciliation: list[Reconciliation]
    consignments: list[ConsignmentResult]
    storage_weights: dict[str, StorageWeight] = field(default_factory=dict)
    storages_of: dict[str, tuple[str, ...]] = field(default_factory=dict)  # every storage
    checks: list[CheckResult] = field(default_factory=list)

    def feed_storage(self, source: str, target: str) -> str:
        """The storage a feed allocation from ``source`` into ``target`` draws on."""
        return feed_storage_for(self.config, self.storages_of, source, self.batches[target].unit)

    def line(self, storage: str, batch: str) -> LedgerLine | None:
        return next((x for x in self.ledger(storage).lines if x.batch == batch), None)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def ledger(self, storage: str) -> LedgerResult:
        return next(x for x in self.ledgers if x.storage == storage)

    def consignment(self, consignment_id: str) -> ConsignmentResult:
        return next(c for c in self.consignments if c.id == consignment_id)

    @property
    def carried_value_MJ(self) -> float | None:
        """Objective 2: compliant RFNBO carried forward, in MJ of final product (storage
        weights). None if a storage carrying RFNBO has no known weight."""
        total = 0.0
        for led in self.ledgers:
            mj = sum(x.carried_kg * x.lhv_MJ_per_kg for x in led.lines if x.compliant)
            if mj <= 0:
                continue
            w = self.storage_weights[led.storage].weight
            if w is None:
                return None
            total += mj * w
        return total


def calculate(cfg: PlantConfig, frame: Frame, decisions: Decisions) -> RunResult:
    batches = compute_batches(cfg, frame, decisions)
    storages_of = batch_storages(cfg, batches)
    ledgers = compute_ledgers(cfg, frame, batches, decisions)
    lines: dict[tuple[str, str], LedgerLine] = {
        (ledger.storage, x.batch): x for ledger in ledgers for x in ledger.lines
    }
    consignments = compute_consignments(cfg, decisions, lines)
    run = RunResult(
        config=cfg,
        frame=frame,
        decisions=decisions,
        batches=batches,
        storage_of={b: s[0] for b, s in storages_of.items()},
        storages_of=storages_of,
        ledgers=ledgers,
        reconciliation=compute_reconciliation(cfg, frame),
        consignments=consignments,
        storage_weights=storage_weights(cfg, frame),
    )
    run.checks.extend(run_checks(run))
    return run
