# AP Intelligent Processing Pipeline

Reads a daily batch of vendor invoices, validates each one against the AP business rules,
surfaces every exception with a named reason and a single next action, and hands the AP lead
a concise summary — **before anything reaches an approver**.

Built for the Flix take-home assessment (BA – Automation & AI Solutions). One command,
one repo, local-only: no API keys, no cloud, no specialist infrastructure.

**Measured result on the provided 17-invoice batch:** 17/17 read · 14/17 totals verified by
arithmetic · **CLEAR 11 · FLAG-REVIEW 3 · HOLD 3 — invoice-by-invoice match with the expected
dispositions** · 2 impostor pairs flagged (cosine 1.000, quantities 1.00, price scale 69.2% /
168.2%) · every number below is reproducible by `python run.py` and recorded in `RUN_LOG.md`.

---

## 1. Quick start

```bash
# 0. Clone and install dependencies (Python 3.11+; measured on 3.13)
git clone https://github.com/AkashRathi219/ap-invoice-pipeline.git
cd ap-invoice-pipeline
pip install -r requirements.txt          # PyMuPDF + pytest — nothing else

# 1. Input Data: Self-Contained and Pre-Bundled
#    The assessment dataset (17 PDFs, vendor_master.csv, historic_invoices.csv)
#    is bundled directly inside ./ap-automation-assessment so it runs out of the box!
#    (Alternatively: pass --data-dir <path> or set AP_DATA_DIR to point to another folder)

# 2. Run
python run.py                 # AI-primary path — needs local Ollama (see below)
python run.py --no-ai         # Full pipeline with zero AI — deterministic fallback path
python run.py --limit 5       # Quick demo: process first N invoices
python run.py --only 03760    # Process one invoice by filename fragment
python run.py --out-dir sample_output   # Write output artefacts anywhere
python run.py --review        # After the run: interactive terminal review of the exception queue

python -m pytest -q           # 171 offline tests — no live network/LLM needed
```

**The AI path needs a local Ollama** (`ollama pull llama3.2:3b`, then `ollama serve` — default
`127.0.0.1:11434`). It uses **stdlib HTTP — no Python package, no API key**. If Ollama is not
running, `python run.py` falls back to the named `regex_fallback` path with a loud warning and
still completes; `--no-ai` forces that path deliberately. Exit codes: `0` ok · `1` control-record
failure · `2` nothing readable.

**Human review.** `python run.py --review` opens an interactive terminal review of the non-CLEAR
exception queue after the run (ACCEPT / REJECT / OVERRIDE / SKIP / QUIT). The menu opens only
when `stdin` is a TTY — in a pipe or CI it prints `not an interactive terminal` and continues.

**Batch size & the §5.3 oracle — dataset-agnostic ingest.** The ingest count expectation
**defaults to auto-detect**: `expected_invoice_count: null` in `config/pipeline_config.json`
(or the key absent) derives the batch size from the PDFs actually present in the input
directory — any N-PDF batch is processed as-is, with no truncation control. Setting a
**positive integer pins a strict gate** instead: a count mismatch aborts with exit code 1
*before any extraction or model call*, and the `Control record:` console line always names the
active mode (`control mode: strict (expected_invoice_count=<M>)` vs
`control mode: auto-detect (no truncation control)`). Duplicate SHA-256 hashes fail with exit
code 1 in **both** modes. The golden oracle is scoped to the §5.3 assessment set: a batch that
shares **no** invoice number with `config/oracle_dispositions.json` reports
`Oracle Status : N/A` rather than a false DIFF, while any overlap keeps the
invoice-by-invoice MATCH/DIFF evaluation unchanged.

**Sample output.** `sample_output/` is the committed result of the provided batch (AI path).
`output/` is regenerated and git-ignored on every run. `sample_output/` is what a reviewer can
diff their fresh run against.

---

## 2. Approach — one paragraph

AI reads every invoice first; arithmetic decides. A schema-constrained local model (`llama3.2:3b`,
temperature 0) is the **primary reader** of all 17 PDFs (both format classes, imperfect text layers
included). A second, independent regex stream corroborates it. The **trusted total is
`subtotal + VAT`** — published only when both reads agree on the inputs; a line-wrapped or disputed
total is *refused*, never passed. The refused total then becomes an exception with a reason, which
is exactly the AP behaviour the assignment asks for. Vendor identity resolves exact → normalised
fuzzy (≥ 0.85) → country corroboration against the master; FX normalisation uses a committed rate
table with an as-of date and fails closed on an unknown currency; historic and within-batch
duplicate layers publish **scored evidence**; one deterministic ladder turns all of it into
CLEAR / FLAG-REVIEW / HOLD. The model may grade and explain — it never owns a severity or a
disposition.

## 3. The six business rules → what they found

| Rule | Implementation | Measured on the provided batch |
|---|---|---|
| **Vendor validation** | master match → `unknown` or `INACTIVE` → **HOLD** | OldGuard (INACTIVE) and LogiTrans (not in master) both **held** |
| **Currency validation** | invoice currency vs `approved_currency` → **FLAG-REVIEW** | no batch case (all vendors billed in their approved currency); covered by offline tests |
| **Spend threshold** | EUR-normalised amount vs `spend_threshold_eur` → **FLAG-REVIEW** | BritTech £4,998 → **€5,857.16** (rate 1.1719, as-of 2025-06-30) passes under its 20k threshold *with FX evidence attached* |
| **Dual approval** | `requires_dual_approval` → route for two approvers | SecureGuard €18,499.50 **flagged** for dual routing |
| **Duplicate detection** | vs `historic_invoices.csv` (±2% ∧ 30-day window) + whole-batch semantic layer | **0 historic duplicates** — the planted history contains recurring-but-benign services, quoted as scored evidence, not flags; **2 within-batch impostor pairs flagged** (see below) |
| **Mandatory fields** | PO number (and presence checks) missing → **HOLD pending completion** | SparkIT held — no PO reference |

**The duplicates the batch actually plants are cross-vendor impostor pairs**, and they are the
highlight: StationOps ↔ LogiTrans (same 3 services, prices scaled to 69.2%) and
OldGuard ↔ SecureGuard (same 2 services, 168.2%, active/inactive split). Both publish
cosine 1.000, quantity match 1.00 and a uniform price ratio as evidence; flagged pairs get a
structured forensic verdict from the local model — whose grade may **raise** the severity but can
never lower it (it graded both clones LOW; the arithmetic gate kept CRITICAL).

**The full disposition set** (AI path): 3 HOLD — OldGuard (inactive), LogiTrans (unknown), SparkIT
(no PO) · 3 FLAG-REVIEW — CleanStation (total refuses dual-read verification → fail-closed),
StationOps (impostor pair), SecureGuard (dual approval). 11 CLEAR.

## 4. Outputs — what the AP team receives

| File | For | Contents |
|---|---|---|
| `invoices_processed.csv` | auditors | one row per invoice: fields, provenance (agreement score, line math), decision, reason codes, variant ids |
| `exceptions.csv` | **the review queue** | prioritised (HOLD → FLAG-REVIEW, worst risk first): rule fired, scored evidence, next action, counterparty, message subject |
| `summary.md` | **the AP lead** | dispositions, what needs attention with reasons and actions, held/flagged breakdown, cleared, duplicate evidence, AI usage, known limitations |
| `report.html` | the walk-through | self-contained (no CDN): oracle badge, exception queue, duplicate/impostor evidence, AI usage |
| `batch_report.json` | systems | per-invoice decision envelopes + run block + the invoice-by-invoice oracle verdict |
| `audit_log.jsonl` | audit | 1 `RUN_START` + 17 `INVOICE_DECIDED`: hashes, model/prompt, tokens, agreement, decisions, prose sources |
| `ai_telemetry.jsonl` | **HITL channel** | one `ap-telemetry-v1` row per invoice with a `reviewer_feedback: null` slot for ACCEPT / REJECT / OVERRIDE |
| `reviewer_feedback.jsonl` · `review_summary.md` | **human-review loop** (`--review`) | append-only journal of reviewer actions, re-applied on every run — newest event per invoice wins · a markdown digest of the review (FIRST RUN / FEEDBACK RECEIVED / FINAL RESULT) |
| `fine_tuning_dataset.jsonl` · `few_shot_bank.json` · `threshold_suggestions.json` | continuous improvement | Alpaca SFT pairs · batch-derived exemplars · cosine-threshold suggestions reflecting the reviewer journal (never a silent config rewrite) |

CSVs are UTF-8 **with BOM** so `QuickFix OÜ`, `PolyClean Sp.z.o.o.` and `€/£` open correctly in
Excel on Windows. `summary.md`, `report.html` and the JSON/JSONL artefacts are plain UTF-8.

**Reviewer actions never alter the first-run machine artefacts** — the six files
(`invoices_processed.csv` … `audit_log.jsonl`) stay exactly as the machine issued them; the replay
lives in `reviewer_feedback.jsonl` and `review_summary.md`, and `threshold_suggestions.json`
reflects the journal.

## 5. Key decisions (deliberate, named)

1. **AI-primary reader, arithmetic authority** — the model *understands* the documents (the
   assignment's "AI must be meaningful"); math *disposes* (the panel's money line). Both reads are
   logged field-by-field; agreement is scored (146/153 on the batch).
2. **A trusted total must be witnessed twice** — `subtotal + VAT` is only published when the two
   independent reads agree. Self-consistent garbage is precisely what this catches.
3. **Fail-closed everywhere** — unknown currency, unverifiable total, unknown vendor: each becomes
   an *exception with a reason*, never a silent pass.
4. **Duplicates publish scores, the ladder owns dispositions** — 7 of 9 published pairs stayed
   unflagged (including 4 at cosine 1.000); earning a flag needs tolerance ∧ date-window ∧
   counterparty standing, not a big cosine.
5. **Severity is gate-owned** — a model grade may raise, never lower. Reason codes and next
   actions are deterministic even when the prose layer garbles an identifier (measured once).
6. **One committed oracle as the golden test** — `config/oracle_dispositions.json` states, per
   invoice, the rule outcomes that must produce the expected disposition. It is driven offline by
   17 invoice-level ladder tests **and** reconciled invoice-by-invoice on every run.
7. **Small dependency surface** — core = PyMuPDF + pytest; everything else (difflib, decimal,
   csv, urllib) is stdlib. Heavy embeddings are opt-in and never required: the default similarity
   backend is a dependency-free 4-gram cosine, measured end-to-end.
8. **Cross-platform by construction** — `pathlib` only, UTF-8 everywhere, `.gitattributes`
   `eol=lf`, no shell scripts, one entry point. No absolute paths: the starter pack is discovered
   relatively, or via `--data-dir` / `AP_DATA_DIR`.

## 6. Where AI fires (and what stays deterministic)

| AI use | Fires | Guardrail |
|---|---|---|
| **Document understanding** — `ap-extract-v1`, JSON-schema-constrained, temp 0, 17/17 | every invoice | dual-read verification; per-field provenance; regex fallback keeps the pipeline alive |
| **Semantic impostor detection** — embeddings + forensic verdicts on flagged pairs | every batch | deterministic impostor gate owns severity; verdict is evidence |
| **Enrichment** — exception messages (`ap-enrich-v1`) | every non-CLEAR invoice | reason codes and next actions are deterministic inputs; the model drafts prose around them |
| **Decision rationales** — one-paragraph reviewer summary (`ap-decide-v1`) | every non-CLEAR invoice, read-only post-decision | disposition already fixed; the model may describe, never change |
| **HITL loops** — telemetry, few-shot bank, threshold suggestions, SFT export | every run | reviewer actions are validated and append-only; suggestions never rewrite config |

**What stays deterministic (no AI at all):** ingest, the corroboration regex, arithmetic
verification, FX, vendor matching, the six rules, historic duplicates, the whole decide ladder,
all outputs, all thresholds. `python run.py --no-ai` runs the entire chain with zero AI.

## 7. Known limitations (honest)

- **FX is a committed static table** with an as-of date — auditable and honest, but not a live feed.
- **The semantic duplicate threshold is heuristic** and configurable. Every flag carries its
  cosine / quantity / price evidence; 7 of 9 published pairs are unflagged by design, which is the
  harder half of the problem.
- **The two "scan" PDFs carry a text layer.** A true image-only scan would need OCR — out of scope,
  named here.
- **The local 3B model's prose is a draft for a human.** Measured noise: it once garbled a
  counterparty invoice id and once paraphrased evidence beyond its source. Every actionable field
  (severity, reason codes, next action, evidence) is deterministic and unaffected.
- **The oracle's own documentation has a discrepancy** — the source table lists 12 CLEAR / 2 FLAG
  while its stated totals are 11 / 3 / 3. The stated totals are honoured: CleanStation is
  FLAG-REVIEW because its total refuses dual-read verification (the fail-closed behaviour is the
  design, not a bug).
- **The `--no-ai` path grades 12 / 2 / 3**, not the oracle 11 / 3 / 3: the regex second read
  verifies all totals, so CleanStation clears. This is the documented regex-path expectation and
  surfaces as a loud oracle DIFF.
- **No R2 currency case exists in the batch** — every vendor billed in its approved currency. The
  rule is implemented and covered by offline tests, but it does not fire on this data.
- **Cross-platform is by construction, measured on Windows.** macOS/Linux behaviour follows from
  pathlib + UTF-8 + LF, but was not measured on a POSIX box for this submission.
- **`historic_invoices.csv` has no description column** — historic duplicates are metadata-only
  (id/vendor/amount/date/PO); text-semantic comparison is possible only within the batch.

## 8. Tools used

| Area | Tool | Why |
|---|---|---|
| LLM runtime | **Ollama** · `llama3.2:3b` · temperature 0 · JSON-schema-constrained | fully local, no API key, free |
| PDF text | **PyMuPDF (fitz)** | parses these malformed PDFs cleanly (page-wrapped totals included) |
| Tests | **pytest** | 171 offline tests; the oracle doubles as the golden fixture |
| Similarity | stdlib `char4gram-cosine` (default) · `sentence-transformers` opt-in | zero heavy deps on the core path; the assignment's reference embeddings remain an upgrade, not a requirement |
| Everything else | Python stdlib | `csv` · `difflib` · `decimal` (money is never float) · `urllib` (Ollama over stdlib HTTP) · `hashlib` |

## 9. Repository map

| Path | Role |
|---|---|
| `run.py` | the pipeline, S1–S11 + the `--review` terminal review, one command |
| `src/` | one module per owned link/rule (`DEVELOPMENT_PLAN.md` §3 maps file → step) |
| `config/` | committed config: thresholds, FX rates, **the oracle**, few-shot seed |
| `tests/` | 171 offline tests |
| `RUN_LOG.md` | **measured results at consumption** — the only place numbers live |
| `DEVELOPMENT_PLAN.md` | plan of record + status log (SPEC → PLAN → BUILD → TEST → REVIEW → SHIP) |
| `sample_output/` | the committed result of the provided batch |

---

*Status: working code, deliberate decisions, honest reflection. A partial but well-structured
solution is preferable to an over-engineered one — this one is structured, measured, and honest
about both what it catches and what it cannot.*
