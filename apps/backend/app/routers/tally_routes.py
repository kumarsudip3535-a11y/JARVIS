from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, TallyInvoiceLog
from app.schemas import TallyBillDraft, TallyBillResult, TallyInvoiceOut, TallyStatusOut
from app.auth import get_current_user
from app import tally_client

router = APIRouter(prefix="/api/tally", tags=["tally"])


@router.get("/status", response_model=TallyStatusOut)
def tally_status(current_user: User = Depends(get_current_user)):
    if not settings.tally_enabled:
        return {"reachable": False, "message": "Tally integration is disabled"}
    reachable, message = tally_client.check_tally_reachable(
        settings.tally_host, settings.tally_port, settings.tally_timeout_seconds
    )
    return {"reachable": reachable, "message": message}


@router.post("/create-bill", response_model=TallyBillResult)
def create_bill(
    draft: TallyBillDraft,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not settings.tally_enabled:
        raise HTTPException(status_code=403, detail="Tally integration is disabled")
    if not settings.tally_company_name:
        raise HTTPException(
            status_code=400,
            detail=(
                "TALLY_COMPANY_NAME isn't set in the backend's .env yet - "
                "add your exact Tally company name there first."
            ),
        )
    if not draft.party_name.strip():
        raise HTTPException(status_code=400, detail="A customer/party name is required")
    if not draft.items:
        raise HTTPException(status_code=400, detail="A bill needs at least one line item")

    def _log(status: str, message: str, total: float) -> None:
        entry = TallyInvoiceLog(
            user_id=current_user.id,
            party_name=draft.party_name,
            total_amount=total,
            status=status,
            message=message,
            draft_json=draft.model_dump_json(),
        )
        db.add(entry)
        db.commit()

    # Step 1 (REMOVED 2026-09-20, per Sudeep's explicit instruction): this
    # used to always send a bare-bones "create ledger" XML (NAME + PARENT
    # only) before every voucher, on the theory that Tally's own "duplicate
    # name" response made this harmless for a party that already existed.
    # That assumption was wrong and is the likely root cause of the
    # persistent, generic EXCEPTIONS:1 voucher failures seen while debugging
    # the GST-compliance fix (see progress-tracker.md, 2026-09-19/20): Tally
    # can treat a "Create" for an existing ledger as an ALTER and apply it -
    # so a request carrying only NAME+PARENT for a party ledger that Sudeep
    # had already fully configured with GST registration details in Tally
    # would silently strip that GST master data back down to nothing, right
    # before the voucher (which depends on that master data) was sent.
    # Sudeep's real parties (e.g. "Indian Oil Corporation Limited Dhanbad
    # DO") are already created in his Tally with the correct GST setup -
    # JARVIS must never create or touch ledger masters, only use what's
    # already there.
    #
    # Step 2: work out the real tax lines. If JARVIS gave a rate + interstate/
    # intrastate (the normal path), this is the AUTHORITATIVE computation -
    # whatever tax_lines the request body carried (e.g. echoed back from the
    # chat review card's preview) is ignored and recomputed fresh here, in
    # real Python arithmetic, so nothing the client sent can smuggle in a
    # wrong tax amount. Only when gst_rate/tax_type are both absent does the
    # manual tax_lines override apply as-is (e.g. an unusual split Sudeep
    # dictated directly).
    items_total = round(sum(item.amount for item in draft.items), 2)
    if draft.gst_rate is not None and draft.tax_type:
        try:
            tax_lines = tally_client.compute_gst_tax_lines(
                items_total,
                draft.gst_rate,
                draft.tax_type,
                settings.tally_igst_ledger,
                settings.tally_cgst_ledger,
                settings.tally_sgst_ledger,
            )
        except tally_client.TallyError as e:
            _log("failed", str(e), 0)
            raise HTTPException(status_code=400, detail=str(e))
    else:
        tax_lines = [t.model_dump() for t in draft.tax_lines]

    # Step 3: create the sales voucher itself. total_amount is computed in
    # real Python arithmetic from the items/tax lines here - never trusted
    # from whatever total (if any) the AI-drafted JSON might have implied.
    try:
        voucher_xml, total_amount = tally_client.build_sales_voucher_xml(
            settings.tally_company_name,
            draft.party_name,
            settings.tally_sales_ledger,
            [item.model_dump() for item in draft.items],
            tax_lines,
            draft.voucher_date,
            draft.narration,
            draft.buyer_order_no,
            draft.other_reference_no,
            draft.destination,
            company_gstin=settings.tally_company_gstin,
            company_state=settings.tally_company_state,
            company_gst_registration_name=settings.tally_company_gst_registration_name,
            # Interstate place-of-supply (where it must differ from the
            # company's own state) is a known open follow-up - not needed
            # for today's fix, so this always uses the intrastate default.
            place_of_supply=settings.tally_company_state,
            # Confirmed 2026-09-19: a real GST invoice needs the buyer's
            # GSTIN too. Looked up from Sudeep's known regular parties
            # (see tally_client.KNOWN_PARTY_GSTINS); None for anyone else,
            # same as before this fix.
            party_gstin=tally_client.lookup_party_gstin(draft.party_name),
        )
    except tally_client.TallyError as e:
        _log("failed", str(e), 0)
        raise HTTPException(status_code=400, detail=str(e))

    # Step 3.5 (added 2026-09-20, self-diagnosis): check the XML about to be
    # sent against every known-bad pattern this integration has actually hit
    # in production (see tally_client.diagnose_voucher_xml). A "critical"
    # finding is a confirmed root cause of a past real failure (e.g. the
    # ALLLEDGERENTRIES.LIST bug) - refuse to send and explain exactly why,
    # rather than burning a round trip to reproduce a bug Tally's own
    # gateway won't explain anyway. This never edits the XML itself, only
    # blocks and explains - any actual code fix still needs a human/Claude
    # session, per Sudeep's explicit choice not to let JARVIS auto-patch its
    # own billing code.
    pre_findings = tally_client.diagnose_voucher_xml(voucher_xml)
    critical_pre = [f["text"] for f in pre_findings if f["severity"] == "critical"]
    if critical_pre:
        reason = " ".join(critical_pre)
        tally_client.log_tally_failure(voucher_xml, None, reason)
        _log("failed", reason, total_amount)
        raise HTTPException(
            status_code=400,
            detail=f"Caught before sending to Tally (known issue, not sent): {reason}",
        )

    try:
        voucher_response = tally_client.send_to_tally(
            voucher_xml, settings.tally_host, settings.tally_port, settings.tally_timeout_seconds
        )
    except tally_client.TallyError as e:
        _log("failed", str(e), total_amount)
        raise HTTPException(status_code=502, detail=str(e))

    ok, message = tally_client.parse_tally_response(voucher_response)
    _log("success" if ok else "failed", message, total_amount)
    if not ok:
        # Self-diagnosis (added 2026-09-20): log the full request/response
        # permanently and fold the top finding (if any) into the message
        # shown in chat, instead of a bare "Tally didn't confirm" with no
        # lead on why - this is the permanent, always-on replacement for the
        # throwaway _debug_dump() helper used to crack the 2026-09-20 bug.
        findings = tally_client.log_tally_failure(voucher_xml, voucher_response, message)
        if findings:
            top = findings[0]["text"]
            message = f"{message} | Likely cause: {top}"
        raise HTTPException(
            status_code=502, detail=f"Tally didn't confirm the bill was created: {message}"
        )

    return {"success": True, "message": message, "total_amount": total_amount}


@router.get("/invoices", response_model=list[TallyInvoiceOut])
def list_invoices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(TallyInvoiceLog)
        .filter(TallyInvoiceLog.user_id == current_user.id)
        .order_by(TallyInvoiceLog.created_at.desc())
        .limit(100)
        .all()
    )
