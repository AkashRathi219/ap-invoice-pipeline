# Data Dictionary

## vendor_master.csv

| Field | Type | Description |
|---|---|---|
| vendor_id | String | Unique vendor identifier (format: V001–V025) |
| vendor_name | String | Registered trading name of the vendor |
| category | String | Spend category: Fuel, Maintenance, Station Operations, IT Services, Cleaning, Security, Marketing, or Legal |
| payment_terms_days | Integer | Standard payment terms in days (30, 45, or 60) |
| approved_currency | String | ISO 4217 currency code approved for invoicing this vendor (EUR, USD, GBP, CHF) |
| spend_threshold_eur | Integer | Maximum single-invoice amount in EUR before additional approval is required |
| status | String | Vendor standing: ACTIVE (eligible to process invoices) or INACTIVE (invoices must be rejected) |
| country | String | Country of vendor registration |
| contact_email | String | Vendor accounts payable contact email address |
| requires_dual_approval | Boolean | TRUE if invoices from this vendor require two approvers regardless of amount; FALSE otherwise |

## historic_invoices.csv

| Field | Type | Description |
|---|---|---|
| invoice_id | String | Unique invoice identifier (format: INV-YYYY-NNNNN) |
| vendor_id | String | Reference to vendor_master.vendor_id |
| vendor_name | String | Vendor name as stated on the invoice (may differ from master in error scenarios) |
| invoice_date | Date | Invoice issue date (YYYY-MM-DD) |
| amount_eur | Decimal | Invoice amount converted to EUR (or original EUR amount if currency is EUR) |
| currency | String | ISO 4217 currency code as stated on the invoice |
| status | String | Processing status: PROCESSED, APPROVED, or PAID |
| po_number | String | Purchase order reference linked to this invoice (PO-NNNNN format); blank if not provided |
| payment_reference | String | Payment run reference (PAY-NNNNNN); populated only when status is PAID |
