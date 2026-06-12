# ARGUS — Analyst Module Specification v1.0

**Extends:** argus-blueprint.md (v2.0) · **Phase:** 5 (post-MVP; independently buildable)
**Purpose:** Given a ticker, produce a grounded, framework-driven fundamental analysis dossier — then support collaborative deep-dive with Omar. Frameworks: Benjamin Graham (The Intelligent Investor), Warren Buffett, Nassim Taleb.

---

## 0. Laws Applied

- **Law 2 (facts retrieved, never generated) is everything here.** LLMs hallucinate financials from memory. Every number in a dossier comes from a pulled filing or API response, cited to its source. The LLM interprets; the pipeline supplies.
- **Law 1 adapted:** the module renders *framework verdicts* on the business and its price (cheap/expensive, quality, fragility) — that is analysis. It never issues timing or sizing instructions ("buy now," "enter at $X," "put 30% in"). The capital decision is Omar's.
- **Law 4 adapted:** no single-point forecasts. All forward views are scenario ranges with explicit assumptions, plus reverse-DCF extraction of what the current price implies. Graham, Buffett, and Taleb all demand this.

---

## 1. The Analysis Pipeline (8 stages)

### Stage 1 — Business Understanding (Buffett: circle of competence)
What does it sell, who pays, why do they keep paying, what breaks the repeat purchase. If the business can't be explained in one paragraph from the 10-K, that itself is flagged in the dossier.

### Stage 2 — Financial Statement Analysis (10 years where available)
Source: SEC EDGAR company-facts API (XBRL) — official, free, audited numbers.
- **Income:** revenue CAGR (3/5/10y), gross→operating→net margin trends, earnings consistency (Graham: no loss years for a defensive holding)
- **Balance sheet:** debt/equity, current ratio, goodwill share of assets, **share count trend** (dilution is a silent tax — flagged hard)
- **Cash flow:** FCF vs. net income (earnings quality ratio), capex intensity, stock-based comp as % of revenue
- **Buffett metrics:** ROE consistency (>15% bar), ROIC vs. estimated cost of capital, **owner earnings** (NI + D&A − maintenance capex)
- **Red flags (auto-checked):** receivables growing faster than revenue, recurring "one-time" charges, auditor changes, negative FCF with positive reported earnings

### Stage 3 — Moat & Competitors
Peer set pulled from same industry; side-by-side table: margins, ROIC, growth, share-count discipline.
- Moat evidence = sustained ROIC spread over peers + gross-margin stability through downturns (pricing power)
- Moat classified: network effects / switching costs / brand / cost advantage / regulatory
- Anti-moat flags: price competition, customer concentration, technology dependency

### Stage 4 — Management & Board (DEF 14A proxy + 10-K)
- **Insider ownership %** — skin in the game (Taleb). Founder-led? How much of their net worth rides with shareholders?
- Compensation structure: paid for per-share value creation, or for empire size?
- Capital allocation record: buybacks at lows or highs, acquisition history and write-downs, dividend policy
- Board: independence, tenure, related-party transactions

### Stage 5 — Future Plans & Guidance Calibration
- Stated strategy from MD&A; capex and R&D trend as revealed (not claimed) priorities; reinvestment runway
- TAM claims treated skeptically (every deck claims a trillion-dollar market)
- **Guidance calibration:** did the last 3 years of management guidance come true? A management team's forecast record is data about their forecasts.

### Stage 6 — Taleb Fragility Audit
- **Ruin risks:** leverage level, debt maturity wall, covenant proximity, single-customer or single-product concentration, regulatory single point of failure, key-man risk
- **Hidden fragilities:** thin margins × high operating leverage, currency/commodity exposure, supply-chain chokepoints
- **Optionality (antifragile side):** net cash, multiple independent businesses, R&D pipeline as cheap options, ability to gain from competitors' distress
- **Verdict: FRAGILE / ROBUST / ANTIFRAGILE** with the explicit list of what kills this company

### Stage 7 — Valuation (the scenario engine — no prophecy)
- Current multiples (P/E, EV/EBIT, P/FCF) vs. the company's own 10-year history and vs. peers
- **Owner-earnings scenario model:** bear / base / bull over 5 years — each with explicit assumptions (revenue CAGR × margin path × exit multiple − expected dilution) → **per-share value RANGE**, never a point
- **Sensitivity table:** which assumption moves the answer most (usually the exit multiple — said out loud)
- **Base-rate check:** bull scenarios tested against historical base rates (very few companies sustain >20% growth for a decade; if the bull case requires it, that's flagged)
- **Margin of safety:** current price vs. the conservative (bear-weighted) estimate, expressed as %
- **Reverse DCF:** extract the growth rate the *current price* implies and state it plainly: "the market is pricing X% for Y years — believable given Stages 2–5?"

### Stage 8 — Consensus & Sentiment Context (Mr. Market's mood)
Analyst targets and estimates (Finnhub/yfinance), short interest, recent narrative from the news layer Argus already ingests. Framed strictly as *what is priced in and how the crowd feels* — context, never evidence of value.

---

## 2. The Verdict Block (every dossier ends with this)

| Lens | Verdict | Basis |
|---|---|---|
| **Graham** | CHEAP / FAIR / EXPENSIVE + margin of safety % | Price vs. conservative intrinsic range |
| **Buffett** | {Wonderful / Good / Mediocre} business at a {Discount / Fair / Premium} price | Quality score × price paid |
| **Taleb** | FRAGILE / ROBUST / ANTIFRAGILE + ruin list | Stage 6 audit |

Plus three honesty sections:
- **What would change this verdict** (explicit falsifiers — e.g., "ROIC drops below 10% for two consecutive years")
- **What kills this company** (the section sell-side research never writes)
- **Open questions for Omar** (what the data couldn't resolve — seeds for the collaborative session)

Final line of every dossier: *"Framework verdicts rendered. Timing and sizing are yours."*

---

## 3. Data Sources (all free)

| Need | Source | Notes |
|---|---|---|
| Financial statements (10y) | SEC EDGAR company-facts API (XBRL) | Official, free, no key. The fiddly part is tag mapping — budget time |
| Filings text (10-K MD&A, risk factors) | EDGAR full-text | For Stages 1, 5, 6 |
| Board / comp / ownership | DEF 14A proxy via EDGAR | Stage 4 |
| Price history & multiples | Tiingo (already in spine) + yfinance | Reuses prices_eod where held tickers |
| Analyst consensus, estimates, peers | Finnhub free + yfinance | Stage 8 + peer discovery |
| News context | Argus headlines/sentiment tables | Already ingested |
| Non-US tickers | Flagged limitation: EDGAR covers SEC filers; foreign companies via 20-F where available, else reduced-depth dossier |

New tables: `fundamentals` (XBRL facts cache: symbol, tag, period, value, filing_ref), `analyses` (symbol, run_at, dossier_text, data_pack_json, verdicts).

---

## 4. Interaction Model (the "together" part)

1. **Trigger:** `/analyze NVDA` in Telegram → Vercel replies "Building dossier, ~5 min ⏳" → workflow_dispatch fires the analysis run in GitHub Actions.
2. **Run (~3–5 min):** EDGAR + peer + market pulls → computations → Sonnet writes the dossier under the 8-stage structure with the 5-clause synthesis contract (grounding, no timing/sizing, fixed structure, interpretation beside every number, uncertainty marking).
3. **Delivery:** dossier to Telegram (and stored in `analyses` with its full data pack).
4. **Deep-dive:** Omar opens a Claude session with the dossier + data pack — argues assumptions, stress-tests scenarios, asks follow-ups. The dossier's "Open questions" section seeds the session. Revised conclusions can be appended to the stored analysis.

This split plays to each surface's strength: Argus does grounded retrieval and consistent structure; Claude does Socratic collaboration.

---

## 5. Build Estimate & Cost

| Component | Est. |
|---|---|
| EDGAR XBRL ingestion + tag mapping | 8–12 h (the genuinely fiddly part) |
| Peer comparison engine | 3–4 h |
| Scenario + reverse-DCF engine | 4–6 h |
| Dossier prompt + iteration on 2–3 known companies (start with TSLA — Omar will instantly spot shallowness) | 4–6 h |
| **Total** | **~20–28 h** |

Running cost: ~$0.30–0.60 per dossier (Sonnet, large context: full financials + peers + filings excerpts). Even at one analysis per week: **~$2/month added.**

**Sequencing:** Phase 5 — after MVP (Phases 0–2) ships. It shares the spine but doesn't block or depend on the digest/journal build. Resist the temptation to build this first; Argus's daily value comes from the digest and journal, and the Analyst module is better built on a working spine.

---

## 6. Validation (build-time)

- Numbers check: dossier financials vs. the actual 10-K PDF for 2 companies — must match to the reported figure
- Verdict sanity: run on one obviously expensive glamour stock and one obviously cheap cigar-butt — the lenses must disagree in the expected directions
- Hallucination probe: run on a ticker with a sparse EDGAR record — the dossier must say "not available" rather than fill gaps
