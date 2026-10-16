"""The optimisation pipeline (build plan §6) and the objective values it reports.

    stage A (batching) → stage B (feed) → stage C (consignments) → write-off rule → calculator

Stage C's carried shares feed back into stage B's second objective for a few rounds; the best
round by the lexicographic objectives is kept. If a consignment cannot be met, stage B gives
up RFNBO output step by step (objective 1 outranks objective 2) until it can, or reports the
shortfall. Every number reported comes from the calculator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rfnbo.calc import RunResult, calculate
from rfnbo.calc.batch import unit_order
from rfnbo.config.schema import PlantConfig
from rfnbo.data.quality import Frame
from rfnbo.model.entities import ConsignmentSpec, Decisions, FeedAllocation
from rfnbo.optimise import allocate
from rfnbo.optimise.baseline import baseline_decisions
from rfnbo.optimise.batching import EPS_MJ, BatchingResult, optimise_batching

ROUNDS = 4
RELAX = (1.0, 0.999, 0.99, 0.97, 0.95, 0.9, 0.8, 0.6, 0.4, 0.2, 0.0)


@dataclass(frozen=True)
class Objectives:
    """The §2.8 objectives, read from a calculated run."""

    consignments_met: bool
    final_rfnbo_kg: float  # compliant RFNBO of the final product
    carried_emissions_g: float  # all storages, carried forward at quarter end
    carried_ghg: dict[str, float | None]  # per storage, gCO2e/MJ
    batches: int

    def key(self) -> tuple[bool, float, float, int]:
        """Larger is better: met, more RFNBO, less carried GHG, fewer batches."""
        return (
            self.consignments_met,
            round(self.final_rfnbo_kg, 3),
            -round(self.carried_emissions_g, 0),
            -self.batches,
        )


def objectives(run: RunResult) -> Objectives:
    final = allocate.final_units(run.config)
    cons_checks = [c for c in run.checks if c.name.startswith(("consignment", "aggregated"))]
    carried = sum(
        x.carried_kg * x.lhv_MJ_per_kg * x.ghg_gCO2e_per_MJ
        for led in run.ledgers
        for x in led.lines
    )
    return Objectives(
        consignments_met=all(c.passed for c in cons_checks),
        final_rfnbo_kg=allocate.batch_rfnbo(run.batches, final),
        carried_emissions_g=carried,
        carried_ghg={led.storage: led.carried_ghg_gCO2e_per_MJ for led in run.ledgers},
        batches=len(run.batches),
    )


@dataclass(frozen=True)
class Outcome:
    decisions: Decisions
    run: RunResult
    objectives: Objectives


@dataclass(frozen=True)
class Bound:
    """Upper bound on final RFNBO for the chosen batching: stage A's relaxation (blend
    limit over the whole period, feed movable between months)."""

    upper_kg: float
    achieved_kg: float

    @property
    def gap(self) -> float:
        return 0.0 if self.upper_kg <= 0 else max(0.0, 1 - self.achieved_kg / self.upper_kg)


@dataclass
class OptimisationResult:
    optimised: Outcome
    baseline: Outcome
    batching: BatchingResult
    shortfalls: list[allocate.Shortfall] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.shortfalls

    def bound(self) -> Bound | None:
        """Gap of the final RFNBO to stage A's relaxation (single downstream stage only)."""
        run = self.optimised.run
        final = allocate.final_units(run.config)
        if len(self.batching.units) != 1 or len(final) != 1:
            return None
        down = [b for b in run.batches.values() if b.unit == final[0]]
        consumed = sum(b.feed_consumed_MJ for b in down)
        if consumed <= 0:
            return None
        product = sum(b.product_kg for b in down)
        upper = self.batching.units[0].score.feedable_MJ * product / consumed
        return Bound(upper, self.optimised.objectives.final_rfnbo_kg)


def _assemble(cfg: PlantConfig, frame: Frame, decisions: Decisions) -> Outcome:
    """Add the write-offs the rule requires and calculate."""
    first = calculate(cfg, frame, decisions)
    full = decisions.model_copy(update={"write_offs": allocate.write_offs(first)})
    run = calculate(cfg, frame, full)
    return Outcome(full, run, objectives(run))


def _feed_all(
    cfg: PlantConfig,
    frame: Frame,
    decisions: Decisions,
    weights: dict[str, dict[str, float]],
    floor: float | None,
    log: list[str],
) -> Decisions:
    """Stage B for every downstream unit, upstream first."""
    for unit in allocate.downstream_units(cfg, unit_order(cfg)):
        current = calculate(cfg, frame, decisions)
        plan = allocate.stage_b(cfg, current, unit, weights.get(unit.id), floor)
        log.append(
            f"stage B {unit.id}: {plan.status}, RFNBO output {plan.rfnbo_kg:,.1f} kg "
            f"from {len(plan.feed)} allocations"
        )
        kept = [
            a
            for a in decisions.feed
            if a.target not in {b.id for b in current.batches.values() if b.unit == unit.id}
        ]
        decisions = decisions.model_copy(update={"feed": kept + plan.feed})
    return decisions


def optimise(
    cfg: PlantConfig,
    frame: Frame,
    consignments: Sequence[ConsignmentSpec],
    batching: BatchingResult | None = None,
    batch_penalty_MJ: float = EPS_MJ,
) -> OptimisationResult:
    log: list[str] = []
    batching = batching or optimise_batching(cfg, frame, eps=batch_penalty_MJ)
    for ub in batching.units:
        log.append(
            f"stage A {ub.unit}: {ub.score.batches} batches, mu {ub.mu}, "
            f"feedable {ub.score.feedable_MJ:,.0f} MJ"
        )
    base = Decisions(batches=batching.batches, consignments=list(consignments))
    final_units = allocate.final_units(cfg)

    best: Outcome | None = None
    shortfalls: list[allocate.Shortfall] = []
    downstream = allocate.downstream_units(cfg, unit_order(cfg))
    top: float | None = None
    first_shortfalls: list[allocate.Shortfall] | None = None
    for relax in RELAX:
        floor: float | None = None
        weights: dict[str, dict[str, float]] = {}
        rounds = ROUNDS
        if relax < 1.0:
            # Consignments first: give up RFNBO output and aim for the lowest-GHG product.
            if top is None:
                unconstrained = _feed_all(cfg, frame, base, {}, None, [])
                top = allocate.batch_rfnbo(
                    calculate(cfg, frame, unconstrained).batches, final_units
                )
            floor = top * relax
            weights = {
                u.id: {b.id: 1.0 for b in batching.batches if b.unit == u.id} for u in downstream
            }
            rounds = 1
            log.append(f"relaxing RFNBO output to {relax:.1%} ({floor:,.1f} kg)")
        round_best: Outcome | None = None
        for rnd in range(rounds):
            fed = _feed_all(cfg, frame, base, weights, floor, log)
            plan = allocate.stage_c(cfg, calculate(cfg, frame, fed), consignments)
            if not plan.feasible:
                if first_shortfalls is None:
                    first_shortfalls = plan.shortfalls  # diagnosed at full RFNBO output
                log.append(f"stage C infeasible: {[s.reason for s in plan.shortfalls]}")
                break
            outcome = _assemble(
                cfg, frame, fed.model_copy(update={"aggregated_batches": plan.aggregated})
            )
            log.append(
                f"round {rnd + 1}: RFNBO {outcome.objectives.final_rfnbo_kg:,.1f} kg, "
                f"carried {outcome.objectives.carried_emissions_g:,.0f} g"
            )
            if round_best is None or outcome.objectives.key() > round_best.objectives.key():
                round_best = outcome
            elif rnd > 0:
                break
            weights = {u.id: allocate.carried_weights(outcome.run, u.id) for u in downstream}
        if round_best is not None:
            best, shortfalls = round_best, []
            break

    if best is None:
        shortfalls = first_shortfalls or []
        unmet = {x.consignment for x in shortfalls}
        keep = [c for c in consignments if c.id not in unmet]
        log.append(f"consignments that cannot be met: {sorted(unmet)}; optimising the rest")
        if keep and len(keep) < len(consignments):
            rest = optimise(cfg, frame, keep, batching, batch_penalty_MJ)
            best = rest.optimised
            shortfalls = shortfalls + rest.shortfalls
            log += rest.log
        else:
            fed = _feed_all(cfg, frame, base.model_copy(update={"consignments": []}), {}, None, log)
            best = _assemble(cfg, frame, fed)

    base_decisions = baseline_decisions(cfg, frame, consignments)
    baseline = _assemble(cfg, frame, base_decisions)
    return OptimisationResult(best, baseline, batching, shortfalls, log)


def feed_summary(feed: Sequence[FeedAllocation]) -> dict[str, float]:
    out: dict[str, float] = {}
    for a in feed:
        out[a.target] = out.get(a.target, 0.0) + a.kg
    return out
