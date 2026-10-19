"""Proof of sustainability records: import from CSV or JSON, and the rules a record must meet
before a run uses it (build plan §11.1 rule 2 and decisions 1 and 4).

CSV columns (header row, one PoS per row; dates YYYY-MM-DD; numbers with a point):

    id,supplier,product,dispatch_date,quantity_kg,ghg_gCO2e_per_MJ

Nothing is filled or corrected: a missing or malformed value is an error.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from rfnbo.calc.purchase import quarter_of
from rfnbo.calc.tolerance import meets_threshold
from rfnbo.config.schema import PlantConfig, RfnboSupply
from rfnbo.config.validate import Issue
from rfnbo.model.entities import ProofOfSustainability

CSV_COLUMNS = ("id", "supplier", "product", "dispatch_date", "quantity_kg", "ghg_gCO2e_per_MJ")


class PoSError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))


def load_pos(path: str | Path) -> list[ProofOfSustainability]:
    """Read PoS records from a .csv file or a JSON list."""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    adapter = TypeAdapter(list[ProofOfSustainability])
    try:
        if p.suffix.lower() != ".csv":
            return adapter.validate_json(text)
        rows = list(csv.DictReader(text.splitlines()))
        missing = [c for c in CSV_COLUMNS if rows and c not in rows[0]]
        if missing:
            raise PoSError([Issue("pos_columns", f"missing column(s) {missing}", str(p))])
        return adapter.validate_python([{c: r[c] for c in CSV_COLUMNS} for r in rows])
    except ValidationError as exc:
        raise PoSError(
            [
                Issue("pos_schema", e["msg"], ".".join(str(x) for x in e["loc"]))
                for e in exc.errors()
            ]
        ) from exc
    except json.JSONDecodeError as exc:
        raise PoSError([Issue("pos_schema", str(exc), str(p))]) from exc


def pos_rules(
    cfg: PlantConfig, records: Sequence[ProofOfSustainability], quarter: str
) -> Iterator[Issue]:
    """A PoS must be compliant (decision 4), belong to the run's quarter (decision 1) and come
    from a supplier block of its product in the plant."""
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    suppliers = {(s.supplier, s.product) for s in cfg.blocks_of(RfnboSupply)}
    ids = [p.id for p in records]
    for p in records:
        if ids.count(p.id) > 1:
            yield Issue("pos_unique", "PoS id is used more than once", p.id)
        if not meets_threshold(p.ghg_gCO2e_per_MJ, threshold):
            yield Issue(
                "pos_compliant",
                f"{p.ghg_gCO2e_per_MJ} gCO2e/MJ is above the RFNBO threshold "
                f"({threshold:.4g}); a PoS must be for compliant RFNBO",
                p.id,
            )
        if quarter_of(p.dispatch_date) != quarter:
            yield Issue(
                "pos_in_quarter",
                f"dispatched {p.dispatch_date} ({quarter_of(p.dispatch_date)}), not in {quarter}",
                p.id,
            )
        if (p.supplier, p.product) not in suppliers:
            yield Issue(
                "pos_supplier",
                f"no supplier block for {p.supplier!r} delivering {p.product!r}",
                p.id,
            )
