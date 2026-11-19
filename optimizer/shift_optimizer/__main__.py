import argparse
import json
import os
from pathlib import Path

from .api import DEFAULT_DEMO
from .models import SolveRequest, SolveResult, ValidationRequest
from .solver import load_solver, run_solver
from .validation import validate


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or run the shift solver contract")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema").add_argument("model", choices=["request", "result"])
    commands.add_parser("solve").add_argument("path", type=Path)
    commands.add_parser("validate").add_argument("path", type=Path)
    args = parser.parse_args()

    if args.command == "schema":
        model = SolveRequest if args.model == "request" else SolveResult
        print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
    elif args.command == "validate":
        req = ValidationRequest.model_validate_json(args.path.read_text())
        print(validate(req.problem, req.assignments).model_dump_json(indent=2))
    else:
        req = SolveRequest.model_validate_json(args.path.read_text())
        solver = load_solver(os.environ.get("OPTIMIZER_SOLVER", "none"),
                             Path(os.environ.get("DEMO_DATA_PATH", DEFAULT_DEMO)))
        print(run_solver(req, solver).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
