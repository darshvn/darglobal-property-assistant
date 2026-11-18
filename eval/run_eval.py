"""Outcome-graded evaluation of the property assistant.

Every case is graded against the database, not against an LLM's opinion of the
answer. A grader asks "is this claim true of the data", so a confident,
fluent, wrong answer fails. The four things worth catching here:

  grounding     every URL the bot cites exists in our data
  price_filter  a price ceiling in the question is respected in the answer
  refusal       a question with no data gets a "no" rather than an invention
  routing       the right tool is called for the question

Run:  OPENROUTER_API_KEY=sk-or-... python eval/run_eval.py
      python eval/run_eval.py --selftest     # graders only, no API calls
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from agent import DB  # noqa: E402

URL_RE = re.compile(r"https?://[^\s<>\")\]]+")


def known_urls() -> set[str]:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        return {r[0] for r in db.execute("SELECT url FROM docs")}


def prices_by_url() -> dict[str, int]:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        return {u: p for u, p in db.execute(
            "SELECT url, price_sar FROM listings WHERE price_sar IS NOT NULL")}


# --- graders: pure functions, so they can be checked without the model -------

def grade_grounding(reply: str, urls: set[str], **_) -> tuple[bool, str]:
    cited = {u.rstrip(".,);") for u in URL_RE.findall(reply)}
    invented = cited - urls
    return not invented, (f"invented {len(invented)}: {list(invented)[:2]}"
                          if invented else f"{len(cited)} cited, all real")


def grade_price_cap(reply: str, prices: dict[str, int], cap: int = 0,
                    **_) -> tuple[bool, str]:
    cited = [u.rstrip(".,);") for u in URL_RE.findall(reply)]
    over = [(u, prices[u]) for u in cited if u in prices and prices[u] > cap]
    return not over, (f"over cap: {over[:2]}" if over
                      else f"{len([u for u in cited if u in prices])} within cap")


def grade_refusal(reply: str, prices: dict[str, int], **_) -> tuple[bool, str]:
    """No data for the question, so no listing may be cited."""
    cited = [u.rstrip(".,);") for u in URL_RE.findall(reply)]
    listings = [u for u in cited if u in prices]
    return not listings, (f"cited {len(listings)} listings anyway"
                          if listings else "cited no listings")


def grade_tool(tools: list[str], expect: str = "", **_) -> tuple[bool, str]:
    return expect in tools, f"called {tools or ['nothing']}"


CASES = [
    dict(name="routing_pages", ask="What is DarGlobal's partnership with the "
         "Trump Organization?", grade=grade_tool, expect="search_pages"),
    dict(name="routing_listings", ask="Show me villas for sale in Riyadh.",
         grade=grade_tool, expect="search_listings"),
    dict(name="grounding", ask="Show me three properties in Jeddah with their "
         "links.", grade=grade_grounding),
    dict(name="price_cap", ask="Which properties in Riyadh are under 1000000 "
         "SAR? Include links.", grade=grade_price_cap, cap=1_000_000),
    dict(name="refusal", ask="Show me listings in Reykjavik with links.",
         grade=grade_refusal),
]


def _selftest() -> None:
    urls = {"https://a.test/1", "https://a.test/2"}
    prices = {"https://a.test/1": 500_000, "https://a.test/2": 9_000_000}

    ok, _ = grade_grounding("see https://a.test/1 and https://a.test/2", urls)
    assert ok
    bad, why = grade_grounding("see https://evil.test/9", urls)
    assert not bad and "invented" in why, why

    ok, _ = grade_price_cap("https://a.test/1", prices, cap=1_000_000)
    assert ok
    bad, _ = grade_price_cap("https://a.test/2", prices, cap=1_000_000)
    assert not bad, "a listing over the cap must fail"

    ok, _ = grade_refusal("I have nothing for that city.", prices)
    assert ok
    bad, _ = grade_refusal("Try https://a.test/1", prices)
    assert not bad, "citing a listing when there is no data must fail"

    assert grade_tool(["search_pages"], expect="search_pages")[0]
    assert not grade_tool([], expect="search_pages")[0]

    # Trailing punctuation must not turn a real URL into an invented one.
    ok, _ = grade_grounding("look at https://a.test/1.", urls)
    assert ok, "trailing full stop should be stripped"
    print("graders ok")


def main() -> int:
    from langchain_core.messages import HumanMessage

    from agent import build_agent

    agent, urls, prices = build_agent(), known_urls(), prices_by_url()
    print(f"{len(urls)} pages, {len(prices)} priced listings\n")

    failures = 0
    for case in CASES:
        out = agent.invoke({"messages": [HumanMessage(case["ask"])]},
                           {"recursion_limit": 12})
        reply = out["messages"][-1].content
        tools = [c["name"] for m in out["messages"]
                 for c in getattr(m, "tool_calls", []) or []]
        ok, why = case["grade"](reply=reply, tools=tools, urls=urls,
                                prices=prices, cap=case.get("cap", 0),
                                expect=case.get("expect", ""))
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['name']:<18} {why}")
        if not ok:
            print(f"      asked: {case['ask']}\n      got:   {reply[:200]}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    if ap.parse_args().selftest:
        _selftest()
    else:
        sys.exit(main())
