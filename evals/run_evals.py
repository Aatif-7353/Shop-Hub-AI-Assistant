"""R9 - mini evaluation harness. Per-case PASS/FAIL (no blended score). Exit code 1 if any case fails.

  python -m evals.run_evals                 # fake LLM (default, offline)
  python -m evals.run_evals --llm anthropic # real model; wording-based checks may legitimately differ
"""
import argparse
import asyncio
import os
import sys

from langgraph.checkpoint.memory import MemorySaver

WHO = {
    "acme": ("tok_acme_dev", {"user": "sam@acme-gadgets.test", "merchant": "acme"}),
    "bloom": ("tok_bloom_dev", {"user": "lee@bloom-boutique.test", "merchant": "bloom"}),
    "orphan": ("tok_orphan_dev", {"user": "ghost@nowhere.test", "merchant": "none"}),
}


async def run_case(case: dict) -> list[tuple[str, bool, str]]:
    from common import inproc
    from orchestrator.graph import build_graph
    from orchestrator.runner import make_config, resume, stream_events, user_input

    inproc.wire()  # fresh seeded store per case
    graph = build_graph(MemorySaver())
    token, ident = WHO[case["who"]]
    cfg = make_config(token, ident, case["name"][:40].replace(" ", "_"), "eval")
    events, answer = [], ""
    for kind, arg in case["turns"]:
        payload = user_input(arg) if kind == "say" else resume(arg)
        evs = [e async for e in stream_events(graph, payload, cfg)]
        events += evs
        answer = next(e for e in reversed(evs) if e["event"] == "done")["answer"] or answer
    called = {e["name"] for e in events if e["event"] == "tool_started"}
    raised = any(e["event"] == "proposal" for e in events)
    exp, checks = case["expect"], []

    def check(label, ok, detail=""):
        checks.append((label, bool(ok), detail))

    for t in exp.get("tools", []):
        check(f"tool called: {t}", t in called, f"called={sorted(called)}")
    for t in exp.get("no_tools", []):
        check(f"tool NOT called: {t}", t not in called, f"called={sorted(called)}")
    for s in exp.get("answer_has", []):
        check(f"answer has {s!r}", s.lower() in answer.lower(), f"answer={answer[:90]!r}")
    for s in exp.get("answer_lacks", []):
        check(f"answer lacks {s!r}", s.lower() not in answer.lower(), f"answer={answer[:90]!r}")
    if "proposal" in exp:
        check(f"proposal raised == {exp['proposal']}", raised == exp["proposal"])
    for oid, st in exp.get("status", {}).items():
        actual = inproc.order(oid)["status"]
        check(f"order {oid} status == {st}", actual == st, f"actual={actual}")
    return checks


async def main(verbose: bool) -> int:
    from evals.cases import CASES
    failed = 0
    print(f"LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'fake')}   cases={len(CASES)}\n")
    for case in CASES:
        try:
            checks = await run_case(case)
        except Exception as e:  # a crashing case is a failing case
            checks = [(f"ran without exception", False, f"{type(e).__name__}: {e}")]
        ok = all(c[1] for c in checks)
        failed += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {case['name']}  ({sum(c[1] for c in checks)}/{len(checks)} checks)")
        for label, good, detail in checks:
            if verbose or not good:
                print(f"        {'ok  ' if good else 'FAIL'} {label}" + (f"   -- {detail}" if not good and detail else ""))
    print(f"\n{len(CASES) - failed}/{len(CASES)} cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default=None, help="fake (default) | anthropic | openai | ollama")
    ap.add_argument("-v", "--verbose", action="store_true", help="show every check")
    a = ap.parse_args()
    if a.llm:
        os.environ["LLM_PROVIDER"] = a.llm
    sys.exit(asyncio.run(main(a.verbose)))
