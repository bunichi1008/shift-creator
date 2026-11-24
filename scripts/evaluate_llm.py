"""Evaluate proposals for the December demo using a running OpenAI-enabled app."""
import argparse
import base64
import json
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

DATASET = Path(__file__).resolve().parents[1] / "contracts" / "llm-eval.json"


def rule_counts(rules):
    return Counter(json.dumps(rule, sort_keys=True) for rule in rules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--allow-api-calls", action="store_true", required=True,
                        help="Run live OpenAI requests that may incur API charges.")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if token := os.environ.get("DEMO_ADMIN_TOKEN"):
        credential = base64.b64encode(f"admin:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {credential}"

    def call(path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        with urlopen(Request(url + path, data, headers), timeout=40) as response:
            return json.load(response)

    state = call("/api/plan")
    if not state["has_llm"]:
        parser.error("Configure OPENAI_API_KEY on the app before evaluating.")
    plan = state["plan"]
    if plan["problem"]["start_date"] != "2026-12-01":
        parser.error("Load the December 2026 demonstration dataset first.")
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    exact = expected_rules = clarified = expected_clarifications = wrong_proposals = 0
    latencies = []
    for index, case in enumerate(cases, 1):
        started = time.perf_counter()
        response = call("/api/plan/proposals", {"revision": plan["revision"], "text": case["text"]})
        latencies.append(round((time.perf_counter() - started) * 1000))
        actual = response.get("rules") or ([response["rule"]] if response.get("rule") else [])
        expected = case.get("rules") or ([case["rule"]] if case.get("rule") else [])
        if expected:
            expected_rules += 1
            correct = response["status"] == "proposal" and rule_counts(actual) == rule_counts(expected)
            exact += correct
            wrong_proposals += response["status"] == "proposal" and not correct
        else:
            expected_clarifications += 1
            correct = response["status"] == "clarify"
            clarified += correct
            wrong_proposals += response["status"] == "proposal"
        if not correct:
            print(json.dumps({"case": index, "expected": expected, "actual": actual,
                              "status": response["status"]}, ensure_ascii=False))
    print(json.dumps({
        "exact_rule_matches": f"{exact}/{expected_rules}",
        "correct_clarifications": f"{clarified}/{expected_clarifications}",
        "wrong_proposals": f"{wrong_proposals}/{len(cases)}",
        "median_ms": statistics.median(latencies),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
