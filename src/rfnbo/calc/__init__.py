"""The calculator: the only source of truth for reported figures."""

from rfnbo.calc.batch import CalculationError
from rfnbo.calc.calculator import RunResult, calculate

__all__ = ["CalculationError", "RunResult", "calculate"]
