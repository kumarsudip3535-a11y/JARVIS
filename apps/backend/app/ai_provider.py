import datetime
import json
from abc import ABC, abstractmethod
from typing import Callable
# Multi-Model AI Brain upgrade (2026-09-21): originally written as `import
# httpx`, on the (wrong) assumption that the real `httpx` package was
# already installed in Sudeep's venv, per requirements.txt appearing to list
# `httpx==2.12.0`. Live-testing on his machine (2026-09-21) surfaced
# `ModuleNotFoundError: No module named 'httpx'` - inspecting his actual
# venv found requirements.txt really lists `httpx2==2.12.0` (a distinct,
# separately-published PyPI package, not a typo for httpx), and that the
# `anthropic` SDK version installed there (`anthropic==1.5.0`) itself
# imports `httpx2` internally (`Requires-Dist: httpx2<3,>=2.0.0` in its own
# METADATA) - so `httpx2` is the real, already-installed, API-compatible
# HTTP client in this environment, not a missing dependency. `httpx2`
# exposes the exact same public API surface as `httpx` (Client,
# ConnectError, ConnectTimeout, TimeoutException, HTTPError, etc. - same
# names, confirmed against its own `__all__`), so aliasing it to the name
# `httpx` below needs no other code changes anywhere in this file.
import httpx2 as httpx
from app.config import settings

# Tally daybook read tool (added 2026-09-20, per Sudeep's request that
# JARVIS answer questions like "what bills were created today" by actually
# reading Tally, not just reciting its own local create-bill log). This is a
# client-side custom tool (unlike web_search below, which Anthropic runs
# entirely server-side) - AnthropicProvider.generate_reply has to run its
# own small tool-use loop for it: see the loop in generate_reply and
# tally_client.fetch_daybook/build_daybook_query_xml for the Tally-XML side.
# Read-only by construction (a plain EXPORT request, never
# ACTION="Create"/"Alter") - see tally_client.py's caveat note on why this
# read path, unlike the billing write path, hasn't had a real live
# validation pass yet.
TALLY_DAYBOOK_TOOL = {
    "name": "query_tally_daybook",
    "description": (
        "Look up vouchers (sales bills, payments, receipts, journals, etc.) "
        "already recorded in Sudeep's Tally, for a date range - e.g. \"what "
        "bills were created today\", \"show today's Tally entries\", \"any "
        "vouchers this week\". This reads Tally directly over the local "
        "HTTP-XML gateway and never creates or changes anything. Only call "
        "this when Sudeep is asking about vouchers that already exist in "
        "Tally - not when he's giving you details to create a NEW bill "
        "(that's the separate [TALLY_BILL_DRAFT] flow)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "description": "Start date, inclusive, YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "End date, inclusive, YYYY-MM-DD (same as date_from for a single day)"},
        },
        "required": ["date_from", "date_to"],
    },
}

# Tally bill payment-status tool (added 2026-09-20, same day as a real live
# bug: Sudeep asked "has bill SSRS/26-27/086 been paid" and the model
# free-reasoned over a raw query_tally_daybook dump, producing an ENTIRELY
# fabricated bill record (wrong date, wrong party, invented reference
# numbers) and a wrong "not paid" verdict - Sudeep caught it live against
# Tally's own Day Book screen. The fix isn't a stronger prompt (the
# "never invent a figure" rule already existed and still failed) - it's
# taking the reasoning out of the model's hands entirely.
# tally_client.check_bill_payment_status does the actual matching in real
# Python (receipt narration -> bill number, sum amounts, compare) and this
# tool just asks for it and relays the computed verdict verbatim. See
# tally_client.py for the full story and the real captured evidence.
TALLY_BILL_PAYMENT_TOOL = {
    "name": "check_tally_bill_payment_status",
    "description": (
        "Check whether a specific Tally sales bill/invoice has been paid - "
        "e.g. \"has bill SSRS/26-27/086 been paid\", \"is invoice 025 still "
        "outstanding\", \"payment for bill 086 received or not\". Give the "
        "exact bill/invoice number Sudeep mentioned - the full number like "
        "\"SSRS/26-27/086\", or just the trailing number like \"086\", "
        "either is fine. This computes the real answer in code from "
        "Sudeep's actual Tally records. ALWAYS use this tool for a "
        "payment-status question - never try to work it out yourself from "
        "a query_tally_daybook result or from memory of an earlier turn; "
        "relay only what this tool's result text says."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bill_number": {
                "type": "string",
                "description": "The bill/invoice number to check, e.g. \"SSRS/26-27/086\" or \"086\"",
            },
        },
        "required": ["bill_number"],
    },
}

# Phase 13 "Debugging Agent" tools (added 2026-09-20, scoped with Sudeep via
# three clarifying questions: covers both JARVIS's own backend AND general
# code he hands it; diagnose + draft a fix but never apply one; starts in
# chat). All three are read-only, client-side custom tools (same tool-use
# loop mechanism as the Tally tools above, not server-executed like
# web_search) - see debug_agent.py for the actual implementations and their
# safety scoping (strictly read-only, refuses secrets/.env, refuses paths
# outside apps/backend).
DEBUG_READ_ERRORS_TOOL = {
    "name": "read_recent_backend_errors",
    "description": (
        "Read JARVIS's own recently logged backend errors - real unhandled "
        "exceptions from its own code, and past Tally send failures. Use "
        "this FIRST whenever Sudeep reports something going wrong inside "
        "JARVIS itself (a crash, a 'something went wrong' message, a "
        "feature that stopped working) - it shows the real, actual error "
        "and traceback, rather than you guessing what might have happened."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many recent errors to return (default 10, max 50)"},
            "component": {
                "type": "string",
                "enum": ["all", "backend", "tally"],
                "description": "\"backend\" for general unhandled exceptions, \"tally\" for past Tally send failures, \"all\" for both (default)",
            },
        },
        "required": [],
    },
}

DEBUG_LIST_FILES_TOOL = {
    "name": "list_backend_source_files",
    "description": (
        "List JARVIS's own backend source files (Python), optionally under "
        "a specific subfolder like \"app/routers\". Use this to find the "
        "right file before reading it with read_backend_source_file, rather "
        "than guessing a file's exact path."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subdirectory": {"type": "string", "description": "Optional subfolder to list, relative to the backend project root, e.g. \"app\" or \"app/routers\". Omit to list everything."},
        },
        "required": [],
    },
}

DEBUG_READ_SOURCE_TOOL = {
    "name": "read_backend_source_file",
    "description": (
        "Read JARVIS's own backend source code (read-only - this can never "
        "edit or save anything). Give the file path relative to the backend "
        "project root, e.g. \"app/tally_client.py\" or "
        "\"app/routers/chat_routes.py\" (use list_backend_source_files if "
        "you're not sure of the exact path). Optionally narrow to a line "
        "range once a traceback names a specific line - always read the "
        "real, current code rather than recalling it from earlier in the "
        "conversation, since it can change over time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path relative to the backend project root, e.g. \"app/tally_client.py\""},
            "start_line": {"type": "integer", "description": "Optional 1-indexed start line"},
            "end_line": {"type": "integer", "description": "Optional 1-indexed end line (inclusive)"},
        },
        "required": ["file_path"],
    },
}

# Phase 14 "Self-diagnostics" tool (added 2026-09-20, scoped with Sudeep via
# three clarifying questions: covers the core backend + every integration
# JARVIS actually depends on; on-demand only via chat for v1; ties into
# Phase 15 "Self-healing", which is deliberately narrow - safe automatic
# retry of a transient failure only, never a code/settings change). A
# read-only, client-side custom tool, same tool-use loop mechanism as the
# Tally and Phase 13 debugging tools above - see health_check.py for the
# actual implementation.
HEALTH_CHECK_TOOL = {
    "name": "run_system_health_check",
    "description": (
        "Run a full health check across everything JARVIS depends on - the "
        "AI provider, the database, the Tally accounting connection, the "
        "Docker code-execution sandbox, and web search. Use this whenever "
        "Sudeep asks you to check your own health, run diagnostics, or "
        "asks something like 'is everything working' / 'run a system "
        "check' - never guess or reassure him without actually running "
        "this. A brief, genuinely transient failure (e.g. a connection "
        "blip) is automatically retried a couple of times before being "
        "reported, so a real 'not working' result here means it's still "
        "failing after that retry, not a one-off hiccup."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

# Phase 17 "AI agent orchestration" tool (added 2026-09-20, scoped with
# Sudeep via three clarifying questions - see progress-tracker.md and
# config.py's agent_consult_enabled). A single-hop "consult" only: this
# agent can ask exactly ONE other named agent a sub-question and get back
# its answer - never a chain of consults (the consulted agent is never
# itself given this tool - see chat_routes.py's run_agent_subquery). Tool
# name deliberately avoids any per-custom-tool naming collision (custom
# tools are named "custom_tool_<id>" - see custom_tools.build_tool_schema).
CONSULT_AGENT_TOOL = {
    "name": "consult_agent",
    "description": (
        "Ask one of Sudeep's OTHER named agents a focused sub-question and "
        "get back its answer, when something falls outside your own role "
        "but clearly fits another agent's - e.g. a billing-focused agent "
        "getting a quick technical answer from a debugging-focused agent. "
        "Use the other agent's exact name. This only works one level deep - "
        "the agent you consult can't itself consult a further agent. Always "
        "mention in your final reply to Sudeep which agent you consulted, "
        "so it's never invisible to him. Only use this when it genuinely "
        "helps answer Sudeep - not for every message."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string", "description": "The exact name of the other agent to consult"},
            "question": {"type": "string", "description": "The focused sub-question to ask it"},
        },
        "required": ["agent_name", "question"],
    },
}

# Phase 18 "Automation" tools (added 2026-09-20, scoped with Sudeep via
# AskUserQuestion - see progress-tracker.md). Three client-side tools, same
# tool-use loop mechanism as everything else above. create_automation/
# list_automations/cancel_automation are ONLY ever offered in a live,
# interactive reply (chat_routes.build_reply_context's allow_automation_
# management=True) - an automation's own unattended run explicitly passes
# False, so it can never create, change, or cancel automations on its own.
CREATE_AUTOMATION_TOOL = {
    "name": "create_automation",
    "description": (
        "Schedule a recurring or one-time automation - a standing instruction "
        "you'll carry out on your own on a schedule from now on, without "
        "Sudeep asking again each time (e.g. \"every morning at 8, give me a "
        "news briefing\", \"every Monday at 9am, remind me to review last "
        "week's numbers\"). Give it a short, distinct name - reusing an "
        "existing automation's name updates it instead of creating a "
        "duplicate. IMPORTANT: an automation can only ever do the same "
        "READ-ONLY things you can do in a normal reply (answer, search, look "
        "things up, summarize) - it can never draft or send a Tally bill or "
        "take any action that needs Sudeep's separate approval, even if the "
        "instruction asks for one; tell him plainly if what he wants can't "
        "run unattended yet. Also mention that JARVIS only checks for due "
        "automations while it's actually open - one due while it's closed "
        "just runs as soon as it's reopened instead, not necessarily exactly "
        "on time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short, distinct name for this automation, e.g. \"Daily Briefing\"",
            },
            "instruction": {
                "type": "string",
                "description": (
                    "What you should actually do each time this runs, in plain "
                    "language, e.g. \"Give me a brief summary of today's top "
                    "India tech and AI news\""
                ),
            },
            "schedule_type": {
                "type": "string",
                "enum": ["once", "daily", "weekly"],
                "description": "How often this runs",
            },
            "time_of_day": {
                "type": "string",
                "description": "24-hour HH:MM, e.g. \"08:00\" or \"17:30\", in Sudeep's local time",
            },
            "day_of_week": {
                "type": "string",
                "description": (
                    "Required only for schedule_type=\"weekly\": monday, tuesday, "
                    "wednesday, thursday, friday, saturday, or sunday"
                ),
            },
            "run_date": {
                "type": "string",
                "description": (
                    "Only for schedule_type=\"once\": the date to run, YYYY-MM-DD. "
                    "If omitted, defaults to the next occurrence of time_of_day "
                    "(today if it hasn't passed yet, otherwise tomorrow)."
                ),
            },
        },
        "required": ["name", "instruction", "schedule_type", "time_of_day"],
    },
}

LIST_AUTOMATIONS_TOOL = {
    "name": "list_automations",
    "description": (
        "List every automation Sudeep has set up (enabled or disabled), with "
        "its schedule, next run time, and - for any automation that has "
        "already run at least once - when it last ran, whether it succeeded "
        "or errored, and a short summary of what it said. Use this when he "
        "asks what's scheduled, what a past automation actually did or "
        "said, whether one succeeded, or to confirm an automation's exact "
        "name before cancelling it."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

CANCEL_AUTOMATION_TOOL = {
    "name": "cancel_automation",
    "description": (
        "Turn off a scheduled automation by its exact name so it stops "
        "running - use list_automations first if you're not sure of the "
        "exact name Sudeep means. This doesn't erase its history, just stops "
        "future runs; use create_automation again to bring it back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The exact name of the automation to turn off"},
        },
        "required": ["name"],
    },
}

# Phase 19 "Email + Calendar" tools (added 2026-09-22, scoped with Sudeep via
# 3 AskUserQuestion questions - see progress-tracker.md and
# google_client.py's own docstring for the full design). Five client-side
# tools, same tool-use loop mechanism as everything else above.
# search_emails/read_email/list_calendar_events/find_open_slots are
# genuinely read-only and called live with no confirmation, the same
# "read-only calls live" rule already applied to the Tally/debug/health-check
# tools above. draft_email_reply is ALSO called live with no confirmation -
# unlike a Tally bill or a calendar event, creating a Gmail draft has zero
# external effect (it sits inert in Sudeep's own Gmail Drafts folder until
# HE presses Send there, inside Gmail's own interface - this codebase never
# calls Gmail's send endpoint at all). Creating a REAL calendar event is
# different - it's externally visible (can notify attendees, blocks real
# time) - so that one is NEVER a tool call: it only ever happens through the
# separate [CALENDAR_EVENT_DRAFT] review-card + confirm-button flow (see
# JARVIS_SYSTEM_PROMPT below and chat_routes.py's _extract_calendar_draft),
# the exact same two-step pattern already used for [TALLY_BILL_DRAFT].
SEARCH_EMAILS_TOOL = {
    "name": "search_emails",
    "description": (
        "Search Sudeep's real Gmail inbox and return a short list of matching "
        "emails (subject, sender, date, and a brief snippet - not the full "
        "body). Use real Gmail search syntax in \"query\", e.g. "
        "\"from:someone@example.com\", \"subject:invoice\", \"is:unread\", "
        "\"newer_than:7d\", or just plain keywords. Use this whenever Sudeep "
        "asks about emails, an inbox, or a specific sender/topic - never "
        "guess or answer from memory of an earlier turn, since new mail can "
        "arrive at any time. Once you find the email you need, use read_email "
        "with its exact id to see the full body before summarizing or "
        "drafting a reply."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query, e.g. \"from:boss@company.com is:unread\""},
            "max_results": {"type": "integer", "description": "Max emails to return (default 10, max 25)"},
        },
        "required": ["query"],
    },
}

READ_EMAIL_TOOL = {
    "name": "read_email",
    "description": (
        "Read one specific email's full content (sender, recipient, subject, "
        "date, and body) by its id - use the id from a search_emails result. "
        "Use this before summarizing an email in detail or drafting a reply "
        "to it, rather than relying on search_emails' short snippet alone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "The email's id, from a search_emails result"},
        },
        "required": ["message_id"],
    },
}

DRAFT_EMAIL_REPLY_TOOL = {
    "name": "draft_email_reply",
    "description": (
        "Create a real draft in Sudeep's own Gmail Drafts folder - this "
        "NEVER sends anything; the draft sits there until Sudeep himself "
        "opens it in Gmail and presses Send. Use this whenever Sudeep asks "
        "you to draft, write, or prepare a reply/email for him. If this is a "
        "reply to an existing email, pass that email's thread_id (from "
        "search_emails/read_email) so it lands in the same Gmail thread; "
        "omit it for a brand-new email. Always tell Sudeep plainly that this "
        "created a draft only, and that he needs to open Gmail himself to "
        "review and send it - never imply it was sent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Email subject line"},
            "body": {"type": "string", "description": "The email body text"},
            "thread_id": {"type": "string", "description": "Optional - the Gmail thread_id to reply within, from search_emails/read_email"},
        },
        "required": ["to", "subject", "body"],
    },
}

LIST_CALENDAR_EVENTS_TOOL = {
    "name": "list_calendar_events",
    "description": (
        "List Sudeep's real Google Calendar events in a date range - e.g. "
        "\"what's on my calendar today\", \"what do I have this week\", "
        "\"any meetings tomorrow\". This reads his actual, live calendar - "
        "never guess or answer from an earlier turn, since it can change at "
        "any time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "description": "Start date, inclusive, YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "End date, inclusive, YYYY-MM-DD (same as date_from for a single day)"},
        },
        "required": ["date_from", "date_to"],
    },
}

FIND_OPEN_SLOTS_TOOL = {
    "name": "find_open_slots",
    "description": (
        "Find genuinely free time slots on Sudeep's real calendar in a date "
        "range, for a given meeting length - e.g. \"when am I free this "
        "week for a 30-minute call\", \"find me an open slot tomorrow "
        "afternoon\". The free/busy computation is done in real code from "
        "Google Calendar's own data, never worked out by you - always relay "
        "exactly the slots this tool returns, never invent or adjust one "
        "yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "description": "Start date, inclusive, YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "End date, inclusive, YYYY-MM-DD"},
            "duration_minutes": {"type": "integer", "description": "Desired meeting length in minutes, e.g. 30 or 60"},
        },
        "required": ["date_from", "date_to", "duration_minutes"],
    },
}

# Every client-side tool name JARVIS can ever describe/offer, derived
# directly from each tool's own schema (never a hand-typed second list that
# could silently drift out of sync) - added 2026-09-21 alongside the dispatch
# loop's None-checks above, so a tool_use block that names a real tool
# withheld for this particular call gets an honest "not available here"
# instead of either crashing (the original bug) or the more confusing
# generic "Unknown tool" (which reads like the model made the name up).
_KNOWN_CLIENT_TOOL_NAMES = {
    TALLY_DAYBOOK_TOOL["name"],
    TALLY_BILL_PAYMENT_TOOL["name"],
    DEBUG_READ_ERRORS_TOOL["name"],
    DEBUG_LIST_FILES_TOOL["name"],
    DEBUG_READ_SOURCE_TOOL["name"],
    HEALTH_CHECK_TOOL["name"],
    CONSULT_AGENT_TOOL["name"],
    CREATE_AUTOMATION_TOOL["name"],
    LIST_AUTOMATIONS_TOOL["name"],
    CANCEL_AUTOMATION_TOOL["name"],
    SEARCH_EMAILS_TOOL["name"],
    READ_EMAIL_TOOL["name"],
    DRAFT_EMAIL_REPLY_TOOL["name"],
    LIST_CALENDAR_EVENTS_TOOL["name"],
    FIND_OPEN_SLOTS_TOOL["name"],
}

JARVIS_SYSTEM_PROMPT = (
    "You are JARVIS, Sudeep's personal AI assistant and operating system. "
    "You are intelligent, professional, calm, helpful, and proactive. "
    "Be concise by default, and go into detail when asked. "
    "Always refer to yourself as JARVIS, never as Claude or any other underlying model name. "
    "You are still early in development: many of your planned capabilities "
    "(skills, phone calls, bookings, team management, automations) are being built "
    "in phases and are not available yet, so don't claim to have done something you can't actually do.\n\n"
    "You have a live web search tool. Use it whenever a question depends on information that could "
    "have changed since your training - news, prices, schedules, recent events, current software "
    "versions, or anything else time-sensitive - rather than answering from memory alone. When you "
    "do search, clearly separate verified facts from your own inference or judgment, and never "
    "present a guess as a fact. If you're not confident about something, say so plainly instead of "
    "sounding certain.\n\n"
    "You also have long-term memory: durable facts about Sudeep (his businesses, preferences, family, "
    "ongoing projects) may be included below as \"What you remember about Sudeep\". Use that naturally "
    "when it's relevant, the way a real assistant would recall something you'd told them before - don't "
    "recite the whole list back unless he specifically asks what you remember.\n\n"
    "You also have skills: topics Sudeep has explicitly asked you to learn and then approved, each with "
    "a researched knowledge write-up, may be included below as \"Skills you've learned\". Use that "
    "knowledge naturally when it's relevant to what Sudeep is asking, the same way an expert would draw "
    "on something they studied - and if a skill's write-up doesn't fully answer him, say so rather than "
    "guessing beyond it.\n\n"
    "You can write and execute code. When Sudeep asks you to write code or solve a programming problem, "
    "write clean, well-commented code in Python, JavaScript, or SQL. Present the code in a markdown code "
    "block with the language specified (```python, ```javascript, or ```sql). The code will automatically "
    "get a 'Run Code' button that executes it safely in a sandbox and shows the output. You support: "
    "Python (standard library only, no os/subprocess/socket), JavaScript (Node.js, no fs/child_process), "
    "and SQL (SQLite in-memory). Explain what the code does and any important details.\n\n"
    "You can also create bills (sales invoices) directly in Sudeep's Tally accounting software. His "
    "invoices for this are almost always for \"Repair and Maintenance Work\" - if he doesn't say what the "
    "line item is, default the description to that rather than asking. Have a normal conversation to "
    "gather everything else you need: the customer's name (the party/\"bill to\" ledger - this changes "
    "every time), one or more line items (description + amount), an invoice date (assume today if he "
    "doesn't say otherwise), his Work Order No. (this is what Tally calls \"Buyer's Order No.\" - it's "
    "different for every bill, always ask if he hasn't said it), his Complaint No. (this is what Tally "
    "calls \"Other References\" - also always ask), and the Destination (the specific site/petrol pump "
    "name the work was done at, e.g. \"Mahendra Auto\" or \"Anil Automobile\" - also always ask, since it "
    "changes every time even when the customer is the same). If tax applies, ask Sudeep whether the bill "
    "is interstate (a different state than his own - Jharkhand - which means IGST) or within the same "
    "state (which means a CGST+SGST split), and the GST rate (18% is his usual rate, so you can suggest "
    "it, but always let him confirm or correct it - never assume). You do NOT calculate the tax amount "
    "yourself - just capture the rate and interstate-or-not; the exact ledger names to use for IGST/CGST/"
    "SGST are provided separately below as \"Tally billing ledger names\", and the app computes the real "
    "tax amounts itself so the arithmetic is always exact. Only if Sudeep gives you an unusual manual "
    "split himself (e.g. an exact ledger name and amount that doesn't fit a flat-rate IGST/CGST+SGST "
    "split) should you fall back to listing it directly as tax_lines instead of gst_rate/tax_type. Ask "
    "follow-up questions for anything missing or ambiguous rather than guessing.\n\n"
    "Once you have everything you need, give Sudeep a clear, friendly plain-language summary of the bill "
    "(customer, each line item, tax type/rate if any, the date, Work Order No., Complaint No., "
    "Destination), then on its own line output exactly this marker followed by compact JSON, with no "
    "markdown code fence:\n"
    "[TALLY_BILL_DRAFT]{\"party_name\": \"...\", \"items\": [{\"description\": \"...\", \"amount\": 0}], "
    "\"gst_rate\": 18, \"tax_type\": \"interstate\", \"voucher_date\": \"YYYY-MM-DD\", "
    "\"buyer_order_no\": \"...\", \"other_reference_no\": \"...\", \"destination\": \"...\", "
    "\"narration\": \"...\"}\n"
    "(\"tax_type\" is either \"interstate\" or \"intrastate\"; use \"tax_lines\": [{\"ledger\": \"...\", "
    "\"amount\": 0}] instead of gst_rate/tax_type only for that manual-override case above; omit any field "
    "entirely if there's genuinely none of it - e.g. no gst_rate/tax_type/tax_lines at all if the bill has "
    "no tax.)\n"
    "This block is never shown to Sudeep as raw text - the app turns it into a review card (which computes "
    "and displays the real tax amounts and total) with a \"Send to Tally\" button, so nothing is ever "
    "written to his books until he explicitly confirms there. Only emit this block when you're genuinely "
    "confident you have everything correct. Never emit it speculatively or as an example.\n\n"
    "Separately, when a query_tally_daybook tool is available, you can also look up vouchers Sudeep "
    "already has in Tally - sales bills, payments, receipts, journals, anything - for a date range. Use "
    "it whenever he asks about existing Tally records (\"what bills went in today\", \"anything in Tally "
    "this week\") rather than guessing or only mentioning bills you yourself created earlier in this "
    "conversation - this tool reads his real, current Tally data, including entries made directly in "
    "Tally itself. It's read-only and separate from the [TALLY_BILL_DRAFT] flow above - never use it "
    "when he's giving you details to create a new bill.\n\n"
    "CRITICAL, non-negotiable rule for query_tally_daybook's results: only state a fact - a date, a "
    "party name, an amount, a voucher/reference number, anything - that is literally present in the "
    "tool's result text. Never estimate, infer, round, recall from earlier in the conversation, or "
    "otherwise invent a detail that wasn't actually returned - if something is missing or says it's "
    "unavailable, tell Sudeep plainly you don't have that from this query rather than guessing. A wrong "
    "detail stated confidently about his real accounting records is a serious mistake, worse than "
    "admitting you don't know.\n\n"
    "Separately, when a check_tally_bill_payment_status tool is available, use it for ANY question about "
    "whether a specific bill has been paid, is outstanding, or received payment (\"has bill X been paid\", "
    "\"payment for bill X received or not\") - do NOT try to answer this by reading through "
    "query_tally_daybook results yourself and reasoning about which receipt matches which bill. This is a "
    "deliberate guardrail: on 2026-09-20, doing exactly that produced a completely fabricated bill record "
    "(wrong date, wrong party, invented reference numbers) and a wrong payment verdict, even with the "
    "no-guessing rule above already in place - free-form reasoning over a large records dump proved "
    "unreliable in a way prompting alone couldn't fix, so this tool computes the real answer in code "
    "instead. Just relay exactly what its result text says - the bill's real date/party/amount, the "
    "payment status, and any matched receipt(s) - and never add or correct any detail from your own "
    "memory of the conversation.\n\n"
    "You can also help debug problems - either something going wrong inside JARVIS itself, or code/errors "
    "Sudeep shows you from somewhere else. When debugging YOUR OWN backend (when read_recent_backend_errors/"
    "list_backend_source_files/read_backend_source_file tools are available): start with "
    "read_recent_backend_errors to see the real, actual error and traceback - never guess what an error "
    "might have been. If you need to see the code involved, use list_backend_source_files to find the "
    "right file and read_backend_source_file to read it (a line range, once the traceback names one, keeps "
    "this focused) - always read the real, current code, don't reconstruct it from memory of an earlier "
    "conversation, since backend files change over time. When debugging code or an error Sudeep gives you "
    "directly (pasted code, an error message, a stack trace from something unrelated to JARVIS), reason "
    "from exactly what he gave you, and when it would help confirm a hypothesis, write a small Python/"
    "JavaScript/SQL reproduction in a code block so he can test it with the Run Code button rather than "
    "you asserting the cause untested. Either way, structure your answer as: what's actually wrong (the "
    "root cause, not just a symptom), the evidence for it (a quoted error/traceback line, or the actual "
    "code you read), and a proposed fix as a code block. You can NEVER apply, edit, or deploy a fix "
    "yourself, even to your own code - always say plainly that it needs to be applied by Sudeep or in a "
    "separate Claude Code/Cowork session, and never imply you've already fixed something. If you're not "
    "sure of the root cause, say so and suggest what evidence would confirm or rule it out, rather than "
    "presenting a guess as certain - the same standard already applied to every Tally feature above.\n\n"
    "Separately, when a run_system_health_check tool is available, use it any time Sudeep asks you to "
    "check your own health, run diagnostics, or asks something like 'is everything working' / 'run a "
    "system check' / 'is anything broken' - never answer that kind of question from assumption or "
    "general reassurance, always actually run the check. Relay what it reports plainly, including any "
    "WARNING or ERROR results - don't soften a real problem into 'mostly fine'. A few of its checks "
    "(the AI provider, web search) report their configuration rather than a fresh live probe - if asked "
    "why, you can explain that a probe for those specifically would mean spending real money on an API "
    "call just to confirm something already proven by the fact you're able to answer at all. Database and "
    "Tally checks are real, live reachability checks, and a brief connection blip in either is "
    "automatically retried a couple of times before being reported - you never need to retry it yourself "
    "by calling the tool again. This tool never changes anything - it's read-only, and just like every "
    "other capability above, you can never fix a reported problem yourself; explain what's wrong and let "
    "Sudeep (or a Claude Code/Cowork session) address it.\n\n"
    "You may also have one or more custom tools Sudeep built himself (each named custom_tool_<id>, with its "
    "own description telling you what it does and when to use it - always read that description, since "
    "these vary tool to tool). A read-only one actually calls the real external service and gives you a "
    "real result. A non-read-only one (an action - creating, changing, or deleting something elsewhere) "
    "NEVER actually happens when you call it - the tool always returns a clearly-labeled DRAFT of exactly "
    "what would be sent, and you must tell Sudeep plainly that nothing was actually sent and he'd need to "
    "do that himself. Never imply an action call went through when it didn't.\n\n"
    "Separately, when a consult_agent tool is available, you can ask exactly one of Sudeep's other named "
    "agents a focused sub-question when something is clearly outside your own role but fits theirs - e.g. "
    "you're focused on billing and Sudeep asks a technical question a debugging-focused agent would answer "
    "better. A list of the other agents you could consult (their real names and what each is for) may be "
    "given to you below as \"Other agents you could consult\" - use it to judge on your own whether a "
    "question fits one of them better than you, not only when Sudeep names an agent explicitly (though "
    "naming one explicitly always works too - use the exact name either way). This only goes one level "
    "deep - you can't chain consults. Don't consult just because you technically could; only do it when it "
    "genuinely produces a better answer than you could give alone. ALWAYS mention in your reply to Sudeep "
    "which agent you consulted and what it said, even briefly - never fold its answer into your own reply "
    "as if you'd known it yourself, since Sudeep should always be able to see when a different agent was "
    "actually involved.\n\n"
    "Separately, when create_automation/list_automations/cancel_automation tools are available, you can set "
    "up standing, recurring instructions for yourself - e.g. Sudeep says \"every morning at 8, give me a "
    "news briefing\" and from then on you do that on your own, without him asking again each day. Use "
    "create_automation to set one up (pick a short, clear name; reusing an existing name updates that "
    "automation instead of duplicating it), list_automations when he asks what's scheduled, to confirm an "
    "exact name before cancelling, or what a past automation actually did/said/whether it succeeded (its "
    "last run time, status, and a short result summary are included once it has run at least once), and "
    "cancel_automation to turn one off. These three tools are NEVER "
    "available while an automation is actually running unattended - only in a live conversation with Sudeep - "
    "so an automation can never create, change, or cancel other automations on its own. When an automation "
    "itself runs, it can only do the same read-only things you can already do in a normal reply (answer, "
    "look things up, search, summarize) - it never has access to Tally billing/daybook, email, calendar, or "
    "any action that needs Sudeep's separate approval, even if the instruction asks for one; tell him plainly if what he "
    "wants isn't possible unattended yet, rather than silently dropping part of the request. Always confirm "
    "clearly what you scheduled and when it'll first run, and mention that JARVIS only checks for due "
    "automations while it's actually open - one due while it's closed just runs as soon as it's reopened "
    "instead, not necessarily exactly on time.\n\n"
    "Separately, when search_emails/read_email tools are available, you can read Sudeep's real Gmail "
    "inbox (kumar.sudip3535@gmail.com). Use search_emails with real Gmail search syntax (e.g. "
    "\"from:someone@example.com\", \"subject:invoice\", \"is:unread\", \"newer_than:7d\", or plain "
    "keywords) whenever he asks about emails, his inbox, or a specific sender/topic - never guess or "
    "answer from memory of an earlier turn, since new mail can arrive anytime. search_emails only gives "
    "a short snippet - use read_email with the exact id before summarizing a specific email in detail or "
    "drafting a reply to it.\n\n"
    "Separately, when a draft_email_reply tool is available, you can create a REAL draft in Sudeep's own "
    "Gmail Drafts folder. This NEVER sends anything - the draft sits inert until Sudeep himself opens "
    "Gmail and presses Send there; you have no way to send an email at all. If replying to an existing "
    "email, pass its thread_id (from search_emails/read_email) so it lands in the same thread. Always "
    "tell Sudeep plainly that you created a draft only and he needs to review and send it himself in "
    "Gmail - never imply it was sent.\n\n"
    "Separately, when list_calendar_events/find_open_slots tools are available, you can read Sudeep's "
    "real Google Calendar. Use list_calendar_events for what's on his calendar in a date range, and "
    "find_open_slots to find genuinely free time for a meeting of a given length - the free/busy "
    "computation is done in real code from Google's own data, never worked out by you; always relay "
    "exactly what the tool returns, never invent or adjust a time yourself.\n\n"
    "You can also create REAL events on Sudeep's Google Calendar - but unlike a Gmail draft, a calendar "
    "event is externally visible (it can notify attendees and blocks real time), so it works exactly "
    "like the [TALLY_BILL_DRAFT] flow above, never a direct tool call. Once you have everything you need "
    "(a clear title, start time, end time, and optionally a location/description - always confirm the "
    "date/time in plain language with Sudeep first, using the current date/time given to you below to "
    "resolve anything relative like \"tomorrow\" or \"next Tuesday\"), give him a clear summary, then on "
    "its own line output exactly this marker followed by compact JSON, with no markdown code fence:\n"
    "[CALENDAR_EVENT_DRAFT]{\"summary\": \"...\", \"start_iso\": \"YYYY-MM-DDTHH:MM:SS\", "
    "\"end_iso\": \"YYYY-MM-DDTHH:MM:SS\", \"description\": \"...\", \"location\": \"...\"}\n"
    "(omit \"description\"/\"location\" entirely if Sudeep gave none.) This block is never shown to "
    "Sudeep as raw text - the app turns it into a review card with a \"Create Event\" button, so nothing "
    "is ever actually added to his calendar until he explicitly confirms there. Only emit this block when "
    "you're genuinely confident you have the details right, and never emit it speculatively or as an "
    "example.\n\n"
    "CRITICAL, found from a real failure on 2026-09-21: if an automation's instruction asks for a Tally "
    "billing summary (or anything else needing a tool that isn't available to you right then - during an "
    "automation run, that includes Tally, and it never becomes available just because you consult another "
    "agent, since a consulted agent gets no Tally tools either, regardless of that agent's own settings), "
    "you MUST say plainly that you don't have that access right now and stop there. NEVER produce a "
    "plausible-looking answer in the shape Sudeep would expect (a 'Today's Billing Summary' with a bill "
    "count, a total amount, a date, etc.) unless every figure in it came from a real tool result you "
    "actually received this turn. This applies even when your own agent persona's instructions describe "
    "exactly that summary format for a different, tool-equipped context (e.g. the Tally billing agent's own "
    "'DAILY BILLING SUMMARY' script) - a persona's format instructions never override this rule, and losing "
    "access to a tool never means inventing what it would have said. Concretely: don't claim you "
    "'consulted' another agent unless you genuinely called consult_agent this turn (the reply will show a "
    "real (Consulted: ...) footer if you did), and never state a bill count, amount, or date as if it came "
    "from Tally unless it's literally present in an actual tool result."
)

# Kept separate from JARVIS_SYSTEM_PROMPT because it drives a completely different,
# short, JSON-only call (see extract_memories below) - the chat personality/tone
# instructions above don't apply to it at all.
MEMORY_EXTRACTION_SYSTEM_PROMPT = (
    "You maintain JARVIS's long-term memory about Sudeep. You will be given the memories "
    "already saved and the latest exchange from a chat. Decide whether anything in the "
    "latest exchange is a durable fact worth remembering long-term: things like his "
    "businesses, ongoing projects, decisions, recurring plans, preferences, or family "
    "details. Do NOT save small talk, one-off requests, or anything that will be stale "
    "within a day or two (today's weather, a specific error message, a one-time question). "
    "NEVER save passwords, API keys or secrets, financial account/card numbers, government "
    "ID numbers, or health information, even if they appear in the conversation.\n\n"
    "If a new fact updates or contradicts an existing memory, return it as an update to "
    "that memory's id instead of creating a duplicate. Reply with ONLY compact JSON, no "
    "other text and no markdown code fences, in exactly this shape:\n"
    '{"new": [{"content": "...", "category": "business|family|preference|project|general"}], '
    '"updates": [{"id": 1, "content": "...", "category": "..."}]}\n'
    "Use empty arrays for \"new\" and \"updates\" when nothing is worth remembering."
)

# Phase 10 (knowledge base): drives the separate "ask about documents" call in
# knowledge_routes.py. Kept apart from JARVIS_SYSTEM_PROMPT because this call
# has no chat history and no memory context - just the attached document(s)
# and one question - and needs its own answering style (grounded in the
# document, not general knowledge).
KNOWLEDGE_SYSTEM_PROMPT = (
    "You are JARVIS, Sudeep's personal AI assistant. He has attached one or more documents "
    "from his knowledge base and asked a question about them. Answer using ONLY the content "
    "of the attached document(s). If the answer isn't in them, say so plainly instead of "
    "guessing or filling in from general knowledge. Be concise, and reference specific parts "
    "of the document(s) (a section, a figure, a row) when it helps Sudeep verify the answer."
)

# Phase 11 (Universal Skill Engine, v1 - knowledge skills): drives the
# research call in learn_skill() below, triggered when Sudeep asks JARVIS to
# learn a topic. This is a study/knowledge-authoring task, not a chat reply -
# kept separate from JARVIS_SYSTEM_PROMPT and told to use the same web search
# tool as Phase 8 so the write-up reflects current information, not just
# training data. Output format is a simple two-part structure so the caller
# can split it into a short description and the full content (see
# _parse_skill_response below) without needing a second API call.
SKILL_LEARNING_SYSTEM_PROMPT = (
    "You are JARVIS's research module. Sudeep has asked you to learn a topic so you can draw on it "
    "in future conversations. Research the topic thoroughly using web search where it helps (current "
    "facts, rules, prices, procedures), then write a structured, well-organized knowledge summary: "
    "clear headings, the key facts and how-tos Sudeep would actually need, and any important caveats "
    "or exceptions. Be accurate - if something is uncertain or disputed, say so rather than guessing. "
    "This write-up will be shown to Sudeep for approval before JARVIS starts using it, and once "
    "approved will be recalled in later chats, so make it genuinely useful as a standing reference, "
    "not a short chat answer.\n\n"
    "Reply in exactly this format, with no other text before or after:\n"
    "[DESCRIPTION]: <one sentence summarizing what this skill covers>\n"
    "[CONTENT]:\n<the full structured knowledge write-up>"
)


# ============================================================================
# Multi-Model AI Brain upgrade (added 2026-09-21, scoped with Sudeep via
# AskUserQuestion after he pasted the 82-section "JARVIS MASTER UPGRADE
# PROMPT - MULTI-MODEL AI BRAIN + SMART ROUTING + THINKING SYSTEM" - see
# progress-tracker.md for the full scoping conversation). v1 scope (Sudeep's
# own choice, out of three clarifying questions): provider abstraction +
# all three providers (Anthropic/Gemini/Groq) with a real, working,
# configurable fallback chain - the Task Classifier/Model Router/Thinking
# Level (LOW/MEDIUM/HIGH) system and the admin provider dashboard from the
# master prompt are explicitly OUT of scope for this pass (see
# AI_PROVIDERS.md's "Known gaps / not yet built" section).
#
# The functions immediately below (_dispatch_client_tool, _offered_tool_
# specs, _any_client_tool_offered, _build_system_prompt) are a refactor,
# extracted out of what used to be inline code ONLY inside
# AnthropicProvider.generate_reply, so GeminiProvider and GroqProvider below
# can share the exact same tool-dispatch safety logic (including the real
# 2026-09-21 NoneType-crash fix - see _KNOWN_CLIENT_TOOL_NAMES above) and
# the exact same real-time-grounding system prompt fix (also 2026-09-21),
# rather than needing those fixes hand-copied into three separate tool-use
# loops where they could silently drift out of sync over time. This is a
# behavior-preserving refactor, not a rewrite: AnthropicProvider.
# generate_reply below was changed to CALL these functions instead of
# containing this logic inline, but every condition, argument, and error
# message is unchanged from before the refactor - verified by re-running
# the full existing test suite (748 checks across 21 files) after making
# this change, with zero regressions, before this upgrade was considered
# safe to ship (see progress-tracker.md for the actual re-run results).
# ============================================================================


def _dispatch_client_tool(
    name: str,
    tool_input: "dict | None",
    *,
    tally_daybook_query: "Callable[[str, str], str] | None" = None,
    tally_bill_payment_query: "Callable[[str], str] | None" = None,
    debug_read_errors: "Callable[..., str] | None" = None,
    debug_list_files: "Callable[..., str] | None" = None,
    debug_read_source: "Callable[..., str] | None" = None,
    health_check_query: "Callable[[], str] | None" = None,
    consult_agent_query: "Callable[[str, str], str] | None" = None,
    create_automation_action: "Callable[..., str] | None" = None,
    list_automations_action: "Callable[[], str] | None" = None,
    cancel_automation_action: "Callable[[str], str] | None" = None,
    custom_tool_invoke: "Callable[[str, dict], str] | None" = None,
    search_emails_query: "Callable[[str, int], str] | None" = None,
    read_email_query: "Callable[[str], str] | None" = None,
    draft_email_reply_query: "Callable[[str, str, str, str], str] | None" = None,
    list_calendar_events_query: "Callable[[str, str], str] | None" = None,
    find_open_slots_query: "Callable[[str, str, int], str] | None" = None,
) -> str:
    """The single shared "which client-side tool, or none" decision used by
    every provider's own tool-use loop (Anthropic/Gemini/Groq) - given one
    tool call's name and input dict, returns the plain-text result to send
    back to the model. Never raises: every branch that calls a real
    callable wraps it in its own try/except (identical error messages to
    the original inline AnthropicProvider code this was extracted from), so
    a failing Tally query, a failing automation action, etc. always comes
    back as a plain-language explanation the model can relay, never an
    unhandled exception that would break the whole reply. See
    _KNOWN_CLIENT_TOOL_NAMES above for why a real-but-withheld tool name
    gets an honest "not available" message instead of either crashing (the
    original 2026-09-21 bug) or a generic "Unknown tool" (which reads like
    the model invented the name)."""
    tool_input = tool_input or {}
    if name == "query_tally_daybook" and tally_daybook_query is not None:
        try:
            return tally_daybook_query(
                tool_input.get("date_from", ""), tool_input.get("date_to", "")
            )
        except Exception as e:
            return f"Couldn't query Tally's daybook: {e}"
    elif name == "check_tally_bill_payment_status" and tally_bill_payment_query is not None:
        try:
            return tally_bill_payment_query(tool_input.get("bill_number", ""))
        except Exception as e:
            return f"Couldn't check Tally's bill payment status: {e}"
    elif name == "read_recent_backend_errors" and debug_read_errors is not None:
        try:
            return debug_read_errors(
                tool_input.get("limit", 10), tool_input.get("component", "all")
            )
        except Exception as e:
            return f"Couldn't read the error log: {e}"
    elif name == "list_backend_source_files" and debug_list_files is not None:
        try:
            return debug_list_files(tool_input.get("subdirectory", ""))
        except Exception as e:
            return f"Couldn't list backend files: {e}"
    elif name == "read_backend_source_file" and debug_read_source is not None:
        try:
            return debug_read_source(
                tool_input.get("file_path", ""),
                tool_input.get("start_line"),
                tool_input.get("end_line"),
            )
        except Exception as e:
            return f"Couldn't read that source file: {e}"
    elif name == "run_system_health_check" and health_check_query is not None:
        try:
            return health_check_query()
        except Exception as e:
            return f"Couldn't run the health check: {e}"
    elif name == "consult_agent" and consult_agent_query is not None:
        try:
            return consult_agent_query(
                tool_input.get("agent_name", ""), tool_input.get("question", "")
            )
        except Exception as e:
            return f"Couldn't consult that agent: {e}"
    elif name == "create_automation" and create_automation_action is not None:
        try:
            return create_automation_action(
                tool_input.get("name", ""),
                tool_input.get("instruction", ""),
                tool_input.get("schedule_type", ""),
                tool_input.get("time_of_day", ""),
                tool_input.get("day_of_week"),
                tool_input.get("run_date"),
            )
        except Exception as e:
            return f"Couldn't schedule that automation: {e}"
    elif name == "list_automations" and list_automations_action is not None:
        try:
            return list_automations_action()
        except Exception as e:
            return f"Couldn't list automations: {e}"
    elif name == "cancel_automation" and cancel_automation_action is not None:
        try:
            return cancel_automation_action(tool_input.get("name", ""))
        except Exception as e:
            return f"Couldn't cancel that automation: {e}"
    elif name == "search_emails" and search_emails_query is not None:
        try:
            return search_emails_query(
                tool_input.get("query", ""), tool_input.get("max_results", 10)
            )
        except Exception as e:
            return f"Couldn't search Gmail: {e}"
    elif name == "read_email" and read_email_query is not None:
        try:
            return read_email_query(tool_input.get("message_id", ""))
        except Exception as e:
            return f"Couldn't read that email: {e}"
    elif name == "draft_email_reply" and draft_email_reply_query is not None:
        try:
            return draft_email_reply_query(
                tool_input.get("to", ""),
                tool_input.get("subject", ""),
                tool_input.get("body", ""),
                tool_input.get("thread_id"),
            )
        except Exception as e:
            return f"Couldn't create that Gmail draft: {e}"
    elif name == "list_calendar_events" and list_calendar_events_query is not None:
        try:
            return list_calendar_events_query(
                tool_input.get("date_from", ""), tool_input.get("date_to", "")
            )
        except Exception as e:
            return f"Couldn't read Google Calendar: {e}"
    elif name == "find_open_slots" and find_open_slots_query is not None:
        try:
            return find_open_slots_query(
                tool_input.get("date_from", ""),
                tool_input.get("date_to", ""),
                tool_input.get("duration_minutes", 30),
            )
        except Exception as e:
            return f"Couldn't find open slots: {e}"
    elif custom_tool_invoke is not None and name.startswith("custom_tool_"):
        try:
            return custom_tool_invoke(name, tool_input)
        except Exception as e:
            return f"That tool call failed: {e}"
    elif name in _KNOWN_CLIENT_TOOL_NAMES:
        return (
            f"\"{name}\" isn't available in this conversation "
            "(it's withheld for this context - e.g. an automation's "
            "own unattended run, or a consulted agent - not a bug). "
            "Tell Sudeep honestly that this isn't accessible here "
            "rather than guessing or fabricating an answer."
        )
    else:
        return f"Unknown tool: {name}"


def _offered_tool_specs(
    *,
    tally_daybook_query=None,
    tally_bill_payment_query=None,
    debug_read_errors=None,
    debug_list_files=None,
    debug_read_source=None,
    health_check_query=None,
    custom_tool_specs=None,
    custom_tool_invoke=None,
    consult_agent_query=None,
    create_automation_action=None,
    list_automations_action=None,
    cancel_automation_action=None,
    search_emails_query=None,
    read_email_query=None,
    draft_email_reply_query=None,
    list_calendar_events_query=None,
    find_open_slots_query=None,
) -> list:
    """Builds the canonical, Anthropic-native-SHAPED list of client-side
    tool schemas to offer for this call, purely from which callables are
    non-None - the same enforcement-by-omission pattern used throughout
    this project (see chat_routes.py's build_reply_context). Shared by all
    three providers so a new client-side tool only ever needs to be wired
    up in ONE place: each provider's own generate_reply translates this
    canonical list into its own wire format right before the API call
    (Anthropic uses it as-is; Gemini/Groq run it through
    _tool_schema_to_gemini/_tool_schema_to_groq below). Deliberately does
    NOT include web_search - that's Anthropic's own server-side tool with
    no equivalent wired up for Gemini/Groq yet (see AI_PROVIDERS.md's
    "Known gaps" section); each provider handles its own search
    tool (or lack of one) separately, outside this shared list."""
    tools = []
    if tally_daybook_query is not None:
        tools.append(TALLY_DAYBOOK_TOOL)
    if tally_bill_payment_query is not None:
        tools.append(TALLY_BILL_PAYMENT_TOOL)
    if debug_read_errors is not None:
        tools.append(DEBUG_READ_ERRORS_TOOL)
    if debug_list_files is not None:
        tools.append(DEBUG_LIST_FILES_TOOL)
    if debug_read_source is not None:
        tools.append(DEBUG_READ_SOURCE_TOOL)
    if health_check_query is not None:
        tools.append(HEALTH_CHECK_TOOL)
    if custom_tool_invoke is not None and custom_tool_specs:
        tools.extend(custom_tool_specs)
    if consult_agent_query is not None:
        tools.append(CONSULT_AGENT_TOOL)
    if create_automation_action is not None:
        tools.append(CREATE_AUTOMATION_TOOL)
    if list_automations_action is not None:
        tools.append(LIST_AUTOMATIONS_TOOL)
    if cancel_automation_action is not None:
        tools.append(CANCEL_AUTOMATION_TOOL)
    if search_emails_query is not None:
        tools.append(SEARCH_EMAILS_TOOL)
    if read_email_query is not None:
        tools.append(READ_EMAIL_TOOL)
    if draft_email_reply_query is not None:
        tools.append(DRAFT_EMAIL_REPLY_TOOL)
    if list_calendar_events_query is not None:
        tools.append(LIST_CALENDAR_EVENTS_TOOL)
    if find_open_slots_query is not None:
        tools.append(FIND_OPEN_SLOTS_TOOL)
    return tools


def _any_client_tool_offered(
    *,
    tally_daybook_query=None,
    tally_bill_payment_query=None,
    debug_read_errors=None,
    debug_list_files=None,
    debug_read_source=None,
    health_check_query=None,
    custom_tool_specs=None,
    custom_tool_invoke=None,
    consult_agent_query=None,
    create_automation_action=None,
    list_automations_action=None,
    cancel_automation_action=None,
    search_emails_query=None,
    read_email_query=None,
    draft_email_reply_query=None,
    list_calendar_events_query=None,
    find_open_slots_query=None,
) -> bool:
    """True if this call offers at least one client-side tool that needs a
    tool-use loop (as opposed to Anthropic's server-side web_search, which
    never needs one). Shared condition, same as _offered_tool_specs above."""
    return (
        tally_daybook_query is not None
        or tally_bill_payment_query is not None
        or debug_read_errors is not None
        or debug_list_files is not None
        or debug_read_source is not None
        or health_check_query is not None
        or (custom_tool_invoke is not None and bool(custom_tool_specs))
        or consult_agent_query is not None
        or create_automation_action is not None
        or list_automations_action is not None
        or cancel_automation_action is not None
        or search_emails_query is not None
        or read_email_query is not None
        or draft_email_reply_query is not None
        or list_calendar_events_query is not None
        or find_open_slots_query is not None
    )


def _build_system_prompt(
    memory_context: "str | None",
    skill_context: "str | None",
    tally_context: "str | None",
    agent_context: "str | None",
    consult_agent_query,
    consult_directory_context: "str | None",
) -> str:
    """Shared system-prompt assembly for every provider. Extracted out of
    AnthropicProvider.generate_reply's own inline code so the real
    current-time grounding fix (added 2026-09-21, after a real live bug:
    asked to schedule a one-time automation "2 minutes from now", the model
    guessed a time ~13.5 hours off because nothing ever told it what time it
    actually is) and the memory/skill/tally/agent-persona/consult-directory
    context folding automatically apply IDENTICALLY to Gemini and Groq too -
    a new provider adapter can never silently ship without this grounding
    just because whoever wrote it forgot to hand-copy this block from
    AnthropicProvider."""
    _now = datetime.datetime.now()
    system_prompt = (
        f"Current date and time (local time on Sudeep's machine, which is "
        f"also what automation scheduling and every timestamp in this app "
        f"use): {_now.strftime('%A, %Y-%m-%d %H:%M')}. Always use this as "
        f"\"now\" - never guess or assume the date/time from training data "
        f"or conversation context. When scheduling an automation (create_"
        f"automation) or answering anything involving \"today\"/\"tomorrow\"/"
        f"\"in N minutes\"/\"next week\" etc., compute the actual date/time "
        f"from this real value, not from a guess.\n\n{JARVIS_SYSTEM_PROMPT}"
    )
    if agent_context:
        system_prompt = f"{system_prompt}\n\n{agent_context}"
    if consult_agent_query is not None and consult_directory_context:
        system_prompt = f"{system_prompt}\n\n{consult_directory_context}"
    if memory_context:
        system_prompt = f"{system_prompt}\n\n{memory_context}"
    if skill_context:
        system_prompt = f"{system_prompt}\n\n{skill_context}"
    if tally_context:
        system_prompt = f"{system_prompt}\n\n{tally_context}"
    return system_prompt


def _tool_schema_to_gemini(tool: dict) -> dict:
    """Translates one canonical (Anthropic-native-shaped)
    {"name","description","input_schema"} tool dict into Gemini's
    functionDeclarations shape - {"name","description","parameters"}. Every
    existing tool schema in this file (and every dynamically-built custom
    tool from custom_tools.build_tool_schema) is a flat JSON Schema object
    (type/properties/enum/description/required, no $ref/oneOf/anyOf/nested
    unions - confirmed by inspection before writing this), which Gemini's
    REST API accepts as-is under "parameters" - this is a rename, not a
    restructure. Verified against ai.google.dev's own current
    generateContent REST reference (functionDeclarations[].parameters)
    2026-09-21."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }


def _tool_schema_to_groq(tool: dict) -> dict:
    """Translates one canonical tool dict into Groq's OpenAI-compatible
    function-calling shape - {"type": "function", "function":
    {"name","description","parameters"}}. Same "rename, not restructure"
    reasoning as _tool_schema_to_gemini above - Groq's chat/completions
    endpoint is a direct OpenAI-compatible drop-in (confirmed against
    console.groq.com/docs/api-reference 2026-09-21), so the existing flat
    JSON Schema input_schema works unchanged as "parameters"."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


class ProviderError(Exception):
    """Raised by a provider adapter (currently GeminiProvider/GroqProvider -
    see each provider's own _post method) on a genuine, provider-level
    failure, carrying a `category` matching the master upgrade prompt's own
    normalized error-category list (TIMEOUT, PROVIDER_UNAVAILABLE,
    RATE_LIMIT, SERVER_ERROR, AUTHENTICATION_ERROR, MODEL_UNAVAILABLE,
    INVALID_REQUEST, etc.), so AIProviderManager can decide whether this is
    worth falling back to the next provider on without re-parsing
    provider-specific exception/status details every time (see
    AIProviderManager._should_fall_back_on). AnthropicProvider is NOT
    changed to raise this - it's pre-existing, already-tested, already
    live-verified code that predates this upgrade (see the "no rewrite of
    tested code" principle in progress-tracker.md); AIProviderManager
    treats any UNCLASSIFIED exception (including AnthropicProvider's own
    raw anthropic.* exceptions) as fallback-worthy by default instead."""

    def __init__(self, category: str, message: str, provider: str):
        self.category = category
        self.provider = provider
        super().__init__(message)


class ProviderCapabilityError(Exception):
    """Raised when a provider is asked to do something it doesn't
    implement in this version - e.g. GeminiProvider.upload_document(), since
    no provider besides Anthropic has a Files-API equivalent wired up yet
    (see each provider's SUPPORTS_DOCUMENTS class attribute).
    AIProviderManager never tries to "fall back" on this the way it does for
    ProviderError - a capability gap isn't a transient failure, so trying
    the SAME unsupported call against a different provider that also lacks
    it wouldn't help either; AIProviderManager instead routes document/
    skill-learning calls directly to whichever configured provider's
    SUPPORTS_DOCUMENTS flag says it can actually do the job (see
    AIProviderManager._first_supporting_documents)."""

    def __init__(self, provider: str, capability: str):
        self.provider = provider
        self.capability = capability
        super().__init__(
            f"{provider} does not support {capability} in this version of JARVIS"
        )


class ProviderNotConfiguredError(Exception):
    """Raised at provider construction time when its required API key isn't
    set (e.g. GeminiProvider() with no GEMINI_API_KEY in .env).
    AIProviderManager catches this while building its provider chain and
    simply leaves that provider out (not a startup failure) - Sudeep can add
    a key later and it becomes available on the next backend restart, no
    code change needed."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(message)


class AIProvider(ABC):
    """Base interface every AI backend (Anthropic, OpenAI, etc.) must implement.
    This is the layer that keeps JARVIS from being locked into one AI company:
    to add a new provider later, write one more class like AnthropicProvider
    below and add it to get_ai_provider() - nothing else in the app changes.
    """

    @abstractmethod
    def generate_reply(
        self,
        messages: list[dict],
        memory_context: str | None = None,
        skill_context: str | None = None,
        tally_context: str | None = None,
        agent_context: str | None = None,
        allow_web_search: bool = True,
        tally_daybook_query: "Callable[[str, str], str] | None" = None,
        tally_bill_payment_query: "Callable[[str], str] | None" = None,
        debug_read_errors: "Callable[..., str] | None" = None,
        debug_list_files: "Callable[..., str] | None" = None,
        debug_read_source: "Callable[..., str] | None" = None,
        health_check_query: "Callable[[], str] | None" = None,
        custom_tool_specs: "list[dict] | None" = None,
        custom_tool_invoke: "Callable[[str, dict], str] | None" = None,
        consult_agent_query: "Callable[[str, str], str] | None" = None,
        consult_directory_context: "str | None" = None,
        create_automation_action: "Callable[..., str] | None" = None,
        list_automations_action: "Callable[[], str] | None" = None,
        cancel_automation_action: "Callable[[str], str] | None" = None,
        search_emails_query: "Callable[[str, int], str] | None" = None,
        read_email_query: "Callable[[str], str] | None" = None,
        draft_email_reply_query: "Callable[[str, str, str, str | None], str] | None" = None,
        list_calendar_events_query: "Callable[[str, str], str] | None" = None,
        find_open_slots_query: "Callable[[str, str, int], str] | None" = None,
    ) -> str:
        """messages is a list of {"role": "user"|"assistant", "content": str}.
        memory_context, if given, is a short block of remembered facts about
        Sudeep to fold into the system prompt for this reply. skill_context,
        if given, is a block of approved skills' knowledge write-ups (Phase
        11) to fold in the same way. tally_context, if given, is the
        configured Tally ledger names (custom Tally billing feature) so
        JARVIS uses Sudeep's real ledger names rather than inventing one.
        agent_context, if given (Agent Factory v1, see progress-tracker.md),
        is a persona/role overlay identifying which named agent this reply is
        being generated as, folded into the system prompt the same way.
        allow_web_search, when False, omits the web search tool from this
        one call entirely (used to enforce an agent's allow_web_search=False
        setting in code, not just by asking the model not to use it) -
        defaults to True so every existing caller keeps today's behavior.
        tally_daybook_query, if given (added 2026-09-20, self-diagnosis
        session follow-up), is a callable(date_from, date_to) -> str that
        actually queries Tally's real daybook and returns a text summary -
        when present, the query_tally_daybook tool is offered to the model
        and a real client-side tool-use loop runs (unlike web_search, which
        Anthropic runs entirely server-side). None (the default) omits the
        tool from this call entirely, same enforcement pattern as
        allow_web_search - a caller with Tally billing turned off for this
        agent simply never passes one.
        tally_bill_payment_query, if given (added 2026-09-20, same day as a
        real live fabrication bug on a payment-status question), is a
        callable(bill_number) -> str that computes a bill's real payment
        status in code (tally_client.check_bill_payment_status) and returns
        a text summary - when present, the check_tally_bill_payment_status
        tool is offered alongside query_tally_daybook. None (the default)
        omits it, same enforcement pattern as tally_daybook_query.
        debug_read_errors/debug_list_files/debug_read_source, if given
        (Phase 13 "Debugging Agent", added 2026-09-20), are
        debug_agent.read_recent_errors/list_backend_source_files/
        read_backend_source - read-only introspection into JARVIS's own
        backend errors and source code, offered as three more client-side
        tools in the same loop. Unlike the Tally tools, these are NOT gated
        per-agent in v1 - a caller either passes all three (debug agent
        enabled) or none (disabled), the same enforcement-by-omission
        pattern as everything else here.
        health_check_query, if given (Phase 14 "Self-diagnostics", added
        2026-09-20), is health_check.run_and_format_health_check - a
        callable() -> str that checks the AI provider, database, Tally,
        Docker, and web search and returns a formatted report. Also not
        gated per-agent in v1, same reasoning as the debug tools above.
        custom_tool_specs/custom_tool_invoke, if given (Phase 16 "Tool/
        plugin architecture", added 2026-09-20), are the dynamically-built
        Anthropic tool schemas for whichever CustomTool rows the current
        agent is assigned (see custom_tools.build_tool_schema) and a single
        dispatcher callable(tool_name, params) -> str that looks up the
        right CustomTool by its "custom_tool_<id>" name and actually invokes
        it (custom_tools.invoke_custom_tool) - a read-only tool is called
        for real, a non-read-only one only ever returns a draft, never
        actually sends anything (see custom_tools.py). Unlike the built-in
        tools, these ARE effectively gated per-agent already, one level up
        in chat_routes.py (only the calling agent's own assigned_custom_
        tool_ids are ever turned into specs at all).
        consult_agent_query, if given (Phase 17 "AI agent orchestration",
        added 2026-09-20), is a callable(agent_name, question) -> str that
        runs a single-turn, single-hop sub-query against one of Sudeep's
        OTHER named agents (see chat_routes.py's run_agent_subquery) and
        returns its answer. Not gated per-agent - offered whenever there's
        at least one other agent to consult and settings.agent_consult_
        enabled is on.
        consult_directory_context, if given alongside consult_agent_query
        (added 2026-09-20, a same-day follow-up once Sudeep asked how to
        actually test consulting - without this, the model has no way to
        know any other agent's name or role unless Sudeep spells it out
        himself every time, which defeats the point of an "automatic"
        consult), is a short block listing each OTHER active agent's real
        name and role description, folded into the system prompt the same
        way memory_context/skill_context are. This is what lets the model
        genuinely decide on its own when a question fits a different
        agent's role, rather than only consulting when Sudeep names the
        agent explicitly in his message (explicitly naming one still always
        works too, since consult_agent takes any agent_name).
        create_automation_action/list_automations_action/cancel_automation_
        action, if given (Phase 18 "Automation", added 2026-09-20, scoped
        with Sudeep via AskUserQuestion), are callables backing the three
        automation-management tools (see chat_routes.py's build_reply_
        context and the *_AUTOMATION_TOOL schemas above) - offered only in
        a live, interactive reply; a caller running an automation's own
        scheduled instruction (automation_engine.run_one) never passes
        these, so an automation can never manage automations on its own.
        search_emails_query/read_email_query/draft_email_reply_query/
        list_calendar_events_query/find_open_slots_query, if given (Phase 19
        "Email + Calendar", added 2026-09-22, scoped with Sudeep via
        AskUserQuestion - see progress-tracker.md and google_client.py),
        back the five Gmail/Calendar client-side tools the same
        enforcement-by-omission way as everything else here - a caller with
        Sudeep's Google account not connected, or email/calendar turned off
        for this agent/run, simply never passes them. Creating a REAL
        calendar event is NEVER one of these tool calls (it's externally
        visible, so it goes through the separate [CALENDAR_EVENT_DRAFT]
        review-card + confirm flow instead, same as [TALLY_BILL_DRAFT]) -
        only viewing/finding-slots on the calendar and reading/drafting
        (never sending) email are offered as live tool calls."""
        raise NotImplementedError

    @abstractmethod
    def extract_memories(
        self, existing: list[dict], user_message: str, assistant_reply: str
    ) -> dict:
        """existing is a list of {"id": int, "content": str, "category": str|None}
        for memories already saved. Returns {"new": [...], "updates": [...]} -
        see MEMORY_EXTRACTION_SYSTEM_PROMPT for the exact shape. Must never
        raise - callers treat this as best-effort and run it in the
        background, so any failure here should just come back empty."""
        raise NotImplementedError

    @abstractmethod
    def upload_document(self, filename: str, content: bytes, mime_type: str) -> str:
        """Uploads a file to the provider's file storage and returns an id
        that can later be referenced in ask_about_documents(). Phase 10:
        knowledge_routes.py calls this once per upload; the returned id is
        what gets stored on the Document row."""
        raise NotImplementedError

    @abstractmethod
    def delete_document(self, file_id: str) -> None:
        """Removes a previously uploaded file from the provider's side.
        Must never raise - deleting Sudeep's own knowledge-base entry should
        always succeed from his point of view even if the remote side is
        already gone or briefly unreachable."""
        raise NotImplementedError

    @abstractmethod
    def ask_about_documents(self, file_ids: list[str], question: str) -> str:
        """One-off Q&A grounded in the given previously-uploaded file(s) -
        deliberately NOT part of any conversation history (see
        knowledge_routes.py for why)."""
        raise NotImplementedError

    @abstractmethod
    def learn_skill(self, topic: str) -> dict:
        """Researches a topic Sudeep asked JARVIS to learn and returns
        {"description": str, "content": str} - a one-line summary and the
        full knowledge write-up. Phase 11: skill_routes.py saves the result
        as a Skill row with status="pending_review"; it isn't used in chat
        until Sudeep approves it. Should raise on genuine failure (unlike
        extract_memories/delete_document) since there's no silent background
        fallback here - the caller shows Sudeep a clear error instead."""
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

        # The web search tool runs entirely on Anthropic's side (Claude decides
        # on its own when a question needs a live search, runs it, and gets the
        # results back) - we don't have to implement any search loop ourselves.
        # It's billed per search (not per token), which is why max_uses caps how
        # many searches a single reply can trigger - see Phase 8 in the progress
        # tracker for the "controlled" research requirement this satisfies.
        self.tools = (
            [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": settings.web_search_max_uses,
                }
            ]
            if settings.web_search_enabled
            else None
        )

    def generate_reply(
        self,
        messages: list[dict],
        memory_context: str | None = None,
        skill_context: str | None = None,
        tally_context: str | None = None,
        agent_context: str | None = None,
        allow_web_search: bool = True,
        tally_daybook_query: "Callable[[str, str], str] | None" = None,
        tally_bill_payment_query: "Callable[[str], str] | None" = None,
        debug_read_errors: "Callable[..., str] | None" = None,
        debug_list_files: "Callable[..., str] | None" = None,
        debug_read_source: "Callable[..., str] | None" = None,
        health_check_query: "Callable[[], str] | None" = None,
        custom_tool_specs: "list[dict] | None" = None,
        custom_tool_invoke: "Callable[[str, dict], str] | None" = None,
        consult_agent_query: "Callable[[str, str], str] | None" = None,
        consult_directory_context: "str | None" = None,
        create_automation_action: "Callable[..., str] | None" = None,
        list_automations_action: "Callable[[], str] | None" = None,
        cancel_automation_action: "Callable[[str], str] | None" = None,
        search_emails_query: "Callable[[str, int], str] | None" = None,
        read_email_query: "Callable[[str], str] | None" = None,
        draft_email_reply_query: "Callable[[str, str, str, str | None], str] | None" = None,
        list_calendar_events_query: "Callable[[str, str], str] | None" = None,
        find_open_slots_query: "Callable[[str, str, int], str] | None" = None,
    ) -> str:
        # Real current-time grounding, added 2026-09-21 after a real failure:
        # asked to schedule a one-time automation "2 minutes from now", the
        # model had no actual notion of what time it currently is (nothing
        # anywhere in this call ever told it) and guessed a time ~13.5 hours
        # off - the same "never trust the AI's arithmetic" lesson this
        # project has hit repeatedly elsewhere (GST tax math, bill amounts,
        # payment status), just never applied to date/time before. This is
        # unconditional (every reply, not just automation-related ones) since
        # any "today"/"tomorrow"/"in an hour" question in normal chat has the
        # exact same ungrounded-guess problem, not just create_automation.
        # System-prompt assembly and the client-tool list below are now built
        # by shared helpers (_build_system_prompt/_offered_tool_specs - see
        # their docstrings above) as of the 2026-09-21 Multi-Model AI Brain
        # upgrade, so GeminiProvider/GroqProvider get the exact same
        # time-grounding fix and tool-offering rules - this is a
        # behavior-preserving extraction, not a logic change (every
        # condition and ordering below is identical to before the
        # refactor).
        system_prompt = _build_system_prompt(
            memory_context, skill_context, tally_context, agent_context,
            consult_agent_query, consult_directory_context,
        )

        # allow_web_search=False (an agent with web search turned off) omits
        # the tool from this call entirely - enforced here in code, not just
        # by asking the model not to use it, so a prompt-injected or
        # non-compliant reply still can't trigger a real search. web_search
        # is Anthropic's own server-side tool (no shared-helper equivalent -
        # see _offered_tool_specs's docstring), so it's still added here
        # directly, before the shared client-tool list.
        tools = list(self.tools) if (allow_web_search and self.tools) else []
        tools.extend(_offered_tool_specs(
            tally_daybook_query=tally_daybook_query,
            tally_bill_payment_query=tally_bill_payment_query,
            debug_read_errors=debug_read_errors,
            debug_list_files=debug_list_files,
            debug_read_source=debug_read_source,
            health_check_query=health_check_query,
            custom_tool_specs=custom_tool_specs,
            custom_tool_invoke=custom_tool_invoke,
            consult_agent_query=consult_agent_query,
            create_automation_action=create_automation_action,
            list_automations_action=list_automations_action,
            cancel_automation_action=cancel_automation_action,
            search_emails_query=search_emails_query,
            read_email_query=read_email_query,
            draft_email_reply_query=draft_email_reply_query,
            list_calendar_events_query=list_calendar_events_query,
            find_open_slots_query=find_open_slots_query,
        ))
        tools = tools or None

        conversation = list(messages)
        try:
            response = self._create_message(conversation, tools, system_prompt)
        except Exception:
            # If a tool itself is ever the problem (a transient API issue, an
            # account/plan restriction, etc.), fall back to a plain reply
            # instead of breaking the whole conversation. A real, unrelated
            # error will still raise on this second attempt.
            if tools:
                response = self._create_message(conversation, None, system_prompt)
                return self._extract_reply(response)
            raise

        # Client-side tool-use loop for the Tally tools and the Phase 13
        # debugging tools ONLY - web_search is a server-executed tool
        # (type="web_search_20250305") that Anthropic runs and resolves
        # entirely on its own side, so it never surfaces a tool_use block
        # this code needs to answer. Capped at a few rounds so a confused
        # model can't loop forever - raised from 3 to 5 when the debugging
        # tools were added, since a real investigation can genuinely need
        # list -> read -> read-errors as separate calls in one reply.
        rounds = 0
        any_client_tool = _any_client_tool_offered(
            tally_daybook_query=tally_daybook_query,
            tally_bill_payment_query=tally_bill_payment_query,
            debug_read_errors=debug_read_errors,
            debug_list_files=debug_list_files,
            debug_read_source=debug_read_source,
            health_check_query=health_check_query,
            custom_tool_specs=custom_tool_specs,
            custom_tool_invoke=custom_tool_invoke,
            consult_agent_query=consult_agent_query,
            create_automation_action=create_automation_action,
            list_automations_action=list_automations_action,
            cancel_automation_action=cancel_automation_action,
            search_emails_query=search_emails_query,
            read_email_query=read_email_query,
            draft_email_reply_query=draft_email_reply_query,
            list_calendar_events_query=list_calendar_events_query,
            find_open_slots_query=find_open_slots_query,
        )
        while (
            any_client_tool
            and rounds < 5
            and any(getattr(b, "type", None) == "tool_use" for b in response.content)
        ):
            rounds += 1
            conversation.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                # Dispatch delegated to the shared _dispatch_client_tool
                # helper as of the 2026-09-21 Multi-Model AI Brain upgrade -
                # see that function's own docstring above for the full
                # history of the None-check safety fix this preserves
                # (originally added inline right here, after a real live
                # "'NoneType' object is not callable" crash). Identical
                # behavior to the inline chain this replaced - only the
                # location of the logic changed, verified by re-running the
                # full existing test suite unchanged after this refactor.
                result_text = _dispatch_client_tool(
                    block.name,
                    block.input,
                    tally_daybook_query=tally_daybook_query,
                    tally_bill_payment_query=tally_bill_payment_query,
                    debug_read_errors=debug_read_errors,
                    debug_list_files=debug_list_files,
                    debug_read_source=debug_read_source,
                    health_check_query=health_check_query,
                    consult_agent_query=consult_agent_query,
                    create_automation_action=create_automation_action,
                    list_automations_action=list_automations_action,
                    cancel_automation_action=cancel_automation_action,
                    custom_tool_invoke=custom_tool_invoke,
                    search_emails_query=search_emails_query,
                    read_email_query=read_email_query,
                    draft_email_reply_query=draft_email_reply_query,
                    list_calendar_events_query=list_calendar_events_query,
                    find_open_slots_query=find_open_slots_query,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            conversation.append({"role": "user", "content": tool_results})
            response = self._create_message(conversation, tools, system_prompt)

        return self._extract_reply(response)

    def extract_memories(
        self, existing: list[dict], user_message: str, assistant_reply: str
    ) -> dict:
        empty = {"new": [], "updates": []}
        try:
            existing_text = "\n".join(
                f"[{m['id']}] ({m.get('category') or 'general'}) {m['content']}" for m in existing
            ) or "(none saved yet)"
            prompt = (
                f"Memories saved so far:\n{existing_text}\n\n"
                f"Latest exchange:\nSudeep: {user_message}\nJARVIS: {assistant_reply}\n\n"
                "Return the JSON now."
            )
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system=MEMORY_EXTRACTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(
                block.text for block in response.content if getattr(block, "type", None) == "text"
            ).strip()
            # The model is told not to use markdown fences, but strip them
            # defensively in case it does anyway - a fenced block would
            # otherwise fail json.loads() and silently discard real facts.
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)
            return {
                "new": data.get("new") or [],
                "updates": data.get("updates") or [],
            }
        except Exception:
            # Memory extraction is a nice-to-have running in the background -
            # never let a parsing hiccup or API error surface to Sudeep.
            return empty

    def upload_document(self, filename: str, content: bytes, mime_type: str) -> str:
        import io
        uploaded = self.client.files.upload(file=(filename, io.BytesIO(content), mime_type))
        return uploaded.id

    def delete_document(self, file_id: str) -> None:
        try:
            self.client.files.delete(file_id)
        except Exception:
            # If it's already gone on Anthropic's side (or this call fails for
            # any other reason), don't block Sudeep from removing it from his
            # own knowledge base list - our Document row is what he sees.
            pass

    def ask_about_documents(self, file_ids: list[str], question: str) -> str:
        content = [
            {"type": "document", "source": {"type": "file", "file_id": fid}}
            for fid in file_ids
        ]
        content.append({"type": "text", "text": question})
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1536,
            system=KNOWLEDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        return self._extract_reply(response)

    def learn_skill(self, topic: str) -> dict:
        user_prompt = f"Topic to learn: {topic}"
        messages = [{"role": "user", "content": user_prompt}]
        try:
            response = self._create_message(
                messages, self.tools, SKILL_LEARNING_SYSTEM_PROMPT, max_tokens=4096
            )
        except Exception:
            if self.tools:
                response = self._create_message(
                    messages, None, SKILL_LEARNING_SYSTEM_PROMPT, max_tokens=4096
                )
            else:
                raise

        text = self._extract_reply(response)
        return _parse_skill_response(text, fallback_topic=topic)

    def _create_message(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        system_prompt: str,
        max_tokens: int = 1536,
    ):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        return self.client.messages.create(**kwargs)

    @staticmethod
    def _extract_reply(response) -> str:
        """Pulls the actual reply text out of the response, plus a de-duplicated
        list of every source Claude cited while web-searching (if any), and
        appends them as a plain "Sources:" section. When search isn't used,
        this behaves exactly like before - response.content is just one text
        block with no citations."""
        text_parts: list[str] = []
        sources: list[tuple[str, str]] = []
        seen_urls: set[str] = set()

        for block in response.content:
            if getattr(block, "type", None) != "text":
                continue
            text_parts.append(block.text)
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = getattr(citation, "title", None) or url
                sources.append((title, url))

        reply = "".join(text_parts).strip()
        if not reply:
            reply = "I couldn't come up with a reply that time - please try again."

        if sources:
            source_lines = "\n".join(f"- {title}: {url}" for title, url in sources)
            reply = f"{reply}\n\nSources:\n{source_lines}"

        return reply


def _parse_skill_response(text: str, fallback_topic: str) -> dict:
    """Splits learn_skill()'s raw model output into {"description", "content"}
    using the [DESCRIPTION]/[CONTENT] markers requested in
    SKILL_LEARNING_SYSTEM_PROMPT. Deliberately tolerant: the model usually
    follows the format, but if it doesn't (extra preamble, missing marker,
    etc.) this still returns something sensible - the whole point of a
    review-before-activate flow is that Sudeep sees the result before it's
    used anywhere, so a slightly-off split is a UI annoyance, not a safety
    problem."""
    description = None
    content = text.strip()

    if "[CONTENT]:" in text:
        before, after = text.split("[CONTENT]:", 1)
        content = after.strip()
        if "[DESCRIPTION]:" in before:
            description = before.split("[DESCRIPTION]:", 1)[1].strip()
    elif "[DESCRIPTION]:" in text:
        # [CONTENT] marker missing but [DESCRIPTION] present - treat
        # everything after the description line as the content.
        _, after = text.split("[DESCRIPTION]:", 1)
        lines = after.strip().split("\n", 1)
        description = lines[0].strip()
        content = lines[1].strip() if len(lines) > 1 else description

    if not description:
        description = f"Notes on {fallback_topic}"
    if not content:
        content = text.strip() or f"(JARVIS didn't return any content for '{fallback_topic}'.)"

    return {"description": description, "content": content}


def _messages_to_gemini_contents(messages: list) -> list:
    """Converts JARVIS's own {"role": "user"|"assistant", "content": str}
    chat history into Gemini's {"role": "user"|"model", "parts": [...]}
    shape - Gemini uses "model" where Anthropic/JARVIS use "assistant";
    everything else is a straight rename, wrapping the plain string content
    in one text part. Only ever used for the original, unmodified history
    passed into generate_reply - once GeminiProvider's own tool loop starts
    appending its own model/functionCall and user/functionResponse turns,
    those are built directly in Gemini's shape without going through here."""
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    return contents


def _gemini_candidate_parts(data: dict) -> list:
    try:
        return data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return []


def _extract_gemini_function_calls(data: dict) -> list:
    calls = []
    for part in _gemini_candidate_parts(data):
        fc = part.get("functionCall") if isinstance(part, dict) else None
        if fc:
            calls.append({"name": fc.get("name", ""), "args": fc.get("args") or {}})
    return calls


def _extract_gemini_text(data: dict) -> str:
    """Mirrors AnthropicProvider._extract_reply's own "never return a blank
    reply" fallback - a real consideration here too: a candidate can come
    back with only a functionCall part and no text (if a tool-loop round
    limit is hit) or be cut short by a non-STOP finishReason (safety
    filters, MAX_TOKENS, etc.)."""
    text = "".join(
        part.get("text", "") for part in _gemini_candidate_parts(data) if isinstance(part, dict)
    ).strip()
    if text:
        return text
    finish_reason = None
    try:
        finish_reason = data["candidates"][0].get("finishReason")
    except (KeyError, IndexError, TypeError):
        pass
    if finish_reason and finish_reason != "STOP":
        return f"I couldn't come up with a reply that time (Gemini stopped early: {finish_reason})."
    return "I couldn't come up with a reply that time - please try again."


class GeminiProvider(AIProvider):
    """Google Gemini adapter (added 2026-09-21, Multi-Model AI Brain
    upgrade - see progress-tracker.md and the module-level comment block
    above AIProvider for the full scoping story). Talks directly to the
    Gemini REST API via httpx (already an installed dependency - see
    requirements.txt) rather than adding the google-genai SDK as a new
    dependency, keeping Sudeep's Windows venv install surface unchanged.

    Model ID verified against ai.google.dev/gemini-api/docs/models (the
    official current model list) on 2026-09-21 - gemini-3.8-flash is real,
    current, and listed production-available as of that check, not blindly
    copied from the upgrade prompt that suggested it (see AI_PROVIDERS.md's
    "Model verification" section for the check itself).

    KNOWN GAPS in this v1 (documented, not silent - see AI_PROVIDERS.md):
    no web-search-grounding tool wired up yet (Gemini has its own
    googleSearch tool, a different shape from Anthropic's server-side
    web_search - a real future addition, not done here); does not implement
    the Knowledge Base document methods (SUPPORTS_DOCUMENTS = False below) -
    Sudeep's existing Knowledge Base feature is built around Anthropic's own
    Files API and the Document.anthropic_file_id database column, so
    AIProviderManager always routes upload_document/delete_document/
    ask_about_documents to whichever configured provider's SUPPORTS_
    DOCUMENTS flag is True (Claude, in the default chain), never to this
    class - this gap is invisible to Sudeep as long as Claude stays
    configured somewhere in the chain.
    """

    SUPPORTS_DOCUMENTS = False

    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: "str | None" = None):
        if not settings.gemini_api_key:
            raise ProviderNotConfiguredError("gemini", "GEMINI_API_KEY is not set")
        self.api_key = settings.gemini_api_key
        self.model = model or settings.gemini_model
        self._client = httpx.Client(timeout=settings.ai_request_timeout_ms / 1000)

    def _post(self, model_path: str, payload: dict) -> dict:
        """POSTs to the Gemini REST API and classifies any failure into a
        ProviderError with a normalized category (see ProviderError's own
        docstring) - this is what lets AIProviderManager decide whether a
        Gemini failure is worth falling back to Groq/Claude on, without
        knowing anything Gemini-specific itself."""
        url = f"{self._BASE_URL}/{model_path}"
        try:
            resp = self._client.post(
                url,
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
            )
        except httpx.TimeoutException as e:
            raise ProviderError("TIMEOUT", f"Gemini request timed out: {e}", "gemini") from e
        except httpx.ConnectError as e:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Couldn't reach Gemini: {e}", "gemini") from e
        except httpx.HTTPError as e:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Gemini request failed: {e}", "gemini") from e

        if resp.status_code in (401, 403):
            raise ProviderError(
                "AUTHENTICATION_ERROR",
                f"Gemini authentication failed ({resp.status_code}): {resp.text[:300]}",
                "gemini",
            )
        if resp.status_code == 429:
            raise ProviderError("RATE_LIMIT", f"Gemini rate limit hit: {resp.text[:300]}", "gemini")
        if resp.status_code >= 500:
            raise ProviderError(
                "SERVER_ERROR", f"Gemini server error ({resp.status_code}): {resp.text[:300]}", "gemini"
            )
        if resp.status_code == 404:
            raise ProviderError(
                "MODEL_UNAVAILABLE",
                f"Gemini model \"{self.model}\" not found (404): {resp.text[:300]}",
                "gemini",
            )
        if resp.status_code != 200:
            # Includes 400 (INVALID_REQUEST) - a malformed request is
            # JARVIS's own bug, not a Gemini outage; AIProviderManager
            # deliberately does NOT fall back on this category (see
            # _ERROR_CATEGORIES_WORTH_FALLBACK below).
            category = "INVALID_REQUEST" if resp.status_code == 400 else "SERVER_ERROR"
            raise ProviderError(
                category, f"Gemini rejected the request ({resp.status_code}): {resp.text[:300]}", "gemini"
            )
        return resp.json()

    def generate_reply(
        self,
        messages: list,
        memory_context=None,
        skill_context=None,
        tally_context=None,
        agent_context=None,
        allow_web_search=True,
        tally_daybook_query=None,
        tally_bill_payment_query=None,
        debug_read_errors=None,
        debug_list_files=None,
        debug_read_source=None,
        health_check_query=None,
        custom_tool_specs=None,
        custom_tool_invoke=None,
        consult_agent_query=None,
        consult_directory_context=None,
        create_automation_action=None,
        list_automations_action=None,
        cancel_automation_action=None,
        search_emails_query=None,
        read_email_query=None,
        draft_email_reply_query=None,
        list_calendar_events_query=None,
        find_open_slots_query=None,
    ) -> str:
        system_prompt = _build_system_prompt(
            memory_context, skill_context, tally_context, agent_context,
            consult_agent_query, consult_directory_context,
        )
        canonical_tools = _offered_tool_specs(
            tally_daybook_query=tally_daybook_query,
            tally_bill_payment_query=tally_bill_payment_query,
            debug_read_errors=debug_read_errors,
            debug_list_files=debug_list_files,
            debug_read_source=debug_read_source,
            health_check_query=health_check_query,
            custom_tool_specs=custom_tool_specs,
            custom_tool_invoke=custom_tool_invoke,
            consult_agent_query=consult_agent_query,
            create_automation_action=create_automation_action,
            list_automations_action=list_automations_action,
            cancel_automation_action=cancel_automation_action,
            search_emails_query=search_emails_query,
            read_email_query=read_email_query,
            draft_email_reply_query=draft_email_reply_query,
            list_calendar_events_query=list_calendar_events_query,
            find_open_slots_query=find_open_slots_query,
        )

        contents = _messages_to_gemini_contents(messages)
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
        }
        if canonical_tools:
            payload["tools"] = [
                {"functionDeclarations": [_tool_schema_to_gemini(t) for t in canonical_tools]}
            ]

        model_path = f"models/{self.model}:generateContent"
        data = self._post(model_path, payload)

        any_client_tool = _any_client_tool_offered(
            tally_daybook_query=tally_daybook_query,
            tally_bill_payment_query=tally_bill_payment_query,
            debug_read_errors=debug_read_errors,
            debug_list_files=debug_list_files,
            debug_read_source=debug_read_source,
            health_check_query=health_check_query,
            custom_tool_specs=custom_tool_specs,
            custom_tool_invoke=custom_tool_invoke,
            consult_agent_query=consult_agent_query,
            create_automation_action=create_automation_action,
            list_automations_action=list_automations_action,
            cancel_automation_action=cancel_automation_action,
            search_emails_query=search_emails_query,
            read_email_query=read_email_query,
            draft_email_reply_query=draft_email_reply_query,
            list_calendar_events_query=list_calendar_events_query,
            find_open_slots_query=find_open_slots_query,
        )

        # Same 5-round cap and same shared dispatcher as AnthropicProvider's
        # own loop (see _dispatch_client_tool) - Gemini surfaces a pending
        # tool call as a functionCall part instead of a tool_use block, and
        # expects the result back as a functionResponse part in a "user"
        # turn (see ai.google.dev's function-calling REST reference), but
        # the underlying "which tool, or none, and what to say back" logic
        # is identical.
        rounds = 0
        while any_client_tool and rounds < 5:
            function_calls = _extract_gemini_function_calls(data)
            if not function_calls:
                break
            rounds += 1
            contents.append({"role": "model", "parts": _gemini_candidate_parts(data)})
            response_parts = []
            for fc in function_calls:
                result_text = _dispatch_client_tool(
                    fc["name"],
                    fc["args"],
                    tally_daybook_query=tally_daybook_query,
                    tally_bill_payment_query=tally_bill_payment_query,
                    debug_read_errors=debug_read_errors,
                    debug_list_files=debug_list_files,
                    debug_read_source=debug_read_source,
                    health_check_query=health_check_query,
                    consult_agent_query=consult_agent_query,
                    create_automation_action=create_automation_action,
                    list_automations_action=list_automations_action,
                    cancel_automation_action=cancel_automation_action,
                    custom_tool_invoke=custom_tool_invoke,
                    search_emails_query=search_emails_query,
                    read_email_query=read_email_query,
                    draft_email_reply_query=draft_email_reply_query,
                    list_calendar_events_query=list_calendar_events_query,
                    find_open_slots_query=find_open_slots_query,
                )
                response_parts.append(
                    {"functionResponse": {"name": fc["name"], "response": {"result": result_text}}}
                )
            contents.append({"role": "user", "parts": response_parts})
            payload["contents"] = contents
            data = self._post(model_path, payload)

        return _extract_gemini_text(data)

    def extract_memories(self, existing: list, user_message: str, assistant_reply: str) -> dict:
        empty = {"new": [], "updates": []}
        try:
            existing_text = "\n".join(
                f"[{m['id']}] ({m.get('category') or 'general'}) {m['content']}" for m in existing
            ) or "(none saved yet)"
            prompt = (
                f"Memories saved so far:\n{existing_text}\n\n"
                f"Latest exchange:\nSudeep: {user_message}\nJARVIS: {assistant_reply}\n\n"
                "Return the JSON now."
            )
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "systemInstruction": {"parts": [{"text": MEMORY_EXTRACTION_SYSTEM_PROMPT}]},
            }
            data = self._post(f"models/{self.model}:generateContent", payload)
            raw = _extract_gemini_text(data).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            return {"new": parsed.get("new") or [], "updates": parsed.get("updates") or []}
        except Exception:
            # Same best-effort contract as every other provider's
            # extract_memories - never let a parsing hiccup or API error
            # surface to Sudeep.
            return empty

    def upload_document(self, filename: str, content: bytes, mime_type: str) -> str:
        raise ProviderCapabilityError("gemini", "upload_document (Knowledge Base)")

    def delete_document(self, file_id: str) -> None:
        raise ProviderCapabilityError("gemini", "delete_document (Knowledge Base)")

    def ask_about_documents(self, file_ids: list, question: str) -> str:
        raise ProviderCapabilityError("gemini", "ask_about_documents (Knowledge Base)")

    def learn_skill(self, topic: str) -> dict:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": f"Topic to learn: {topic}"}]}],
            "systemInstruction": {"parts": [{"text": SKILL_LEARNING_SYSTEM_PROMPT}]},
        }
        data = self._post(f"models/{self.model}:generateContent", payload)
        text = _extract_gemini_text(data)
        return _parse_skill_response(text, fallback_topic=topic)


def _groq_message(data: dict) -> dict:
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {"role": "assistant", "content": ""}


def _extract_groq_text(data: dict) -> str:
    message = _groq_message(data)
    text = (message.get("content") or "").strip()
    if text:
        return text
    finish_reason = None
    try:
        finish_reason = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        pass
    if finish_reason and finish_reason != "stop":
        return f"I couldn't come up with a reply that time (Groq stopped early: {finish_reason})."
    return "I couldn't come up with a reply that time - please try again."


class GroqProvider(AIProvider):
    """Groq adapter (added 2026-09-21, Multi-Model AI Brain upgrade - see
    progress-tracker.md). Groq's API is OpenAI-compatible (confirmed
    against console.groq.com/docs/api-reference 2026-09-21), so this talks
    to it via httpx as a direct chat/completions call rather than adding an
    SDK dependency - same reasoning as GeminiProvider.

    Model ID verified against console.groq.com/docs/models (the official
    current model list) on 2026-09-21 - openai/gpt-oss-120b is real,
    current, and listed as a Production model as of that check (500
    tokens/sec, $0.15/$0.60 per 1M input/output tokens), not blindly copied
    from the upgrade prompt that suggested it. openai/gpt-oss-20b
    (GROQ_FAST_MODEL) is also verified current/production but not yet used
    by any routing logic in this v1 (see config.py's own comment on
    groq_fast_model).

    KNOWN GAPS in this v1 (same reasoning as GeminiProvider): no web-search
    tool (Groq/gpt-oss has no native search grounding equivalent to
    Anthropic's web_search); SUPPORTS_DOCUMENTS = False (no Files-API
    equivalent - AIProviderManager always routes document methods to a
    provider that supports them instead)."""

    SUPPORTS_DOCUMENTS = False

    _BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, model: "str | None" = None):
        if not settings.groq_api_key:
            raise ProviderNotConfiguredError("groq", "GROQ_API_KEY is not set")
        self.api_key = settings.groq_api_key
        self.model = model or settings.groq_model
        self._client = httpx.Client(timeout=settings.ai_request_timeout_ms / 1000)

    def _post(self, payload: dict) -> dict:
        """Same classify-on-the-way-out pattern as GeminiProvider._post -
        see ProviderError's docstring."""
        try:
            resp = self._client.post(
                f"{self._BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as e:
            raise ProviderError("TIMEOUT", f"Groq request timed out: {e}", "groq") from e
        except httpx.ConnectError as e:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Couldn't reach Groq: {e}", "groq") from e
        except httpx.HTTPError as e:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Groq request failed: {e}", "groq") from e

        if resp.status_code in (401, 403):
            raise ProviderError(
                "AUTHENTICATION_ERROR",
                f"Groq authentication failed ({resp.status_code}): {resp.text[:300]}",
                "groq",
            )
        if resp.status_code == 429:
            raise ProviderError("RATE_LIMIT", f"Groq rate limit hit: {resp.text[:300]}", "groq")
        if resp.status_code >= 500:
            raise ProviderError(
                "SERVER_ERROR", f"Groq server error ({resp.status_code}): {resp.text[:300]}", "groq"
            )
        if resp.status_code == 404:
            raise ProviderError(
                "MODEL_UNAVAILABLE",
                f"Groq model \"{self.model}\" not found (404): {resp.text[:300]}",
                "groq",
            )
        if resp.status_code != 200:
            category = "INVALID_REQUEST" if resp.status_code == 400 else "SERVER_ERROR"
            raise ProviderError(
                category, f"Groq rejected the request ({resp.status_code}): {resp.text[:300]}", "groq"
            )
        return resp.json()

    def generate_reply(
        self,
        messages: list,
        memory_context=None,
        skill_context=None,
        tally_context=None,
        agent_context=None,
        allow_web_search=True,
        tally_daybook_query=None,
        tally_bill_payment_query=None,
        debug_read_errors=None,
        debug_list_files=None,
        debug_read_source=None,
        health_check_query=None,
        custom_tool_specs=None,
        custom_tool_invoke=None,
        consult_agent_query=None,
        consult_directory_context=None,
        create_automation_action=None,
        list_automations_action=None,
        cancel_automation_action=None,
        search_emails_query=None,
        read_email_query=None,
        draft_email_reply_query=None,
        list_calendar_events_query=None,
        find_open_slots_query=None,
    ) -> str:
        system_prompt = _build_system_prompt(
            memory_context, skill_context, tally_context, agent_context,
            consult_agent_query, consult_directory_context,
        )
        canonical_tools = _offered_tool_specs(
            tally_daybook_query=tally_daybook_query,
            tally_bill_payment_query=tally_bill_payment_query,
            debug_read_errors=debug_read_errors,
            debug_list_files=debug_list_files,
            debug_read_source=debug_read_source,
            health_check_query=health_check_query,
            custom_tool_specs=custom_tool_specs,
            custom_tool_invoke=custom_tool_invoke,
            consult_agent_query=consult_agent_query,
            create_automation_action=create_automation_action,
            list_automations_action=list_automations_action,
            cancel_automation_action=cancel_automation_action,
            search_emails_query=search_emails_query,
            read_email_query=read_email_query,
            draft_email_reply_query=draft_email_reply_query,
            list_calendar_events_query=list_calendar_events_query,
            find_open_slots_query=find_open_slots_query,
        )
        groq_tools = [_tool_schema_to_groq(t) for t in canonical_tools] or None

        chat_messages = [{"role": "system", "content": system_prompt}]
        chat_messages.extend({"role": m["role"], "content": m["content"]} for m in messages)

        payload = {"model": self.model, "messages": chat_messages}
        if groq_tools:
            payload["tools"] = groq_tools

        data = self._post(payload)

        any_client_tool = _any_client_tool_offered(
            tally_daybook_query=tally_daybook_query,
            tally_bill_payment_query=tally_bill_payment_query,
            debug_read_errors=debug_read_errors,
            debug_list_files=debug_list_files,
            debug_read_source=debug_read_source,
            health_check_query=health_check_query,
            custom_tool_specs=custom_tool_specs,
            custom_tool_invoke=custom_tool_invoke,
            consult_agent_query=consult_agent_query,
            create_automation_action=create_automation_action,
            list_automations_action=list_automations_action,
            cancel_automation_action=cancel_automation_action,
            search_emails_query=search_emails_query,
            read_email_query=read_email_query,
            draft_email_reply_query=draft_email_reply_query,
            list_calendar_events_query=list_calendar_events_query,
            find_open_slots_query=find_open_slots_query,
        )

        # Same shared dispatcher and 5-round cap as Anthropic/Gemini above -
        # Groq (OpenAI-compatible) surfaces a pending tool call as a
        # tool_calls entry on the assistant message, with its arguments as a
        # JSON-encoded STRING (not a dict, unlike Anthropic/Gemini - a real
        # OpenAI-API-format detail, not a JARVIS choice) that has to be
        # parsed before it reaches the shared dispatcher.
        rounds = 0
        while any_client_tool and rounds < 5:
            message = _groq_message(data)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                break
            rounds += 1
            chat_messages.append(message)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result_text = _dispatch_client_tool(
                    name,
                    args,
                    tally_daybook_query=tally_daybook_query,
                    tally_bill_payment_query=tally_bill_payment_query,
                    debug_read_errors=debug_read_errors,
                    debug_list_files=debug_list_files,
                    debug_read_source=debug_read_source,
                    health_check_query=health_check_query,
                    consult_agent_query=consult_agent_query,
                    create_automation_action=create_automation_action,
                    list_automations_action=list_automations_action,
                    cancel_automation_action=cancel_automation_action,
                    custom_tool_invoke=custom_tool_invoke,
                    search_emails_query=search_emails_query,
                    read_email_query=read_email_query,
                    draft_email_reply_query=draft_email_reply_query,
                    list_calendar_events_query=list_calendar_events_query,
                    find_open_slots_query=find_open_slots_query,
                )
                chat_messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id", ""), "content": result_text}
                )
            payload["messages"] = chat_messages
            data = self._post(payload)

        return _extract_groq_text(data)

    def extract_memories(self, existing: list, user_message: str, assistant_reply: str) -> dict:
        empty = {"new": [], "updates": []}
        try:
            existing_text = "\n".join(
                f"[{m['id']}] ({m.get('category') or 'general'}) {m['content']}" for m in existing
            ) or "(none saved yet)"
            prompt = (
                f"Memories saved so far:\n{existing_text}\n\n"
                f"Latest exchange:\nSudeep: {user_message}\nJARVIS: {assistant_reply}\n\n"
                "Return the JSON now."
            )
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": MEMORY_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }
            data = self._post(payload)
            raw = _extract_groq_text(data).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            return {"new": parsed.get("new") or [], "updates": parsed.get("updates") or []}
        except Exception:
            return empty

    def upload_document(self, filename: str, content: bytes, mime_type: str) -> str:
        raise ProviderCapabilityError("groq", "upload_document (Knowledge Base)")

    def delete_document(self, file_id: str) -> None:
        raise ProviderCapabilityError("groq", "delete_document (Knowledge Base)")

    def ask_about_documents(self, file_ids: list, question: str) -> str:
        raise ProviderCapabilityError("groq", "ask_about_documents (Knowledge Base)")

    def learn_skill(self, topic: str) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SKILL_LEARNING_SYSTEM_PROMPT},
                {"role": "user", "content": f"Topic to learn: {topic}"},
            ],
        }
        data = self._post(payload)
        text = _extract_groq_text(data)
        return _parse_skill_response(text, fallback_topic=topic)


# Provider-level failure categories worth trying the NEXT provider in the
# chain for (see AIProviderManager._should_fall_back_on). Deliberately does
# NOT include INVALID_REQUEST - a malformed request is JARVIS's own bug, not
# a provider outage, so trying the exact same bad request against a
# different provider would almost certainly fail there too, wasting a
# network round-trip and hiding the real bug behind a slower response (the
# master upgrade prompt's own explicit "classify errors before fallback"
# rule). Every other category here represents the provider itself being
# unavailable, overloaded, misconfigured, or temporarily broken - exactly
# the kind of failure a different provider is likely NOT experiencing at
# the same moment.
_ERROR_CATEGORIES_WORTH_FALLBACK = {
    "TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMIT",
    "QUOTA_EXCEEDED",
    "SERVER_ERROR",
    "AUTHENTICATION_ERROR",
    "MODEL_UNAVAILABLE",
}


class AIProviderManager(AIProvider):
    """Routes every AIProvider call through a configurable chain of
    providers with automatic fallback - the "Multi-Model AI Brain" upgrade
    (added 2026-09-21, scoped with Sudeep via AskUserQuestion after he
    pasted the 82-section master upgrade prompt - see progress-tracker.md).

    get_ai_provider() below returns one of these instead of a bare
    AnthropicProvider now. Every existing caller (chat_routes.py's
    send_message/run_agent_subquery/_update_memories, knowledge_routes.py,
    skill_routes.py, automation_engine.py) already just calls
    get_ai_provider() once per use and treats whatever comes back as "the
    AI provider" via the same AIProvider interface - confirmed by
    re-reading every get_ai_provider() call site in the codebase before
    writing this class - so this is a genuine drop-in replacement; no
    caller needed to change.

    Chain order comes from settings.ai_primary_provider/ai_secondary_
    provider/ai_tertiary_provider (env vars AI_PRIMARY_PROVIDER/AI_
    SECONDARY_PROVIDER/AI_TERTIARY_PROVIDER) - defaults to
    anthropic -> gemini -> groq, a DELIBERATE deviation from the master
    prompt's own suggested gemini-first default, per Sudeep's explicit
    choice ("keep Claude live for now" - every existing tool's anti-
    fabrication/anti-crash fix was live-tested specifically against
    Claude's behavior; each tool path should be individually re-verified
    against Gemini before it becomes the default - see progress-
    tracker.md). Flip AI_PRIMARY_PROVIDER=gemini in .env (and restart) to
    promote Gemini once ready - no code change needed.

    A provider whose API key isn't configured (ProviderNotConfiguredError)
    or that fails to construct for any other reason is left out of the
    chain rather than failing the whole app at startup - Sudeep can add
    Gemini/Groq keys whenever he's ready. If NO configured provider is left
    (e.g. every key removed, or all three genuinely down), generate_reply/
    learn_skill degrade to the friendly "temporarily unable to reach my AI
    providers" message the master upgrade prompt asks for (never a raw
    stack trace) instead of crashing the chat endpoint."""

    _PROVIDER_CLASSES = {
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
        "groq": GroqProvider,
    }

    _FRIENDLY_UNAVAILABLE_MESSAGE = (
        "I'm temporarily unable to reach my AI providers. Please try again shortly."
    )

    def __init__(self):
        chain_names = [
            settings.ai_primary_provider,
            settings.ai_secondary_provider,
            settings.ai_tertiary_provider,
        ]
        self.providers = []  # list[tuple[str, AIProvider]], in chain order
        self.not_configured = []  # provider names skipped for a missing API key
        self.init_errors = {}  # provider name -> str, any other construction failure
        seen = set()
        for name in chain_names:
            name = (name or "").strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            provider_cls = self._PROVIDER_CLASSES.get(name)
            if provider_cls is None:
                self.init_errors[name] = f"Unknown provider name in AI_*_PROVIDER config: \"{name}\""
                continue
            try:
                self.providers.append((name, provider_cls()))
            except ProviderNotConfiguredError:
                self.not_configured.append(name)
            except Exception as e:
                # A genuinely broken provider setup (e.g. a missing SDK
                # package) shouldn't take the whole app down at
                # construction time - it's just unavailable this call, same
                # as an unconfigured one, but worth its own diagnostic
                # message since it's a real setup problem, not just a
                # missing key.
                self.init_errors[name] = str(e)

    def _first_supporting_documents(self):
        for name, provider in self.providers:
            if getattr(provider, "SUPPORTS_DOCUMENTS", False):
                return name, provider
        return None, None

    def generate_reply(self, *args, **kwargs) -> str:
        if not self.providers:
            return self._FRIENDLY_UNAVAILABLE_MESSAGE
        last_exc = None
        for name, provider in self.providers:
            try:
                return provider.generate_reply(*args, **kwargs)
            except ProviderError as e:
                last_exc = e
                if e.category not in _ERROR_CATEGORIES_WORTH_FALLBACK:
                    # An application-level bug (JARVIS built a malformed
                    # request), not a provider outage - a different
                    # provider would almost certainly reject the identical
                    # bad request too, so raise immediately instead of
                    # wasting time and hiding the real bug behind more
                    # fallback attempts (the master prompt's own "classify
                    # errors before fallback" rule).
                    raise
                continue
            except Exception as e:
                # Any UNCLASSIFIED exception - notably AnthropicProvider's
                # own raw anthropic.* exceptions, since that class predates
                # this upgrade and was never changed to raise ProviderError
                # (see the "no rewrite of tested code" principle) - is
                # treated as fallback-worthy by default: an unclassified
                # failure from a provider adapter is far more likely to be
                # that provider having a bad moment than a JARVIS-side bug,
                # and trying the next configured provider costs little
                # compared to surfacing a raw error when a working fallback
                # was available.
                last_exc = e
                continue
        return self._FRIENDLY_UNAVAILABLE_MESSAGE

    def extract_memories(self, existing: list, user_message: str, assistant_reply: str) -> dict:
        empty = {"new": [], "updates": []}
        if not self.providers:
            return empty
        # Deliberately NOT a fallback loop across providers (unlike
        # generate_reply/learn_skill above) - extract_memories is
        # contracted to never raise (every provider already swallows its
        # own failures internally and returns empty - see each provider's
        # own extract_memories), so a primary-provider blip here just means
        # this one background pass quietly learns nothing new; it
        # self-heals on the very next message, since this runs after every
        # single reply. Keeping this off the fallback chain avoids a subtle
        # correctness trap: a provider that swallows its own errors can
        # never signal "I failed, try the next one" to begin with, so a
        # real fallback loop here would need a second, different error-
        # signaling contract just for this one best-effort background path -
        # not worth the complexity for background housekeeping (see this
        # class's own docstring on where fallback effort is best spent).
        _, provider = self.providers[0]
        return provider.extract_memories(existing, user_message, assistant_reply)

    def upload_document(self, filename: str, content: bytes, mime_type: str) -> str:
        _, provider = self._first_supporting_documents()
        if provider is None:
            raise RuntimeError(
                "No configured AI provider supports the Knowledge Base document "
                "feature yet (only Claude/Anthropic does in this version) - make "
                "sure an Anthropic API key is configured and Anthropic is "
                "somewhere in the AI_*_PROVIDER chain."
            )
        return provider.upload_document(filename, content, mime_type)

    def delete_document(self, file_id: str) -> None:
        _, provider = self._first_supporting_documents()
        if provider is None:
            # Matches AnthropicProvider.delete_document's own "never block
            # Sudeep from removing his own knowledge-base entry" contract -
            # if nothing configured can even reach the remote file to begin
            # with, that's equivalent to it already being gone from
            # Sudeep's point of view.
            return
        provider.delete_document(file_id)

    def ask_about_documents(self, file_ids: list, question: str) -> str:
        _, provider = self._first_supporting_documents()
        if provider is None:
            raise RuntimeError(
                "No configured AI provider supports the Knowledge Base document "
                "feature yet (only Claude/Anthropic does in this version)."
            )
        return provider.ask_about_documents(file_ids, question)

    def learn_skill(self, topic: str) -> dict:
        if not self.providers:
            raise RuntimeError(self._FRIENDLY_UNAVAILABLE_MESSAGE)
        last_exc = None
        for name, provider in self.providers:
            try:
                return provider.learn_skill(topic)
            except ProviderError as e:
                last_exc = e
                if e.category not in _ERROR_CATEGORIES_WORTH_FALLBACK:
                    raise
                continue
            except Exception as e:
                last_exc = e
                continue
        # Every configured provider failed - re-raise the last real error
        # (unlike generate_reply, learn_skill's own ABC contract says it
        # "should raise on genuine failure... the caller shows Sudeep a
        # clear error instead", so skill_routes.py already expects and
        # handles an exception here, unlike chat_routes.py's generate_reply
        # call).
        raise last_exc if last_exc is not None else RuntimeError(self._FRIENDLY_UNAVAILABLE_MESSAGE)


def get_ai_provider() -> AIProvider:
    """Returns the app-wide AI provider. As of the 2026-09-21 Multi-Model AI
    Brain upgrade, this is always an AIProviderManager wrapping the
    configured AI_PRIMARY_PROVIDER/AI_SECONDARY_PROVIDER/AI_TERTIARY_
    PROVIDER chain (default: anthropic -> gemini -> groq - see
    AIProviderManager's own docstring for why Claude stays first by
    default). The older, pre-upgrade AI_PROVIDER env var (settings.
    ai_provider) is now IGNORED for provider selection - kept only so an
    existing .env that still sets AI_PROVIDER=anthropic doesn't need to
    change, but it no longer controls anything; AI_PRIMARY_PROVIDER (etc.)
    is the real control now. A fresh AIProviderManager is constructed on
    every call - not a behavior change from before this upgrade, which
    already constructed a fresh AnthropicProvider on every call; no
    existing caller ever caches the result across requests."""
    return AIProviderManager()
