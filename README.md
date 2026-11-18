# DarGlobal property assistant

A chatbot that answers questions about DarGlobal developments and Wasalt
property listings, using only data scraped from the two public sites.

Live: **<DEPLOYED_URL>**

```
scraper/scrape.py   real Chrome -> SQLite (listings + full-text pages)
app/agent.py        LangGraph agent, two tools over that SQLite file
app/server.py       FastAPI: GET / (UI), POST /chat, GET /health
eval/run_eval.py    outcome-graded checks against the database
Dockerfile          one image, serves on $PORT (7860 by default)
```

## The three decisions worth explaining

**Scraping goes through a real browser, not an HTTP client.**
`darglobal.co.uk` sits behind Imperva and `wasalt.sa` behind Cloudflare, so
`curl` and `requests` get a challenge page instead of content. The scraper
drives a real Chrome over CDP through the `agent-browser` CLI, which renders
the page the way a visitor would. Wasalt's `robots.txt` allows crawling and
publishes the sitemaps the scraper reads; about half of the 60,000 sitemap
URLs are stale 404s, which the scraper detects and skips rather than storing
as empty records.

**No vector database.**
Most property questions are structured: "3-bedroom villa in Riyadh under 2
million". A `WHERE` clause answers that exactly; embeddings answer it
approximately and cost an extra service plus an embedding model. So listings
go into a normal SQLite table that the agent filters, and the prose half of
the data (project pages, partner pages, descriptions) goes into SQLite's
built-in FTS5 index for keyword search. One file, no extra infrastructure, and
the price filter is actually correct.

**Two tools, not one retrieval step.**
The LangGraph agent picks between `search_listings` (structured filter) and
`search_pages` (keyword search) per question, and can call both. The response
reports which tools ran, so a wrong answer can be traced to the retrieval step
rather than guessed at.

## How I know it works

```bash
OPENROUTER_API_KEY=sk-or-... python eval/run_eval.py
python eval/run_eval.py --selftest      # graders only, no API calls
```

Every case is graded against the database rather than by another model, so a
fluent wrong answer fails:

| Check | What fails it |
|---|---|
| `grounding` | citing a URL that isn't in the scraped data |
| `price_cap` | returning a listing above the ceiling the question set |
| `refusal` | inventing listings for a city we hold no data for |
| `routing` | answering a project question without searching the pages |

The graders are pure functions over the reply and the database, so
`--selftest` checks them without spending an API call — including that a
trailing full stop doesn't turn a real URL into a reported hallucination.

This matters more than it looks: an LLM grading another LLM agrees with
confident prose, and confident prose about a price that doesn't exist is the
failure mode that costs a property business real money.

## Running it

Needs an OpenRouter API key (the free tier is enough) from
<https://openrouter.ai/keys>.

```bash
docker build -t darglobal-bot .
docker run -p 7860:7860 -e OPENROUTER_API_KEY=sk-or-... darglobal-bot
```

Open <http://localhost:7860>.

Without Docker:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
OPENROUTER_API_KEY=sk-or-... .venv/bin/python app/server.py
```

### Rebuilding the data

```bash
python3 scraper/scrape.py --site wasalt --limit 170   # listings via sitemap
python3 scraper/scrape.py --site darglobal            # project + partner pages
python3 scraper/scrape.py --selftest                  # parser check
```

Both need a Chrome running with `--remote-debugging-port=9222` and the
`agent-browser` CLI on PATH. `data/properties.db` is committed so the image
builds without re-scraping.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | required |
| `OPENROUTER_MODEL` | `google/gemma-4-31b-it:free` | any OpenRouter model id |
| `DB_PATH` | `data/properties.db` | path to the SQLite file |
| `PORT` | `7860` | HTTP port |

## Known limits

- **Free-model rate limits.** OpenRouter's free tier caps requests per minute
  and per day. The server returns a readable "rate-limited, try again" message
  instead of a 500 when that happens. Setting `OPENROUTER_MODEL` to a paid
  model removes the cap.
- **Sample, not a full crawl.** The database holds a spread of Wasalt listings
  across cities, not all 60,000, and DarGlobal's project and partner pages.
  Enough to answer real questions; `--limit` raises it.
- **Listing fields come from rendered text.** Wasalt's class names are hashed
  and change on deploy, so the parser reads the visible text instead. It is
  checked by `--selftest`, and a field it cannot find is stored as `NULL`
  rather than guessed.
- Prices are in SAR, as published by Wasalt.
