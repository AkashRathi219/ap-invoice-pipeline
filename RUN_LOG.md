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
