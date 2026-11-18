"""LangGraph agent over the scraped DarGlobal + Wasalt data.

Two tools, both reading the SQLite file the scraper writes:

  search_listings  structured filter over Wasalt listings (city, price, beds)
  search_pages     FTS5 keyword search over DarGlobal project and Wasalt pages

No vector store. Property questions are mostly structured ("3-bed villa in
Riyadh under 2M"), and a WHERE clause answers those exactly, where embeddings
only answer them approximately. FTS5 covers the prose half.
"""

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

Rules:
- Never invent a price, size or project. If the tools return nothing, say so \
and suggest what you can search instead.
- Quote prices with their currency: Wasalt listings are in SAR.
- Keep answers short. Use a compact list when showing more than two properties.
- Include the source URL for anything you quote."""


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
    return "\n".join(
        f"- {r['title']} | {r['ptype']} for {r['deal']} | "
        f"{r['district'] or '?'}, {r['city'] or '?'} | "
        f"{r['price_sar'] or '?'} SAR | {r['bedrooms'] or '?'} bed | "
        f"{r['area_sqm'] or '?'} sqm | {r['url']}"
        for r in rows)


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


def build_agent():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    llm = ChatOpenAI(
        model=MODEL,
        api_key=key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
        timeout=60,
    )
    return create_react_agent(llm, [search_listings, search_pages],
                              prompt=SYSTEM)


if __name__ == "__main__":
    # Tools must work without an API key, so they can be checked on their own.
    print(search_listings.invoke({"city": "Riyadh", "limit": 3}))
    print()
    print(search_pages.invoke({"query": "Trump", "limit": 2}))
