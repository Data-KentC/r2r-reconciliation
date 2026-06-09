# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# tests/synthetic/generate_synthetic.py
#
# Generates a realistic but entirely fake NetSuite GL export CSV covering
# all 13 edge cases required to test the 5-tier matching engine.
#
# Run this file to regenerate test data:
#   python tests/synthetic/generate_synthetic.py
#
# Output: data/synthetic/synthetic_gl_jun2026.csv
# =============================================================================

import csv
import hashlib
import os
import random
from datetime import date, timedelta

# -----------------------------------------------------------------------------
# CONFIGURATION
# Mirrors config.yaml — kept separate so generator has no external dependencies
# -----------------------------------------------------------------------------

ENTITIES = ["ANTH-SG", "ANTH-AU", "ANTH-IN", "ANTH-JP", "ANTH-HK"]

CURRENCIES = {
    "ANTH-SG": "SGD",
    "ANTH-AU": "AUD",
    "ANTH-IN": "INR",
    "ANTH-JP": "JPY",
    "ANTH-HK": "HKD",
}

# Illustrative IC account codes — update with real CoA before go-live
IC_DUE_FROM_ACCOUNTS = {
    "ANTH-SG": "18001 Due From ANTH-SG",
    "ANTH-AU": "18002 Due From ANTH-AU",
    "ANTH-IN": "18003 Due From ANTH-IN",
    "ANTH-JP": "18004 Due From ANTH-JP",
    "ANTH-HK": "18005 Due From ANTH-HK",
}

IC_DUE_TO_ACCOUNTS = {
    "ANTH-SG": "28001 Due To ANTH-SG",
    "ANTH-AU": "28002 Due To ANTH-AU",
    "ANTH-IN": "28003 Due To ANTH-IN",
    "ANTH-JP": "28004 Due To ANTH-JP",
    "ANTH-HK": "28005 Due To ANTH-HK",
}

NON_IC_ACCOUNTS = [
    "61200 Consulting Fees",
    "71000 IT Shared Services",
    "40100 IC Revenue",
    "50200 IC Cost of Goods Sold",
    "61100 Management Fees",
    "99999 Miscellaneous Expense",  # Deliberately not an IC account
]

PERIOD_JUN = "Jun 2026"
PERIOD_JUL = "Jul 2026"

OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../data/synthetic/synthetic_gl_jun2026.csv"
)

# CSV column headers — must exactly match config.yaml netsuite.fields
HEADERS = [
    "internalid",
    "linesequencenumber",
    "trandate",
    "tranid",
    "account",
    "debitamount",
    "creditamount",
    "currency",
    "fxamount",
    "subsidiarynohierarchy",
    "cseg_apac_ic",
    "memo",
]

# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------------------------------

_id_counter = 1000


def next_id():
    """Generates a sequential internal ID."""
    global _id_counter
    _id_counter += 1
    return str(_id_counter)


def make_tranid(prefix="JE"):
    """Generates a realistic NetSuite document number."""
    global _id_counter
    return f"{prefix}-2026-{_id_counter:05d}"


def make_row(
    internalid,
    lineseq,
    trandate,
    tranid,
    account,
    debit,
    credit,
    currency,
    fxamount,
    subsidiary,
    cseg,
    memo,
):
    """Builds one CSV row dict matching NetSuite export format."""
    return {
        "internalid":             internalid,
        "linesequencenumber":     lineseq,
        "trandate":               trandate.strftime("%Y-%m-%d"),
        "tranid":                 tranid,
        "account":                account,
        "debitamount":            f"{debit:.2f}" if debit else "",
        "creditamount":           f"{credit:.2f}" if credit else "",
        "currency":               currency,
        "fxamount":               f"{fxamount:.2f}" if fxamount is not None else "",
        "subsidiarynohierarchy":  subsidiary,
        "cseg_apac_ic":           cseg,
        "memo":                   memo,
    }


# -----------------------------------------------------------------------------
# EDGE CASE GENERATORS
# Each function returns a list of rows representing one edge case
# -----------------------------------------------------------------------------

def case_01_perfect_aicje_match():
    """
    CASE 1: Perfect AICJE match — Tier 1 should match on shared tranid.
    ANTH-SG bills ANTH-AU for consulting services.
    Both sides share the same tranid (Advanced IC Journal Entry).
    Expected result: Tier 1 exact match.
    """
    iid = next_id()
    tid = make_tranid("AICJE")
    amount = 15000.00
    dt = date(2026, 6, 5)

    return [
        # ANTH-SG side: debit IC Receivable
        make_row(iid, 1, dt, tid,
                 IC_DUE_FROM_ACCOUNTS["ANTH-AU"], amount, None,
                 "SGD", amount, "ANTH-SG", "ANTH-AU",
                 "Consulting fee Jun 2026"),
        # ANTH-SG side: credit IC Revenue
        make_row(iid, 2, dt, tid,
                 "40100 IC Revenue", None, amount,
                 "SGD", amount, "ANTH-SG", "ANTH-AU",
                 "Consulting fee Jun 2026"),
        # ANTH-AU side: debit IC Expense (same tranid — AICJE)
        make_row(iid, 3, dt, tid,
                 "61200 Consulting Fees", amount, None,
                 "SGD", amount, "ANTH-AU", "ANTH-SG",
                 "Consulting fee Jun 2026"),
        # ANTH-AU side: credit IC Payable
        make_row(iid, 4, dt, tid,
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, amount,
                 "SGD", amount, "ANTH-AU", "ANTH-SG",
                 "Consulting fee Jun 2026"),
    ]


def case_02_sha256_hash_match():
    """
    CASE 2: SHA-256 hash match — Tier 2 should match.
    Manual JEs with no shared tranid but matching period/entity/currency/amount.
    ANTH-SG and ANTH-JP posted manually — different internalids.
    Expected result: Tier 2 hash match.
    """
    iid_sg = next_id()
    iid_jp = next_id()
    amount = 8500.00
    dt = date(2026, 6, 10)

    return [
        make_row(iid_sg, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-JP"], amount, None,
                 "USD", amount, "ANTH-SG", "ANTH-JP",
                 "IT recharge Jun 2026"),
        make_row(iid_sg, 2, dt, make_tranid("JE"),
                 "71000 IT Shared Services", None, amount,
                 "USD", amount, "ANTH-SG", "ANTH-JP",
                 "IT recharge Jun 2026"),
        make_row(iid_jp, 1, dt, make_tranid("JE"),
                 "71000 IT Shared Services", amount, None,
                 "USD", amount, "ANTH-JP", "ANTH-SG",
                 "IT recharge received Jun 2026"),
        make_row(iid_jp, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, amount,
                 "USD", amount, "ANTH-JP", "ANTH-SG",
                 "IT recharge received Jun 2026"),
    ]


def case_03_sha256_collision():
    """
    CASE 3: SHA-256 hash collision — two different transactions same hash bucket.
    ANTH-SG charges ANTH-JP $10,000 management fee AND $10,000 IT recharge
    in the same period. Same hash. Collision detection must handle this.
    Expected result: Tier 2 groups into collision bucket, pairs sequentially.
    """
    amount = 10000.00
    dt = date(2026, 6, 15)

    rows = []
    for memo_sg, memo_jp in [
        ("Management fee Jun 2026", "Management fee received"),
        ("IT recharge Jun 2026", "IT recharge received"),
    ]:
        iid_sg = next_id()
        iid_jp = next_id()
        rows += [
            make_row(iid_sg, 1, dt, make_tranid("JE"),
                     IC_DUE_FROM_ACCOUNTS["ANTH-JP"], amount, None,
                     "USD", amount, "ANTH-SG", "ANTH-JP", memo_sg),
            make_row(iid_sg, 2, dt, make_tranid("JE"),
                     "61100 Management Fees", None, amount,
                     "USD", amount, "ANTH-SG", "ANTH-JP", memo_sg),
            make_row(iid_jp, 1, dt, make_tranid("JE"),
                     "61100 Management Fees", amount, None,
                     "USD", amount, "ANTH-JP", "ANTH-SG", memo_jp),
            make_row(iid_jp, 2, dt, make_tranid("JE"),
                     IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, amount,
                     "USD", amount, "ANTH-JP", "ANTH-SG", memo_jp),
        ]
    return rows


def case_04_fx_tolerance_match():
    """
    CASE 4: FX tolerance match — Tier 3.
    ANTH-SG and ANTH-HK used slightly different exchange rates.
    Variance is $43.21 USD — within the $100 AND 1% dual threshold.
    Expected result: Tier 3 tolerance match, FX_ROUNDING variance noted.
    """
    iid_sg = next_id()
    iid_hk = next_id()
    dt = date(2026, 6, 18)

    return [
        make_row(iid_sg, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-HK"], 5000.00, None,
                 "USD", 5000.00, "ANTH-SG", "ANTH-HK",
                 "Shared services Jun 2026"),
        make_row(iid_sg, 2, dt, make_tranid("JE"),
                 "71000 IT Shared Services", None, 5000.00,
                 "USD", 5000.00, "ANTH-SG", "ANTH-HK",
                 "Shared services Jun 2026"),
        # HK recorded slightly different amount due to FX rate difference
        make_row(iid_hk, 1, dt, make_tranid("JE"),
                 "71000 IT Shared Services", 4956.79, None,
                 "USD", 4956.79, "ANTH-HK", "ANTH-SG",
                 "Shared services received Jun 2026"),
        make_row(iid_hk, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, 4956.79,
                 "USD", 4956.79, "ANTH-HK", "ANTH-SG",
                 "Shared services received Jun 2026"),
    ]


def case_05_amount_outside_tolerance():
    """
    CASE 5: Amount mismatch OUTSIDE tolerance.
    ANTH-SG billed for 10 hours, ANTH-IN only recorded 8 hours.
    Variance is $2,000 — exceeds both $100 and 1% threshold.
    Expected result: AMOUNT_MISMATCH exception, P1 priority.
    """
    iid_sg = next_id()
    iid_in = next_id()
    dt = date(2026, 6, 20)

    return [
        make_row(iid_sg, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-IN"], 10000.00, None,
                 "USD", 10000.00, "ANTH-SG", "ANTH-IN",
                 "Consulting 10hrs Jun 2026"),
        make_row(iid_sg, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", None, 10000.00,
                 "USD", 10000.00, "ANTH-SG", "ANTH-IN",
                 "Consulting 10hrs Jun 2026"),
        # IN only recorded 8 hours
        make_row(iid_in, 1, dt, make_tranid("JE"),
                 "61200 Consulting Fees", 8000.00, None,
                 "USD", 8000.00, "ANTH-IN", "ANTH-SG",
                 "Consulting 8hrs Jun 2026"),
        make_row(iid_in, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, 8000.00,
                 "USD", 8000.00, "ANTH-IN", "ANTH-SG",
                 "Consulting 8hrs Jun 2026"),
    ]


def case_06_timing_gap():
    """
    CASE 6: Timing gap — Tier 3 with period mismatch.
    ANTH-HK shipped goods on Jun 29. ANTH-IN received Jul 4.
    One side in Jun 2026, other in Jul 2026.
    Expected result: TIMING_GAP exception — in-transit, within ±1 period.
    """
    iid_hk = next_id()
    iid_in = next_id()
    amount = 45000.00

    return [
        # HK posts in June
        make_row(iid_hk, 1, date(2026, 6, 29), make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-IN"], amount, None,
                 "USD", amount, "ANTH-HK", "ANTH-IN",
                 "Hardware shipment Jun 2026"),
        make_row(iid_hk, 2, date(2026, 6, 29), make_tranid("JE"),
                 "40100 IC Revenue", None, amount,
                 "USD", amount, "ANTH-HK", "ANTH-IN",
                 "Hardware shipment Jun 2026"),
        # IN posts in July — goods in transit
        make_row(iid_in, 1, date(2026, 7, 4), make_tranid("JE"),
                 "50200 IC Cost of Goods Sold", amount, None,
                 "USD", amount, "ANTH-IN", "ANTH-HK",
                 "Hardware received Jul 2026"),
        make_row(iid_in, 2, date(2026, 7, 4), make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-HK"], None, amount,
                 "USD", amount, "ANTH-IN", "ANTH-HK",
                 "Hardware received Jul 2026"),
    ]


def case_07_one_to_many_subset_sum():
    """
    CASE 7: 1-to-many — Tier 4 subset sum.
    ANTH-SG posts one consolidated line of $30,000.
    ANTH-AU posts three itemised lines of $10,000 each.
    Expected result: Tier 4 subset sum groups the three AU lines.
    """
    iid_sg = next_id()
    iid_au_1 = next_id()
    iid_au_2 = next_id()
    iid_au_3 = next_id()
    dt = date(2026, 6, 12)

    return [
        # SG: one consolidated line
        make_row(iid_sg, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-AU"], 30000.00, None,
                 "USD", 30000.00, "ANTH-SG", "ANTH-AU",
                 "Quarterly recharge Q2 2026"),
        make_row(iid_sg, 2, dt, make_tranid("JE"),
                 "61100 Management Fees", None, 30000.00,
                 "USD", 30000.00, "ANTH-SG", "ANTH-AU",
                 "Quarterly recharge Q2 2026"),
        # AU: three itemised lines
        make_row(iid_au_1, 1, dt, make_tranid("JE"),
                 "61100 Management Fees", 10000.00, None,
                 "USD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Recharge Apr 2026"),
        make_row(iid_au_1, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, 10000.00,
                 "USD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Recharge Apr 2026"),
        make_row(iid_au_2, 1, dt, make_tranid("JE"),
                 "61100 Management Fees", 10000.00, None,
                 "USD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Recharge May 2026"),
        make_row(iid_au_2, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, 10000.00,
                 "USD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Recharge May 2026"),
        make_row(iid_au_3, 1, dt, make_tranid("JE"),
                 "61100 Management Fees", 10000.00, None,
                 "USD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Recharge Jun 2026"),
        make_row(iid_au_3, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, 10000.00,
                 "USD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Recharge Jun 2026"),
    ]


def case_08_full_reversal_net_zero():
    """
    CASE 8: Full reversal netting to zero.
    ANTH-SG posted a JE and then fully reversed it.
    Net amount = zero. Both lines should be dropped before matching.
    Expected result: Pre-filter drops these rows entirely.
    """
    iid = next_id()
    iid_rev = next_id()
    dt = date(2026, 6, 8)
    amount = 5000.00

    return [
        # Original entry
        make_row(iid, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-JP"], amount, None,
                 "USD", amount, "ANTH-SG", "ANTH-JP",
                 "Original entry Jun 2026"),
        make_row(iid, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", None, amount,
                 "USD", amount, "ANTH-SG", "ANTH-JP",
                 "Original entry Jun 2026"),
        # Full reversal — same account, opposite signs
        make_row(iid_rev, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-JP"], None, amount,
                 "USD", amount, "ANTH-SG", "ANTH-JP",
                 "Reversal of original entry Jun 2026"),
        make_row(iid_rev, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", amount, None,
                 "USD", amount, "ANTH-SG", "ANTH-JP",
                 "Reversal of original entry Jun 2026"),
    ]


def case_09_partial_reversal():
    """
    CASE 9: Partial reversal leaving non-zero net.
    ANTH-AU posted $10,000, reversed $6,000, re-posted $6,000.
    Net = $10,000. Collapsed to single net line for matching.
    Expected result: PARTIAL_REVERSAL flag, net $10,000 passed to matcher.
    """
    iid_orig = next_id()
    iid_rev = next_id()
    iid_repost = next_id()
    dt = date(2026, 6, 14)

    return [
        # Original $10,000
        make_row(iid_orig, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-SG"], 10000.00, None,
                 "AUD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Service fee original Jun 2026"),
        make_row(iid_orig, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", None, 10000.00,
                 "AUD", 10000.00, "ANTH-AU", "ANTH-SG",
                 "Service fee original Jun 2026"),
        # Partial reversal of $6,000
        make_row(iid_rev, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-SG"], None, 6000.00,
                 "AUD", 6000.00, "ANTH-AU", "ANTH-SG",
                 "Partial reversal Jun 2026"),
        make_row(iid_rev, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", 6000.00, None,
                 "AUD", 6000.00, "ANTH-AU", "ANTH-SG",
                 "Partial reversal Jun 2026"),
        # Re-post of $6,000
        make_row(iid_repost, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-SG"], 6000.00, None,
                 "AUD", 6000.00, "ANTH-AU", "ANTH-SG",
                 "Corrected re-entry Jun 2026"),
        make_row(iid_repost, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", None, 6000.00,
                 "AUD", 6000.00, "ANTH-AU", "ANTH-SG",
                 "Corrected re-entry Jun 2026"),
    ]


def case_10_missing_cseg_valid_account():
    """
    CASE 10: Missing CSEG tag — fallback to account code range.
    Junior accountant forgot to tag cseg_apac_ic on manual JE.
    BUT used a valid IC account code (18xxx range).
    Expected result: Account code fallback identifies as IC transaction.
    """
    iid = next_id()
    dt = date(2026, 6, 22)
    amount = 12000.00

    return [
        # No CSEG tag — cseg_apac_ic is blank
        make_row(iid, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-HK"], amount, None,
                 "SGD", amount, "ANTH-SG", "",  # blank CSEG
                 "Management fee no tag Jun 2026"),
        make_row(iid, 2, dt, make_tranid("JE"),
                 "61100 Management Fees", None, amount,
                 "SGD", amount, "ANTH-SG", "",  # blank CSEG
                 "Management fee no tag Jun 2026"),
    ]


def case_11_missing_cseg_wrong_account():
    """
    CASE 11: Missing CSEG AND wrong account code — silent miss risk.
    Junior accountant forgot CSEG and used a non-IC account (99999).
    Memo field contains entity name hint — tertiary heuristic layer test.
    Expected result: Tertiary memo-based identification flags for review.
    """
    iid = next_id()
    dt = date(2026, 6, 25)
    amount = 3500.00

    return [
        # No CSEG, non-IC account, but memo contains entity name
        make_row(iid, 1, dt, make_tranid("JE"),
                 "99999 Miscellaneous Expense", amount, None,
                 "SGD", amount, "ANTH-SG", "",  # blank CSEG
                 "Recharge to ANTH-IN for office supplies Jun 2026"),
        make_row(iid, 2, dt, make_tranid("JE"),
                 "61200 Consulting Fees", None, amount,
                 "SGD", amount, "ANTH-SG", "",  # blank CSEG
                 "Recharge to ANTH-IN for office supplies Jun 2026"),
    ]


def case_12_currency_mismatch():
    """
    CASE 12: Currency mismatch between entities.
    ANTH-SG posted in SGD. ANTH-AU recorded receipt in AUD.
    Same transaction, incompatible currencies — cannot match on fxamount.
    Expected result: CURRENCY_MISMATCH exception flagged immediately.
    """
    iid_sg = next_id()
    iid_au = next_id()
    dt = date(2026, 6, 16)

    return [
        # SG posts in SGD
        make_row(iid_sg, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-AU"], 20000.00, None,
                 "SGD", 20000.00, "ANTH-SG", "ANTH-AU",
                 "License fee SGD Jun 2026"),
        make_row(iid_sg, 2, dt, make_tranid("JE"),
                 "40100 IC Revenue", None, 20000.00,
                 "SGD", 20000.00, "ANTH-SG", "ANTH-AU",
                 "License fee SGD Jun 2026"),
        # AU recorded in AUD — wrong currency for this transaction
        make_row(iid_au, 1, dt, make_tranid("JE"),
                 "40100 IC Revenue", 20000.00, None,
                 "AUD", 20000.00, "ANTH-AU", "ANTH-SG",
                 "License fee AUD Jun 2026"),
        make_row(iid_au, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, 20000.00,
                 "AUD", 20000.00, "ANTH-AU", "ANTH-SG",
                 "License fee AUD Jun 2026"),
    ]


def case_13_orphan_llm_required():
    """
    CASE 13: True orphan — no counterpart exists anywhere.
    ANTH-JP posted an IC payable with an ambiguous memo.
    No matching entry on any other entity.
    LLM should suggest most likely counterparty from account description.
    Expected result: ORPHAN exception, LLM suggests counterparty.
    """
    iid = next_id()
    dt = date(2026, 6, 28)
    amount = 75000.00

    return [
        # JP has an IC payable with no matching receivable anywhere
        make_row(iid, 1, dt, make_tranid("JE"),
                 "61200 Consulting Fees", amount, None,
                 "JPY", amount, "ANTH-JP", "ANTH-SG",
                 "Consulting svcs recd - quarterly advisory"),
        make_row(iid, 2, dt, make_tranid("JE"),
                 IC_DUE_TO_ACCOUNTS["ANTH-SG"], None, amount,
                 "JPY", amount, "ANTH-JP", "ANTH-SG",
                 "Consulting svcs recd - quarterly advisory"),
        # NOTE: No corresponding entry on ANTH-SG side
        # This is intentional — tests the orphan detection and LLM suggestion
    ]


def case_14_null_fxamount():
    """
    CASE 14: Null fxamount — auto-generated elimination entry.
    NetSuite sometimes generates entries with null fxamount for
    base-currency elimination journals.
    Expected result: Null guard fires, coalesces to base amount with warning.
    """
    iid = next_id()
    dt = date(2026, 6, 30)
    amount = 9000.00

    return [
        # fxamount is intentionally blank — simulates NetSuite null export
        make_row(iid, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-AU"], amount, None,
                 "SGD", None,  # NULL fxamount — triggers null guard
                 "ANTH-SG", "ANTH-AU",
                 "Elimination entry Jun 2026"),
        make_row(iid, 2, dt, make_tranid("JE"),
                 "40100 IC Revenue", None, amount,
                 "SGD", None,  # NULL fxamount
                 "ANTH-SG", "ANTH-AU",
                 "Elimination entry Jun 2026"),
    ]


def case_15_out_of_scope_entity():
    """
    CASE 15: Out-of-scope entity — ANTH-US appears in APAC export.
    Saved Search parameter leakage — US entity included in APAC export.
    Expected result: Filtered out immediately, logged to quarantine.
    """
    iid = next_id()
    dt = date(2026, 6, 15)
    amount = 50000.00

    return [
        # ANTH-US — not in the APAC 5 entity list
        make_row(iid, 1, dt, make_tranid("JE"),
                 IC_DUE_FROM_ACCOUNTS["ANTH-SG"], amount, None,
                 "USD", amount, "ANTH-US",  # Out of scope
                 "ANTH-SG",
                 "US to SG recharge Jun 2026"),
        make_row(iid, 2, dt, make_tranid("JE"),
                 "61100 Management Fees", None, amount,
                 "USD", amount, "ANTH-US",  # Out of scope
                 "ANTH-SG",
                 "US to SG recharge Jun 2026"),
    ]


# -----------------------------------------------------------------------------
# MAIN GENERATOR
# -----------------------------------------------------------------------------

def generate_all_cases():
    """
    Generates all edge case rows and writes to CSV.
    Returns the total row count for verification.
    """
    all_rows = []

    cases = [
        ("01 — Perfect AICJE match",           case_01_perfect_aicje_match),
        ("02 — SHA-256 hash match",             case_02_sha256_hash_match),
        ("03 — SHA-256 collision",              case_03_sha256_collision),
        ("04 — FX tolerance match",             case_04_fx_tolerance_match),
        ("05 — Amount outside tolerance",       case_05_amount_outside_tolerance),
        ("06 — Timing gap Jun/Jul",             case_06_timing_gap),
        ("07 — 1-to-many subset sum",           case_07_one_to_many_subset_sum),
        ("08 — Full reversal net zero",         case_08_full_reversal_net_zero),
        ("09 — Partial reversal",               case_09_partial_reversal),
        ("10 — Missing CSEG valid account",     case_10_missing_cseg_valid_account),
        ("11 — Missing CSEG wrong account",     case_11_missing_cseg_wrong_account),
        ("12 — Currency mismatch",              case_12_currency_mismatch),
        ("13 — Orphan LLM required",            case_13_orphan_llm_required),
        ("14 — Null fxamount",                  case_14_null_fxamount),
        ("15 — Out of scope entity",            case_15_out_of_scope_entity),
    ]

    print("=" * 60)
    print("R2R Synthetic Data Generator")
    print("=" * 60)

    for name, fn in cases:
        rows = fn()
        all_rows.extend(rows)
        print(f"  ✓ Case {name}: {len(rows)} rows generated")

    # Shuffle rows to simulate a real mixed GL export
    # Seed for reproducibility — same seed = same output every time
    random.seed(42)
    random.shuffle(all_rows)

    # Write to CSV
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_PATH)), exist_ok=True)

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(all_rows)

    print("=" * 60)
    print(f"  Total rows generated: {len(all_rows)}")
    print(f"  Output: {os.path.abspath(OUTPUT_PATH)}")
    print("=" * 60)

    return len(all_rows)


# -----------------------------------------------------------------------------
# VERIFICATION
# Prints a summary of what was generated for manual inspection
# -----------------------------------------------------------------------------

def verify_output():
    """Reads the generated CSV and prints a summary for verification."""
    if not os.path.exists(OUTPUT_PATH):
        print("ERROR: Output file not found. Run generate_all_cases() first.")
        return

    with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    entities = {}
    null_fx = 0
    blank_cseg = 0
    out_of_scope = 0

    for row in rows:
        entity = row["subsidiarynohierarchy"]
        entities[entity] = entities.get(entity, 0) + 1
        if not row["fxamount"]:
            null_fx += 1
        if not row["cseg_apac_ic"]:
            blank_cseg += 1
        if entity not in ENTITIES:
            out_of_scope += 1

    print("\nVERIFICATION SUMMARY")
    print("-" * 40)
    print(f"Total rows:          {len(rows)}")
    print(f"Null fxamount rows:  {null_fx}  (should be >0)")
    print(f"Blank CSEG rows:     {blank_cseg}  (should be >0)")
    print(f"Out-of-scope rows:   {out_of_scope}  (should be >0)")
    print("\nRows per entity:")
    for entity, count in sorted(entities.items()):
        marker = "  ← OUT OF SCOPE" if entity not in ENTITIES else ""
        print(f"  {entity}: {count} rows{marker}")
    print("-" * 40)
    print("✓ Synthetic data verified. Ready for matching engine testing.")


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    generate_all_cases()
    verify_output()