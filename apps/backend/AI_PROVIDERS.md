# AI Providers (Multi-Model AI Brain upgrade)

Added 2026-09-21. Sudeep pasted an 82-section "JARVIS MASTER UPGRADE
PROMPT - MULTI-MODEL AI BRAIN + SMART ROUTING + THINKING SYSTEM" asking
JARVIS to move from a single hard-wired Claude integration to a
multi-provider architecture (Gemini, Groq, Claude) with automatic
fallback, a task classifier, a model router, and a "thinking level"
system. Given the size of that spec, it was scoped down with Sudeep via
three clarifying questions before any code was written (see
progress-tracker.md for the full scoping conversation). This document
covers what was actually built in that v1 pass, and is explicit about
what was deliberately left out.

## v1 scope (Sudeep's choice)

1. **Provider abstraction + all three providers, with a real, working,
   configurable fallback chain.** This is what's documented below.
2. **API keys**: Sudeep has both a Gemini and a Groq key; they go in
   `.env` (never in chat or source files - see "Configuration" below).
3. **Claude stays the live default provider for now.** Every existing
   tool's anti-fabrication/anti-crash fix (Tally daybook, bill payment
   status, the automation NoneType crash, etc.) was live-tested
   specifically against Claude's behavior. Each tool path should be
   individually re-verified against Gemini before it's promoted to
   primary - see "Promoting Gemini to primary" below for how, once ready.

**Explicitly OUT of scope for this pass** (a deliberate deviation from
the master prompt, agreed with Sudeep up front, not an oversight):

- The Task Classifier / Model Router "smart routing by task type"
  system.
- The Thinking Level (LOW/MEDIUM/HIGH/AUTO) system and its per-provider
  translation layer.
- The admin AI Provider Dashboard / AI Settings UI (provider status,
  [Test Gemini]/[Test Groq]/[Test Claude] buttons, usage graphs).
- Structured per-request AI usage/cost logging and budget controls
  (`DAILY_AI_BUDGET`/`MONTHLY_AI_BUDGET`).
- An intra-provider retry loop (`AI_MAX_RETRIES` is accepted in config
  for forward-compatibility but not yet wired to anything - see
  "Retries vs fallback" below).

These are natural follow-ups once the core provider swap has been live
for a while and Sudeep wants to build further on top of it.

## Architecture

```
chat_routes.py / knowledge_routes.py / skill_routes.py / automation_engine.py
                              |
                       get_ai_provider()
                              |
                       AIProviderManager   <- reads AI_PRIMARY_PROVIDER /
                              |                AI_SECONDARY_PROVIDER /
              +---------------+---------------+  AI_TERTIARY_PROVIDER
              |               |               |
      AnthropicProvider  GeminiProvider  GroqProvider
       (anthropic SDK)     (httpx2)        (httpx2)
```

- `AIProvider` (`app/ai_provider.py`) is the existing abstract interface
  (predates this upgrade) - `generate_reply`, `extract_memories`,
  `upload_document`, `delete_document`, `ask_about_documents`,
  `learn_skill`. Every caller in the app already only ever talks to this
  interface, never to a concrete provider class directly.
- `AnthropicProvider` is **unchanged in behavior** - this upgrade
  refactored some of its inline logic into shared helper functions (see
  "Shared helpers" below) but did not touch what it actually does. It
  still talks to Claude via the official `anthropic` Python SDK.
- `GeminiProvider` and `GroqProvider` are new. Both talk to their
  provider's REST API directly via `httpx2` (imported in code as
  `import httpx2 as httpx`, so the rest of the file just says `httpx.*`) -
  no new dependency was added. **Correction, 2026-09-21**: this was
  originally written assuming the real `httpx` package was already
  installed; live-testing on Sudeep's actual machine surfaced
  `ModuleNotFoundError: No module named 'httpx'` - his real venv (and the
  `anthropic==1.5.0` SDK build installed there) uses `httpx2`, a separate,
  API-compatible package (same class/exception names), not a typo for
  `httpx`. Fixed by aliasing the import instead of adding a dependency, so
  Sudeep's Windows venv install surface is still unchanged.
- `AIProviderManager` is new. It wraps a configured chain of provider
  instances and implements the fallback logic. `get_ai_provider()` now
  always returns one of these instead of a bare `AnthropicProvider` -
  **every existing call site was re-read before this change and needs no
  modification**, since they already just call `get_ai_provider()` once
  and use whatever comes back through the same `AIProvider` interface.

### Shared helpers (why AnthropicProvider's code changed at all)

To give Gemini and Groq the exact same real, live-tested safety behavior
Claude already had - especially the 2026-09-21 fix for the
`'NoneType' object is not callable'` crash and the current-time-grounding
fix (also 2026-09-21) - four pieces of logic that used to live only
inline inside `AnthropicProvider.generate_reply` were extracted into
shared functions, and `AnthropicProvider` was changed to call them
instead of containing the logic directly:

- `_dispatch_client_tool` - the "which client-side tool, or none" decision
  for one tool call, including the honest "not available in this
  conversation" message for a real-but-withheld tool name.
- `_offered_tool_specs` / `_any_client_tool_offered` - which tools to
  offer this call, purely from which callables are non-`None`.
- `_build_system_prompt` - memory/skill/tally/agent-persona context
  folding plus the current-date/time grounding block.

This was a **behavior-preserving refactor, not a rewrite**: every
condition, argument, and error message is unchanged from before. It was
verified safe by re-running the full existing test suite (748 checks
across 21 files) immediately after the refactor, before writing any new
provider code on top of it - all 748 still passed, zero regressions.

## Fallback logic

`AIProviderManager.generate_reply` (and `.learn_skill`) tries each
configured provider in chain order. On success, it returns immediately -
**the normal case is exactly one provider handling one request**, never
multiple providers queried in parallel (per the master prompt's own
explicit cost-control rule: never fan a request out to every provider,
never duplicate work).

On failure, the error is classified before deciding whether to try the
next provider:

- **Worth falling back on**: `TIMEOUT`, `PROVIDER_UNAVAILABLE`,
  `RATE_LIMIT`, `QUOTA_EXCEEDED`, `SERVER_ERROR`, `AUTHENTICATION_ERROR`,
  `MODEL_UNAVAILABLE` - these represent the provider itself being down,
  overloaded, misconfigured, or temporarily broken, which a different
  provider is likely not experiencing at the same moment.
- **NOT worth falling back on**: `INVALID_REQUEST` - a malformed request
  is JARVIS's own bug, not a provider outage. Trying the identical bad
  request against a different provider would almost certainly fail there
  too, wasting time and hiding the real bug. This raises immediately
  instead.
- Any **unclassified** exception (in practice, `AnthropicProvider`'s own
  raw `anthropic.*` exceptions, since that class wasn't changed to raise
  the new `ProviderError` type) is treated as fallback-worthy by default -
  an unclassified failure is far more likely to be that provider having a
  bad moment than a JARVIS-side bug.

If every configured provider fails, `generate_reply` returns the friendly
message `"I'm temporarily unable to reach my AI providers. Please try
again shortly."` **as a normal return value, not a raised exception** -
this flows straight through to the chat reply and gets saved to
conversation history like any other reply, exactly matching the master
prompt's own explicit requirement to never show Sudeep a raw stack trace.
`learn_skill` still raises on total failure (unlike `generate_reply`),
matching its pre-existing "the caller shows a clear error" contract in
`skill_routes.py`.

### Retries vs fallback

The master prompt's suggested `AI_MAX_RETRIES=1` / exponential-backoff
retry policy is **not** implemented as a separate intra-provider retry
loop in this v1. Trying the *next configured provider* on a failure is a
stronger recovery than retrying the *same* failing provider again, so v1
deliberately doesn't add a second retry layer on top of the fallback
chain. `AI_MAX_RETRIES`/`AI_REQUEST_TIMEOUT_MS` are accepted in config for
forward-compatibility with the master prompt's suggested env var names;
`AI_REQUEST_TIMEOUT_MS` **is** used directly as the `httpx2` (imported as `httpx`) client
timeout for `GeminiProvider`/`GroqProvider`.

## Capability routing (Knowledge Base documents)

Sudeep's existing Knowledge Base feature (Phase 10 - upload a PDF/Word/
Excel/text file, ask questions about it) is built around Anthropic's own
Files API, down to the database column name
(`Document.anthropic_file_id`). Neither `GeminiProvider` nor
`GroqProvider` implement `upload_document`/`delete_document`/
`ask_about_documents` in this v1 (each has a class attribute
`SUPPORTS_DOCUMENTS = False`; calling one of these three methods on them
raises `ProviderCapabilityError`).

`AIProviderManager` never routes these three calls through the fallback
chain in provider order - instead it always picks the **first configured
provider whose `SUPPORTS_DOCUMENTS` is `True`**, regardless of whether
that provider is primary/secondary/tertiary. With Claude configured
anywhere in the chain (the default), Knowledge Base keeps working exactly
as before, even after Gemini is promoted to primary for everyday chat.
If no configured provider supports documents, `upload_document`/
`ask_about_documents` raise a clear `RuntimeError` explaining why (and
`delete_document` quietly no-ops, matching its pre-existing "never block
Sudeep from removing his own entry" contract).

`extract_memories` (the background best-effort memory pass that runs
after every reply) is **not** on the fallback chain at all - it always
uses only the primary provider. It's contracted to never raise (every
provider swallows its own failures internally and returns empty), so a
primary-provider blip there just means one background pass quietly learns
nothing new; it self-heals on the very next message. See
`AIProviderManager.extract_memories`'s own docstring for the full
reasoning.

## Tool-schema translation

Every client-side tool in this codebase (the built-in Tally/debug/
health-check/consult/automation tools, and every dynamically-built
`custom_tools.build_tool_schema` tool) is a flat JSON Schema object -
`{"name", "description", "input_schema": {"type", "properties", ...,
"required"}}` - with no `$ref`/`oneOf`/`anyOf`/nested unions (confirmed
by inspection before writing the translators). Both Gemini and Groq
accept this same flat shape almost verbatim:

- `_tool_schema_to_gemini` renames `input_schema` -> `parameters` and
  wraps it as one entry in a `functionDeclarations` list.
- `_tool_schema_to_groq` wraps it as OpenAI's
  `{"type": "function", "function": {"name", "description",
  "parameters"}}` shape (Groq's API is a direct OpenAI-compatible
  drop-in).

A new client-side tool only ever needs to be added once, in its native
Anthropic-shaped schema dict - both translators pick it up automatically
through `_offered_tool_specs`.

## Known gaps / not yet built (documented, not silent)

- **No web-search-grounding tool for Gemini or Groq.** Anthropic's
  server-side `web_search_20250305` tool has no equivalent wired up for
  either new provider in this v1 (Gemini has its own `googleSearch` tool
  with a different shape; Groq/gpt-oss has no native search). A real
  future addition, not implemented here.
- **Knowledge Base documents are Claude-only** (see "Capability routing"
  above).
- **No Thinking Level system, Task Classifier, or Model Router** - every
  request goes to whichever provider is next in the fixed
  primary/secondary/tertiary chain; there's no per-request routing by
  task complexity yet.
- **No admin dashboard** for provider health/usage/testing.
- **No per-request usage/cost tracking or budget controls.**
- **AnthropicProvider's own exceptions aren't classified** into the
  normalized error categories (`ProviderError`) the way Gemini/Groq's
  are - `AnthropicProvider` was deliberately left untouched to avoid
  risking its already-tested, already-live-verified behavior; its raw
  exceptions are treated as fallback-worthy by default instead (see
  "Fallback logic" above). A future pass could classify these too for
  more precise fallback decisions.

## Model verification (2026-09-21)

Per the master prompt's own explicit, high-priority requirement ("VERIFY
CURRENT MODEL AVAILABILITY... this requirement takes priority over the
example model IDs above"), the suggested model IDs were **not** trusted
blindly - each was checked against that provider's own official current
documentation before being set as a default:

| Provider | Model ID | Verified against |
|---|---|---|
| Gemini | `gemini-3.8-flash` | ai.google.dev/gemini-api/docs/models (listed current, production-available) |
| Groq | `openai/gpt-oss-120b` | console.groq.com/docs/models (listed under "Production Models") |
| Groq (fast, not yet routed to) | `openai/gpt-oss-20b` | console.groq.com/docs/models (listed under "Production Models") |
| Claude | `claude-sonnet-4-5-20250929` (unchanged - see note) | Pre-existing config, not modified by this upgrade |

All three of the master prompt's suggested Gemini/Groq IDs turned out to
be accurate as of this check - they weren't changed, but they also
weren't assumed correct without independently checking. The existing
`ANTHROPIC_MODEL` default was **not** changed as part of this upgrade
(out of scope - it's Claude's own already-configured, already-tested
model, untouched here); Sudeep can update it separately whenever he wants
to move to a newer Claude model ID.

## Configuration

Add these to your `.env` file (see `.env.example` in the project root for
the full template with every setting, not just these). **Never** paste
real API keys into chat or into any source file - they belong only in
`.env`, which JARVIS reads automatically and which is never shared.

```
AI_PRIMARY_PROVIDER=anthropic
AI_SECONDARY_PROVIDER=gemini
AI_TERTIARY_PROVIDER=groq

GEMINI_API_KEY=<your real Gemini key from https://aistudio.google.com/apikey>
GEMINI_MODEL=gemini-3.8-flash

GROQ_API_KEY=<your real Groq key from https://console.groq.com/keys>
GROQ_MODEL=openai/gpt-oss-120b
GROQ_FAST_MODEL=openai/gpt-oss-20b
```

`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` are unchanged - if they're already
set, nothing else needs to change for JARVIS to keep working exactly as
it does today, even before Gemini/Groq keys are added (an unconfigured
provider is automatically skipped in the chain, not a startup failure).

## Promoting Gemini (or Groq) to primary

Once Gemini's own behavior has been individually re-verified against
each tool path Sudeep cares about (Tally daybook/bill-payment accuracy,
automation scheduling, debugging tools, etc. - the same "verify before
trusting" standard already used everywhere else in this project), switch
the live default with a single `.env` change and a backend restart:

```
AI_PRIMARY_PROVIDER=gemini
AI_SECONDARY_PROVIDER=anthropic
AI_TERTIARY_PROVIDER=groq
```

No code change is required. Claude stays in the chain as a fallback (and
keeps handling Knowledge Base documents regardless of chain position -
see "Capability routing" above).

## Disabling a provider

Leave its API key blank (or remove it) in `.env` - `AIProviderManager`
detects the missing key at construction time and simply leaves that
provider out of the chain, without needing to also change the
`AI_*_PROVIDER` ordering.

## Adding a future provider (e.g. OpenAI, Mistral, a local Ollama model)

1. Write a new class implementing the full `AIProvider` interface (see
   `GeminiProvider`/`GroqProvider` for the current pattern: use `httpx2` (via `import httpx2 as httpx`)
   directly rather than adding an SDK dependency where reasonably
   possible; set `SUPPORTS_DOCUMENTS` accordingly; raise
   `ProviderNotConfiguredError` in `__init__` if its API key isn't set;
   classify failures into `ProviderError` with the normalized categories
   this file's "Fallback logic" section lists).
2. Add it to `AIProviderManager._PROVIDER_CLASSES`.
3. Add its config fields to `app/config.py` and `.env.example`, following
   the existing pattern.
4. No other file needs to change - every caller already goes through
   `get_ai_provider()` / `AIProviderManager`.

## Troubleshooting

- **"I'm temporarily unable to reach my AI providers"** in chat means
  every configured provider in the chain failed for this one request.
  Check `AIProviderManager.not_configured`/`init_errors` (currently only
  inspectable via a debugger or a quick script - there's no admin UI for
  this yet, see "Known gaps" above) or the backend's own console output
  for the underlying `ProviderError`/exception from each attempt.
- **A provider never gets tried at all**: check it's actually spelled
  correctly in `AI_PRIMARY_PROVIDER`/`AI_SECONDARY_PROVIDER`/
  `AI_TERTIARY_PROVIDER` (`anthropic`/`gemini`/`groq`, case-insensitive)
  and that its API key is set in `.env`.
- **Knowledge Base upload/ask fails with "No configured AI provider
  supports the Knowledge Base document feature"**: make sure an Anthropic
  API key is configured and `anthropic` is somewhere in the
  `AI_*_PROVIDER` chain (it doesn't need to be primary).
