"""A small sparse LP builder on HiGHS with fixed, recorded options (deterministic runs)."""

from __future__ import annotations

import importlib.metadata
import math
from dataclasses import dataclass
from typing import Any

import highspy
import numpy as np
import numpy.typing as npt

INF = math.inf
# Fixed so the same model always gives the same answer; recorded in every sealed run.
SOLVER_OPTIONS: dict[str, Any] = {
    "solver": "simplex",
    "presolve": "on",
    "random_seed": 0,
    "primal_feasibility_tolerance": 1e-9,
    "dual_feasibility_tolerance": 1e-9,
}


def solver_info() -> dict[str, Any]:
    return {
        "name": "HiGHS",
        "version": highspy.Highs().version(),
        "highspy": importlib.metadata.version("highspy"),
        "options": dict(SOLVER_OPTIONS),
    }


class LPError(RuntimeError):
    pass


@dataclass(frozen=True)
class Solution:
    status: str
    x: npt.NDArray[np.float64]
    objective: float

    @property
    def optimal(self) -> bool:
        return self.status == "Optimal"


class LinearProgram:
    def __init__(self) -> None:
        self.cost: list[float] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.rows: list[tuple[float, float, list[int], list[float]]] = []

    @property
    def n(self) -> int:
        return len(self.cost)

    def var(self, lower: float = 0.0, upper: float = INF, cost: float = 0.0) -> int:
        self.cost.append(cost)
        self.lower.append(lower)
        self.upper.append(upper)
        return len(self.cost) - 1

    def row(
        self, index: list[int], value: list[float], lower: float = -INF, upper: float = INF
    ) -> int:
        if len(index) != len(value):
            raise LPError("row index and value lengths differ")
        self.rows.append((lower, upper, list(index), [float(v) for v in value]))
        return len(self.rows) - 1

    def set_cost(self, cost: dict[int, float] | list[float]) -> None:
        """Replace the objective (unlisted variables cost 0)."""
        self.cost = [0.0] * self.n
        items = cost.items() if isinstance(cost, dict) else enumerate(cost)
        for i, c in items:
            self.cost[i] = float(c)

    def solve(self, maximise: bool = False) -> Solution:
        lp = highspy.HighsLp()
        lp.num_col_ = self.n
        lp.num_row_ = len(self.rows)
        lp.col_cost_ = np.array(self.cost, dtype=np.float64)
        lp.col_lower_ = np.array(self.lower, dtype=np.float64)
        lp.col_upper_ = np.array(self.upper, dtype=np.float64)
        lp.row_lower_ = np.array([r[0] for r in self.rows], dtype=np.float64)
        lp.row_upper_ = np.array([r[1] for r in self.rows], dtype=np.float64)
        starts, index, value = [0], [], []
        for _, _, idx, val in self.rows:
            index.extend(idx)
            value.extend(val)
            starts.append(len(index))
        lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
        lp.a_matrix_.start_ = np.array(starts, dtype=np.int32)
        lp.a_matrix_.index_ = np.array(index, dtype=np.int32)
        lp.a_matrix_.value_ = np.array(value, dtype=np.float64)
        lp.a_matrix_.num_col_ = self.n
        lp.a_matrix_.num_row_ = len(self.rows)
        lp.sense_ = highspy.ObjSense.kMaximize if maximise else highspy.ObjSense.kMinimize

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        for k, v in SOLVER_OPTIONS.items():
            h.setOptionValue(k, v)
        h.passModel(lp)
        h.run()
        status = h.modelStatusToString(h.getModelStatus())
        if status != "Optimal":
            return Solution(status, np.zeros(self.n), math.nan)
        x = np.array(h.getSolution().col_value, dtype=np.float64)
        return Solution(status, x, float(h.getInfo().objective_function_value))
