import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL", "")
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

    ai_provider: str = os.getenv("AI_PROVIDER", "anthropic")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

    # Phase 8 (internet research): JARVIS can search the web when a question
    # needs current information. On by default; set WEB_SEARCH_ENABLED=false
    # in .env to turn it off entirely (e.g. to keep replies fast/offline-only).
    # WEB_SEARCH_MAX_USES caps how many searches ONE reply can trigger, since
    # each search is billed - keeps usage "controlled" rather than unlimited.
    web_search_enabled: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"
    web_search_max_uses: int = int(os.getenv("WEB_SEARCH_MAX_USES", "3"))

    # Phase 9 (memory): JARVIS automatically notices durable facts worth
    # remembering (businesses, preferences, family, ongoing projects) after
    # each reply, and recalls them in future chats. This is the "Memory"
    # on/off switch from the project charter's Privacy Controls - set
    # MEMORY_ENABLED=false in .env to turn it off completely (JARVIS will
    # neither save nor recall anything automatically; existing memories are
    # kept but ignored). MEMORY_MAX_RECALL caps how many saved memories get
    # pulled into each chat, so the prompt doesn't grow without bound as
    # memories pile up over time.
    memory_enabled: bool = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
    memory_max_recall: int = int(os.getenv("MEMORY_MAX_RECALL", "40"))

    # Phase 10 (knowledge base): documents Sudeep uploads so JARVIS can answer
    # questions about them (PDF, Word, Excel, text files - converted server-side
    # where needed, then stored with Anthropic's Files API). Asking about a
    # document is a separate, one-off call (see knowledge_routes.py) rather
    # than something folded into regular chat history, so attaching a big
    # document doesn't get re-sent - and re-billed - on every later unrelated
    # message in that conversation. KNOWLEDGE_MAX_UPLOAD_MB caps file size so
    # one huge upload can't stall the app or blow past Anthropic's limits.
    knowledge_base_enabled: bool = os.getenv("KNOWLEDGE_BASE_ENABLED", "true").lower() == "true"
    knowledge_max_upload_mb: int = int(os.getenv("KNOWLEDGE_MAX_UPLOAD_MB", "20"))

    # Phase 11 (Universal Skill Engine, v1 - knowledge skills): Sudeep asks
    # JARVIS to "learn" a topic; it researches it (via Phase 8's web search)
    # and writes up a knowledge summary, which only becomes usable in chat
    # after Sudeep reviews and approves it (the charter's approval-before-
    # activate safety rule). SKILL_ENGINE_ENABLED=false in .env turns the
    # whole feature off. SKILL_MAX_ACTIVE_RECALL caps how many approved
    # skills get folded into every chat's system prompt (like memory_max_
    # recall does for memories) - kept low by default because a skill's
    # full write-up can be long, unlike a one-line memory. SKILL_CONTEXT_
    # CHARS_PER_SKILL truncates each skill's content when folding it into
    # the prompt, so a handful of long skills can't blow up the prompt size
    # on every single chat message - the full untruncated content is always
    # visible on the Skills page itself.
    skill_engine_enabled: bool = os.getenv("SKILL_ENGINE_ENABLED", "true").lower() == "true"
    skill_max_active_recall: int = int(os.getenv("SKILL_MAX_ACTIVE_RECALL", "5"))
    skill_context_chars_per_skill: int = int(os.getenv("SKILL_CONTEXT_CHARS_PER_SKILL", "3000"))

    # Tally billing integration (custom feature, added at Sudeep's request -
    # not one of the charter's 29 numbered phases, tracked separately in
    # progress-tracker.md). JARVIS talks directly to TallyPrime's own HTTP-XML
    # server over the local network (no cloud account involved) to create
    # sales vouchers. TALLY_COMPANY_NAME must exactly match the company name
    # as it appears in Tally (case-sensitive) - REQUIRED, there's no sane
    # default. TALLY_SALES_LEDGER is the ledger credited for the item total;
    # most businesses call this "Sales" or "Sales Accounts" in Tally, so it
    # defaults to "Sales" but should be checked against Sudeep's actual books.
    # TALLY_NEW_CUSTOMER_GROUP is the parent group used when JARVIS has to
    # create a brand-new customer ledger on the fly (Sudeep chose this
    # behavior over requiring the customer to already exist).
    tally_enabled: bool = os.getenv("TALLY_ENABLED", "true").lower() == "true"
    tally_host: str = os.getenv("TALLY_HOST", "127.0.0.1")
    tally_port: int = int(os.getenv("TALLY_PORT", "9000"))
    tally_company_name: str = os.getenv("TALLY_COMPANY_NAME", "")
    tally_sales_ledger: str = os.getenv("TALLY_SALES_LEDGER", "Sales")
    tally_new_customer_group: str = os.getenv("TALLY_NEW_CUSTOMER_GROUP", "Sundry Debtors")
    tally_timeout_seconds: int = int(os.getenv("TALLY_TIMEOUT_SECONDS", "15"))

    # GST ledger names, used when JARVIS computes tax itself from a rate
    # Sudeep gives it (e.g. "IGST, 18%") instead of him supplying pre-
    # calculated amounts. TALLY_IGST_LEDGER defaults to "OUTPUT IGST" because
    # that's the actual label on one of Sudeep's real invoices - a real data
    # point, not a guess. TALLY_CGST_LEDGER/TALLY_SGST_LEDGER default to the
    # same "OUTPUT ..." naming pattern for consistency, but that part IS a
    # guess (no same-state invoice was seen to confirm it) - Sudeep should
    # check these three against Chart of Accounts -> Duties & Taxes and
    # correct any that don't match his real ledger names.
    tally_igst_ledger: str = os.getenv("TALLY_IGST_LEDGER", "OUTPUT IGST")
    tally_cgst_ledger: str = os.getenv("TALLY_CGST_LEDGER", "OUTPUT CGST")
    tally_sgst_ledger: str = os.getenv("TALLY_SGST_LEDGER", "OUTPUT SGST")

    # GST registration details for Sudeep's own company (fixed 2026-09-19).
    # A real Tally "Accounting Invoice" under GST needs these on every
    # voucher - without them, Tally accepted the old XML as a plain voucher
    # but rejected it outright once the voucher was correctly marked as a
    # GST invoice (see progress-tracker.md, "Tally billing integration").
    # These three defaults are REAL, confirmed values read directly off a
    # genuine, successfully-created invoice Sudeep exported from his own
    # Tally (SS Retail Services, Jharkhand registration) - not guesses - so
    # no .env change is needed unless Sudeep's registration details change.
    tally_company_gstin: str = os.getenv("TALLY_COMPANY_GSTIN", "20DTAPS6494E3ZL")
    tally_company_state: str = os.getenv("TALLY_COMPANY_STATE", "Jharkhand")
    tally_company_gst_registration_name: str = os.getenv(
        "TALLY_COMPANY_GST_REGISTRATION_NAME", "Jharkhand Registration"
    )

    # Agent Factory v1 (custom feature, not one of the charter's 29 numbered
    # phases - built at Sudeep's request, jumping ahead of Phases 16/17; see
    # progress-tracker.md). AGENT_FACTORY_ENABLED=false in .env turns the
    # whole feature off (existing agents are kept but every /api/agents and
    # agent-scoped chat call is refused). AGENT_MAX_ACTIVE caps how many
    # active (non-paused) agents one account can have at once - a small,
    # simple stand-in for the charter's "prevent runaway agent creation"
    # resource-governance rule (section 23/29), enforced on both create and
    # resume.
    agent_factory_enabled: bool = os.getenv("AGENT_FACTORY_ENABLED", "true").lower() == "true"
    agent_max_active: int = int(os.getenv("AGENT_MAX_ACTIVE", "10"))

    # Phase 13 "Debugging Agent" (custom scoping, built 2026-09-20 - see
    # progress-tracker.md). Lets JARVIS read its own recently-logged backend
    # errors and its own backend source code (read-only, never edits or
    # deploys anything) so it can actually investigate a bug instead of
    # guessing - see debug_agent.py. Unlike Tally billing/daybook, this is
    # NOT gated per-agent in v1 (every agent and the main chat get it, the
    # same way every agent already gets the coding sandbox) - a deliberate,
    # documented v1 scoping choice, not an oversight. DEBUG_AGENT_ENABLED=
    # false in .env turns the whole feature off.
    debug_agent_enabled: bool = os.getenv("DEBUG_AGENT_ENABLED", "true").lower() == "true"

    # Phase 14 "Self-diagnostics" / Phase 15 "Self-healing" (built 2026-09-20
    # - see progress-tracker.md and health_check.py). Lets JARVIS run a
    # read-only health check across the AI provider, database, Tally, the
    # Docker sandbox, and web search - on-demand via chat only in v1, not a
    # periodic background job. "Self-healing" here is deliberately narrow:
    # a couple of automatic retries for a genuinely transient failure
    # (health_check.py's checks, and the real Tally send path in
    # tally_client.py's send_to_tally), never a code or settings change.
    # Same on/off pattern as every other feature - not gated per-agent, the
    # same v1 choice already made for the Phase 13 debugging tools.
    health_check_enabled: bool = os.getenv("HEALTH_CHECK_ENABLED", "true").lower() == "true"

    # Phase 16 "Tool/plugin architecture" (built 2026-09-20 - see
    # progress-tracker.md and custom_tools.py). Lets Sudeep define his own
    # simple HTTP-based tools (name, description, URL, method, parameters,
    # optional API key) that any agent can be assigned, instead of only the
    # handful of built-in capabilities JARVIS ships with. A read-only
    # (GET) custom tool is actually called live; a non-read-only one
    # (POST/PUT/PATCH/DELETE) is DELIBERATELY drafted-only in v1 - JARVIS
    # shows exactly what it would send but never sends it automatically, the
    # same "never take an autonomous action with a real side effect" caution
    # used everywhere else in this project. Full one-click sending for
    # action calls is a natural v2 addition once there's a real use case for
    # it. TOOL_REGISTRY_ENABLED=false in .env turns the whole feature off.
    # TOOL_SECRETS_KEY is a Fernet key used to encrypt any API key/secret a
    # custom tool needs before it's stored in the database - never stored or
    # logged in plain text. Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # and put it in .env as TOOL_SECRETS_KEY=... A custom tool that needs a
    # secret simply can't be created (or invoked) until this is set - see
    # custom_tools.py. TOOL_CALL_TIMEOUT_SECONDS/TOOL_MAX_RESPONSE_CHARS cap
    # how long a call can take and how much of the response JARVIS reads, so
    # one misbehaving external API can't hang or flood a chat reply.
    tool_registry_enabled: bool = os.getenv("TOOL_REGISTRY_ENABLED", "true").lower() == "true"
    tool_secrets_key: str = os.getenv("TOOL_SECRETS_KEY", "")
    tool_call_timeout_seconds: int = int(os.getenv("TOOL_CALL_TIMEOUT_SECONDS", "15"))
    tool_max_response_chars: int = int(os.getenv("TOOL_MAX_RESPONSE_CHARS", "8000"))

    # Phase 17 "AI agent orchestration" (built 2026-09-20, scoped with Sudeep
    # via three clarifying questions: v1 is a single-hop "consult" only, not
    # open-ended multi-agent chains - one agent can automatically loop in ONE
    # other enabled agent mid-conversation, then gives Sudeep one final
    # combined reply. Automatic, but always disclosed - the reply always
    # states which agent(s) were consulted, enforced in code (chat_routes.py
    # appends this if the model doesn't), not just by asking the model to
    # mention it. The consulted agent answers using its own persona + web
    # search only (no Tally/debug/health-check/custom tools, and it is never
    # itself given a consult_agent tool) - a deliberately conservative v1
    # scope, since Sudeep never directly reviews this intermediate exchange.
    # Not gated per-agent, same v1 choice already made for the Phase 13/14
    # tools. AGENT_CONSULT_ENABLED=false in .env turns the whole feature off.
    agent_consult_enabled: bool = os.getenv("AGENT_CONSULT_ENABLED", "true").lower() == "true"

    # Phase 18 "Automation" (built 2026-09-20, scoped with Sudeep via
    # AskUserQuestion: v1 is schedule-based only (once/daily/weekly - no
    # event-triggered/conditional automations yet), every run is restricted
    # to read-only actions only (no Tally billing/daybook access, and an
    # automation can never create/change/cancel another automation on its
    # own - see automation_engine.py and chat_routes.py's build_reply_
    # context), and it's all managed via chat (create_automation/
    # list_automations/cancel_automation tools) - no dedicated page in v1.
    # Because JARVIS's backend only runs while Sudeep has it open, a due
    # automation "catches up" - it runs as soon as the backend is next
    # started or, while already running, on the next periodic check (see
    # main.py) - rather than firing at the exact scheduled instant.
    # AUTOMATION_ENGINE_ENABLED=false in .env turns the whole feature off
    # (existing automations are kept but never run, and the three tools are
    # withheld from chat). AUTOMATION_CHECK_INTERVAL_SECONDS controls how
    # often the periodic background loop checks for anything newly due
    # while the backend stays open across a scheduled time.
    automation_engine_enabled: bool = os.getenv("AUTOMATION_ENGINE_ENABLED", "true").lower() == "true"
    automation_check_interval_seconds: int = int(os.getenv("AUTOMATION_CHECK_INTERVAL_SECONDS", "60"))

    # Reminder feature (added 2026-09-26, Sudeep's explicit request - see
    # reminder_engine.py's own module docstring for the full picture).
    # Reuses the existing Twilio account (TWILIO_ACCOUNT_SID/TWILIO_AUTH_
    # TOKEN/TWILIO_FROM_NUMBER above); the only new setting is where to
    # actually send the reminder.
    reminder_engine_enabled: bool = os.getenv("REMINDER_ENGINE_ENABLED", "true").lower() == "true"
    reminder_check_interval_seconds: int = int(os.getenv("REMINDER_CHECK_INTERVAL_SECONDS", "300"))
    reminder_lead_minutes: int = int(os.getenv("REMINDER_LEAD_MINUTES", "60"))
    reminder_to_number: str = os.getenv("PHONE_AGENT_REMINDER_TO_NUMBER", "")

    # Multi-Model AI Brain upgrade (added 2026-09-21, scoped with Sudeep via
    # AskUserQuestion after he pasted the 82-section "JARVIS MASTER UPGRADE
    # PROMPT" - see progress-tracker.md and ai_provider.py's AIProviderManager
    # docstring). Lets JARVIS fall back across Gemini, Groq, and Claude
    # instead of hard-failing when one provider is down. AI_PRIMARY_PROVIDER/
    # AI_SECONDARY_PROVIDER/AI_TERTIARY_PROVIDER (values: "anthropic",
    # "gemini", "groq") set the fallback chain order - defaults to
    # anthropic -> gemini -> groq, a DELIBERATE deviation from the master
    # prompt's own suggested gemini-first default: Sudeep chose to keep
    # Claude as the live default for now, since every existing tool's
    # anti-fabrication/anti-crash fix (Tally daybook, bill payment status,
    # the automation NoneType crash, etc.) was live-tested specifically
    # against Claude's behavior - each tool path should be individually
    # re-verified against Gemini before it's promoted to primary. Flip
    # AI_PRIMARY_PROVIDER=gemini in .env (and restart) once ready - no code
    # change needed. A provider whose API key isn't set below is
    # automatically left out of the chain (not a startup failure), so
    # Gemini/Groq can be added whenever Sudeep is ready without breaking
    # anything in the meantime.
    #
    # GEMINI_MODEL/GROQ_MODEL default to the exact model IDs verified
    # against each provider's own official current documentation on
    # 2026-09-21 (ai.google.dev/gemini-api/docs/models and
    # console.groq.com/docs/models - see AI_PROVIDERS.md's "Model
    # verification" section) - not blindly copied from the upgrade prompt's
    # suggested IDs, even though (checked independently) they happened to
    # match exactly. GROQ_FAST_MODEL is wired up here but not yet USED by
    # any routing logic - the master prompt's Task Classifier/Model Router/
    # "fast vs quality" routing and the whole Thinking Level (LOW/MEDIUM/
    # HIGH) system were explicitly scoped OUT of this v1 pass (see
    # progress-tracker.md) - it's here so a future routing pass can use it
    # without another .env change.
    #
    # AI_MAX_RETRIES is accepted for forward-compatibility with the master
    # prompt's own suggested env var name but ISN'T wired into an intra-
    # provider retry loop in v1 - trying the NEXT provider in the configured
    # chain on a failure (AIProviderManager's real fallback mechanism) is a
    # stronger recovery than retrying the same failing provider again, so v1
    # deliberately doesn't add a second retry layer on top of it. AI_
    # REQUEST_TIMEOUT_MS IS used directly, as the httpx client timeout for
    # GeminiProvider/GroqProvider's own HTTP calls (AnthropicProvider keeps
    # using the anthropic SDK's own default timeout, unchanged, since it
    # predates this upgrade and wasn't touched).
    ai_primary_provider: str = os.getenv("AI_PRIMARY_PROVIDER", "anthropic")
    ai_secondary_provider: str = os.getenv("AI_SECONDARY_PROVIDER", "gemini")
    ai_tertiary_provider: str = os.getenv("AI_TERTIARY_PROVIDER", "groq")
    ai_max_retries: int = int(os.getenv("AI_MAX_RETRIES", "1"))
    ai_request_timeout_ms: int = int(os.getenv("AI_REQUEST_TIMEOUT_MS", "60000"))

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    groq_fast_model: str = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")

    # Phase 19 "Email + Calendar" (added 2026-09-22, Sudeep's explicit choice
    # of what to build right after the Multi-Model AI Brain upgrade was
    # live-verified - scoped with him via 3 AskUserQuestion questions, see
    # progress-tracker.md and google_client.py's own docstring for the full
    # design). GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET come from a Google Cloud
    # Console OAuth Client ID Sudeep creates himself (never pasted into chat -
    # same credential-handling rule as every other secret in this project).
    # GOOGLE_OAUTH_REDIRECT_URI must match, character-for-character, the
    # "Authorized redirect URI" Sudeep registers on that Client ID in Google
    # Cloud Console - defaults to the backend's own callback route
    # (google_routes.py) on localhost, matching how every other local-only
    # callback in this project is addressed. EMAIL_CALENDAR_ENABLED=false in
    # .env turns the whole feature off (mirrors automation_engine_enabled/
    # tool_registry_enabled's own on/off-switch pattern) - the Google-connect
    # settings page and every email/calendar tool are withheld from chat
    # without needing Google credentials removed. No new encryption key is
    # needed: google_client.py reuses tool_secrets_key (the same Fernet key
    # Phase 16's custom tool secrets already use) to encrypt the stored
    # Google access/refresh tokens at rest.
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_oauth_redirect_uri: str = os.getenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/api/google/oauth/callback"
    )
    email_calendar_enabled: bool = os.getenv("EMAIL_CALENDAR_ENABLED", "true").lower() == "true"

    # Phase 20 "Incoming phone agent" (added 2026-09-22, scoped with Sudeep
    # via 3 AskUserQuestion questions - see progress-tracker.md and
    # phone_agent.py's own docstring for the full design). Twilio credentials
    # - Sudeep adds these to .env himself once his Twilio account and number
    # are set up, never pasted into chat, matching every other API credential
    # in this project.
    phone_agent_enabled: bool = os.getenv("PHONE_AGENT_ENABLED", "true").lower() == "true"
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    # Which JARVIS account's persona/settings a call uses - Twilio's webhook
    # carries no Bearer token, so this has to be configured explicitly
    # rather than inferred (same "who is this for" gap every other
    # unauthenticated webhook in this project has had to solve - see
    # google_routes.py's own OAuth-state docstring for the email/calendar
    # equivalent).
    phone_agent_owner_email: str = os.getenv("PHONE_AGENT_OWNER_EMAIL", "")
    # The public URL Twilio can actually reach this backend at (an ngrok URL
    # for now, a real domain later) - deliberately explicit rather than
    # inferred from the incoming request. A request arriving through a
    # tunnel/proxy often doesn't reliably report its own public-facing URL
    # back to the app (a well-known FastAPI/Starlette gotcha without
    # carefully configured forwarded-header trust), so both the TwiML
    # action URL and the signature-validation URL are built from this
    # setting instead of trusted from the request itself.
    phone_agent_public_base_url: str = os.getenv("PHONE_AGENT_PUBLIC_BASE_URL", "")
    # Store database timestamps in UTC, but present call times in the
    # business's own timezone. Defaults to India for SS Retail Services.
    phone_agent_timezone: str = os.getenv("PHONE_AGENT_TIMEZONE", "Asia/Kolkata")
    phone_agent_validate_signature: bool = os.getenv("PHONE_AGENT_VALIDATE_SIGNATURE", "true").lower() == "true"
    phone_agent_business_name: str = os.getenv("PHONE_AGENT_BUSINESS_NAME", "SS Retail Services")
    phone_agent_greeting: str = os.getenv("PHONE_AGENT_GREETING", "")
    phone_agent_persona: str = os.getenv("PHONE_AGENT_PERSONA", "")
    phone_agent_language: str = os.getenv("PHONE_AGENT_LANGUAGE", "en-IN")
    phone_agent_voice: str = os.getenv("PHONE_AGENT_VOICE", "Polly.Aditi")
    # Off by default: an anonymous caller with no identity check is a real
    # abuse/cost surface (every web search is a billed API call) - Sudeep
    # can turn it on once he's comfortable with how calls are behaving.
    phone_agent_allow_web_search: bool = os.getenv("PHONE_AGENT_ALLOW_WEB_SEARCH", "false").lower() == "true"
    phone_agent_max_turns: int = int(os.getenv("PHONE_AGENT_MAX_TURNS", "15"))
    # Twilio cannot wait indefinitely for a webhook response. Bound each
    # phone-specific AI turn so a slow provider becomes a spoken fallback
    # instead of Twilio's generic "application error" message.
    phone_agent_reply_timeout_seconds: float = float(
        os.getenv("PHONE_AGENT_REPLY_TIMEOUT_SECONDS", "10")
    )

    class Config:
        env_file = ".env"
        # Fixed 2026-09-21: pydantic-settings' default for BaseSettings is
        # extra="forbid" - meaning ANY line in .env that doesn't exactly
        # match a field defined above crashes the whole backend at startup
        # with "Extra inputs are not permitted", before a single request can
        # be served. This bit Sudeep for real: he added `CLAUDE_MODEL=...`
        # to .env (the actual field is `ANTHROPIC_MODEL` - see anthropic_model
        # above), and that one stray line took the entire app down. A typo'd
        # or leftover env var should never be able to do that - same
        # "one bad input can't crash the whole app" pattern as every other
        # defensive fix in this project (see health_check.py, ai_provider.py
        # error handling, etc.). extra="ignore" makes unrecognized .env keys
        # exactly that - ignored - instead of fatal.
        extra = "ignore"

settings = Settings()
