from datetime import datetime
from pydantic import BaseModel, EmailStr, Field

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None = None

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class ChatMessageIn(BaseModel):
    message: str
    conversation_id: int | None = None
    # Agent Factory v1: only used to START a brand-new conversation with a
    # specific agent (ignored once conversation_id is given, since that
    # conversation already has its agent - or lack of one - fixed from when
    # it was created; see chat_routes.py).
    agent_id: int | None = None

class TallyLineItem(BaseModel):
    description: str
    amount: float

class TallyTaxLine(BaseModel):
    ledger: str
    amount: float

class TallyBillDraft(BaseModel):
    party_name: str
    items: list[TallyLineItem]
    # tax_lines is a manual override/escape hatch (an exact ledger+amount
    # list, used as-is with no recalculation) - the normal path is gst_rate +
    # tax_type below, which the backend turns into tax_lines itself with real
    # arithmetic (see tally_client.compute_gst_tax_lines). If both are given,
    # gst_rate/tax_type wins and tax_lines is recomputed from scratch.
    tax_lines: list[TallyTaxLine] = []
    gst_rate: float | None = None  # e.g. 18 for 18% - JARVIS only picks the rate, never does the math
    tax_type: str | None = None  # "interstate" (IGST) or "intrastate" (CGST+SGST split evenly)
    voucher_date: str  # YYYY-MM-DD
    narration: str | None = None
    # Sudeep's own field names for these (see progress-tracker.md): "Buyer's
    # Order No." is his Work Order No, "Other References" is his Complaint
    # No, and Destination is the specific site/petrol pump name. No confirmed
    # Tally XML tag for these yet, so they're folded into the voucher's
    # narration line by tally_client.py rather than a dedicated field.
    buyer_order_no: str | None = None
    other_reference_no: str | None = None
    destination: str | None = None

class OutboundCallDraft(BaseModel):
    """A proposed Twilio call shown for review; creating the draft never
    places a call. The user must press the frontend's explicit call button."""
    to_number: str = Field(..., min_length=6, max_length=32)
    purpose: str = Field(..., min_length=3, max_length=500)
    opening_message: str = Field(..., min_length=10, max_length=700)


class CalendarEventDraft(BaseModel):
    """A reviewable calendar event. Calendar writes require a separate user click."""
    summary: str
    start_iso: str  # RFC3339, e.g. "2026-09-25T15:00:00+05:30"
    end_iso: str
    description: str | None = None
    location: str | None = None
    # None with use_default_reminder=True uses Google Calendar's defaults.
    reminder_minutes_before: int | None = Field(default=None, ge=0, le=40320)
    use_default_reminder: bool = True

class ChatMessageOut(BaseModel):
    conversation_id: int
    reply: str
    # Tally billing integration: set only when JARVIS's reply included a
    # [TALLY_BILL_DRAFT] block (see ai_provider.py's system prompt and
    # chat_routes.py's parsing) - the JSON is stripped out of `reply` and
    # returned here instead, so the frontend can render a proper review
    # card with a "Send to Tally" button rather than showing raw JSON.
    tally_draft: TallyBillDraft | None = None
    # Phase 19 "Email + Calendar": same pattern as tally_draft above, for a
    # [CALENDAR_EVENT_DRAFT] block.
    calendar_draft: CalendarEventDraft | None = None
    # Phase 22: this is only an editable/reviewable proposal. The separate
    # authenticated outbound-call endpoint is invoked only by a user button.
    outbound_call_draft: OutboundCallDraft | None = None
    # Agent Factory v1: set whenever this conversation is tied to a named
    # agent (whether from agent_id on this call or from an earlier message
    # in the same conversation), so the frontend can show/keep showing which
    # agent Sudeep is talking to without a separate lookup.
    agent_id: int | None = None
    agent_name: str | None = None

class ConversationOut(BaseModel):
    id: int
    title: str | None = None

    class Config:
        from_attributes = True

class MemoryOut(BaseModel):
    id: int
    content: str
    category: str | None = None
    source: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class MemoryCreate(BaseModel):
    content: str
    category: str | None = None

class MemoryUpdate(BaseModel):
    content: str
    category: str | None = None

class DocumentOut(BaseModel):
    id: int
    filename: str
    source_type: str
    size_bytes: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True

class AskDocumentsIn(BaseModel):
    document_ids: list[int]
    question: str

class AskDocumentsOut(BaseModel):
    answer: str

class SkillLearnIn(BaseModel):
    topic: str

class SkillOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    content: str
    status: str
    version: int
    created_at: datetime
    approved_at: datetime | None = None

    class Config:
        from_attributes = True

class AgentCreate(BaseModel):
    name: str
    role_description: str
    system_instructions: str | None = None
    allow_web_search: bool = True
    allow_tally_billing: bool = False
    # Phase 19 "Email + Calendar": off by default - not every agent persona
    # should be able to read Sudeep's inbox or touch his calendar, same
    # opt-in reasoning as allow_tally_billing.
    allow_email_calendar: bool = False
    # Phase 23 "Team management": off by default, same reasoning again -
    # not every agent persona should see/manage Sudeep's real team roster,
    # tasks, or attendance data.
    allow_team_management: bool = False
    assigned_skill_ids: list[int] = []
    # Phase 16 "Tool/plugin architecture": which of Sudeep's own CustomTool
    # rows this agent can use - same "empty means none, not all" rule as
    # assigned_skill_ids.
    assigned_custom_tool_ids: list[int] = []

class AgentUpdate(BaseModel):
    name: str | None = None
    role_description: str | None = None
    system_instructions: str | None = None
    allow_web_search: bool | None = None
    allow_tally_billing: bool | None = None
    allow_email_calendar: bool | None = None
    allow_team_management: bool | None = None
    assigned_skill_ids: list[int] | None = None
    assigned_custom_tool_ids: list[int] | None = None

class AgentOut(BaseModel):
    id: int
    name: str
    role_description: str
    system_instructions: str | None = None
    allow_web_search: bool
    allow_tally_billing: bool
    allow_email_calendar: bool
    allow_team_management: bool
    assigned_skill_ids: list[int]
    assigned_custom_tool_ids: list[int]
    status: str
    created_at: datetime
    updated_at: datetime

class CustomToolParam(BaseModel):
    name: str
    description: str = ""
    required: bool = False

class CustomToolCreate(BaseModel):
    name: str
    description: str
    http_method: str = "GET"
    url: str
    param_schema: list[CustomToolParam] = []
    static_headers: dict[str, str] = {}
    auth_header_name: str | None = None
    # Write-only: the raw secret value, if this tool needs one. Never echoed
    # back by the API - see CustomToolOut.has_auth_value.
    auth_value: str | None = None
    is_read_only: bool = True

class CustomToolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    http_method: str | None = None
    url: str | None = None
    param_schema: list[CustomToolParam] | None = None
    static_headers: dict[str, str] | None = None
    auth_header_name: str | None = None
    auth_value: str | None = None
    # Explicit removal - a plain None on auth_value/auth_header_name above
    # means "leave as-is" (standard partial-update semantics), so clearing a
    # previously-set secret needs its own flag rather than overloading None.
    clear_auth: bool = False
    is_read_only: bool | None = None
    enabled: bool | None = None

class CustomToolOut(BaseModel):
    id: int
    name: str
    description: str
    http_method: str
    url: str
    param_schema: list[CustomToolParam]
    static_headers: dict[str, str]
    auth_header_name: str | None = None
    # Never the real secret - just whether one is currently stored, so the
    # UI can show "API key set" without ever re-displaying it.
    has_auth_value: bool
    is_read_only: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

class TallyBillResult(BaseModel):
    success: bool
    message: str
    total_amount: float

class TallyInvoiceOut(BaseModel):
    id: int
    party_name: str
    total_amount: float
    status: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True

class TallyStatusOut(BaseModel):
    reachable: bool
    message: str

class CodeExecuteIn(BaseModel):
    language: str  # "python", "javascript", or "sql"
    code: str

class CodeExecuteOut(BaseModel):
    success: bool
    output: str | None = None
    error: str | None = None
    execution_time: float | None = None  # seconds

class GoogleStatusOut(BaseModel):
    """Phase 19 "Email + Calendar": what the Google-connect settings page
    (and the chat health check, mirroring how Tally's own status is shown)
    reads to know whether Sudeep has a real connected Google account, and
    which one, without ever exposing a token."""
    connected: bool
    google_email: str | None = None
    connected_at: datetime | None = None
    scopes: list[str] = []

class GoogleAuthUrlOut(BaseModel):
    auth_url: str

class CalendarEventResult(BaseModel):
    success: bool
    message: str
    event_id: str | None = None
    html_link: str | None = None

class PhoneCallRecordOut(BaseModel):
    id: int
    conversation_id: int
    caller_number: str
    caller_name: str | None = None
    reason: str | None = None
    preferred_callback_time: str | None = None
    callback_requested: bool
    appointment_requested: bool = False
    appointment_summary: str | None = None
    appointment_start_iso: str | None = None
    appointment_location: str | None = None
    is_read: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

