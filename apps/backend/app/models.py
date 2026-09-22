from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Float, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversations = relationship("Conversation", back_populates="user")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=True)
    # Agent Factory v1 (custom feature, see progress-tracker.md): set once,
    # at creation, when this conversation was started with a specific named
    # agent rather than general JARVIS chat - every later message in this
    # conversation keeps using that same agent's persona/tool permissions
    # (see chat_routes.py), so Sudeep doesn't have to keep re-specifying it.
    # Null means an ordinary general-JARVIS conversation, same as before this
    # feature existed.
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    # Phase 20 "Incoming phone agent" (added 2026-09-22, see phone_agent.py
    # and progress-tracker.md): Twilio's CallSid, so a phone call's own
    # conversation can be looked back up on every subsequent /api/phone/
    # gather turn of the SAME call (Twilio's webhooks are stateless HTTP
    # requests - CallSid is the one stable identifier across all of them).
    # Null for every conversation that isn't a phone call, same "nullable
    # column added on an existing table" pattern as agent_id above - see
    # main.py's _ensure_conversation_phone_call_sid_column().
    phone_call_sid = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String, nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversation = relationship("Conversation", back_populates="messages")


class Memory(Base):
    """Phase 9: a durable fact JARVIS knows about Sudeep, independent of any
    one conversation. Most rows are created automatically (source="auto")
    after a chat reply, when JARVIS notices something worth remembering
    long-term (a business, a preference, a family detail, an ongoing
    project). Sudeep can also add/edit/delete these directly (source
    becomes "manual" once he edits one by hand) - see memory_routes.py."""
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String, nullable=True)  # business | family | preference | project | general
    source = Column(String, nullable=False, default="auto")  # "auto" or "manual"
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class Document(Base):
    """Phase 10: a file Sudeep has uploaded to JARVIS's knowledge base. The
    actual file content lives with Anthropic (uploaded via the Files API,
    referenced here by anthropic_file_id) - this row is just JARVIS's own
    record of what's been uploaded, who it belongs to, and how to ask about
    it later (see knowledge_routes.py). Word/Excel files are converted to
    plain text before upload, since Anthropic's Files API reads PDF/text
    natively but not .docx/.xlsx - that's why source_type (what Sudeep
    actually gave us) and mime_type (what got uploaded to Anthropic) can
    differ."""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename = Column(String, nullable=False)
    source_type = Column(String, nullable=False)  # pdf | docx | xlsx | txt | csv
    mime_type = Column(String, nullable=False)  # what was actually uploaded to Anthropic
    anthropic_file_id = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")


class Skill(Base):
    """Phase 11 (Universal Skill Engine, v1 - knowledge skills only): a topic
    Sudeep asked JARVIS to "learn". JARVIS researches it (reusing Phase 8's
    web search) and writes up a structured knowledge summary, saved here with
    status="pending_review" - it does NOT get used in chat until Sudeep
    reviews and approves it (status="active"), per the charter's "get
    Sudeep's approval -> activate" safety rule. Only active skills get folded
    into the chat system prompt (see chat_routes.py), the same way Phase 9's
    memories are. This is a deliberately simple v1 of the charter's much
    bigger vision (sub-skills, sandboxed practice, automated tests, security
    scans, versioned learning history) - see progress-tracker.md."""
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)  # the topic Sudeep asked to learn
    description = Column(String, nullable=True)  # one-line summary
    content = Column(Text, nullable=False)  # the researched knowledge write-up
    status = Column(String, nullable=False, default="pending_review")  # pending_review | active
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    approved_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")


class Agent(Base):
    """Agent Factory v1 (custom feature, NOT one of the charter's 29 numbered
    phases - built at Sudeep's explicit request, jumping ahead of Phases
    16/17, see progress-tracker.md for the full scoping discussion). An
    agent is a named, reusable persona layered on top of JARVIS's EXISTING
    chat engine - not a separate AI, model, or sandboxed process. Chatting
    with an agent reuses the exact same /api/chat/message pipeline as
    general JARVIS chat (same model, same memory, same Tally billing
    machinery) - the only things that change per-agent are: (1) a system-
    prompt overlay built from role_description/system_instructions telling
    JARVIS to stay in that role, and (2) a narrower toolbox: allow_web_search
    and allow_tally_billing gate whether that call gets the web-search tool /
    Tally ledger context and billing-draft ability at all (enforced in code
    in chat_routes.py, not just by asking nicely in the prompt), and
    assigned_skill_ids restricts which of Sudeep's already-learned, already-
    approved Skills (see the Skill model above) this agent draws on, instead
    of all active skills. Deliberately does NOT include: a document/
    knowledge-base assignment (Phase 10's knowledge base is a manual per-
    question "ask", not chat context, so an agent-level document list would
    be a no-op in v1 - a natural v2 addition once there's a real use for it),
    multi-agent teams/delegation/messaging, a coding sandbox, or any new tool
    category beyond what JARVIS already has."""
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    role_description = Column(Text, nullable=False)  # what this agent is for, Sudeep's own words
    system_instructions = Column(Text, nullable=True)  # optional extra persona/behavior detail
    allow_web_search = Column(Boolean, nullable=False, default=True)
    allow_tally_billing = Column(Boolean, nullable=False, default=False)
    # Phase 19 "Email + Calendar" (added 2026-09-22) - same gating pattern as
    # allow_tally_billing above: plain chat gets email/calendar tools by
    # default (build_reply_context's allow_email_calendar param), but an
    # agent must explicitly opt in, since not every persona should be able to
    # read Sudeep's inbox or touch his calendar. Added via main.py's
    # idempotent startup migration, same technique already used for
    # assigned_custom_tool_ids.
    allow_email_calendar = Column(Boolean, nullable=False, default=False)
    # JSON-encoded list of Skill.id (text column, not a join table, to match
    # this project's existing "no Alembic, keep it simple" style) - only
    # skills that are status="active" are ever actually recalled, same rule
    # as general chat.
    assigned_skill_ids = Column(Text, nullable=False, default="[]")
    # Phase 16 "Tool/plugin architecture" (added 2026-09-20, see
    # progress-tracker.md and the CustomTool model below): JSON-encoded list
    # of CustomTool.id, same pattern/enforcement style as assigned_skill_ids
    # above - empty means "no custom tools", not "all of them". Added via
    # main.py's idempotent startup migration, same technique already used
    # for Conversation.agent_id.
    assigned_custom_tool_ids = Column(Text, nullable=False, default="[]")
    status = Column(String, nullable=False, default="active")  # active | paused
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class CustomTool(Base):
    """Phase 16 "Tool/plugin architecture" (added 2026-09-20 - see
    progress-tracker.md and custom_tools.py). A tool Sudeep defines himself:
    a plain HTTP call JARVIS can make on his behalf without needing a new
    coding session for every new integration. Any Agent can be assigned any
    subset of these (Agent.assigned_custom_tool_ids). is_read_only decides
    how it's actually used: a read-only (GET) tool is called for real by
    JARVIS; a non-read-only one is DELIBERATELY drafted-only in v1 - see
    custom_tools.invoke_custom_tool and ai_provider.py's tool-use loop for
    why. auth_header_name/encrypted_auth_value hold an optional secret (an
    API key, a bearer token) needed to call the tool - the raw value is
    never stored: encrypted_auth_value is Fernet-encrypted with
    settings.tool_secrets_key and only ever decrypted right before making
    the real HTTP call, never returned by the API or shown in any log."""
    __tablename__ = "custom_tools"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)  # a short identifier, e.g. "check_shipment_status"
    description = Column(Text, nullable=False)  # tells JARVIS what this does and when to use it
    http_method = Column(String, nullable=False, default="GET")  # GET | POST | PUT | PATCH | DELETE
    url = Column(String, nullable=False)
    # JSON-encoded list of {"name": str, "description": str, "required": bool}
    # - the parameters JARVIS can/must supply when calling this tool.
    param_schema = Column(Text, nullable=False, default="[]")
    # JSON-encoded dict of static, non-secret headers (e.g. {"Accept": "application/json"}).
    static_headers = Column(Text, nullable=False, default="{}")
    auth_header_name = Column(String, nullable=True)  # e.g. "Authorization" or "X-API-Key"
    encrypted_auth_value = Column(Text, nullable=True)  # Fernet-encrypted, never plaintext
    is_read_only = Column(Boolean, nullable=False, default=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class Automation(Base):
    """Phase 18 "Automation" v1 (added 2026-09-20, scoped with Sudeep via
    AskUserQuestion - see progress-tracker.md). A standing instruction
    Sudeep sets up in chat (via the create_automation tool - see
    chat_routes.py/ai_provider.py) that JARVIS then runs on its own on a
    schedule, without him asking again each time. v1 scope, exactly as
    Sudeep chose: schedule-based only (once/daily/weekly - no event-
    triggered or conditional automations yet, a natural v2), and every run
    is restricted to read-only actions only (see automation_engine.run_one -
    Tally billing/daybook access is forced off regardless of the agent's
    own settings, and the create/list/cancel tools themselves are withheld
    during a run so an automation can never create, change, or cancel
    another automation on its own) - so a run never needs Sudeep's
    approval. Because the backend only runs while Sudeep has JARVIS open
    (see progress-tracker.md), scheduling uses a "catch up on next open"
    model: next_due_at is checked both at startup and on a periodic
    background loop while running (see main.py), and anything overdue just
    runs then, labeled as late if it was overdue by more than a few
    minutes (automation_engine.LATE_THRESHOLD_MINUTES). agent_id carries
    over whichever agent's persona/tool-permissions the automation was
    created under (None means plain JARVIS). conversation_id starts as
    None and is filled in the first time the automation actually runs
    (automation_engine.run_one creates a dedicated conversation for it then,
    titled after the automation, so its results don't mix into whatever
    conversation Sudeep happened to create it from) - every later run
    appends to that same conversation as an ordinary user/assistant message
    pair, so Sudeep just sees the result next time he opens it. There is no
    dedicated Automations page in v1 - everything is managed via chat."""
    __tablename__ = "automations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    name = Column(String, nullable=False)
    instruction = Column(Text, nullable=False)
    schedule_type = Column(String, nullable=False)  # "once" | "daily" | "weekly"
    time_of_day = Column(String, nullable=False)  # "HH:MM", 24-hour, Sudeep's local time
    day_of_week = Column(String, nullable=True)  # "monday".."sunday" - weekly only
    run_date = Column(String, nullable=True)  # "YYYY-MM-DD" - once only
    enabled = Column(Boolean, nullable=False, default=True)
    next_due_at = Column(DateTime(timezone=False), nullable=True)
    last_run_at = Column(DateTime(timezone=False), nullable=True)
    last_status = Column(String, nullable=True)  # "success" | "error"
    last_result_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class TallyInvoiceLog(Base):
    """Custom Tally billing feature (not one of the charter's 29 numbered
    phases - added at Sudeep's request, see progress-tracker.md): an audit
    record of every attempt to create a sales voucher in Tally, whether it
    succeeded or failed. This is the ONLY record JARVIS itself keeps of a
    bill - the real, authoritative bill lives in Tally's own books. Kept so
    Sudeep (or a future session) can see what was sent and what Tally said
    back, especially useful for troubleshooting a failed attempt."""
    __tablename__ = "tally_invoice_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    party_name = Column(String, nullable=False)
    total_amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)  # "success" or "failed"
    message = Column(Text, nullable=False)  # Tally's response summary or the error
    draft_json = Column(Text, nullable=True)  # the draft that was sent, for troubleshooting
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")


class GoogleAccount(Base):
    """Phase 19 "Email + Calendar" (added 2026-09-22, scoped with Sudeep via
    3 AskUserQuestion questions - see progress-tracker.md and
    google_client.py's own docstring for the full design). One row per
    connected Google account - v1 is a single account (Sudeep's own Gmail,
    kumar.sudip3535@gmail.com, his explicit choice), but this is keyed by
    user_id (unique) rather than being a single global row, matching how
    every other per-user credential in this project is modeled, and leaving
    room for a second connected account later without a schema change.

    encrypted_access_token/encrypted_refresh_token are Fernet-encrypted with
    settings.tool_secrets_key (see google_client.encrypt_token/decrypt_token)
    - the SAME key Phase 16's custom tool secrets already use, reusing an
    existing standard of protection rather than asking Sudeep to generate and
    configure a second key for the same purpose. The raw tokens are never
    logged or returned by any API response - only google_client.py ever
    decrypts them, right before making a real Gmail/Calendar call.

    access_token_expiry lets get_valid_access_token (google_client.py) know
    whether the stored access_token is still usable or needs a real refresh
    call first - Google's access tokens are short-lived (typically ~1 hour)
    by design, so this is checked on every single Gmail/Calendar call, never
    assumed valid."""
    __tablename__ = "google_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    google_email = Column(String, nullable=False)
    encrypted_access_token = Column(Text, nullable=False)
    encrypted_refresh_token = Column(Text, nullable=False)
    access_token_expiry = Column(DateTime(timezone=True), nullable=False)
    scopes = Column(Text, nullable=False, default="[]")  # JSON-encoded list, what was actually granted
    connected_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")
