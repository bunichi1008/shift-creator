from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .models import Report, SolveRequest, SolveResult, ValidationRequest
from .solver import Solver, load_solver, run_solver
from .validation import validate

logger = logging.getLogger(__name__)
DEFAULT_DEMO = Path(__file__).resolve().parents[2] / "contracts" / "demo.json"


def create_app(solver: Solver | None = None) -> FastAPI:
    if solver is None:
        solver = load_solver(os.environ.get("OPTIMIZER_SOLVER", "none"),
                             Path(os.environ.get("DEMO_DATA_PATH", DEFAULT_DEMO)))
    app = FastAPI(title="Shift Creator Optimizer", version="1.0")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/validate", response_model=Report)
    def check(request: ValidationRequest) -> Report:
        return validate(request.problem, request.assignments)

    @app.post("/v1/solve", response_model=SolveResult)
    def solve(request: SolveRequest) -> SolveResult:
        try:
            return run_solver(request, solver)
        except Exception:
            logger.exception("Solver adapter failed")
            raise HTTPException(status_code=500, detail="solver adapter failed") from None

    return app
