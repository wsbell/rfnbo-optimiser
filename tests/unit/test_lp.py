from __future__ import annotations

import math

from rfnbo.optimise.lp import LinearProgram, solver_info


def test_solves_and_reports_infeasibility() -> None:
    lp = LinearProgram()
    a, b = lp.var(), lp.var(upper=3.0)
    lp.row([a, b], [1.0, 1.0], upper=4.0)
    lp.set_cost({a: 1.0, b: 2.0})
    sol = lp.solve(maximise=True)
    assert sol.optimal and math.isclose(sol.objective, 7.0)
    assert list(sol.x) == [1.0, 3.0]
    lp.row([a, b], [1.0, 1.0], lower=10.0)
    assert lp.solve(maximise=True).status == "Infeasible"


def test_solver_and_options_are_recorded() -> None:
    info = solver_info()
    assert info["name"] == "HiGHS"
    assert info["options"]["solver"] == "simplex" and info["options"]["random_seed"] == 0
