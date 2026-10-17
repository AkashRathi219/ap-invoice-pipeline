# RUN LOG — measured results (never the plan)

Per ground rule G6: measured numbers live here, at the point of consumption. The plan
(`DEVELOPMENT_PLAN.md`) states intent only.

## Environment

| | |
|---|---|
| Date | 2026-10-17 |
| Python | 3.13.13 |
| PyMuPDF | 1.28.0 |
| pytest | 9.1.1 |
| LLM runtime | Ollama 0.35.0 · model `llama3.2:3b` · temperature 0 · JSON-schema constrained |
| LLM memory | `keep_alive=5m` during the batch, then **unloaded (`keep_alive=0`) at end of run** (G9) |
| Platform | Windows 11 (all paths via `pathlib`; identical behaviour on macOS/Linux) |

## Command

```
python run.py
```

## Control record

- `17/17` PDFs found · unique SHA-256 · **0 extraction failures**
- Control record gate passed before any AI call was made.

## Measured results (AI-primary, `llama3.2:3b`, prompt `ap-extract-v1`)

| # | Invoice | Vendor | Trusted total | Cur | Line math | AI↔regex | Total status |
|---|---|---|---|---|---|---|---|
| 1 | INV-2025-03701 | EuroFuel GmbH | 8551.67 | EUR | ok | 9/9 | ok |
| 2 | INV-2025-03712 | FleetMech GmbH | 6799.99 | EUR | ok | 9/9 | ok |
| 3 | INV-2025-03719 | AutoServ S.R.L. | 5400.01 | EUR | **FAIL** | 9/9 | ok |
| 4 | INV-2025-03731 | BritTech Solutions Ltd | 4998.00 | GBP | ok | 9/9 | ok |
| 5 | INV-2025-03744 | CleanStation S.A. | **—** | EUR | ok | 8/9 | **REVIEW** |
| 6 | INV-2025-03752 | SparkIT BV | **—** | EUR | **FAIL** | 6/9 | **REVIEW** |
| 7 | INV-2025-03760 | StationOps GmbH | 10010.46 | EUR | ok | 9/9 | **WRAP-join** |
| 8 | INV-2025-03771 | MediaBoost GmbH | 4512.02 | EUR | ok | 9/9 | ok |
| 9 | INV-2025-03779 | NordClean ApS | 2900.01 | EUR | ok | 9/9 | ok |
| 10 | INV-2025-03788 | AlpinaDiesel AG | 42121.24 | EUR | ok | 8/9 | **WRAP-join** |
| 11 | INV-2025-03791 | IberMaint S.L. | 8200.00 | EUR | ok | 9/9 | ok |
| 12 | INV-2025-03847 | TotalRoute S.A. | 5788.11 | EUR | **FAIL** | 9/9 | ok |
| 13 | INV-2025-03880 | OldGuard Security NV | 11000.59 | EUR | ok | 9/9 | **WRAP-join** |
| 14 | INV-2025-03901 | SecureGuard AG | 18499.50 | EUR | **FAIL** | 9/9 | ok |
| 15 | INV-2025-03955 | LogiTrans Express SRL | **—** | EUR | **FAIL** | 7/9 | **REVIEW** |
| 16 | INV-2025-SCAN-001 | QuickFix OÜ | 1606.50 | EUR | ok | 9/9 | ok |
| 17 | INV-2025-SCAN-002 | PolyClean Sp.z.o.o. | 2058.70 | EUR | ok | 9/9 | ok |

### Roll-up

| Metric | Result |
|---|---|
| Extraction layer | `ai_primary` **17/17** — AI reads every file by default |
| Trusted totals published | **14/17 — and all 14 equal the oracle totals** |
| Page-wrapped totals joined correctly | **3** (StationOps, AlpinaDiesel, OldGuard) |
| Totals refused (sent to review) | **3** (CleanStation, SparkIT, LogiTrans) |
| AI↔regex field agreement | **146/153 = 95.4 %** |
| Line math verified | 12/17 ok |
| Mean AI latency | **4792 ms / invoice** |
| Model left in RAM | none — unloaded at end |

## Honest reading

- `llama3.2:3b` is a small model and it **does misread numbers**. That is a measured fact,
  not a hidden one. What matters is where each misread ends up:
  - a **page-wrapped** total (the trap) → the witness tokens join and the **arithmetic
    authority publishes the correct total** (3/3 correct: 10010.46, 42121.24, 11000.59);
  - a **genuine** misread → the second independent read disagrees, so **no trusted total is
    published** and the invoice is flagged for review (3/3 caught).
- **No wrong total was ever published as trusted.** The 3 refusals are the correct behaviour,
  not a defect: two independent reads disagreed on the authority's inputs.
- The 5 line-math failures did **not** corrupt any total (totals are validated independently
  of the rows); they are line-item transcription misses worth a reviewer's eye.
- SparkIT and LogiTrans also carry no usable PO / unknown vendor, so they are destined for
  HOLD at the decide stage regardless of numbers.

## Model is a configuration lever, not an architecture change

`config/pipeline_config.json` → `llm.model`. The default (`llama3.2:3b`, 2.0 GB) is chosen for
RAM footprint per directive G9. A larger local model raises raw extraction accuracy with **no
code change** — the verification layer, provenance, and refusal policy are unchanged. The
pipeline is deliberately model-agnostic.

---

# B2 — FX · vendor match · rules (S4–S6) — measured

New modules: `src/fx.py` · `src/vendor_match.py` · `src/rules.py` · `config/fx_rates.csv`.
Every record now carries `vendor_match`, `fx`, and `rules` blocks in
`output/extraction_records.json`; the console prints the disposition per invoice.

## FX normalisation (L5)

| Item | Measured |
|---|---|
| Committed table | `config/fx_rates.csv` — EUR 1.0 · GBP 1.1719 · as-of **2025-06-30** (P-E07) |
| BritTech (the only non-EUR invoice) | GBP 4,998.00 → **€5,857.16** via rate 1.1719, as-of evidence attached |
| Currencies without a committed rate | convert to **nothing** (`fx_known=false`) — never a guessed rate |
| EUR invoices | identity conversion (rate 1.0), `fx_known=true` |

## Vendor match (L1 → L2 → L3)

| Item | Measured |
|---|---|
| Exact (normalised) | **16/17** — incl. `QuickFix OÜ` and `PolyClean Sp.z.o.o.` (Unicode + case folding) |
| Unknown | **1/17** — `LogiTrans Express SRL` (no Romanian vendor in the master) |
| Fuzzy path | **not exercised by this batch** — the invoice and master strings normalise to the *same* key, so L1 correctly wins; the diacritic-variant case (`QuickFix OÜ` vs `QuickFix OAo`, ratio 0.870 ≥ 0.85) is covered by `tests/test_vendor_match.py` |
| Country corroboration | recorded per invoice (`country_match`); no conflicts in this batch |

## Rules R1/R2/R3/R4/R6 (L1–L6) — dispositions

| Disposition | Count | Records |
|---|---|---|
| **CLEAR** | **12** | the 11 oracle CLEARs + StationOps |
| **FLAG-REVIEW** | **1** | SecureGuard (`R4_dual_approval_required`) |
| **HOLD** | **4** | OldGuard (`R1_inactive_vendor`) · LogiTrans (`R1_unknown_vendor`) · SparkIT (`R6_po_missing`) · CleanStation (`R3_amount_unavailable`) |

Every oracle rule fired on its oracle case and **nowhere else**:
`R1` inactive → OldGuard, `R1` unknown → LogiTrans, `R4` → SecureGuard, `R6` → SparkIT,
`R2` currency mismatch → none (every invoice currency is master-approved), `R3` above
threshold → none (BritTech clears 20,000 only *after* FX: €5,857.16 ≤ €20,000).

### Two deliberate deviations from the §5.3 oracle — and why

1. **StationOps is CLEAR here, FLAG-REVIEW in the oracle.** Its oracle flag is the R5a
   near-duplicate pair with LogiTrans — that is the `duplicates.py` increment (**B3**), not B2.
2. **CleanStation is HOLD, not CLEAR.** Its total was one of the three *refused* by the
   dual-read verification in B1 (the 3B model misread its numbers), so B2 has no trusted EUR
   amount to compare against the 8,000 threshold and `R3` **fails closed** rather than clearing
   on an unverified number. This is the designed behaviour of *"no unverified amount reaches an
   approver"*; it reconciles at B4 when `decide.py` folds the verification flags into one ladder
   (a stronger model removes the refusal entirely — no code change).

## Tests

```
python -m pytest -q        # 61 passed
```

20 new offline tests: FX conversion + fail-closed (§ `test_fx.py`), exact/fuzzy/unknown vendor
resolution (§ `test_vendor_match.py`), each rule's reason code + the disposition ladder
(§ `test_rules.py`).

## Noted environment variance

Mean AI latency this run was **7,743 ms/invoice** (B1 measured 4,792 ms) with identical model and
prompt — the box was running several full 17-file verification passes back-to-back during B2.
Extraction, verification, and disposition results are unchanged across those runs; latency is
quoted as measured, not as a specification.

---

# B3 — Duplicates (S7–S8) · Enrichment (S9) — measured

New modules: `src/duplicates.py` (R5b metadata + R5a semantic + forensic verdict) · `src/enrich.py`.
`src/rules.py` changed twice, on purpose: an **unverified total is a review flag**, not a structural
hold, and R2 **skips** when there is no master record to compare against. `run.py` is now two-pass —
pass 1 extracts, verifies and links (R1–R6 + R5b), pass 2 compares the batch (R5a), folds every
layer into one ladder, and writes the messages.

Command: `python run.py` · AI-primary `llama3.2:3b` · prompt `ap-extract-v1` · temperature 0.

## R5a — within-batch semantic duplication

Embedding backend **`char4gram-cosine`**: `sentence-transformers` is *not installed on this box*, so
the dependency-free character 4-gram cosine ran. The backend is named inside every score
(`embedding_backend`), and installing `requirements-extras.txt` switches it to `all-MiniLM-L6-v2`
with no code change — no score is ever anonymous about how it was produced.

Pairs at/above the 0.60 evidence floor — **9 published, 2 flagged**:

| A ↔ B | cosine | qty match | unit price B/A | pattern | fires |
|---|---|---|---|---|---|
| StationOps ↔ LogiTrans | 1.000 | 1.00 | 69.2% | CLONE_PACKAGE_BLOCKED_VENDOR | **FLAG** |
| OldGuard ↔ SecureGuard | 1.000 | 1.00 | 168.2% | CLONE_PACKAGE_BLOCKED_VENDOR | **FLAG** |
| CleanStation ↔ NordClean | 1.000 | 1.00 | 90.6% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| FleetMech ↔ AutoServ | 1.000 | 1.00 | 79.4% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| FleetMech ↔ IberMaint | 1.000 | 1.00 | 120.6% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| AutoServ ↔ IberMaint | 1.000 | 1.00 | 151.9% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| EuroFuel ↔ TotalRoute | 1.000 | 0.78 | 100.0% | SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY | evidence |
| EuroFuel ↔ AlpinaDiesel | 0.889 | 0.51 | 100.0% | SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY | evidence |
| AlpinaDiesel ↔ TotalRoute | 0.889 | 0.13 | 100.0% | SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY | evidence |

The **fuel trio** is separated by quantity arithmetic alone (cosine 1.000 on identical route
descriptions, quantities 2291/2291/200 vs 1537/1537/200 vs 8500/8500/200 → qty match 0.78/0.51/0.13).

The maintenance and cleaning clusters are *also* cosine 1.000 with matching quantities — so similarity
alone would have produced **6 flags instead of 2**. The gate therefore requires one more fact: a
counterparty that is **not payment-eligible** (no master record, or INACTIVE). Two approved ACTIVE
vendors quoting the same standard package is a framework-agreement fact, published as scored
evidence for the approver. The planted pairs both carry a blocked counterparty (LogiTrans unknown ·
OldGuard INACTIVE) — that is what makes them impostors, and the pipeline says so explicitly.

## R5b — metadata duplicates vs history

**0 exact · 0 flagged.** No invoice number and no PO on this batch exists in the historic file, and no
same-vendor row falls inside **both** the committed tolerance (±2%) and window (30 days):

| Invoice | same-vendor rows | closest row | amount Δ | date gap | verdict |
|---|---|---|---|---|---|
| EuroFuel | 5 | INV-2024-10118 | 0.30% | 176 d | in tolerance, outside window → evidence |
| AlpinaDiesel | 6 | INV-2024-10034 | 0.30% | 176 d | in tolerance, outside window → evidence |
| IberMaint | 10 | INV-2024-10111 | 1.36% | 155 d | in tolerance, outside window → evidence |
| StationOps | 5 | INV-2024-10062 | 25.29% | 3 d | inside window, far in amount → evidence |
| AutoServ | 3 | INV-2024-10019 | 3.72% | 259 d | neither |
| StationOps | 5 | INV-2024-10035 | 2.33% | 156 d | neither |

Both single-signal near-misses are recorded with their scores and neither is flagged: a recurring
fuel delivery 5–6 months later is a delivery, and a 25%-different amount 3 days after a previous
invoice is a different order. The historic status ladder is carried on each row (PAID > APPROVED >
PROCESSED); the historic file has **no description column**, so R5b is deliberately metadata-only —
the semantic work belongs to R5a.

## R5a → local LLM forensic verdict — and who owns the severity

Both flagged pairs were escalated to `llama3.2:3b` (`ap-forensic-v1`). The model graded **both
confirmed clones `LOW`** (and `is_impostor_risk: false`) while its own prose described *"a
cloned/discounted copy of an approved charge package"*. The measured output, verbatim:

```
verdict source=llm  risk_level=CRITICAL  gate_risk_level=CRITICAL  ai_risk_level=LOW
```

So the arithmetic gate owns the operative severity and is only ever *raised* by the model, never
lowered. The model's grade is retained verbatim as `ai_risk_level` and the deterministic
`gate_explanation` is always present alongside the model's `explanation`. This is *"AI proposes,
arithmetic disposes"* doing real work on real model output — not a hypothetical guard.

## S9 — enrichment

**6 messages authored (6 LLM · 0 template)** — every non-CLEAR invoice. The prose is model-written;
the actionable field is not: `next_action` always comes from the deterministic action map
(a model suggestion is kept as `ai_next_action`), and `reason_codes` are copied from the rule layer.
Note tells: **1 record** (TotalRoute) → `VENDOR_RESUBMISSION` + `DELIVERY_DOCUMENTATION`, impact
`informational` — surfaced to the human, never a blocker.

## Dispositions — all layers folded

| Disposition | Count | Records |
|---|---|---|
| **CLEAR** | **11** | the 11 oracle CLEARs |
| **FLAG-REVIEW** | **3** | StationOps (`R5a_clone_package`) · CleanStation (`R3_amount_unavailable`) · SecureGuard (`R4_dual_approval_required` + `R5a_clone_package`) |
| **HOLD** | **3** | OldGuard (`R1_inactive_vendor` + `R5a_clone_package`) · LogiTrans (`R1_unknown_vendor` + `R3_amount_unavailable` + `R5a_clone_package`) · SparkIT (`R6_po_missing` + `R3_amount_unavailable`) |

This is exactly the oracle's **stated** totals (11 / 3 / 3). B2's two named deviations are both now
resolved: StationOps is flagged by R5a as the oracle expects, and CleanStation's unverified total is a
review flag rather than a structural hold. Impostor evidence is attached **symmetrically** to both
invoices in a pair, so whichever of the two reaches a human carries the same finding.

### Oracle table vs oracle totals — a documentation discrepancy, not a pipeline one

§5.3 of the source plan states CLEAR 11 / FLAG-REVIEW 3 / HOLD 3 in prose, while its own table lists
CLEAR 12 / FLAG-REVIEW 2 / HOLD 3. The stated totals are honoured here: the table appears to count
CleanStation as CLEAR while also recording it among the refused totals. Nothing in the pipeline
depends on that count — every disposition is derived from a named reason code.

## Honest reading

- Both planted impostor pairs fired at **cosine 1.000 with identical quantities** and a **uniform
  price scale** (69.2% and 168.2%) — found by arithmetic, explained by the model.
- **7 of 9 published pairs stayed unflagged**, including 4 pairs at cosine 1.000. Publishing a
  similarity score is cheap; earning a flag is not.
- The 3B model's prose is a **draft for a human**: this run it garbled one counterparty invoice number
  (03880 → 03801) and mis-stated the price relationship once. Every actionable artifact — severity,
  reason codes, next action, pair evidence — is deterministic and unaffected.
- No unverified number can reach the payment queue: an unverified total is now a review flag, so it
  still never passes silently.

## Tests

```
python -m pytest -q        # 88 passed
```

27 new offline tests: R5b tolerance/window/recurrence, R5a quantity and counterparty gates, verdict
ownership (a model grade **cannot** downgrade the gate; an unreachable model falls back cleanly),
enrichment tells, and message field ownership. No test touches the network.

## Noted environment variance

Mean AI latency was **5,017 ms/invoice** on the measurement run (B1 4,792 ms · B2 7,743 ms) with the
same model and prompt, and **5,086 ms** on an immediate repeat; the B2 figure was taken while several
verification passes ran back-to-back. Latency is quoted as measured, never as a specification.

That repeat run is the reproducibility check: **trusted totals 14/17 · line-math 12/17 · agreement
146/153 · 9 pairs published / 2 flagged · 0 historic flags · 6 LLM messages · CLEAR 11 · FLAG-REVIEW 3 ·
HOLD 3** — identical to the run above, with only latency differing.

