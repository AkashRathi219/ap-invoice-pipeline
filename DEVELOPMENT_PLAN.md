# DEVELOPMENT PLAN — AP Intelligent Processing Pipeline
### Method: SPEC → PLAN → BUILD → TEST → REVIEW → SHIP · Repo: `github.com/akashrathi219/ap-invoice-pipeline`

*Plan of record for the build. Constraint sources: `../CASE_STUDY_ASSIGNMENT.md` (rules, constraints),
`../DATA_MAP_AND_BUILD_PLAN.md` (interlinks L1–L9, oracle §5.3), `../PIPELINE_PLAN.md` (step contracts S1–S11).*

---

## 0. GROUND RULES — decisions made before code

| # | Decision | Rationale |
|---|---|---|
| G1 | **Git identity** = `akashrathi219 <akashrathi219@users.noreply.github.com>`, set **locally** on this repo only | Global `serenityadmin2025` identity untouched; noreply address keeps the personal email off GitHub |
| G2 | **Cross-platform** — `pathlib` only (no string paths), UTF-8 everywhere (`sys.stdout.reconfigure`), `.gitattributes` `eol=lf`, no shell scripts, single entry `python run.py` | Runs identically on Windows / macOS / Linux from cmd, PowerShell, bash, zsh |
| G3 | **Money = `decimal.Decimal`**, never float | Audit-grade arithmetic; the wrap-trap (`TOTAL €4` + `2,121.24`) is exactly where floats betray you |
| G4 | **UI = none in-repo.** The reviewer gets `report.html` — a self-contained static file (inline CSS, zero JS deps) that opens by double-click in any browser | The assignment asks for "a summary suitable for a non-technical reviewer", not a web app. A server-based UI would be over-engineering (assignment's own words). **Production UI = n8n HITL forms / Slack approval cards** (discussed in interview, out of take-home scope) |
| G5 | **Dependency split** — `requirements.txt` (core: PyMuPDF + pytest) vs `requirements-extras.txt` (semantic AI: sentence-transformers only; faiss dropped at B3 — the code never imported it). LLM calls use stdlib HTTP → local Ollama (running, v0.35.0) | Reviewer can run the deterministic path with one small install; heavy ML wheels are opt-in. No API keys anywhere |
| G6 | **Never bake inputs into the plan** — measured results recorded at consumption (`RUN_LOG.md`), not in this plan | PIPELINE_PLAN §0 |
| G7 | **AI-first inversion (directive 2026-10-17):** AI is the **primary reader** of every invoice (17/17); regex demoted to *corroboration evidence + offline fallback*. What stays deterministic is **verification only** — arithmetic authority, line math, master cross-checks | The assignment's differentiator is *meaningful AI*; rules-only is the disqualifier. "AI proposes, math disposes" is the payment-safety line a financial panel expects |
| G8 | **No n8n in this deliverable (directive 2026-10-17):** the test case needs no mail notifications and no human-task orchestration — the Python pipeline *is* the product | Keeps the repo to one `python run.py` with zero extra install steps; n8n added no test-case value |
| G9 | **No idle model in RAM (directive 2026-10-17):** the LLM is unloaded (`keep_alive=0`) the moment a run ends; default model is the smallest that does the job (`llama3.2:3b`, 2.0 GB) | A local model sitting resident is wasted memory on a reviewer's laptop |

---

## 1. SPEC (done — inputs read, contracts frozen)

- 17 PDFs → 14-field record (P1–P14) · 3 format classes (A digital 14 · B scan 2 · A-variant fuzz 1)
- `vendor_master.csv` 25×10 (M1–M10) · `historic_invoices.csv` 120×9 (H1–H9)
- Disposition ladder: CLEAR / FLAG-REVIEW / HOLD — **never auto-approve** (the pipeline is a filter, not an authority)
- **Oracle** (`DATA_MAP §5.3`): CLEAR 11 · FLAG-REVIEW 3 · HOLD 3 — committed as pytest golden fixture

---

## 2. PLAN — build increments (each: code → tests green → commit)

| Inc | Steps | Modules | Exit criterion |
|---|---|---|---|
| **B1** | S2–S4 | `ingest.py` · `llm_provider.py` · `extract_ai.py` **(PRIMARY, 17/17)** · `extract_regex.py` (corroboration) · `validate_extract.py` | control record 17/17 · schema-valid AI extraction on all 17 · arithmetic authority resolves 4 wrapped totals · field-agreement AI↔regex **measured** |
| **B2** | S5–S6 | `fx.py` · `vendor_match.py` · `rules.py` | 14 exact + V015 fuzzy ≥0.85 + LogiTrans unknown · BritTech FX €5,857.16 · R1/R2/R3/R4/R6 reason codes on oracle cases |
| **B3** | S7–S9 | `duplicates.py` (R5b metadata + R5a embeddings + Ollama forensic verdict) · `enrich.py` | impostor pairs fire (cosine ≥0.85 → structured verdict) · recurring-service noise stays unflagged · hold messages AI-authored |
| **B4** | S10–S11 | `decide.py` · `summarize.py` · `feedback.py` · `report.html` template | full disposition set matches oracle · CSVs + summary + audit log + HTML report · telemetry + tuning dataset |
| **B5** | S12 | `review.py` · `run.py` (`--review`) | interactive terminal review (ACCEPT / REJECT / OVERRIDE / SKIP / QUIT) · append-only `reviewer_feedback.jsonl` journal · auto-replay on subsequent runs · Loop A dynamic threshold tuning |
| **B6** | S1–S2, S11 | `ingest.py` · `pipeline_config.json` · `summarize.py` | dataset-agnostic ingest (auto-detect vs strict count modes) · §5.3 oracle scoped to assessment batch (N/A on custom datasets) · 171 offline tests passing |
| **B7** | S8 | `duplicates.py` · `pipeline_config.json` | **Two-Tier Categorical Duplicate Engine**: Layer 1 Intra-Category deep matching (scoped by `vendor_master.csv` category; ~88% compute reduction) + Layer 2 Unmapped Sentinel (cross-scanning unknown/unapproved vendors like LogiTrans) · category-specific threshold matrix |

---

## 3. AI ACTIVITY MATRIX — *where AI fires, and what urgency justifies it*

> **The inversion (directive 2026-10-17):** AI is the **primary reader of every invoice** — schema-constrained
> Ollama extraction runs 17/17, not on leftovers. Regex survives only as (a) a corroboration evidence stream
> (field-level agreement scoring, logged per invoice) and (b) a named offline fallback if no LLM is reachable.
> What stays deterministic is **not a competing reader**: it is *verification* — arithmetic authority
> (`subtotal + VAT = total`), line math, master-data cross-checks. **AI proposes; math disposes.** For a
> function that moves money, that is the line no panel wants crossed — and it is exactly what the assignment's
> *"AI must be meaningful, rules alone insufficient"* asks for.

| # | AI component | Active trigger (fires every run unless stated) | Urgency justification | Deterministic backstop |
|---|---|---|---|---|
| **A1** | **Schema-constrained extraction** (`extract_ai.py`, Ollama `llama3.2:3b`, JSON-schema output) | **EVERY invoice — 17/17, the primary read.** Regex runs *in parallel* as corroboration; field-level agreement is scored and logged per invoice. Any future format class ≠ A/B needs **zero code changes**. Ollama unreachable → named `regex_fallback` provenance + loud warning, pipeline still completes | The invoice is *understood* at all — extraction is where unstructured becomes payable; format drift is the daily reality | Arithmetic authority + master cross-checks verify AI output; per-field provenance records the winner; a bad AI read surfaces as an *exception*, never as silent wrong data |
| **A2** | **Semantic impostor detection** (`duplicates.py` R5a, `char4gram-cosine` default backend (sentence-transformers opt-in) + Ollama forensic verdict) | **Every batch, every invoice** — cross-vendor pairs with cosine ≥ 0.85 are escalated to Ollama `llama3.2:3b` (temp 0) for a structured `is_impostor_risk / risk_level / pattern` verdict. Targets: StationOps↔LogiTrans (identical 3 services @ ~69% price), OldGuard↔SecureGuard (identical security services, inactive/active split) | **CRITICAL** — active fraud vector inside today's batch; money not yet moved | R5b metadata matcher + quantity arithmetic separates the benign fuel trio; the deterministic gate owns severity (measured: the model graded both clones LOW → gate kept CRITICAL) |
| **A3** | **Contextual enrichment** (`enrich.py`) | Every HOLD/FLAG disposition: plain-language reasons authored per invoice (vendor context, M9 contact, M4 terms); TotalRoute note interpretation | Same-day vendor/AP turnaround — hold messages that explain themselves end dispute loops | reason codes from `rules.py` feed the prompt (LLM never invents the reason) |
| **A4** | **AI reason synthesis** (`decide.py`, read-only, post-decision) | Every non-CLEAR disposition: one-paragraph reviewer summary of *why*, with evidence attached | The AP lead reads 6 exceptions, not 17 PDFs | disposition itself is deterministic — LLM cannot upgrade CLEAR→CLEAR-with-nuance or downgrade a HOLD |
| **A5** | **HITL telemetry & tuning loops** (`feedback.py`) | Every AI prediction logged (`ai_telemetry.jsonl`, `ap-telemetry-v1`, with a `reviewer_feedback: null` HITL slot); ACCEPT/REJECT/OVERRIDE from reviewer feeds Loop A (threshold **suggestions** +0.05 per rejected flag, never a silent config rewrite), Loop B (few-shot bank — batch copy in `output\`, committed seed in `config\`), Loop C (Alpaca SFT export) | Continuous — the pipeline must measurably improve per batch | thresholds/config are committed files (P-AC03), never silently drift |

**The honest defence of the surviving deterministic layer:** a local 3B model misreads numbers sometimes —
that is measurable fact, not pessimism (§ RUN_LOG records the agreement rate). The design converts every such
misread into a *caught* event: arithmetic catches total/line errors, the vendor master catches name errors,
format checks catch date/PO errors. There is no path where an unverified AI number reaches an approver silently.
Model + prompt + schema versions are pinned in `ai_telemetry.jsonl`; temperature 0; schema-constrained output
makes malformed JSON a near-impossible event; the regex fallback keeps the pipeline alive if Ollama is down.

---

## 4. TEST — matrix (pytest; oracle = golden fixture)

| Layer | Tests |
|---|---|
| Control record | 17 files, unique SHA-256s, deterministic sort, non-zero exit if count ≠ 17 |
| Extraction | per-invoice: invoice_number, format tag (14 A / 2 B), currency, PO presence (SparkIT → None), Unicode vendor (`QuickFix OÜ`), note (TotalRoute) |
| Arithmetic authority | line math (qty × unit = amount) 17/17 · `subtotal + VAT = total` · **wrap-resolved exactly for 03760 / 03788 / 03880 / 03901** |
| FX (B2) | BritTech £4,998.00 → €5,857.16 with rate + as-of evidence |
| Vendor match (B2) | 14 exact · V015 fuzzy ≥ 0.85 + country corroboration · LogiTrans unknown |
| Rules (B2) | R1/R2/R3/R4/R6 fire on oracle cases only |
| Duplicates (B3) | R5b noise stays unflagged (±20% amounts, 5–11-month gaps) · R5a impostor pairs fire, fuel trio doesn't |
| Decide (B4) | golden-file oracle: 17 invoice-level ladder tests + full-set `verify_oracle` == 11/3/3 · queue mapping · variant tells V1–V6 · gate-owned risk · A4 fallback |
| End-to-end (B4) | full run → dispositions == oracle (11/3/3), exit code 0 · `--no-ai` 12/2/3 == documented regex path |

---

## 5. REVIEW — checklist before ship

- [x] Clone-and-run on a **clean path** (no absolute D:\ dependencies) — data dir resolved relatively (`../ap-automation-assessment` · `input\` · `AP_DATA_DIR` · `--data-dir`); Windows measured · POSIX behaviour by construction (pathlib + UTF-8 + LF), named as unmeasured in README §7
- [x] UTF-8: vendor names (`OÜ`, `Sp.z.o.o.`), `€`/`£` render in CSV + HTML + console — CSVs written UTF-8 with BOM (`utf-8-sig`) for Excel; verified in `sample_output/`
- [x] Every AI claim in README demonstrable from committed output (TRA-03: *demo it or remove it*) — README numbers (14/17 trusted · 146/153 · 11/3/3 · 6+6 LLM prose · 2 pairs 69.2%/168.2%) verified against `sample_output/` batch_report + telemetry
- [x] Honest limitations list (§8 of CASE_STUDY_PLAN) matches measured reality — README §7 quotes measured noise + the `--no-ai` path difference + the unmeasured-POSIX tell
- [x] No input data baked into code/config — thresholds and rates committed as config; invoice data only ever in `output/` / `sample_output/`

## 6. SHIP — checklist

- [x] `sample_output/` = actual run on the provided batch — landed (fresh AI run, oracle MATCH, regenerated artifacts incl. `extraction_records.json`)
- [x] README (approach, decisions, limitations, tools) — landed with setup instructions sufficient for reviewer-alone execution
- [ ] push to `github.com/akashrathi219/ap-invoice-pipeline` — ⬜ the last step
- [ ] Public repo renders; clean-clone verified

---

## STATUS LOG

| Date | Phase | Result |
|---|---|---|
| 2026-10-17 | PLAN | This document · repo initialised · gh auth = AkashRathi219 · Ollama v0.35.0 reachable |
| 2026-10-17 | B1 BUILD | ingest + **AI-primary extraction 17/17** + regex corroboration witness + dual-read arithmetic verification · 40 offline + 1 llm test green · **totals 14/17 trusted, all = oracle incl. 3 wrap-joins**, 3 refused→review, agreement 95.4% · model auto-unloaded · n8n dropped (G8) · numbers in `RUN_LOG.md` |
| 2026-10-17 | B2 BUILD | `fx.py` + committed `config/fx_rates.csv` (GBP 1.1719, as-of 2025-06-30) · `vendor_match.py` (L1 exact 16/17 · L2 fuzzy path tested · L3 country) · `rules.py` (R1/R2/R3/R4/R6 + CLEAR/FLAG-REVIEW/HOLD ladder) · wired into `run.py` per-record + console · **20 new offline tests (61 total) green** · BritTech **£4,998 → €5,857.16** · dispositions **CLEAR 12 · FLAG-REVIEW 1 · HOLD 4**, every oracle rule on its oracle case only · 2 named deviations (StationOps R5a → B3, CleanStation fail-closed) in `RUN_LOG.md` |
| 2026-10-17 | B3 BUILD | `duplicates.py` (R5b vendor-scoped metadata + R5a embedding/quantity/counterparty gate + `ap-forensic-v1` verdict, deterministic fallback) · `enrich.py` (note tells + AI-authored messages, template fallback) · `rules.py` R3→review-flag, R2→skip when no master record · `run.py` refactored to **two passes** (extract/verify/link → batch duplicate layer → ladder + messages) · **27 new offline tests (88 total) green** · R5a **9 pairs published, 2 flagged** (both planted pairs cosine 1.000/qty 1.00, price 69.2% & 168.2%) · R5b **0 exact · 0 flagged**, 3 recurring rows in-tolerance/out-of-window quoted as evidence · fuel + maintenance + cleaning clusters (4 pairs at cosine 1.000) stay unflagged · model graded both clones **LOW** → gate keeps **CRITICAL** (`ai_risk_level` kept as advisory) · 6 messages (6 LLM · 0 template) · dispositions **CLEAR 11 · FLAG-REVIEW 3 · HOLD 3 = oracle stated totals** · §5.3 table/totals discrepancy documented · numbers in `RUN_LOG.md` |
| 2026-10-17 | B4 BUILD | `decide.py` (**one decision object**: disposition/queue/reasons/blockers/gate-owned risk/variant tells V1–V6/next action + A4 rationale `ap-decide-v1`, read-only, template fallback; re-derives disposition from rule results — grades a bare tell-set identically) · `summarize.py` + `report_template.html` (**6 artefacts**: invoices/exceptions CSVs · summary.md · batch_report.json · audit_log.jsonl · report.html) · `feedback.py` (**3 HITL loops**: `ap-telemetry-v1` with HITL slot · Loop A threshold suggestions +0.05 on reviewer REJECT, never rewrites config · Loop B few-shot bank seed in config, batch-derived in output · Loop C 10 Alpaca SFT pairs) · committed oracle fixture `config/oracle_dispositions.json` · **56 new offline tests (144 total) green incl. 17 invoice-level golden oracle tests** · AI run dispositions **CLEAR 11 · FLAG-REVIEW 3 · HOLD 3 → oracle MATCH** (invoice-by-invoice) · `--no-ai` **CLEAR 12 · 2 · HOLD 3 → documented DIFF** · A4 measured 6/6 LLM paragraphs · 6 artefacts regenerated every run, clean clone-and-run tree · numbers in `RUN_LOG.md` |
| 2026-10-17 | B5 BUILD | `review.py` (**interactive terminal review screen** via `--review`) · `reviewer_feedback.jsonl` append-only journal · journal replay (newest-event-wins) re-applied on subsequent runs · non-mutating disposition updates · `review_summary.md` three-part digest (FIRST RUN / FEEDBACK RECEIVED / FINAL RESULT) · Loop A threshold suggestion calibration · **10 new offline tests (156 total) green** · numbers in `RUN_LOG.md` |
| 2026-10-17 | B6 BUILD | `ingest.py` + `pipeline_config.json` (**dataset-agnostic ingest**: `expected_invoice_count: null` enables auto-detect mode processing any N-PDF batch; positive integer pins strict gate) · duplicate SHA-256 rejection in both modes · `summarize.py` **oracle scoping** (`status: N/A` for custom datasets with zero oracle overlap; `ok: True`) · **11 new offline tests (171 total) green** · numbers in `RUN_LOG.md` |
| 2026-10-17 | SHIP PREP | Assignment coverage pass: every §Submission deliverable verified against results · **`README.md` landed** (approach · setup-alone instructions · 8 named decisions · rule-by-rule measured table · AI-usage guardrails · 9 honest limitations incl. the unmeasured-POSIX + no-R2-case tells · tools) · **`sample_output/` landed** from a fresh AI run (11/3/3 → oracle MATCH · 14/17 trusted · agreement 146/153 · 6+6 LLM prose · README numbers verified against the committed sample) · REVIEW checklist 5/5 ticked · **171 offline tests green** · commits `d90824a` + `97fb60d` precede this row |
