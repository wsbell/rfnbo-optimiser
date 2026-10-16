"""Cross-block validation of a plant config.

Each rule is a named function returning :class:`Issue` records, so the editor can show
every problem at once and tests can target one rule at a time.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from rfnbo.config.schema import (
    ELECTRICITY,
    ConsignmentOutput,
    DistributionBlock,
    Flow,
    InputBlock,
    PlantConfig,
    ProcessingUnit,
    StorageUnit,
)


@dataclass(frozen=True)
class Issue:
    rule: str
    message: str
    ref: str | None = None  # block or flow id

    def __str__(self) -> str:
        where = f" [{self.ref}]" if self.ref else ""
        return f"{self.rule}{where}: {self.message}"


class ConfigError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))


# Permitted (source kind, target kind) pairs. Flows run one way; product always passes
# through storage between units so that it enters an RFNBO ledger.
ALLOWED_CONNECTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("input", "processing_unit"),
        ("processing_unit", "storage_unit"),
        ("storage_unit", "processing_unit"),
        ("storage_unit", "distribution"),
        ("distribution", "consignment_output"),
    }
)

Rule = Callable[[PlantConfig], Iterator[Issue]]


def _ids(cfg: PlantConfig) -> set[str]:
    return {b.id for b in cfg.blocks}


def _resolvable(cfg: PlantConfig) -> list[Flow]:
    ids = _ids(cfg)
    return [f for f in cfg.flows if f.source in ids and f.target in ids]


def unique_ids(cfg: PlantConfig) -> Iterator[Issue]:
    seen: set[str] = set()
    for item_id in [b.id for b in cfg.blocks] + [f.id for f in cfg.flows]:
        if item_id in seen:
            yield Issue("unique_ids", f"id {item_id!r} is used more than once", item_id)
        seen.add(item_id)


def flow_endpoints(cfg: PlantConfig) -> Iterator[Issue]:
    ids = _ids(cfg)
    for f in cfg.flows:
        for end in (f.source, f.target):
            if end not in ids:
                yield Issue("flow_endpoints", f"flow refers to unknown block {end!r}", f.id)
        if f.source == f.target:
            yield Issue("flow_endpoints", "flow connects a block to itself", f.id)


def connection_types(cfg: PlantConfig) -> Iterator[Issue]:
    for f in _resolvable(cfg):
        pair = (cfg.block(f.source).kind, cfg.block(f.target).kind)
        if pair not in ALLOWED_CONNECTIONS:
            yield Issue("connection_types", f"a {pair[0]} cannot flow into a {pair[1]}", f.id)


def no_orphans(cfg: PlantConfig) -> Iterator[Issue]:
    connected = {f.source for f in cfg.flows} | {f.target for f in cfg.flows}
    for b in cfg.blocks:
        if b.id not in connected:
            yield Issue("no_orphans", "block has no flows", b.id)


def flow_materials(cfg: PlantConfig) -> Iterator[Issue]:
    """Each flow carries the material its source provides and its target accepts."""
    for f in _resolvable(cfg):
        src, tgt = cfg.block(f.source), cfg.block(f.target)
        provided = src.material if isinstance(src, InputBlock) else getattr(src, "product", None)
        if isinstance(src, DistributionBlock):
            provided = _distribution_product(cfg, src.id)
        if provided is not None and f.material != provided:
            yield Issue(
                "flow_materials",
                f"flow carries {f.material!r} but {src.id!r} provides {provided!r}",
                f.id,
            )
        if isinstance(tgt, StorageUnit) and f.material != tgt.product:
            yield Issue(
                "flow_materials",
                f"flow carries {f.material!r} into storage of {tgt.product!r}",
                f.id,
            )


def _distribution_product(cfg: PlantConfig, dist_id: str) -> str | None:
    feeds = {f.material for f in cfg.flows_into(dist_id)}
    return feeds.pop() if len(feeds) == 1 else None


def distribution_single_product(cfg: PlantConfig) -> Iterator[Issue]:
    for d in cfg.blocks_of(DistributionBlock):
        feeds = {f.material for f in cfg.flows_into(d.id)}
        if len(feeds) > 1:
            yield Issue(
                "distribution_single_product",
                f"distribution block receives several products: {sorted(feeds)}",
                d.id,
            )


def unit_series(cfg: PlantConfig) -> Iterator[Issue]:
    """Every unit has a production series; units using electricity meter it."""
    for u in cfg.blocks_of(ProcessingUnit):
        if not u.production_series:
            yield Issue("unit_series", "processing unit has no production series", u.id)
        if (
            any(f.material == ELECTRICITY for f in cfg.flows_into(u.id))
            and not u.electricity_series
        ):
            yield Issue("unit_series", "unit takes electricity but has no electricity series", u.id)


def flow_series(cfg: PlantConfig) -> Iterator[Issue]:
    """Non-electricity inputs into units and transfers out of storage are metered."""
    for f in _resolvable(cfg):
        src, tgt = cfg.block(f.source), cfg.block(f.target)
        needs = (
            isinstance(src, InputBlock)
            and f.material != ELECTRICITY
            and isinstance(tgt, ProcessingUnit)
        ) or isinstance(src, StorageUnit)
        if needs and not f.series:
            yield Issue("flow_series", "flow needs a metered series (kg per interval)", f.id)
        if isinstance(src, InputBlock) and f.material == ELECTRICITY and f.series:
            yield Issue(
                "flow_series",
                "electricity flows carry no series; meter total kWh on the unit",
                f.id,
            )


def storage_series(cfg: PlantConfig) -> Iterator[Issue]:
    for s in cfg.blocks_of(StorageUnit):
        if not s.inventory_series:
            yield Issue("storage_series", "storage unit has no inventory series", s.id)


def unit_outputs(cfg: PlantConfig) -> Iterator[Issue]:
    """Each unit sends its whole production to one storage (split outputs not supported)."""
    for u in cfg.blocks_of(ProcessingUnit):
        n = len(cfg.flows_out_of(u.id))
        if n != 1:
            yield Issue(
                "unit_outputs",
                f"processing unit needs exactly one product output, has {n}",
                u.id,
            )


def unit_feeds(cfg: PlantConfig) -> Iterator[Issue]:
    """RFNBO share comes from renewable power (LHV units) or from RFNBO feed (others)."""
    for u in cfg.blocks_of(ProcessingUnit):
        into = cfg.flows_into(u.id)
        if u.electricity_adds_to_lhv:
            renewable = [
                f
                for f in into
                if isinstance(b := _block_or_none(cfg, f.source), InputBlock) and b.renewable
            ]
            if not renewable:
                yield Issue(
                    "unit_feeds",
                    "unit where electricity adds to LHV needs a renewable power input",
                    u.id,
                )
        elif not any(isinstance(_block_or_none(cfg, f.source), StorageUnit) for f in into):
            yield Issue(
                "unit_feeds",
                "downstream unit needs a product feed from storage",
                u.id,
            )


def _block_or_none(cfg: PlantConfig, block_id: str) -> object | None:
    try:
        return cfg.block(block_id)
    except KeyError:
        return None


def electricity_inputs(cfg: PlantConfig) -> Iterator[Issue]:
    """One renewable pool and one constant non-renewable factor (scope doc decisions)."""
    elec = [i for i in cfg.blocks_of(InputBlock) if i.material == ELECTRICITY]
    for renewable, label in ((True, "renewable"), (False, "non-renewable")):
        found = [i for i in elec if i.renewable is renewable]
        if len(found) > 1:
            yield Issue(
                "electricity_inputs",
                f"more than one {label} electricity input: {[i.id for i in found]}",
                found[1].id,
            )
    for i in elec:
        if not i.renewable and len(i.emission_factors) > 1:
            yield Issue(
                "electricity_inputs",
                "the non-renewable electricity emission factor is one constant",
                i.id,
            )
    if any(i.renewable for i in elec) and not any(not i.renewable for i in elec):
        yield Issue(
            "electricity_inputs",
            "a renewable power input needs a non-renewable input for unmatched kWh",
        )


def emission_factor_coverage(cfg: PlantConfig) -> Iterator[Issue]:
    for i in cfg.blocks_of(InputBlock):
        try:
            i.emission_factor_at(cfg.effective_from)
        except LookupError:
            yield Issue(
                "emission_factor_coverage",
                f"no emission factor valid on effective_from {cfg.effective_from}",
                i.id,
            )


def consignment_links(cfg: PlantConfig) -> Iterator[Issue]:
    for c in cfg.blocks_of(ConsignmentOutput):
        target = _block_or_none(cfg, c.distribution_block)
        if not isinstance(target, DistributionBlock):
            yield Issue(
                "consignment_links",
                f"linked distribution block {c.distribution_block!r} does not exist",
                c.id,
            )
            continue
        if target.customer != c.customer:
            yield Issue(
                "consignment_links",
                f"customer {c.customer!r} differs from distribution customer {target.customer!r}",
                c.id,
            )
        sources = {f.source for f in cfg.flows_into(c.id)}
        if sources and sources != {target.id}:
            yield Issue(
                "consignment_links",
                f"fed by {sorted(sources)} but linked to {target.id!r}",
                c.id,
            )


def no_cycles(cfg: PlantConfig) -> Iterator[Issue]:
    """Flows run one way: no recycle from storage back upstream (scope doc assumption)."""
    graph: dict[str, list[str]] = {b.id: [] for b in cfg.blocks}
    for f in _resolvable(cfg):
        graph[f.source].append(f.target)
    state: dict[str, int] = {}  # 1 = on stack, 2 = done

    def visit(node: str) -> str | None:
        state[node] = 1
        for nxt in graph[node]:
            if state.get(nxt) == 1:
                return nxt
            if nxt not in state and (hit := visit(nxt)):
                return hit
        state[node] = 2
        return None

    for node in sorted(graph):
        if node not in state and (hit := visit(node)):
            yield Issue("no_cycles", "flows form a cycle", hit)
            return


def layout_blocks(cfg: PlantConfig) -> Iterator[Issue]:
    """Diagram positions refer only to blocks that exist."""
    ids = _ids(cfg)
    for block_id in cfg.layout:
        if block_id not in ids:
            yield Issue("layout_blocks", "position given for a block that does not exist", block_id)


RULES: tuple[Rule, ...] = (
    unique_ids,
    flow_endpoints,
    connection_types,
    no_orphans,
    flow_materials,
    distribution_single_product,
    unit_series,
    flow_series,
    storage_series,
    unit_outputs,
    unit_feeds,
    electricity_inputs,
    emission_factor_coverage,
    consignment_links,
    no_cycles,
    layout_blocks,
)


def validate(cfg: PlantConfig) -> list[Issue]:
    return [issue for rule in RULES for issue in rule(cfg)]


def load_config(path: str | Path) -> PlantConfig:
    """Parse and validate a plant config file; raise :class:`ConfigError` on any issue."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        cfg = PlantConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            [Issue("schema", e["msg"], ".".join(str(p) for p in e["loc"])) for e in exc.errors()]
        ) from exc
    issues = validate(cfg)
    if issues:
        raise ConfigError(issues)
    return cfg
