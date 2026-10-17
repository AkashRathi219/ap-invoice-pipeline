# AP Invoice Processing Pipeline — Case Study Materials

## Overview

These materials form the input dataset for a technical recruitment case study. Candidates are asked to build an automated Accounts Payable invoice processing pipeline that validates, enriches, and routes incoming invoices against a vendor master and historical data.

---

## Files

### Reference Data

| File | Description |
|---|---|
| `vendor_master.csv` | Master list of 25 vendors with categories, payment terms, approved currencies, spend thresholds, and status (3 INACTIVE vendors, 1 requiring dual approval) |
| `historic_invoices.csv` | 120 historical invoice records from Jan 2024 – Jun 2025, used for duplicate detection and pattern validation |
| `data_dictionary.md` | Field-level descriptions for all columns in both CSV files |

## Invoice PDFs (`invoices/`)

| File | Description |
|---|---|
| `invoices/` | A batch of 17 vendor invoices in PDF format representing a typical working day. Invoices vary in layout, quality, and vendor type. Your solution must process all of them. |

---

## Business Rules

Your solution must handle the following scenarios. These reflect the rules the AP team applies manually today.

| Rule | Description |
|---|---|
| **Vendor validation** | Every invoice must be matched against `vendor_master.csv`. Invoices from unknown vendors or vendors with `status = INACTIVE` should be flagged and held. |
| **Currency validation** | Each vendor has an `approved_currency`. Invoices submitted in a different currency should be flagged for review. |
| **Spend threshold** | Each vendor has a `spend_threshold_eur`. Invoices exceeding this value should be flagged before reaching an approver. |
| **Dual approval** | Vendors with `requires_dual_approval = TRUE` must be routed for two approvals regardless of amount. |
| **Duplicate detection** | Cross-reference each invoice against `historic_invoices.csv`. Flag invoices that appear to be duplicates or near-duplicates of previously processed ones. |
| **Mandatory fields** | Invoices missing required fields (such as PO number) should be held pending completion. |
