# AP Invoice Batch — Summary for the AP Lead

*Generated 2026-10-17T09:11:58+00:00 · model `llama3.2:3b` · prompt `ap-extract-v1` · local-only (no invoice data leaves this machine)*

17 invoices were read and validated before reaching an approver. 6 need a human: 3 held (a mandatory condition failed) and 3 flagged for review. 11 cleared and are ready for the approver queue. Arithmetic verified 14/17 totals; a wrapped or disputed total is never passed as trusted. Start with INV-2025-03880 (OldGuard Security NV) — Confirm the vendor is reactivated in the master before payment.

## Dispositions

| Disposition | Count | Meaning |
|---|---|---|
| CLEAR | 11 | ready for the approver queue |
| FLAG-REVIEW | 3 | a human checks before approval |
| HOLD | 3 | a mandatory condition failed — payment blocked |

**Oracle §5.3 check:** MATCH — expected CLEAR 11 · FLAG-REVIEW 3 · HOLD 3. Every invoice matches the committed fixture.

## What needs attention

| # | Invoice | Vendor | Disposition | Why | Next action |
|---|---|---|---|---|---|
| 1 | INV-2025-03880 | OldGuard Security NV | HOLD | R1: vendor V018 status INACTIVE; R5a: SecureGuard AG (ACTIVE) invoices the same 2 services as OldGuard Security NV with matching quantities (match 1.00); unit prices at 168.2% of OldGuard Security NV's — clone-package evidence | Confirm the vendor is reactivated in the master before payment. |
| 2 | INV-2025-03955 | LogiTrans Express SRL | HOLD | R1: vendor 'LogiTrans Express SRL' not in master; R3: readers disagree on the total — no verified EUR amount to compare against the threshold (approver reviews the read); R5a: LogiTrans Express SRL (unknown vendor (no master record)) invoices the same 3 services as StationOps GmbH with matching quantities (match 1.00); unit prices at 69.2% of StationOps GmbH's — clone-package evidence | Add the vendor to the master (or reject the invoice) before any payment. |
| 3 | INV-2025-03752 | SparkIT BV | HOLD | R3: readers disagree on the total — no verified EUR amount to compare against the threshold (approver reviews the read); R6: no PO reference on INV-2025-03752 | Re-read the total (the two readers disagreed) or obtain the vendor's PDF confirmation. |
| 4 | INV-2025-03760 | StationOps GmbH | FLAG-REVIEW | R5a: LogiTrans Express SRL (unknown vendor (no master record)) invoices the same 3 services as StationOps GmbH with matching quantities (match 1.00); unit prices at 69.2% of StationOps GmbH's — clone-package evidence | Verify with the requesting department that this is not a duplicate charge before paying. |
| 5 | INV-2025-03901 | SecureGuard AG | FLAG-REVIEW | R4: dual approval required for V007; R5a: SecureGuard AG (ACTIVE) invoices the same 2 services as OldGuard Security NV with matching quantities (match 1.00); unit prices at 168.2% of OldGuard Security NV's — clone-package evidence | Collect the second approver's sign-off before payment. |
| 6 | INV-2025-03744 | CleanStation S.A. | FLAG-REVIEW | R3: readers disagree on the total — no verified EUR amount to compare against the threshold (approver reviews the read) | Re-read the total (the two readers disagreed) or obtain the vendor's PDF confirmation. |

### Held — payment blocked

- **INV-2025-03880 · OldGuard Security NV** — R1: vendor V018 status INACTIVE; R5a: SecureGuard AG (ACTIVE) invoices the same 2 services as OldGuard Security NV with matching quantities (match 1.00); unit prices at 168.2% of OldGuard Security NV's — clone-package evidence
  - Next action: Confirm the vendor is reactivated in the master before payment.
- **INV-2025-03955 · LogiTrans Express SRL** — R1: vendor 'LogiTrans Express SRL' not in master; R3: readers disagree on the total — no verified EUR amount to compare against the threshold (approver reviews the read); R5a: LogiTrans Express SRL (unknown vendor (no master record)) invoices the same 3 services as StationOps GmbH with matching quantities (match 1.00); unit prices at 69.2% of StationOps GmbH's — clone-package evidence
  - Next action: Add the vendor to the master (or reject the invoice) before any payment.
- **INV-2025-03752 · SparkIT BV** — R3: readers disagree on the total — no verified EUR amount to compare against the threshold (approver reviews the read); R6: no PO reference on INV-2025-03752
  - Next action: Re-read the total (the two readers disagreed) or obtain the vendor's PDF confirmation.

### Flagged for review

- **INV-2025-03760 · StationOps GmbH** — R5a: LogiTrans Express SRL (unknown vendor (no master record)) invoices the same 3 services as StationOps GmbH with matching quantities (match 1.00); unit prices at 69.2% of StationOps GmbH's — clone-package evidence
  - Next action: Verify with the requesting department that this is not a duplicate charge before paying.
- **INV-2025-03901 · SecureGuard AG** — R4: dual approval required for V007; R5a: SecureGuard AG (ACTIVE) invoices the same 2 services as OldGuard Security NV with matching quantities (match 1.00); unit prices at 168.2% of OldGuard Security NV's — clone-package evidence
  - Next action: Collect the second approver's sign-off before payment.
- **INV-2025-03744 · CleanStation S.A.** — R3: readers disagree on the total — no verified EUR amount to compare against the threshold (approver reviews the read)
  - Next action: Re-read the total (the two readers disagreed) or obtain the vendor's PDF confirmation.

## Cleared for approval

11 invoices passed every rule. 14/17 totals were verified by the arithmetic authority (subtotal + VAT); a wrapped or disputed total is never passed as trusted.

## Duplicate & impostor evidence

| Pair | cosine | qty match | unit price B/A | pattern | verdict |
|---|---|---|---|---|---|
| EuroFuel GmbH ↔ TotalRoute S.A. | 1.0 | 0.7806 | 100.0% | SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY | evidence |
| EuroFuel GmbH ↔ AlpinaDiesel AG | 0.8886 | 0.513 | 100.0% | SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY | evidence |
| FleetMech GmbH ↔ AutoServ S.R.L. | 1.0 | 1.0 | 79.4% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| FleetMech GmbH ↔ IberMaint S.L. | 1.0 | 1.0 | 120.6% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| AutoServ S.R.L. ↔ IberMaint S.L. | 1.0 | 1.0 | 151.8% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| CleanStation S.A. ↔ NordClean ApS | 1.0 | 1.0 | 90.6% | SHARED_PACKAGE_APPROVED_VENDORS | evidence |
| StationOps GmbH ↔ LogiTrans Express SRL | 1.0 | 1.0 | 69.2% | CLONE_PACKAGE_BLOCKED_VENDOR | FLAG |
| AlpinaDiesel AG ↔ TotalRoute S.A. | 0.8886 | 0.1318 | 100.0% | SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY | evidence |
| OldGuard Security NV ↔ SecureGuard AG | 1.0 | 1.0 | 168.2% | CLONE_PACKAGE_BLOCKED_VENDOR | FLAG |

## How AI was used in this batch

- **Extraction** — ai_primary: 17
- **Similarity backend** — `char4gram-cosine`
- **Impostor forensic verdicts (LLM)** — 4
- **Exception messages (LLM prose)** — 6
- **Decision rationales (LLM prose)** — 6
- Model, prompt and schema versions are pinned in `ai_telemetry.jsonl` and `audit_log.jsonl`; the disposition ladder itself is deterministic.

## Known limitations

- FX rates are a committed static table with an as-of date, not a live feed.
- The semantic duplicate threshold is heuristic and configurable; every flag carries its cosine/quantity/price evidence.
- The two 'scan' PDFs carry a text layer; a true image-only scan would need OCR.
- The pipeline is a filter before approvers, not an authority: it never approves or pays.
- A small local model misreads numbers; every misread is caught by the arithmetic authority or the second independent read, never passed silently.
