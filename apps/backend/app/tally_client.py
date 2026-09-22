"""Tally billing integration (custom feature, not one of the charter's 29
numbered phases - see progress-tracker.md). Talks directly to TallyPrime's
own HTTP-XML server, which Tally exposes on Sudeep's own PC once he turns it
on in Tally's settings (F1 > Settings > Connectivity > Client/Server
configuration). No cloud account or API key is involved - this is a plain
local HTTP request, same idea as the mobile app talking to the backend over
the LAN.

Kept dependency-free (uses only the standard library: urllib for the HTTP
request, xml.etree for parsing the response) since this doesn't need
anything beyond a synchronous POST with a timeout.

IMPORTANT LIMITATIONS (v1, by design - see progress-tracker.md for why):
- Accounting-voucher style only, no Tally "Stock Item"/inventory tracking -
  a good fit for a services/contracting business like Sudeep's, not a
  retail-goods business with SKUs.
- Tax amounts are NOT calculated here. JARVIS asks Sudeep for the exact tax
  ledger name(s) and amount(s) in the conversation rather than computing GST
  itself, since getting GST splits wrong in real books is a real compliance
  risk - this module just posts whatever ledger/amount pairs it's given.
- Ledger creation is "create and tolerate duplicate": if the customer ledger
  already exists, Tally's duplicate-name response is treated as fine rather
  than fatal, and voucher creation proceeds anyway.
"""
import json
import re
import socket
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Phase 15 "Self-healing" (added 2026-09-20, scoped with Sudeep to "safe
# automatic recovery only" - see health_check.py's module docstring for the
# full scoping story). A brief connection blip or a momentarily-busy Tally
# is a genuinely transient failure worth retrying automatically; a real
# rejection from Tally itself (EXCEPTIONS/LINEERROR in parse_tally_response)
# is NOT retried here - that's a business-logic failure, not a network one,
# and retrying it would be pointless (or, worse, risk a duplicate send if
# the original somehow did land despite the error).
_TRANSIENT_RETRY_ATTEMPTS = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 1.5


class TallyError(Exception):
    """Raised for anything that stops a bill from reaching Tally cleanly -
    unreachable, a malformed response, or Tally itself reporting an error."""


# --- Self-diagnosis (added 2026-09-20, per Sudeep's request after the
# ALLLEDGERENTRIES.LIST/LEDGERENTRIES.LIST bug) -----------------------------
# That bug took a full live-debugging session to find because Tally's HTTP
# gateway only ever returned a bare "EXCEPTIONS:1" with no reason - six
# different, individually-correct fixes were shipped and each one produced
# the exact same content-free failure, and the real cause only surfaced once
# Sudeep imported the same XML through Tally's own Import > Vouchers screen,
# which showed a real message ("No accounting or inventory entries are
# available"). Two permanent capabilities below, chosen deliberately as
# "auto-diagnose, never auto-edit billing code" (Sudeep's explicit choice -
# this integration writes real GST invoices, so an unsupervised auto-fix
# reaching a real bill is a worse outcome than a slower human-in-the-loop
# fix):
#   1. diagnose_voucher_xml() - a checklist of every known-bad XML pattern
#      this integration has actually hit, run BEFORE every send so the same
#      mistake never reaches Tally silently again, and again AFTER a failure
#      to explain it in the chat instead of a bare EXCEPTIONS:1.
#   2. log_tally_failure() - writes the full outgoing XML + Tally's raw
#      response to a permanent, capped log file next to this module, so any
#      future debugging session starts with real evidence already in hand
#      instead of needing a throwaway _debug_dump() added and removed by
#      hand (as happened this session).
_ERROR_LOG_PATH = Path(__file__).parent / "tally_error_log.jsonl"
_MAX_LOG_ENTRIES = 200


def diagnose_voucher_xml(xml_payload: str) -> list[dict]:
    """Checks a voucher XML payload against every known-bad pattern this
    integration has actually hit in production, before or after sending it
    to Tally. Returns a list of {"severity": "critical"|"warning", "text":
    str} findings (empty list = nothing known-bad detected - NOT a
    guarantee the voucher will succeed, since Tally's HTTP gateway can fail
    for reasons this checklist has never seen yet).
    "critical" = confirmed, evidence-based root causes of a past real
    failure - callers should refuse to send rather than waste a round trip
    reproducing a known bug. "warning" = plausible but unconfirmed risk
    factors, surfaced for visibility only."""
    findings: list[dict] = []

    # Root cause of the 2026-09-20 bug: this exact wrong tag name silently
    # discarded every accounting entry in "Invoice Voucher View" mode, on
    # every single attempt, with Tally's HTTP gateway never once hinting why.
    if "ALLLEDGERENTRIES.LIST" in xml_payload:
        findings.append({
            "severity": "critical",
            "text": (
                "Found ALLLEDGERENTRIES.LIST in the XML - in Invoice Voucher "
                "View mode Tally silently discards the whole accounting-"
                "entries block unless the tag is LEDGERENTRIES.LIST. This is "
                "the exact bug fixed 2026-09-20 (see progress-tracker.md)."
            ),
        })

    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as e:
        findings.append({
            "severity": "critical",
            "text": f"The generated XML isn't well-formed ({e}) - Tally would reject this outright.",
        })
        return findings

    ledger_entries = list(root.iter("LEDGERENTRIES.LIST"))
    if not ledger_entries:
        findings.append({
            "severity": "critical",
            "text": (
                "No LEDGERENTRIES.LIST blocks found anywhere in the voucher - "
                "Tally will reject this with 'No accounting or inventory "
                "entries are available.'"
            ),
        })
    else:
        amounts = []
        for entry in ledger_entries:
            amount_text = entry.findtext("AMOUNT")
            if amount_text is None:
                findings.append({
                    "severity": "critical",
                    "text": f"A LEDGERENTRIES.LIST for '{entry.findtext('LEDGERNAME')}' has no AMOUNT.",
                })
                continue
            try:
                amounts.append(float(amount_text))
            except ValueError:
                findings.append({
                    "severity": "critical",
                    "text": f"AMOUNT '{amount_text}' on '{entry.findtext('LEDGERNAME')}' isn't a number.",
                })
        if amounts and round(sum(amounts), 2) != 0.0:
            findings.append({
                "severity": "critical",
                "text": (
                    f"The ledger entries' AMOUNTs sum to {round(sum(amounts), 2)}, "
                    "not zero - Tally requires every voucher's debits and "
                    "credits to balance exactly."
                ),
            })

    is_invoice = root.findtext(".//ISINVOICE") == "Yes"
    has_gst_header = root.find(".//GSTREGISTRATIONTYPE") is not None
    if is_invoice and not has_gst_header:
        findings.append({
            "severity": "warning",
            "text": (
                "ISINVOICE=Yes but no GST registration block (CMPGSTIN/"
                "STATENAME/PLACEOFSUPPLY) was generated - this usually means "
                "the company's GST settings (TALLY_COMPANY_GSTIN / "
                "TALLY_COMPANY_STATE) aren't configured, which made Tally "
                "reject invoices outright the first time this was hit "
                "(2026-09-19)."
            ),
        })

    if "&#4;" in xml_payload or "&#04;" in xml_payload:
        findings.append({
            "severity": "warning",
            "text": (
                "The XML contains the illegal control-character reference "
                "&#4; (Tally's raw 'Not Applicable' placeholder for fields "
                "like GSTCLASS) - some parsers reject this."
            ),
        })

    return findings


def log_tally_failure(xml_payload: str, response_text: str | None, message: str) -> list[dict]:
    """Runs diagnose_voucher_xml() and permanently records this failure
    (timestamp, message, findings, the full request XML, and Tally's raw
    response) to a capped local JSONL log so a future debugging session has
    real evidence on hand immediately, instead of needing a throwaway debug
    helper added and removed by hand. Best-effort: a logging failure here
    must never break the actual bill-creation flow, so every error is
    swallowed. Returns the findings list either way, so the caller can also
    surface the top finding directly in the chat response."""
    findings = diagnose_voucher_xml(xml_payload)
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "findings": findings,
            "request_xml": xml_payload,
            "response": response_text,
        }
        existing_lines: list[str] = []
        if _ERROR_LOG_PATH.exists():
            existing_lines = _ERROR_LOG_PATH.read_text(encoding="utf-8").splitlines()
        existing_lines.append(json.dumps(entry))
        # Cap growth by keeping only the most recent entries - rewriting
        # (not deleting) the file, since this environment can't always
        # delete files outright.
        existing_lines = existing_lines[-_MAX_LOG_ENTRIES:]
        _ERROR_LOG_PATH.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return findings


# Known-party GSTIN lookup (added 2026-09-19). A real GST "Accounting
# Invoice" needs the buyer's own GSTIN (PARTYGSTIN) - confirmed by
# comparing a failed attempt against a genuine invoice Sudeep created
# manually in Tally for this exact same bill (Sales_SSRS_26-27_170.xml),
# which included it. JARVIS's chat flow doesn't currently ask Sudeep for a
# customer's GSTIN, and most of his billing is repeat business to a
# handful of known Indian Oil divisional offices (he's a long-term IOCL
# contractor - see progress-tracker.md), so this is a small, easily-
# extended lookup table of real GSTINs pulled from Sudeep's own genuine
# exported invoices, keyed by the exact party ledger name (case-
# insensitive). Add a new line here as more of Sudeep's regular parties'
# GSTINs are confirmed from a real export or he provides one directly.
KNOWN_PARTY_GSTINS = {
    "indian oil corporation limited dhanbad do": "20AAACI1681G3Z1",
    "indian oil corporation limited durgapur do": "19AAACI1681G1ZM",
}


def lookup_party_gstin(party_name: str) -> str | None:
    """Case-insensitive lookup into KNOWN_PARTY_GSTINS; returns None for an
    unknown party rather than guessing."""
    return KNOWN_PARTY_GSTINS.get((party_name or "").strip().lower())


def _escape_xml(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _voucher_date_ddmmyyyy(iso_date: str) -> str:
    """Tally's XML wants dates as YYYYMMDD, not the YYYY-MM-DD the frontend
    sends. Raises TallyError on anything that doesn't parse rather than
    silently sending a bad date to real accounting records."""
    try:
        year, month, day = iso_date.split("-")
        if len(year) != 4 or len(month) != 2 or len(day) != 2:
            raise ValueError
        return f"{year}{month}{day}"
    except (ValueError, AttributeError):
        raise TallyError(f"'{iso_date}' isn't a valid date (expected YYYY-MM-DD)")


def check_tally_reachable(host: str, port: int, timeout: int) -> tuple[bool, str]:
    """A lightweight reachability probe - just opens and closes a TCP
    connection, without needing a valid Tally XML request. Used to give
    Sudeep a clear "Tally isn't open right now" message before attempting a
    real bill, rather than a confusing timeout partway through."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "Tally is reachable"
    except OSError as e:
        return False, (
            f"Can't reach Tally at {host}:{port} ({e}). Make sure TallyPrime "
            "is open, the company is loaded, and the HTTP-XML server is "
            "enabled (F1 > Settings > Connectivity > Client/Server "
            "configuration)."
        )


def build_ledger_xml(company: str, ledger_name: str, parent_group: str) -> str:
    return f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
 <BODY>
  <IMPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>All Masters</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{_escape_xml(company)}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
   </REQUESTDESC>
   <REQUESTDATA>
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <LEDGER NAME="{_escape_xml(ledger_name)}" ACTION="Create">
      <NAME>{_escape_xml(ledger_name)}</NAME>
      <PARENT>{_escape_xml(parent_group)}</PARENT>
     </LEDGER>
    </TALLYMESSAGE>
   </REQUESTDATA>
  </IMPORTDATA>
 </BODY>
</ENVELOPE>"""


def compute_gst_tax_lines(
    items_total: float,
    gst_rate: float,
    tax_type: str,
    igst_ledger: str,
    cgst_ledger: str,
    sgst_ledger: str,
) -> list[dict]:
    """Computes the CGST+SGST or IGST split from a flat percentage rate, in
    real Python arithmetic. JARVIS only ever decides tax_type (interstate vs
    intrastate) and the rate - both judgment calls it can reasonably make
    from the conversation - never the multiplication itself, so an LLM
    arithmetic slip can never reach a real tax amount written to Sudeep's
    books. tax_type must be "interstate" (a single IGST line) or
    "intrastate" (an even CGST+SGST split); anything else raises rather than
    silently producing an untaxed bill when tax was clearly intended."""
    if tax_type not in ("interstate", "intrastate"):
        raise TallyError(
            f"tax_type must be 'interstate' or 'intrastate', got {tax_type!r}"
        )
    if gst_rate is None or gst_rate < 0:
        raise TallyError("A valid GST rate is required to compute tax")

    # duty_head/rate (added 2026-09-19, GST-compliance fix): a real Tally
    # Accounting Invoice wants each item's GST rate declared explicitly via
    # a RATEDETAILS.LIST block (see build_sales_voucher_xml) using Tally's
    # own duty-head names - "CGST", "SGST/UTGST", "IGST" - not just the
    # ledger names, which are Sudeep's own naming and can differ.
    if tax_type == "interstate":
        return [{
            "ledger": igst_ledger,
            "amount": round(items_total * gst_rate / 100, 2),
            "duty_head": "IGST",
            "rate": gst_rate,
        }]

    half = round(items_total * gst_rate / 200, 2)
    half_rate = round(gst_rate / 2, 3)
    return [
        {"ledger": cgst_ledger, "amount": half, "duty_head": "CGST", "rate": half_rate},
        {"ledger": sgst_ledger, "amount": half, "duty_head": "SGST/UTGST", "rate": half_rate},
    ]


def build_sales_voucher_xml(
    company: str,
    party_name: str,
    sales_ledger: str,
    items: list[dict],
    tax_lines: list[dict],
    voucher_date: str,
    narration: str | None,
    buyer_order_no: str | None = None,
    other_reference_no: str | None = None,
    destination: str | None = None,
    company_gstin: str | None = None,
    company_state: str | None = None,
    company_gst_registration_name: str | None = None,
    place_of_supply: str | None = None,
    party_gstin: str | None = None,
) -> tuple[str, float]:
    """items: [{"description": str, "amount": float}, ...]
    tax_lines: [{"ledger": str, "amount": float, "duty_head": str, "rate":
    float}, ...] - by the time this is called, tax_lines is already the
    final, real numbers (either computed by compute_gst_tax_lines or a
    manual override) - this function just posts them, it doesn't do any tax
    math itself. duty_head/rate are optional (older callers/tests may only
    pass ledger/amount) - when absent, the RATEDETAILS.LIST block for that
    line is simply skipped.
    buyer_order_no/other_reference_no/destination: Sudeep's own field names
    are Work Order No, Complaint No, and the site/petrol-pump name (see
    progress-tracker.md) - there's no confirmed Tally XML tag for these yet
    (Tally's own docs say the reliable way to find one is to export a real
    voucher that has them filled in), so for now they're folded into the
    voucher's narration line, clearly labeled, rather than risking a wrong
    tag that Tally would silently ignore.
    company_gstin/company_state/company_gst_registration_name: Sudeep's own
    GST registration details (settings.tally_company_*) - required for a
    real GST "Accounting Invoice" (see the 2026-09-19 fix note below).
    place_of_supply: defaults to company_state (the correct value for an
    intrastate bill); pass the party's own state explicitly for interstate
    bills, since it must differ from company_state there. Interstate place-
    of-supply handling beyond this is a known open follow-up.
    party_gstin: the customer's GSTIN, when known. Optional and omitted
    from the XML when not given - Tally can pull it from the party ledger
    master itself if that master already has it saved (as Sudeep's real
    customer ledgers generally do), same as it infers GST rate/HSN from a
    ledger master via GSTRATEINFERAPPLICABILITY below.
    Returns (xml, total_amount) - total_amount is computed here in real
    Python arithmetic (never trusted from the AI-drafted JSON), and is what
    the customer's ledger is debited for."""
    if not items:
        raise TallyError("A bill needs at least one line item")

    items_total = round(sum(float(i["amount"]) for i in items), 2)
    tax_total = round(sum(float(t["amount"]) for t in (tax_lines or [])), 2)
    total_amount = round(items_total + tax_total, 2)
    if total_amount <= 0:
        raise TallyError("The bill's total amount must be greater than zero")

    tally_date = _voucher_date_ddmmyyyy(voucher_date)

    # GST fields below (added 2026-09-19). Confirmed against a real,
    # successfully-created Tally invoice Sudeep exported (see progress-
    # tracker.md). NOTE: that real export also shows a legacy field,
    # GSTCLASS, set to Tally's own raw-control-character "Not Applicable"
    # placeholder (a literal ASCII 0x04 + text, written as XML entity
    # &#4;) - deliberately NOT reproduced here, since &#4; is an illegal
    # XML character reference (rejected by standard parsers, including the
    # ones this project's own tests use) and GSTCLASS is the old VAT-era
    # classification field, superseded for GST purposes by the newer
    # GSTOVRDN*/GSTSOURCETYPE override fields below - it's very likely a
    # harmless default Tally fills in on export, not something required on
    # import.
    effective_place_of_supply = place_of_supply or company_state or ""

    # Tally's sign convention: debit amounts are negative, credit amounts
    # are positive, and every voucher must sum to exactly zero. The
    # customer is debited for the full total; the sales ledger and any tax
    # ledgers are credited for their share of it.
    # BILLALLOCATIONS.LIST (added 2026-09-19, removed same day, RE-ADDED
    # 2026-09-19 after Sudeep manually created the exact same bill in Tally
    # and exported it - see Sales_SSRS_26-27_170.xml, progress-tracker.md):
    # that genuine, successfully-created invoice DOES include a bill-wise
    # reference on the party ledger entry, so removing it was the wrong
    # guess - it's back. bill_ref_name can't be the real voucher number
    # (Tally auto-assigns that on creation, using "Auto Retain" numbering,
    # so it isn't known yet at XML-write time) - other_reference_no is used
    # instead as a reasonable stand-in bill reference, since Tally's own
    # bill-wise tracking doesn't require this text to match the eventual
    # voucher number.
    # Diagnostic test 2026-09-20 (ruled out, kept as a note): tried a
    # disambiguated bill_ref_name here, on the theory that Tally's bill-wise
    # tracking might reject a "New Ref" name already used by one of the 8
    # cancelled vouchers left behind by earlier failed attempts. A unique
    # reference produced the exact same EXCEPTIONS:1 failure, so this is NOT
    # the cause - reverted to the real reference Sudeep actually wants on
    # the invoice.
    bill_ref_name = other_reference_no or buyer_order_no or f"Bill-{tally_date}"
    party_gstin_xml = f"\n     <PARTYGSTIN>{_escape_xml(party_gstin)}</PARTYGSTIN>" if party_gstin else ""
    ledger_entries = [
        f"""    <LEDGERENTRIES.LIST>
     <LEDGERNAME>{_escape_xml(party_name)}</LEDGERNAME>
     <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
     <ISPARTYLEDGER>Yes</ISPARTYLEDGER>{party_gstin_xml}
     <AMOUNT>-{total_amount}</AMOUNT>
     <BILLALLOCATIONS.LIST>
      <NAME>{_escape_xml(bill_ref_name)}</NAME>
      <BILLTYPE>New Ref</BILLTYPE>
      <AMOUNT>-{total_amount}</AMOUNT>
     </BILLALLOCATIONS.LIST>
    </LEDGERENTRIES.LIST>"""
    ]

    # In Sudeep's Tally setup, each service type (like "Repair and Maintenance Work")
    # is its own ledger. Use each item's description directly as the ledger name
    # so it appears correctly on the printed invoice, rather than a generic "Sales".
    # GST/HSN fields (2026-09-19 fix): tell Tally to derive the GST rate and
    # HSN/SAC code for this line from the item's own ledger master (Sudeep's
    # real ledgers, like "REPAIR AND MAINTENANCE WORK", already have these
    # configured in Tally) rather than requiring them hardcoded here - this
    # is what GSTRATEINFERAPPLICABILITY/GSTHSNINFERAPPLICABILITY = "As per
    # Masters/Company" does, confirmed from the real exported invoice.
    # RATEDETAILS.LIST still declares this specific bill's actual rate per
    # duty head, computed by compute_gst_tax_lines - matching what the real
    # export shows.
    rate_details_xml = "\n".join(
        f"""     <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>{_escape_xml(t['duty_head'])}</GSTRATEDUTYHEAD>
      <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
      <GSTRATE>{t['rate']}</GSTRATE>
     </RATEDETAILS.LIST>"""
        for t in (tax_lines or []) if t.get("duty_head") and t.get("rate") is not None
    )
    description_lines = "; ".join(f"{i['description']} ({i['amount']})" for i in items)
    for item in items:
        item_amount = round(float(item["amount"]), 2)
        # UDF:HBSDESCRIPTION.LIST (added 2026-09-19, second live-retry fix
        # attempt): a custom TDL field present on the item ledger entry in
        # BOTH of Sudeep's genuine successful exports (Sales_SSRS_26-27_163
        # and _170) - a per-item description block (qty/rate/value plus a
        # free-text "User Description") that his Tally install's own TDL
        # customization writes for every accounting-invoice line. The exact
        # DESC="`...`" backtick syntax and INDEX numbers are reproduced
        # verbatim from the real export since these are TDL field
        # identifiers, not arbitrary text. Reasoning for adding this despite
        # not knowing for certain it's required: the previous PARTYGSTIN/
        # BILLALLOCATIONS/CONSIGNEE fix (also evidence-based, from the same
        # real export) did not resolve the live EXCEPTIONS:1 failure, and
        # this UDF block is the one remaining structural difference between
        # the generated XML and the genuine successful invoice.
        udf_description_xml = f"""     <UDF:HBSDESCRIPTION.LIST DESC="`HBSDescription`" INDEX="5004">
      <UDF:HBSDESCQTY.LIST DESC="`HBSDescQty`" ISLIST="YES" TYPE="Number" INDEX="2200">
       <UDF:HBSDESCQTY DESC="`HBSDescQty`">1</UDF:HBSDESCQTY>
      </UDF:HBSDESCQTY.LIST>
      <UDF:HBSDESCRATE.LIST DESC="`HBSDescRate`" ISLIST="YES" TYPE="Number" INDEX="5001">
       <UDF:HBSDESCRATE DESC="`HBSDescRate`">{item_amount}</UDF:HBSDESCRATE>
      </UDF:HBSDESCRATE.LIST>
      <UDF:HBSDESCVALUE.LIST DESC="`HBSDescVAlue`" ISLIST="YES" TYPE="Amount" INDEX="5002">
       <UDF:HBSDESCVALUE DESC="`HBSDescVAlue`">{item_amount}</UDF:HBSDESCVALUE>
      </UDF:HBSDESCVALUE.LIST>
      <UDF:USERDESCRIPTION.LIST DESC="`User Description`" ISLIST="YES" TYPE="String" INDEX="29">
       <UDF:USERDESCRIPTION DESC="`User Description`">{_escape_xml(item['description'])}</UDF:USERDESCRIPTION>
      </UDF:USERDESCRIPTION.LIST>
     </UDF:HBSDESCRIPTION.LIST>"""
        ledger_entries.append(
            f"""    <LEDGERENTRIES.LIST>
     <LEDGERNAME>{_escape_xml(item['description'])}</LEDGERNAME>
     <GSTOVRDNTAXABILITY>Taxable</GSTOVRDNTAXABILITY>
     <GSTSOURCETYPE>Ledger</GSTSOURCETYPE>
     <GSTLEDGERSOURCE>{_escape_xml(item['description'])}</GSTLEDGERSOURCE>
     <HSNSOURCETYPE>Ledger</HSNSOURCETYPE>
     <HSNLEDGERSOURCE>{_escape_xml(item['description'])}</HSNLEDGERSOURCE>
     <GSTOVRDNTYPEOFSUPPLY>Services</GSTOVRDNTYPEOFSUPPLY>
     <GSTRATEINFERAPPLICABILITY>As per Masters/Company</GSTRATEINFERAPPLICABILITY>
     <GSTHSNINFERAPPLICABILITY>As per Masters/Company</GSTHSNINFERAPPLICABILITY>
     <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
     <AMOUNT>{item_amount}</AMOUNT>
{rate_details_xml}
{udf_description_xml}
    </LEDGERENTRIES.LIST>"""
        )

    for t in tax_lines or []:
        ledger_entries.append(
            f"""    <LEDGERENTRIES.LIST>
     <LEDGERNAME>{_escape_xml(t['ledger'])}</LEDGERNAME>
     <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
     <AMOUNT>{round(float(t['amount']), 2)}</AMOUNT>
    </LEDGERENTRIES.LIST>"""
        )

    # Build the narration from description or individual line items.
    # Include all three reference fields in the narration so they're visible on the invoice,
    # since BASICPURCHASEORDERNO (Buyer's Order No.) doesn't seem to work via XML import.
    narration_parts = []
    if buyer_order_no:
        narration_parts.append(f"Work Order No: {buyer_order_no}")
    if other_reference_no:
        narration_parts.append(f"Complaint No: {other_reference_no}")
    if destination:
        narration_parts.append(f"Destination: {destination}")
    if narration:
        narration_parts.append(narration)
    elif description_lines:
        narration_parts.append(description_lines)

    full_narration = " | ".join(narration_parts) if narration_parts else ""
    ledger_entries_xml = "\n".join(ledger_entries)

    # Build the optional reference fields using the exact XML tags from the exported voucher.
    # Note: BASICORDERREF maps to "Other References" on the invoice.
    # BASICPURCHASEORDERNO (Buyer's Order No.) doesn't seem to work via XML import,
    # so we're including all reference info in the narration as a backup.
    order_ref_xml = f"      <BASICORDERREF>{_escape_xml(other_reference_no)}</BASICORDERREF>" if other_reference_no else ""
    destination_xml = f"      <BASICFINALDESTINATION>{_escape_xml(destination)}</BASICFINALDESTINATION>" if destination else ""

    # --- Item Invoice vs Accounting Invoice (fixed 2026-09-19) ---------
    # Sudeep reported every bill was landing in Tally as an "Item Invoice"
    # (expects stock items - wrong for a services business with no
    # inventory) instead of "Accounting Invoice" (ledger line items only -
    # correct for this business). Root cause: OBJVIEW="Accounting Voucher
    # View" (the old value here) is for a plain non-invoice voucher (Dr/Cr
    # entries only, no GST-invoice printing, no ISINVOICE tag) - NOT one of
    # the two invoice sub-modes at all. Since Sudeep's "Sales" voucher type
    # is configured for GST invoicing, Tally apparently falls back to its
    # own remembered/default invoice sub-mode when the XML doesn't actually
    # say "this is an invoice" - which happened to be Item Invoice.
    # Fix (confirmed against TallyPrime XML documentation): explicitly mark
    # this as an invoice with PERSISTEDVIEW/ISINVOICE and use
    # OBJVIEW="Invoice Voucher View". Whether Tally then shows it as Item
    # Invoice or Accounting Invoice is controlled by whether inventory
    # entries are present - since this module only ever sends
    # LEDGERENTRIES.LIST (never INVENTORYENTRIES.LIST), that now
    # correctly resolves to Accounting Invoice.
    #
    # --- GST-compliant Accounting Invoice fields (fixed 2026-09-19) -----
    # The above fix alone turned out to be necessary but not sufficient: a
    # real GST "Accounting Invoice" needs company/party GST-registration
    # details, place of supply, and an explicit VCHENTRYMODE declaration,
    # none of which this module sent before - Tally accepted the plain
    # non-invoice voucher (no GST validation triggered) but rejected the
    # correctly-marked invoice outright once it started checking for these.
    # All values below are confirmed by cross-referencing a real,
    # successfully-created invoice Sudeep exported from his own Tally.
    gst_header_xml = ""
    if company_gstin and company_state:
        party_gstin_header_xml = f"\n      <PARTYGSTIN>{_escape_xml(party_gstin)}</PARTYGSTIN>" if party_gstin else ""
        # CONSIGNEE* fields (added 2026-09-19, from the same real Sudeep
        # export as BILLALLOCATIONS.LIST above): only emitted when
        # party_gstin is known, mirroring the party as the consignee since
        # no separate ship-to address/GSTIN is collected in this chat flow
        # yet (Sudeep's real invoices ship to the same GST-registered
        # entity that's billed, not a third party).
        consignee_xml = (
            f"\n      <CONSIGNEEGSTIN>{_escape_xml(party_gstin)}</CONSIGNEEGSTIN>"
            f"\n      <CONSIGNEEMAILINGNAME>{_escape_xml(party_name)}</CONSIGNEEMAILINGNAME>"
            f"\n      <CONSIGNEESTATENAME>{_escape_xml(effective_place_of_supply)}</CONSIGNEESTATENAME>"
            f"\n      <CONSIGNEECOUNTRYNAME>India</CONSIGNEECOUNTRYNAME>"
        ) if party_gstin else ""
        gst_header_xml = f"""
      <GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>
      <VATDEALERTYPE>Regular</VATDEALERTYPE>
      <STATENAME>{_escape_xml(effective_place_of_supply)}</STATENAME>
      <COUNTRYOFRESIDENCE>India</COUNTRYOFRESIDENCE>{party_gstin_header_xml}
      <PLACEOFSUPPLY>{_escape_xml(effective_place_of_supply)}</PLACEOFSUPPLY>
      <GSTREGISTRATION TAXTYPE="GST" TAXREGISTRATION="{_escape_xml(company_gstin)}">{_escape_xml(company_gst_registration_name or "")}</GSTREGISTRATION>
      <CMPGSTIN>{_escape_xml(company_gstin)}</CMPGSTIN>
      <CMPGSTREGISTRATIONTYPE>Regular</CMPGSTREGISTRATIONTYPE>
      <PARTYMAILINGNAME>{_escape_xml(party_name)}</PARTYMAILINGNAME>{consignee_xml}
      <CMPGSTSTATE>{_escape_xml(company_state)}</CMPGSTSTATE>
      <BASICBASEPARTYNAME>{_escape_xml(party_name)}</BASICBASEPARTYNAME>
      <VCHSTATUSTAXUNIT>{_escape_xml(company_gst_registration_name or "")}</VCHSTATUSTAXUNIT>
      <VCHENTRYMODE>Accounting Invoice</VCHENTRYMODE>
      <VCHSTATUSISREACCEPTFORHSNDONE>No</VCHSTATUSISREACCEPTFORHSNDONE>
      <VCHSTATUSISREACCEPHSNSIXONEDONE>Yes</VCHSTATUSISREACCEPHSNSIXONEDONE>"""

    xml = f"""<ENVELOPE>
 <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
 <BODY>
  <IMPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Vouchers</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{_escape_xml(company)}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
   </REQUESTDESC>
   <REQUESTDATA>
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <VOUCHER VCHTYPE="Sales" ACTION="Create" OBJVIEW="Invoice Voucher View">
      <DATE>{tally_date}</DATE>
      <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
      <PARTYNAME>{_escape_xml(party_name)}</PARTYNAME>
      <PARTYLEDGERNAME>{_escape_xml(party_name)}</PARTYLEDGERNAME>
      <BASICBUYERNAME>{_escape_xml(party_name)}</BASICBUYERNAME>
      <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
      <ISINVOICE>Yes</ISINVOICE>{gst_header_xml}
      <NARRATION>{_escape_xml(full_narration)}</NARRATION>
{order_ref_xml}
{destination_xml}
{ledger_entries_xml}
     </VOUCHER>
    </TALLYMESSAGE>
   </REQUESTDATA>
  </IMPORTDATA>
 </BODY>
</ENVELOPE>"""
    return xml, total_amount


def send_to_tally(xml_payload: str, host: str, port: int, timeout: int) -> str:
    """Phase 15 "Self-healing": a connection failure or timeout here is
    retried a couple of times with a short delay before finally giving up -
    the same "safe automatic recovery only" scope as health_check.py's
    subsystem checks, applied to the real send path this time, not just a
    diagnostic. Never retries a successful connection whose response Tally
    itself rejects (parse_tally_response's job, downstream of this
    function) - only a genuinely transient network-level failure."""
    url = f"http://{host}:{port}"
    request = urllib.request.Request(
        url,
        data=xml_payload.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
        method="POST",
    )
    last_error = None
    for attempt in range(_TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            last_error = TallyError(
                f"Couldn't reach Tally at {host}:{port} ({e}). Make sure "
                "TallyPrime is open with the company loaded and the HTTP-XML "
                "server is enabled."
            )
        except socket.timeout:
            last_error = TallyError(f"Tally didn't respond within {timeout} seconds")
        if attempt < _TRANSIENT_RETRY_ATTEMPTS:
            time.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
    raise last_error


def parse_tally_response(response_text: str) -> tuple[bool, str]:
    """Returns (ok, message). Tally's response is itself XML - typically a
    <RESPONSE> block with counts (<CREATED>, <ALTERED>, <ERRORS>,
    <EXCEPTIONS>) and, on failure, a <LINEERROR> with a human-readable
    reason. Parsed defensively: Tally's exact response shape isn't something
    this sandbox could test against a real server, so anything unexpected is
    treated as "couldn't confirm success" rather than crashing."""
    try:
        root = ET.fromstring(response_text)
    except ET.ParseError:
        return False, f"Tally sent back a response that wasn't valid XML: {response_text[:300]}"

    line_error = root.findtext(".//LINEERROR")
    errors = root.findtext(".//ERRORS")
    exceptions = root.findtext(".//EXCEPTIONS")
    created = root.findtext(".//CREATED")
    altered = root.findtext(".//ALTERED")

    has_errors = (errors and errors.strip() not in ("0", "")) or (
        exceptions and exceptions.strip() not in ("0", "")
    )

    if line_error and "already exists" in line_error.lower():
        # Expected and harmless for the "create ledger, tolerate duplicate"
        # pattern - the caller decides what to do next, this just reports it
        # wasn't a real failure.
        return True, f"Already existed: {line_error}"

    if has_errors or line_error:
        # Diagnostic improvement (2026-09-19): Tally doesn't always put a
        # human-readable reason in LINEERROR - sometimes ERRORS>0 comes back
        # with no LINEERROR at all, which used to surface as the unhelpful,
        # generic "Tally reported an error creating this entry" with no way
        # to tell what actually went wrong. Now falls back to a snippet of
        # Tally's raw response so the real reason (if Tally put it anywhere
        # else in the XML) is visible in the chat UI instead of hidden.
        if line_error:
            return False, line_error
        return False, (
            "Tally reported an error creating this entry, but didn't give a "
            f"LINEERROR reason. Raw response: {response_text[:500]}"
        )

    created_count = created and created.strip() not in ("0", "")
    altered_count = altered and altered.strip() not in ("0", "")

    if created_count or altered_count:
        # Confirmed live against a real TallyPrime server: re-sending a
        # ledger-create request for a party that already exists (e.g. by
        # exact name match) can come back as ALTERED rather than CREATED or
        # a "already exists" LINEERROR - Tally just updates the existing
        # master in place. That's success with zero errors, not an ambiguous
        # response, so it has to be treated the same as CREATED.
        bits = []
        if created_count:
            bits.append(f"created {created}")
        if altered_count:
            bits.append(f"updated {altered}")
        return True, f"Tally confirmed success ({', '.join(bits)})"

    # No error reported but nothing confirmed created or altered either - be
    # honest about the ambiguity rather than claiming success.
    return False, f"Tally's response didn't confirm success: {response_text[:300]}"


# --- Read-only Tally queries (added 2026-09-20, per Sudeep's request: he ----
# wants JARVIS to be able to answer things like "what bills were created
# today" by actually reading Tally, not just reciting its own local
# create-bill log (which would miss anything entered directly in Tally).
#
# IMPORTANT CAVEAT, unlike everything above: build_sales_voucher_xml's exact
# fields were only trusted once diffed against real, genuine exports from
# Sudeep's own Tally (see progress-tracker.md's whole GST-compliance saga).
# This read path has NOT had that same live validation yet - there was no
# real day-book export to check it against at the time this was written.
# It uses Tally's standard, documented "TDL Collection Export" mechanism
# (TALLYREQUEST=EXPORT, TYPE=COLLECTION) rather than the on-screen "Day
# Book" REPORT export, deliberately - a report export returns rendering-
# oriented display XML (columns/rows meant for a screen) that's unreliable
# to parse programmatically, while a Collection export returns clean
# <VOUCHER> objects with exactly the fields asked for in FETCH. This is the
# standard way third-party tools read structured data out of Tally, but
# "standard" isn't the same as "confirmed against Sudeep's real install" -
# treat the first live use of this as a validation step (same playbook that
# found the ALLLEDGERENTRIES.LIST bug: if it doesn't work, get Tally's own
# richer error/response involved rather than guessing at more fields blind).
#
# REAL BUG HIT 2026-09-20 (fixed same day, hours after the feature first
# shipped): the initial FETCH list didn't include AMOUNT at all, but when
# Sudeep asked "what is the amount" as a follow-up, JARVIS answered with a
# confident, specific, and WRONG figure (Rs 13,500 vs the real Rs
# 1,03,632.60, confirmed against a genuine Tally export Sudeep shared) -
# it fabricated a number instead of saying it didn't have that data. This
# is the exact failure this whole project has tried hardest to prevent
# (see "never trust AI arithmetic" throughout build_sales_voucher_xml/
# compute_gst_tax_lines above) and it happened on the READ side instead.
# Fixed two ways: (1) the FETCH list and parser below now pull the real
# amount straight off the party ledger's own AMOUNT field (never summed or
# estimated), so a correct figure is available whenever Tally returns one;
# (2) JARVIS_SYSTEM_PROMPT (ai_provider.py) now explicitly forbids stating
# any figure that wasn't actually present in a tool's result text - amount
# extraction here is defense in depth, not the only safeguard, since a
# voucher type or Tally response shape this hasn't been tested against yet
# could still come back with amount=None.
def build_daybook_query_xml(company: str, date_from: str, date_to: str) -> str:
    """Read-only query for vouchers (any type - Sales, Payment, Receipt,
    Journal, etc.) recorded in Tally between date_from and date_to
    (inclusive), both YYYY-MM-DD. Never writes anything - this is a plain
    EXPORT request, there's no ACTION="Create"/"Alter" anywhere in it.
    FETCH includes both LEDGERENTRIES.LIST and ALLLEDGERENTRIES.LIST (added
    2026-09-20, live bug fix - see the AMOUNT note on parse_daybook_response
    below) so the party ledger's own AMOUNT comes back regardless of
    whether a given voucher was created in Invoice Voucher View mode (uses
    LEDGERENTRIES.LIST - see the whole ALLLEDGERENTRIES.LIST saga elsewhere
    in this file) or the plain Accounting Voucher View (ALLLEDGERENTRIES.LIST)
    - Tally simply omits whichever tag doesn't apply to a given voucher."""
    date_from_tally = _voucher_date_ddmmyyyy(date_from)
    date_to_tally = _voucher_date_ddmmyyyy(date_to)
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>EXPORT</TALLYREQUEST>
  <TYPE>COLLECTION</TYPE>
  <ID>JarvisVoucherCollection</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVFROMDATE>{date_from_tally}</SVFROMDATE>
    <SVTODATE>{date_to_tally}</SVTODATE>
    <SVCURRENTCOMPANY>{_escape_xml(company)}</SVCURRENTCOMPANY>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="JarvisVoucherCollection" ISMODIFY="No">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERTYPENAME,VOUCHERNUMBER,PARTYLEDGERNAME,NARRATION,LEDGERENTRIES.LIST,ALLLEDGERENTRIES.LIST</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def _extract_daybook_amount(voucher_el, party_name: str) -> float | None:
    """Pulls the real bill total off the party ledger entry's own AMOUNT
    (Tally's sign convention: negative = debit, and the party is always
    debited for the FULL invoice total - the exact same convention
    build_sales_voucher_xml relies on above) - never estimated or summed by
    the AI, always this one real number straight from Tally. Checks both
    LEDGERENTRIES.LIST (Invoice Voucher View, the mode this integration's
    own bills use - see the ALLLEDGERENTRIES.LIST saga elsewhere in this
    file) and ALLLEDGERENTRIES.LIST (plain Accounting Voucher View, used by
    older/other vouchers) since a query result can contain either depending
    on how that particular voucher was created. Returns None (never a
    guess) when no matching entry is found, so the caller can say plainly
    "amount not available" instead of a caller/model inventing a number -
    added 2026-09-20 after JARVIS was caught fabricating a bill amount that
    was never actually part of a daybook query result (see progress-tracker.md)."""
    entries = voucher_el.findall("LEDGERENTRIES.LIST") + voucher_el.findall("ALLLEDGERENTRIES.LIST")
    party_name_lower = (party_name or "").strip().lower()
    for entry in entries:
        is_party = (entry.findtext("ISPARTYLEDGER") or "").strip().lower() == "yes"
        entry_name = (entry.findtext("LEDGERNAME") or "").strip().lower()
        if is_party or (party_name_lower and entry_name == party_name_lower):
            amount_text = entry.findtext("AMOUNT")
            if amount_text is None:
                return None
            try:
                return abs(round(float(amount_text), 2))
            except ValueError:
                return None
    return None


def parse_daybook_response(xml_text: str) -> tuple[bool, list[dict] | str]:
    """Returns (True, [{"date": "YYYYMMDD", "voucher_type": ..., "voucher_number":
    ..., "party_name": ..., "narration": ..., "amount": float | None}, ...]) -
    possibly an empty list for a genuinely quiet date range - on a
    recognized Collection response, or (False, message) when the response
    doesn't look like what build_daybook_query_xml expects. "amount" is
    None whenever the party ledger entry couldn't be found/read (see
    _extract_daybook_amount) - callers must treat None as "not available",
    never substitute a guess. Deliberately defensive, same style as
    parse_tally_response: per the caveat on build_daybook_query_xml, this
    hasn't been checked against a real response for every voucher type yet,
    so a caller should be ready to show the raw message to Sudeep (or log
    it) rather than assume the shape is right.

    REAL BUG FOUND AND FIXED 2026-09-20, live, within hours of shipping this
    feature: Sudeep's actual Tally response used `UDF:...` prefixed tags
    (e.g. `<UDF:HBSDESCRIPTION.LIST>`, the same custom TDL field seen on the
    write side) WITHOUT ever declaring the `xmlns:UDF` namespace anywhere in
    its own response envelope - standard-compliant XML requires every prefix
    to be bound to a namespace, so ET.fromstring correctly refused to parse
    it at all ("unbound prefix"), and every single daybook query failed.
    Confirmed by capturing the actual raw response via _log_daybook_query
    and reproducing the exact ParseError locally - not a guess. Fixed by
    injecting the same `xmlns:UDF="TallyUDF"` declaration this module's own
    outgoing XML already uses (see build_sales_voucher_xml) onto the
    response's <ENVELOPE> tag before parsing, since Tally's response never
    declares it itself."""
    if "xmlns:UDF" not in xml_text:
        xml_text = re.sub(r"<ENVELOPE(\s|>)", r'<ENVELOPE xmlns:UDF="TallyUDF"\1', xml_text, count=1)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False, f"Tally sent back a response that wasn't valid XML: {xml_text[:300]}"

    vouchers = root.findall(".//VOUCHER")
    if not vouchers:
        line_error = root.findtext(".//LINEERROR")
        if line_error:
            return False, line_error
        if root.find(".//COLLECTION") is not None:
            return True, []  # well-formed, just nothing in range
        return False, f"Unrecognized response shape (expected a VOUCHER collection): {xml_text[:300]}"

    results = []
    for v in vouchers:
        party_name = v.findtext("PARTYLEDGERNAME") or ""
        results.append({
            "date": v.findtext("DATE") or "",
            "voucher_type": v.findtext("VOUCHERTYPENAME") or "",
            "voucher_number": v.findtext("VOUCHERNUMBER") or "",
            "party_name": party_name,
            "narration": v.findtext("NARRATION") or "",
            "amount": _extract_daybook_amount(v, party_name),
        })
    return True, results


_RECEIPT_LIKE_TYPES = {"receipt", "payment"}
_BILL_NO_RE = re.compile(r"BILL\s*NO\.?\s*[-:]?\s*(\d+)", re.IGNORECASE)


def extract_bill_ref_from_narration(narration: str) -> str | None:
    """Pulls the bare bill-number digits out of a receipt/payment
    narration, e.g. "PAYMENT RECIEVED FOR BILL NO 086" or "PAYMENT RECIEVED
    FOR BILL NO-434" - both real, confirmed patterns in Sudeep's actual
    Tally narrations (added 2026-09-20, see check_bill_payment_status below
    for why). Returns None when the pattern isn't found - never a guess."""
    if not narration:
        return None
    m = _BILL_NO_RE.search(narration)
    return m.group(1) if m else None


def _bill_ref_key(value: str | None) -> str | None:
    """Normalizes a bill reference - a full voucher number like
    "SSRS/26-27/086", a bare "086"/"86", or a narration-extracted digit
    string - down to a comparable key: just the trailing digits with
    leading zeros stripped (at least one digit kept). Only looks at the
    segment after the last "/" when there is one, so the "26-27"
    financial-year part (also all digits) never causes a false match.
    Returns None when there are no digits at all, so a caller can tell
    "nothing to compare" apart from a real mismatch."""
    if not value:
        return None
    segment = value.rsplit("/", 1)[-1]
    digits = re.sub(r"\D", "", segment)
    if not digits:
        return None
    return digits.lstrip("0") or "0"


def check_bill_payment_status(vouchers: list[dict], bill_number: str) -> dict:
    """Pure, deterministic Python - no AI reasoning, no free-form matching -
    decides whether a given bill has been paid. Built 2026-09-20 after
    JARVIS was caught fabricating an ENTIRE bill record (wrong date, wrong
    party, invented Work Order/Complaint numbers that were never part of
    any tool result at all) when Sudeep asked "has bill 086 been paid" and
    the model free-reasoned over a raw query_tally_daybook dump instead of
    a computed answer - Sudeep caught this live by cross-checking against
    Tally's own Day Book screen (see progress-tracker.md for the full
    story and the real captured evidence).

    Matches the target bill_number (accepts a full voucher number like
    "SSRS/26-27/086", or a bare number like "086") against:
    - the SALES/invoice voucher(s) whose own voucher number ends with that
      same numeric reference, EXCLUDING Receipt/Payment vouchers - a
      receipt can coincidentally share the exact same voucher number as
      the bill it's paying (confirmed a real case of this in Sudeep's own
      data), since Sales and Receipt voucher numbering are independent
      Tally sequences.
    - Receipt/Payment vouchers whose own NARRATION explicitly references
      that bill number (Sudeep's real narrations consistently read like
      "PAYMENT RECIEVED FOR BILL NO 086" - see extract_bill_ref_from_narration
      above) - this is the primary, reliable signal. A receipt voucher
      number that happens to match the target (with no narration
      confirmation) is included too, but flagged as a weaker,
      "coincidental" match, since that's not a deliberate linking
      mechanism in Tally.

    Returns a dict, never a guess:
      {"status": "invalid_input" | "not_found" | "amount_unknown" | "unpaid"
                | "partially_paid" | "paid",
       "bill_vouchers": [...], "matched_receipts": [...],
       "total_invoice_amount": float | None, "total_received": float}
    bill_vouchers/matched_receipts entries are the same voucher dicts
    parse_daybook_response produces (plus "matched_by" on each receipt)."""
    target_key = _bill_ref_key(bill_number)
    if target_key is None:
        return {
            "status": "invalid_input",
            "bill_vouchers": [],
            "matched_receipts": [],
            "total_invoice_amount": None,
            "total_received": 0.0,
        }

    bill_vouchers = [
        v for v in vouchers
        if _bill_ref_key(v.get("voucher_number")) == target_key
        and (v.get("voucher_type") or "").strip().lower() not in _RECEIPT_LIKE_TYPES
    ]
    if not bill_vouchers:
        return {
            "status": "not_found",
            "bill_vouchers": [],
            "matched_receipts": [],
            "total_invoice_amount": None,
            "total_received": 0.0,
        }

    matched_receipts = []
    for v in vouchers:
        if (v.get("voucher_type") or "").strip().lower() not in _RECEIPT_LIKE_TYPES:
            continue
        narration_ref = extract_bill_ref_from_narration(v.get("narration") or "")
        matched_by = None
        if narration_ref is not None and _bill_ref_key(narration_ref) == target_key:
            matched_by = "narration"
        elif _bill_ref_key(v.get("voucher_number")) == target_key:
            matched_by = "coincidental voucher-number match (narration didn't confirm it)"
        if matched_by:
            matched_receipts.append({**v, "matched_by": matched_by})

    total_invoice_amount = None
    amounts = [v["amount"] for v in bill_vouchers if v.get("amount") is not None]
    if amounts:
        total_invoice_amount = round(sum(amounts), 2)

    total_received = round(
        sum(r["amount"] for r in matched_receipts if r.get("amount") is not None), 2
    )

    if total_invoice_amount is None:
        status = "amount_unknown"
    elif total_received <= 0:
        status = "unpaid"
    elif total_received + 0.01 >= total_invoice_amount:
        status = "paid"
    else:
        status = "partially_paid"

    return {
        "status": status,
        "bill_vouchers": bill_vouchers,
        "matched_receipts": matched_receipts,
        "total_invoice_amount": total_invoice_amount,
        "total_received": total_received,
    }


_DAYBOOK_LOG_PATH = Path(__file__).parent / "tally_daybook_log.jsonl"
_MAX_DAYBOOK_LOG_ENTRIES = 100


def _log_daybook_query(xml_payload: str, response_text: str, ok: bool, result) -> None:
    """Best-effort permanent log of every daybook query's raw request AND
    Tally's raw response (added 2026-09-20, same day as the amount-
    fabrication fix, to debug a second real issue found minutes after
    shipping that fix: the query started coming back "unrecognized response
    shape" once LEDGERENTRIES.LIST/ALLLEDGERENTRIES.LIST were added to
    FETCH). Deliberately NOT log_tally_failure/diagnose_voucher_xml - those
    are for the create-voucher write path and know nothing about a
    Collection Export query's very different XML shape. Logs every call
    (not just failures) while this read path is still being shaken out, so
    a future debugging session can see exactly what Tally actually sent
    back rather than needing a throwaway debug helper added by hand again."""
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "request_xml": xml_payload,
            "response": response_text,
            # Log the real outcome either way (fixed 2026-09-20 - this used
            # to blank out "result" on failure, which is exactly when it's
            # most needed: parse_daybook_response's second tuple element IS
            # the failure explanation, not something to discard).
            "result": result,
        }
        existing_lines: list[str] = []
        if _DAYBOOK_LOG_PATH.exists():
            existing_lines = _DAYBOOK_LOG_PATH.read_text(encoding="utf-8").splitlines()
        existing_lines.append(json.dumps(entry))
        existing_lines = existing_lines[-_MAX_DAYBOOK_LOG_ENTRIES:]
        _DAYBOOK_LOG_PATH.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def fetch_daybook(
    host: str, port: int, timeout: int, company: str, date_from: str, date_to: str
) -> tuple[bool, list[dict] | str]:
    """Builds the query, sends it, parses the response - the read-side
    counterpart to build_sales_voucher_xml + send_to_tally + parse_tally_response.
    Raises TallyError only for connection-level failures (unreachable/timeout,
    same as send_to_tally); a recognized-but-empty result or an unexpected
    response shape comes back as (False, message) rather than an exception,
    since "no vouchers today" and "couldn't understand Tally's answer" are
    both ordinary outcomes a caller should handle gracefully, not crashes.
    Every call is permanently logged (see _log_daybook_query) while this
    read path is still new.

    REAL BUG FOUND AND FIXED 2026-09-20, live, same debugging session as the
    XML-namespace fix above: SVFROMDATE/SVTODATE in build_daybook_query_xml's
    STATICVARIABLES do NOT filter a plain TYPE="Voucher" Collection Export -
    confirmed by capturing a real response for a single day (2026-05-26) via
    _log_daybook_query and finding 274 vouchers spanning 80 different dates
    in it, not the requested one. Tally's period variables only filter
    report-shaped exports (like an actual Day Book), not a raw collection -
    which is exactly why this mechanism was chosen over a report export in
    the first place (see the module-level caveat above), but nobody had
    grounds to know the date range itself would be silently ignored until a
    real response proved it. Fixed the only fully reliable way: filter the
    parsed vouchers by their own DATE field in real Python here, the same
    "never trust an external system's claimed filtering, verify for real"
    principle this codebase already applies to GST math. This also means a
    single query can be a genuinely large fetch server-side (Tally sends
    back the WHOLE history every time) - fine for now given Sudeep's current
    voucher count, but worth revisiting (a smarter FILTER clause, or trimming
    FETCH) if response size ever becomes slow."""
    xml_payload = build_daybook_query_xml(company, date_from, date_to)
    response_text = send_to_tally(xml_payload, host, port, timeout)
    ok, result = parse_daybook_response(response_text)
    if ok and isinstance(result, list):
        date_from_tally = _voucher_date_ddmmyyyy(date_from)
        date_to_tally = _voucher_date_ddmmyyyy(date_to)
        result = [v for v in result if date_from_tally <= (v.get("date") or "") <= date_to_tally]
    _log_daybook_query(xml_payload, response_text, ok, result)
    return ok, result


def fetch_all_vouchers(host: str, port: int, timeout: int, company: str) -> tuple[bool, list[dict] | str]:
    """Same read-only Collection Export query as fetch_daybook, but WITHOUT
    fetch_daybook's client-side date post-filter - deliberately returns
    Sudeep's entire voucher history every time. This is genuinely what
    Tally already sends back regardless of the requested date range (see
    the SVFROMDATE/SVTODATE bug note on fetch_daybook above), so this just
    skips re-discarding what Tally already gave us. Added 2026-09-20 for
    check_bill_payment_status/fetch_bill_payment_status below, which search
    by bill NUMBER, not by date - a bill could have been created any time,
    so filtering to a guessed date range up front (which is exactly what
    the free-reasoning payment-status bug that day did wrong) is the wrong
    tool for this question."""
    # A static wide range - Tally ignores it anyway (see fetch_daybook's
    # docstring); this only exists to satisfy build_daybook_query_xml's
    # required arguments.
    xml_payload = build_daybook_query_xml(company, "2000-01-01", "2099-12-31")
    response_text = send_to_tally(xml_payload, host, port, timeout)
    ok, result = parse_daybook_response(response_text)
    _log_daybook_query(xml_payload, response_text, ok, result)
    return ok, result


def fetch_bill_payment_status(
    host: str, port: int, timeout: int, company: str, bill_number: str
) -> tuple[bool, dict | str]:
    """Read-side counterpart to check_bill_payment_status - fetches
    Sudeep's full voucher history (fetch_all_vouchers, not date-limited)
    and runs the deterministic status check on it in real Python, never in
    the model. Raises TallyError only for connection-level failures, same
    convention as fetch_daybook. Added 2026-09-20 after a real live bug:
    see check_bill_payment_status's docstring for the full story."""
    ok, result = fetch_all_vouchers(host, port, timeout, company)
    if not ok:
        return False, result
    return True, check_bill_payment_status(result, bill_number)
