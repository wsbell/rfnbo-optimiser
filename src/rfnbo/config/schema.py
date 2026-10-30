"""Pydantic models for the plant block flow diagram (build plan §2.1).

The saved diagram is the single definition of the plant. It is versioned with an
effective-from date so that a plant change mid-year does not alter past results.

Structural rules that span several blocks (orphans, flow materials, cycles, ...)
live in :mod:`rfnbo.config.validate`, so each rule can be reported by name.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ELECTRICITY = "electricity"

QuantityUnit = Literal["kWh", "kg"]
EmissionFactorUnit = Literal["gCO2e/kWh", "gCO2e/kg"]

EF_UNIT_FOR_QUANTITY: dict[str, str] = {"kWh": "gCO2e/kWh", "kg": "gCO2e/kg"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmissionFactor(_Model):
    """An emission factor with its validity start and source reference."""

    value: float = Field(ge=0)
    unit: EmissionFactorUnit
    valid_from: dt.date
    source: str = Field(min_length=1)


class _Block(_Model):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1)


class InputBlock(_Block):
    """A utility or feedstock entering the plant (renewable power, grid power, water, N₂)."""

    kind: Literal["input"] = "input"
    material: str = Field(min_length=1)
    quantity_unit: QuantityUnit
    renewable: bool = False
    emission_factors: list[EmissionFactor] = Field(min_length=1)
    # Renewable power available (kWh per interval). Other inputs are metered per unit,
    # on the flow into that unit.
    series: str | None = None

    @model_validator(mode="after")
    def _check(self) -> InputBlock:
        expected = EF_UNIT_FOR_QUANTITY[self.quantity_unit]
        for ef in self.emission_factors:
            if ef.unit != expected:
                raise ValueError(
                    f"emission factor unit {ef.unit!r} does not match quantity unit "
                    f"{self.quantity_unit!r} (expected {expected!r})"
                )
        dates = [ef.valid_from for ef in self.emission_factors]
        if len(set(dates)) != len(dates):
            raise ValueError("two emission factors share the same valid_from date")
        if self.renewable and self.material != ELECTRICITY:
            raise ValueError("only electricity inputs can be flagged renewable")
        if self.material == ELECTRICITY and self.quantity_unit != "kWh":
            raise ValueError("electricity inputs must be measured in kWh")
        if self.renewable and self.series is None:
            raise ValueError("a renewable power input needs a renewable-available series")
        return self

    def emission_factor_at(self, date: dt.date) -> EmissionFactor:
        """The emission factor in force on ``date`` (latest ``valid_from`` on or before it)."""
        valid = [ef for ef in self.emission_factors if ef.valid_from <= date]
        if not valid:
            raise LookupError(f"input {self.id!r} has no emission factor valid on {date}")
        return max(valid, key=lambda ef: ef.valid_from)


class ProcessingUnit(_Block):
    """Converts inputs to one product (electrolyser, NH₃ synthesis, compression)."""

    kind: Literal["processing_unit"] = "processing_unit"
    product: str = Field(min_length=1)
    lhv_MJ_per_kg: float = Field(gt=0)
    electricity_adds_to_lhv: bool
    fixed_adder_gCO2e_per_MJ: float = Field(ge=0)
    production_series: str | None = None  # kg per interval; required by the validator
    electricity_series: str | None = None  # total kWh consumed per interval


class OpeningLedgerEntry(_Model):
    """A batch carried into the first quarter of a run (manual opening ledger)."""

    batch_id: str = Field(min_length=1)
    rfnbo_kg: float = Field(ge=0)
    ghg_gCO2e_per_MJ: float = Field(ge=0)
    # None when the source record does not state it; never assumed.
    rfnbo_share: float | None = Field(default=None, ge=0, le=1)
    production_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    # Purchased RFNBO keeps its origin across quarters: it is never aggregated with RFNBO the
    # plant produced (build plan §12 decision 5).
    origin: Literal["produced", "purchased"] = "produced"
    # Purchased RFNBO names its supplier: RFNBO from different suppliers is never averaged or
    # aggregated together (build plan §12.1 decision 7).
    supplier: str | None = Field(default=None, min_length=1)

    @field_validator("supplier", mode="before")
    @classmethod
    def _blank_supplier(cls, v: object) -> object:
        return v or None

    @model_validator(mode="after")
    def _supplier(self) -> OpeningLedgerEntry:
        if (self.origin == "purchased") != (self.supplier is not None):
            raise ValueError(
                f"opening ledger {self.batch_id}: purchased RFNBO names its supplier; "
                "RFNBO the plant produced does not"
            )
        return self


class StorageUnit(_Block):
    """Holds product between units and across periods; keeps a quarterly RFNBO ledger.

    A pass-through storage models a direct unit → unit link (build plan §10.2 rule 5): it has
    no inventory meter, its physical inventory is zero, and its RFNBO ledger must close at
    zero each quarter.
    """

    kind: Literal["storage_unit"] = "storage_unit"
    product: str = Field(min_length=1)
    pass_through: bool = False
    inventory_series: str | None = None  # kg at interval end; required unless pass-through
    opening_inventory_kg: float = Field(ge=0)
    opening_ledger: list[OpeningLedgerEntry] = Field(default_factory=list)
    # MJ of final product per MJ carried here, used only when this quarter's data has no
    # downstream flow to measure it from (rfnbo.calc.weights); --opening-from sets it.
    carried_weight_fallback: float | None = Field(default=None, gt=0)

    @field_validator("inventory_series")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _check(self) -> StorageUnit:
        ids = [e.batch_id for e in self.opening_ledger]
        if len(set(ids)) != len(ids):
            raise ValueError("opening ledger repeats a batch id")
        if self.pass_through:
            if self.inventory_series:
                raise ValueError("a pass-through storage has no inventory series")
            if self.opening_inventory_kg or self.opening_ledger:
                raise ValueError("a pass-through storage opens empty (inventory and ledger zero)")
        return self


class RfnboSupply(_Block):
    """RFNBO bought from a third-party supplier (build plan §11.1 rule 1).

    Each flow out of it carries the metered kg delivered per interval into a unit or a
    storage. Delivered product is RFNBO only where a proof of sustainability from this
    supplier is allocated to it; the rest is non-RFNBO.
    """

    kind: Literal["rfnbo_supply"] = "rfnbo_supply"
    supplier: str = Field(min_length=1)
    product: str = Field(min_length=1)
    lhv_MJ_per_kg: float = Field(gt=0)


class DistributionBlock(_Block):
    """Fixed transport and conversion emissions to one customer, applied after shipping."""

    kind: Literal["distribution"] = "distribution"
    customer: str = Field(min_length=1)
    fixed_adder_gCO2e_per_MJ: float = Field(ge=0)


class ConsignmentOutput(_Block):
    """The delivery point a consignment is claimed against."""

    kind: Literal["consignment_output"] = "consignment_output"
    customer: str = Field(min_length=1)
    distribution_block: str = Field(min_length=1)


Block = Annotated[
    InputBlock | ProcessingUnit | StorageUnit | RfnboSupply | DistributionBlock | ConsignmentOutput,
    Field(discriminator="kind"),
]


class Flow(_Model):
    """A one-way connection carrying one material between two blocks."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    source: str
    target: str
    material: str = Field(min_length=1)
    # Metered quantity per interval: other inputs into a unit (kg) or transfers out of
    # storage (kg). Electricity flows carry no series; units meter total electricity.
    series: str | None = None


class Settings(_Model):
    """Reference data and rule settings (scope doc: Inputs, Batching)."""

    fossil_comparator_gCO2e_per_MJ: float = Field(default=94.0, gt=0)
    required_saving: float = Field(default=0.70, gt=0, lt=1)
    # Hourly temporal correlation for units whose electricity adds to product LHV.
    hourly_correlation_from: dt.date = dt.date(2030, 1, 1)
    # Data quality: a storage meter-balance residual above this is flagged (not corrected).
    balance_tolerance_kg_per_interval: float = Field(default=1.0, ge=0)

    @property
    def rfnbo_threshold_gCO2e_per_MJ(self) -> float:
        """28.2 gCO₂e/MJ with the defaults (70% saving against 94)."""
        return self.fossil_comparator_gCO2e_per_MJ * (1.0 - self.required_saving)


class Electricity(_Model):
    """The plant's electricity, as settings rather than blocks (decided 2026-10-30, build plan
    §13): every processing unit meters its total consumption (``electricity_series``); within a
    batch, renewable power available is matched first and the rest is grid power."""

    renewable_available_series: str | None = None  # kWh per interval; None: grid only
    renewable_gCO2e_per_kWh: float = Field(default=0.0, ge=0)
    renewable_source: str = Field(
        default="EU 2023/1185: matched renewable electricity counts as zero", min_length=1
    )
    # Constant for a run; set it again before the next run if it changes.
    grid_gCO2e_per_kWh: float = Field(ge=0)
    grid_source: str = Field(min_length=1)

    @field_validator("renewable_available_series")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v or None


# Blocks and flows the loader adds for the compact form (:func:`expand`); their ids are
# reserved.
RENEWABLE_ID = "renewable_power"
GRID_ID = "grid_power"
LINK_SUFFIX = "__link"  # pass-through storage of a direct link, after the link's flow id
OUT_SUFFIX = "__out"  # its outflow
_DIRECT = {("processing_unit", "processing_unit"), ("processing_unit", "distribution")}


def expand(data: dict[str, Any]) -> dict[str, Any]:
    """The compact plant config (an ``electricity`` section and direct unit links) in the full
    form the engine reads: electricity input blocks with a flow into each unit that meters
    electricity, and a pass-through storage in each direct link (§10.2 rule 5). New blocks and
    flows go after the ones given, so positions in the file still name the same items."""
    blocks = [b for b in data.get("blocks", []) if isinstance(b, dict)]
    flows = [f for f in data.get("flows", []) if isinstance(f, dict)]
    kind = {b.get("id"): b.get("kind") for b in blocks}
    taken = set(kind) | {f.get("id") for f in flows}
    direct = [  # a unit linked to itself is left for flow_endpoints to report
        f
        for f in flows
        if (kind.get(f.get("source")), kind.get(f.get("target"))) in _DIRECT
        and f.get("source") != f.get("target")
    ]
    elec = data.get("electricity")
    if elec is None and not direct:
        return data
    out = dict(data)
    out_blocks, out_flows = list(data.get("blocks", [])), list(data.get("flows", []))

    def reserve(i: str) -> str:
        if i in taken:
            raise ValueError(f"id {i!r} is reserved for the compact plant config")
        taken.add(i)
        return i

    for f in direct:
        fid = str(f.get("id"))
        src = next(b for b in blocks if b.get("id") == f.get("source"))
        link = reserve(fid + LINK_SUFFIX)
        out_blocks.append(
            {
                "kind": "storage_unit",
                "id": link,
                "name": f"Direct link {f.get('source')} → {f.get('target')}",
                "product": src.get("product") or f.get("material", ""),
                "pass_through": True,
                "opening_inventory_kg": 0.0,
            }
        )
        out_flows[out_flows.index(f)] = dict(f, target=link)
        out_flows.append(
            {
                "id": reserve(fid + OUT_SUFFIX),
                "source": link,
                "target": f.get("target"),
                "material": f.get("material", ""),
            }
        )
    if elec is not None:
        if not isinstance(elec, dict):
            raise ValueError("electricity must be an object")
        if any(b.get("kind") == "input" and b.get("material") == ELECTRICITY for b in blocks):
            raise ValueError(
                "electricity is set in the electricity section; remove the electricity input blocks"
            )
        start = data.get("effective_from")
        users = [
            str(b.get("id"))
            for b in blocks
            if b.get("kind") == "processing_unit" and b.get("electricity_series")
        ]
        sources = [
            (
                GRID_ID,
                "Grid electricity",
                False,
                None,
                elec.get("grid_gCO2e_per_kWh"),
                elec.get("grid_source"),
            )
        ]
        if elec.get("renewable_available_series"):
            sources.insert(
                0,
                (
                    RENEWABLE_ID,
                    "Renewable electricity",
                    True,
                    elec["renewable_available_series"],
                    elec.get("renewable_gCO2e_per_kWh", 0.0),
                    elec.get(
                        "renewable_source", Electricity.model_fields["renewable_source"].default
                    ),
                ),
            )
        for bid, name, renewable, series, value, source in sources:
            block: dict[str, Any] = {
                "kind": "input",
                "id": reserve(bid),
                "name": name,
                "material": ELECTRICITY,
                "quantity_unit": "kWh",
                "renewable": renewable,
                "emission_factors": [
                    {"value": value, "unit": "gCO2e/kWh", "valid_from": start, "source": source}
                ],
            }
            if series:
                block["series"] = series
            out_blocks.append(block)
            for u in users:
                out_flows.append(
                    {
                        "id": reserve(f"{u}__{bid}"),
                        "source": bid,
                        "target": u,
                        "material": ELECTRICITY,
                    }
                )
        Electricity.model_validate(elec)  # its own errors, by field
        out.pop("electricity")
    out["blocks"], out["flows"] = out_blocks, out_flows
    return out


def compact(data: dict[str, Any]) -> dict[str, Any]:
    """The full form in the compact one where nothing is lost: electricity input blocks
    become the ``electricity`` section (one renewable input with a constant factor, one grid
    input with a constant factor, every unit that meters electricity fed by both and no other
    unit fed electricity), and each pass-through storage between a unit and a unit or a
    distribution block becomes a direct link. Anything else is left as it is."""
    out = dict(data)
    blocks = [dict(b) for b in data.get("blocks", [])]
    flows = [dict(f) for f in data.get("flows", [])]
    layout = dict(data.get("layout", {}))
    by_id = {b.get("id"): b for b in blocks}

    # Pass-through storages between a unit and a unit or a distribution block.
    for b in list(blocks):
        if b.get("kind") != "storage_unit" or not b.get("pass_through"):
            continue
        into = [f for f in flows if f.get("target") == b["id"]]
        outof = [f for f in flows if f.get("source") == b["id"]]
        if len(into) != 1 or len(outof) != 1 or outof[0].get("series"):
            continue
        pair = (
            by_id.get(into[0].get("source"), {}).get("kind"),
            by_id.get(outof[0].get("target"), {}).get("kind"),
        )
        if pair not in _DIRECT or b.get("opening_ledger") or b.get("opening_inventory_kg"):
            continue
        into[0]["target"] = outof[0]["target"]
        flows.remove(outof[0])
        blocks.remove(b)
        layout.pop(b["id"], None)

    # Electricity input blocks.
    elec = [b for b in blocks if b.get("kind") == "input" and b.get("material") == ELECTRICITY]
    ren = [b for b in elec if b.get("renewable")]
    grid = [b for b in elec if not b.get("renewable")]
    users = {
        b["id"]
        for b in blocks
        if b.get("kind") == "processing_unit" and b.get("electricity_series")
    }
    fed = {e["id"]: {f.get("target") for f in flows if f.get("source") == e["id"]} for e in elec}
    if (
        not data.get("electricity")
        and len(grid) == 1
        and len(ren) <= 1
        and all(len(e.get("emission_factors", [])) == 1 for e in elec)
        and all(fed[e["id"]] == users for e in elec)
        and not any(e.get("renewable") and not e.get("series") for e in elec)
    ):
        g = grid[0]["emission_factors"][0]
        section: dict[str, Any] = {}
        if ren:
            r = ren[0]["emission_factors"][0]
            section |= {
                "renewable_available_series": ren[0]["series"],
                "renewable_gCO2e_per_kWh": r.get("value", 0.0),
                "renewable_source": r.get("source", ""),
            }
        section |= {"grid_gCO2e_per_kWh": g.get("value"), "grid_source": g.get("source", "")}
        ids = {e["id"] for e in elec}
        blocks = [b for b in blocks if b.get("id") not in ids]
        flows = [f for f in flows if f.get("source") not in ids]
        for i in ids:
            layout.pop(i, None)
        out["electricity"] = section
    out["blocks"], out["flows"] = blocks, flows
    if "layout" in data:
        out["layout"] = layout
    return out


def synthetic_owner(ref: str | None, data: dict[str, Any]) -> str | None:
    """The block or flow of the compact config a loader-made item stands for (None: the
    electricity section)."""
    if ref is None:
        return None
    given = {x.get("id") for x in [*data.get("blocks", []), *data.get("flows", [])]}
    if ref in given:
        return ref
    for suffix in (LINK_SUFFIX, OUT_SUFFIX):
        if ref.endswith(suffix) and ref[: -len(suffix)] in given:
            return ref[: -len(suffix)]
    if ref in (RENEWABLE_ID, GRID_ID):
        return None
    unit, _, src = ref.rpartition("__")
    if src in (RENEWABLE_ID, GRID_ID) and unit in given:
        return unit
    return ref


class Position(_Model):
    """Where a block sits on the diagram canvas (presentation only)."""

    x: float
    y: float


class PlantConfig(_Model):
    """One version of the plant block flow diagram."""

    schema_version: Literal[1] = 1
    plant: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_from: dt.date
    timezone: str
    settings: Settings = Field(default_factory=Settings)
    # Read in the compact form only: the loader turns it into electricity input blocks.
    electricity: Electricity | None = None
    blocks: list[Block]
    flows: list[Flow]
    # Diagram positions written by the editor. Presentation only: never read by the engine
    # and left out of the config hash of a sealed run.
    layout: dict[str, Position] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _expand(cls, data: Any) -> Any:
        return expand(data) if isinstance(data, dict) else data

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _pass_through_outflow(self) -> PlantConfig:
        """A pass-through storage holds nothing, so what leaves it each interval is what came
        in: an outflow given no series of its own is metered by the inflow, that is the inflow
        flow's series (a unit that splits its output) or else the unit's production series
        (decided 2026-10-28). A series given on the outflow is kept."""
        blocks = {b.id: b for b in self.blocks}
        flows = list(self.flows)
        for k, f in enumerate(flows):
            store = blocks.get(f.source)
            if f.series or not isinstance(store, StorageUnit) or not store.pass_through:
                continue
            into = [g for g in flows if g.target == store.id]
            out = [g for g in flows if g.source == store.id]
            if len(into) != 1 or len(out) != 1:
                continue  # not a direct link: pass_through_links reports it
            unit = blocks.get(into[0].source)
            series = into[0].series or (
                unit.production_series if isinstance(unit, ProcessingUnit) else None
            )
            if series:
                flows[k] = f.model_copy(update={"series": series})
        if flows != self.flows:
            object.__setattr__(self, "flows", flows)  # frozen model: set once, at validation
        return self

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(block_id)

    def blocks_of[T](self, cls: type[T]) -> list[T]:
        return [b for b in self.blocks if isinstance(b, cls)]

    def flows_into(self, block_id: str) -> list[Flow]:
        return [f for f in self.flows if f.target == block_id]

    def flows_out_of(self, block_id: str) -> list[Flow]:
        return [f for f in self.flows if f.source == block_id]


def select_version(configs: list[PlantConfig], on: dt.date) -> PlantConfig:
    """The config version in force on ``on``: the latest ``effective_from`` on or before it."""
    valid = [c for c in configs if c.effective_from <= on]
    if not valid:
        raise LookupError(f"no plant config version is effective on {on}")
    dates = [c.effective_from for c in valid]
    latest = max(dates)
    if dates.count(latest) > 1:
        raise ValueError(f"two plant config versions share effective_from {latest}")
    return next(c for c in valid if c.effective_from == latest)
