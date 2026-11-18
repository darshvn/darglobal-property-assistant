"""LangGraph agent over the scraped DarGlobal + Wasalt data.

Two tools, both reading the SQLite file the scraper writes:

  search_listings  structured filter over Wasalt listings (city, price, beds)
  search_pages     FTS5 keyword search over DarGlobal project and Wasalt pages

No vector store. Property questions are mostly structured ("3-bed villa in
Riyadh under 2M"), and a WHERE clause answers those exactly, where embeddings
only answer them approximately. FTS5 covers the prose half.
"""

import json
import os
import sqlite3
from pathlib import Path

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

DB = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent.parent
                    / "data" / "properties.db"))
# Free models that expose tool calling vary in how much capacity they have.
# Gemma was the obvious pick on paper and returns 429 almost every call;
# ling-3.2-flash answers reliably and has a 1M context window.
MODEL = os.getenv("OPENROUTER_MODEL", "inclusionai/ling-3.2-flash:free")

SYSTEM = """You are the property assistant for DarGlobal, a London-listed \
luxury real estate developer, and its advertising partner Wasalt, a Saudi \
property marketplace.

Answer only from the tools. Call search_listings for anything about specific \
properties on sale or rent (prices, sizes, bedrooms, cities). Call \
search_pages for DarGlobal projects, brand partners, locations and company \
information.

search_listings returns JSON rows. The interface renders those as property \
cards on its own, so do not repeat every field in prose.

Rules:
- Never invent a price, size or project. If the tools return nothing, say so \
and suggest what you can search instead.
- Quote prices with their currency: Wasalt listings are in SAR.
- When listings come back, write one or two sentences about them (the range, \
what stands out) and let the cards carry the detail. Do not list every \
property as a bullet with its URL.
- For page content, answer in short prose and cite the source URL.
- Never call the same tool twice with the same arguments."""


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


@tool
def search_listings(city: str = "", deal: str = "", property_type: str = "",
                    max_price_sar: int = 0, min_bedrooms: int = 0,
                    limit: int = 8) -> str:
    """Find Wasalt property listings by city, deal, type, price and bedrooms.

    Args:
        city: e.g. "Riyadh", "Jeddah". Empty means any city.
        deal: "sale" or "rent". Empty means either.
        property_type: "villa", "apartment", "land", "office". Empty means any.
        max_price_sar: upper price bound in SAR. 0 means no limit.
        min_bedrooms: minimum bedrooms. 0 means no limit.
        limit: how many to return, at most 20.
    """
    where, args = ["1=1"], []
    if city:
        where.append("city LIKE ?"); args.append(f"%{city}%")
    if deal:
        where.append("deal = ?"); args.append(deal.lower())
    if property_type:
        where.append("ptype = ?"); args.append(property_type.lower())
    if max_price_sar:
        where.append("price_sar IS NOT NULL AND price_sar <= ?")
        args.append(max_price_sar)
    if min_bedrooms:
        where.append("bedrooms >= ?"); args.append(min_bedrooms)

    sql = (f"SELECT title, city, district, deal, ptype, price_sar, bedrooms,"
           f" area_sqm, url FROM listings WHERE {' AND '.join(where)}"
           f" ORDER BY price_sar IS NULL, price_sar LIMIT ?")
    with _connect() as db:
        rows = db.execute(sql, [*args, min(limit, 20)]).fetchall()
    if not rows:
        return "No listings matched. Try a wider price range or another city."
    # JSON, so the same rows the model reasons over can be rendered as cards
    # in the UI without the server re-running or re-parsing the query.
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@tool
def search_pages(query: str, limit: int = 4) -> str:
    """Search DarGlobal project pages and Wasalt page text by keyword.

    Use for questions about DarGlobal developments, brand partners
    (Trump, Aston Martin, Fendi Casa, Missoni), countries it builds in,
    or company and investor information.

    Args:
        query: keywords, e.g. "Trump Tower Jeddah" or "golf communities Oman".
        limit: how many passages to return, at most 8.
    """
    # FTS5 treats punctuation as syntax, so quote each term.
    terms = " ".join(f'"{w}"' for w in query.split() if w.isalnum())
    if not terms:
        return "Empty query."
    with _connect() as db:
        rows = db.execute(
            "SELECT d.url, d.title, snippet(docs_fts,2,'','','…',60) AS s "
            "FROM docs_fts JOIN docs d ON d.url = docs_fts.url "
            "WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
            (terms, min(limit, 8))).fetchall()
    if not rows:
        return f"Nothing found for {query!r}."
    return "\n\n".join(f"[{r['title']}] {r['s']}\n{r['url']}" for r in rows)


TOOLS = [search_listings, search_pages]

# Free models rate-limit hard, and a demo that answers "try again later" is a
# broken demo. So we keep an ordered list of providers and walk down it the
# moment one refuses. Fireworks is last because it costs credit; it only runs
# when every free option is exhausted.
FALLBACKS = os.getenv(
    "OPENROUTER_FALLBACKS",
    "nvidia/nemotron-3-ultra-550b-a55b:free,"   # 550B, 1M context
    "google/gemma-4-31b-it:free,"
    "baidu/ernie-5.5-flash-preview:free,"
    "nvidia/nemotron-3.5-lightning:free,"
    "cohere/north-mini-code:free",
)
FIREWORKS_MODEL = os.getenv(
    "FIREWORKS_MODEL", "accounts/fireworks/models/qwen3-235b-a22b-instruct")

# Last resort, and the only one that costs anything. The brief asked for a
# free model, and the primary is one; but OpenRouter's free allowance is 50
# requests a day per account until $10 of credit is bought, which a reviewer
# clicking through a demo can exhaust in one sitting. So a paid model sits
# behind the free ones: ~$0.0016 a conversation, and about twice as fast.
PAID_MODEL = os.getenv("OPENROUTER_PAID_MODEL", "deepseek/deepseek-v4-flash")


OPENROUTER = "https://openrouter.ai/api/v1"


def _providers() -> list[tuple[str, str, str, str, str]]:
    """(label, model, base_url, key, bucket) in the order we try them.

    `bucket` groups everything that shares one quota. OpenRouter's free
    allowance is per account per day, so every model on one key empties
    together; a second key is a second allowance.
    """
    chain = []
    models = [MODEL] + [m.strip() for m in FALLBACKS.split(",") if m.strip()]

    for n, var in enumerate(("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"), 1):
        key = os.environ.get(var)
        if not key:
            continue
        for m in models:
            chain.append((f"openrouter{n}:{m}", m, OPENROUTER, key, f"or{n}"))

    fw = os.environ.get("FIREWORKS_API_KEY")
    if fw:
        chain.append((f"fireworks:{FIREWORKS_MODEL}", FIREWORKS_MODEL,
                      "https://api.fireworks.ai/inference/v1", fw, "fw"))

    # Paid, so it sits behind every free option and every free account.
    if PAID_MODEL:
        for n, var in enumerate(("OPENROUTER_API_KEY",
                                 "OPENROUTER_API_KEY_2"), 1):
            key = os.environ.get(var)
            if key:
                chain.append((f"paid{n}:{PAID_MODEL}", PAID_MODEL,
                              OPENROUTER, key, f"paid{n}"))
    if not chain:
        raise RuntimeError("set OPENROUTER_API_KEY or FIREWORKS_API_KEY")
    return chain


def _agent_for(model: str, base_url: str, key: str):
    llm = ChatOpenAI(model=model, api_key=key, base_url=base_url,
                     temperature=0, timeout=60)
    return create_react_agent(llm, TOOLS, prompt=SYSTEM)


def build_agent():
    """One agent per provider, built lazily and reused."""
    cache: dict[str, object] = {}

    def run(messages, config=None):
        last = None
        spent: set[str] = set()
        for label, model, base_url, key, bucket in _providers():
            if bucket in spent:
                continue
            if label not in cache:
                cache[label] = _agent_for(model, base_url, key)
            try:
                out = cache[label].invoke({"messages": messages},
                                          config or {"recursion_limit": 12})
                return out, label
            except Exception as exc:                      # noqa: BLE001
                detail = str(exc)
                last = exc
                # The daily free allowance belongs to the account, not the
                # model, so once it is gone every other model on that key is
                # gone too. Retire the whole bucket rather than burning five
                # more calls proving it.
                if "free-models-per-day" in detail:
                    spent.add(bucket)
                    continue
                # Otherwise only a capacity refusal is worth moving on; a
                # malformed request would fail identically everywhere.
                if not any(s in detail for s in ("429", "502", "503")) and \
                        "rate" not in detail.lower():
                    raise
        raise last                                        # everything refused

    return run


if __name__ == "__main__":
    # Tools must work without an API key, so they can be checked on their own.
    print(search_listings.invoke({"city": "Riyadh", "limit": 3}))
    print()
    print(search_pages.invoke({"query": "Trump", "limit": 2}))
