const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("jarvis_token");
}

export function setToken(token: string) {
  localStorage.setItem("jarvis_token", token);
}

export function clearToken() {
  localStorage.removeItem("jarvis_token");
}

async function request(path: string, options: RequestInit = {}) {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  if (!res.ok) {
    let detail = "Something went wrong";
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      // response wasn't JSON, ignore
    }
    throw new Error(detail);
  }

  return res.json();
}

export function registerUser(email: string, password: string, fullName: string) {
  // Only works when JARVIS has zero accounts yet (fresh install / bootstrap).
  // Once an owner account exists, the backend requires addUser() (authenticated) instead.
  return request("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, full_name: fullName }),
  });
}

export function addUser(email: string, password: string, fullName: string) {
  // Create an additional account while logged in — e.g. adding a family member.
  return request("/api/auth/register", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ email, password, full_name: fullName }),
  });
}

export function loginUser(email: string, password: string) {
  return request("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function getCurrentUser() {
  return request("/api/auth/me", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function sendChatMessage(
  message: string,
  conversationId: number | null,
  agentId?: number | null
) {
  // Agent Factory v1: agentId only matters when conversationId is null (i.e.
  // starting a brand-new conversation) — once a conversation exists, its own
  // agent (if any) is fixed server-side and this is ignored (see
  // schemas.py's ChatMessageIn / chat_routes.py).
  return request("/api/chat/message", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
      agent_id: agentId ?? null,
    }),
  });
}

// Phase 9 (memory): view/edit/delete what JARVIS remembers about you.
// Saving happens automatically after chat replies (see the backend) — these
// calls are for the Memory page, not the chat flow itself.
export function listMemories() {
  return request("/api/memory", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function createMemory(content: string, category: string | null) {
  return request("/api/memory", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ content, category }),
  });
}

export function updateMemory(id: number, content: string, category: string | null) {
  return request(`/api/memory/${id}`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ content, category }),
  });
}

export function deleteMemory(id: number) {
  return request(`/api/memory/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

// Phase 10 (knowledge base): upload/list/delete documents, and ask a one-off
// question grounded in selected document(s). Asking is intentionally NOT part
// of the regular chat flow (see the backend) — it's its own call so a big
// document doesn't get re-sent, and re-billed, on every later unrelated chat
// message in that conversation.
export async function uploadDocument(file: File) {
  const formData = new FormData();
  formData.append("file", file);
  // Deliberately not using request() here — it defaults to a JSON
  // Content-Type, but a multipart upload needs the browser to set its own
  // Content-Type (with the multipart boundary) from the FormData body.
  const res = await fetch(`${API_URL}/api/knowledge`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: formData,
  });
  if (!res.ok) {
    let detail = "Upload failed";
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      // response wasn't JSON, ignore
    }
    throw new Error(detail);
  }
  return res.json();
}

export function listDocuments() {
  return request("/api/knowledge", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function deleteDocument(id: number) {
  return request(`/api/knowledge/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function askAboutDocuments(documentIds: number[], question: string) {
  return request("/api/knowledge/ask", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ document_ids: documentIds, question }),
  });
}

// Phase 11 (Universal Skill Engine, v1 — knowledge skills): ask JARVIS to
// learn a topic, review what it researched, then approve or discard it.
// Only approved (active) skills get used in chat — see the backend.
export function learnSkill(topic: string) {
  return request("/api/skills/learn", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ topic }),
  });
}

export function listSkills() {
  return request("/api/skills", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function approveSkill(id: number) {
  return request(`/api/skills/${id}/approve`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function deleteSkill(id: number) {
  return request(`/api/skills/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

// Custom Tally billing feature (not one of the charter's 29 numbered phases —
// see progress-tracker.md): JARVIS drafts a bill in conversation, the chat
// endpoint returns it as tally_draft (see sendChatMessage's response), and
// the chat page shows a review card with a "Send to Tally" button that calls
// createTallyBill — nothing is written to Sudeep's real books until then.
export type TallyLineItem = { description: string; amount: number };
export type TallyTaxLine = { ledger: string; amount: number };
export type TallyBillDraft = {
  party_name: string;
  items: TallyLineItem[];
  // tax_lines is a manual-override escape hatch; the normal path is
  // gst_rate + tax_type, which the backend turns into real tax_lines itself
  // (see progress-tracker.md) — by the time a draft reaches the frontend,
  // tax_lines is already the real computed preview either way.
  tax_lines: TallyTaxLine[];
  gst_rate: number | null;
  tax_type: string | null; // "interstate" (IGST) or "intrastate" (CGST+SGST)
  voucher_date: string;
  narration: string | null;
  // Sudeep's own field names: Buyer's Order No. = Work Order No, Other
  // References = Complaint No, Destination = the specific site/pump name.
  buyer_order_no: string | null;
  other_reference_no: string | null;
  destination: string | null;
};

export function getTallyStatus() {
  return request("/api/tally/status", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function createTallyBill(draft: TallyBillDraft) {
  return request("/api/tally/create-bill", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(draft),
  });
}

export function listTallyInvoices() {
  return request("/api/tally/invoices", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

// Code execution (Phase 12 "Coding Agent" — added 2026-09-20). The backend
// endpoint (/code/execute) and the system prompt teaching JARVIS to write
// fenced code blocks already existed, but NO client actually rendered a
// "Run Code" button or called this endpoint — the feature was invisible in
// every app (web/desktop/mobile). This is the first real wiring for it; see
// progress-tracker.md for the full Phase 12 audit (also a real sandboxing
// security fix on the backend, done the same session).
export type CodeExecuteResult = {
  success: boolean;
  output: string | null;
  error: string | null;
  execution_time: number;
};

export function executeCode(language: string, code: string): Promise<CodeExecuteResult> {
  return request("/code/execute", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ language, code }),
  });
}

// Agent Factory v1 (custom feature, not one of the charter's 29 numbered
// phases — see progress-tracker.md): named, reusable personas layered on
// JARVIS's existing chat engine, with a restricted toolbox (web search,
// Tally billing, a subset of already-approved Skills) enforced server-side.
export type AgentOut = {
  id: number;
  name: string;
  role_description: string;
  system_instructions: string | null;
  allow_web_search: boolean;
  allow_tally_billing: boolean;
  // Phase 19 "Email + Calendar" — off by default, same opt-in reasoning as
  // allow_tally_billing (see the backend's Agent model docstring).
  allow_email_calendar: boolean;
  assigned_skill_ids: number[];
  assigned_custom_tool_ids: number[];
  status: "active" | "paused";
  created_at: string;
  updated_at: string;
};

export type AgentCreateInput = {
  name: string;
  role_description: string;
  system_instructions?: string | null;
  allow_web_search?: boolean;
  allow_tally_billing?: boolean;
  allow_email_calendar?: boolean;
  assigned_skill_ids?: number[];
  assigned_custom_tool_ids?: number[];
};

export type AgentUpdateInput = Partial<AgentCreateInput>;

export function createAgent(input: AgentCreateInput): Promise<AgentOut> {
  return request("/api/agents", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(input),
  });
}

export function listAgents(): Promise<AgentOut[]> {
  return request("/api/agents", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function getAgent(id: number): Promise<AgentOut> {
  return request(`/api/agents/${id}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function updateAgent(id: number, input: AgentUpdateInput): Promise<AgentOut> {
  return request(`/api/agents/${id}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(input),
  });
}

export function pauseAgent(id: number): Promise<AgentOut> {
  return request(`/api/agents/${id}/pause`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function resumeAgent(id: number): Promise<AgentOut> {
  return request(`/api/agents/${id}/resume`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function deleteAgent(id: number) {
  return request(`/api/agents/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

// Phase 16 "Tool/plugin architecture" (added 2026-09-20 — see
// progress-tracker.md): tools Sudeep defines himself (a name, description,
// URL, method, parameters, optional API key) that any agent can be
// assigned. auth_value is write-only — never returned by the API, see
// has_auth_value instead.
export type CustomToolParam = { name: string; description: string; required: boolean };

export type CustomToolOut = {
  id: number;
  name: string;
  description: string;
  http_method: string;
  url: string;
  param_schema: CustomToolParam[];
  static_headers: Record<string, string>;
  auth_header_name: string | null;
  has_auth_value: boolean;
  is_read_only: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type CustomToolCreateInput = {
  name: string;
  description: string;
  http_method?: string;
  url: string;
  param_schema?: CustomToolParam[];
  static_headers?: Record<string, string>;
  auth_header_name?: string | null;
  auth_value?: string | null;
  is_read_only?: boolean;
};

export type CustomToolUpdateInput = Partial<CustomToolCreateInput> & {
  clear_auth?: boolean;
  enabled?: boolean;
};

export function createTool(input: CustomToolCreateInput): Promise<CustomToolOut> {
  return request("/api/tools", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(input),
  });
}

export function listTools(): Promise<CustomToolOut[]> {
  return request("/api/tools", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function updateTool(id: number, input: CustomToolUpdateInput): Promise<CustomToolOut> {
  return request(`/api/tools/${id}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(input),
  });
}

export function deleteTool(id: number) {
  return request(`/api/tools/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

// Phase 19 "Email + Calendar" (added 2026-09-22 — see progress-tracker.md
// and app/google_client.py's docstring for the full design). Reading email
// and viewing/finding-slots on the calendar happen entirely through chat
// (JARVIS calls the tools itself — nothing to wire up here beyond the chat
// endpoint already returning calendar_draft, below). This section is only
// for what genuinely needs its own page/button: the one-time OAuth connect
// flow (a real browser round-trip through Google's consent screen) and the
// "Create Event" button on a [CALENDAR_EVENT_DRAFT] review card — creating a
// REAL calendar event is externally visible, so it's never fired from a
// single chat tool call, exactly like createTallyBill's own "Send to Tally"
// button above.
export type GoogleStatus = {
  connected: boolean;
  google_email: string | null;
  connected_at: string | null;
  scopes: string[];
};

export type CalendarEventDraft = {
  summary: string;
  start_iso: string;
  end_iso: string;
  description: string | null;
  location: string | null;
  reminder_minutes_before?: number | null;
  use_default_reminder?: boolean;
};

export type CalendarEventResult = {
  success: boolean;
  message: string;
  event_id: string | null;
  html_link: string | null;
};

export function getGoogleStatus(): Promise<GoogleStatus> {
  return request("/api/google/status", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function getGoogleAuthUrl(): Promise<{ auth_url: string }> {
  return request("/api/google/oauth/start", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function disconnectGoogle() {
  return request("/api/google/disconnect", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function createCalendarEvent(draft: CalendarEventDraft): Promise<CalendarEventResult> {
  return request("/api/google/calendar/create-event", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(draft),
  });
}

export type OutboundCallDraft = {
  to_number: string;
  purpose: string;
  opening_message: string;
};

export type OutboundCallResult = {
  success: boolean;
  call_sid: string;
  status: string;
  message: string;
  conversation_id: number | null;
};

export function placeOutboundCall(draft: OutboundCallDraft): Promise<OutboundCallResult> {
  return request("/api/phone/outbound-call", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(draft),
  });
}

// Phase 20 incoming phone notifications.
export type PhoneCallRecord = {
  id: number;
  conversation_id: number;
  caller_number: string;
  caller_name: string | null;
  reason: string | null;
  preferred_callback_time: string | null;
  callback_requested: boolean;
  appointment_requested: boolean;
  appointment_summary: string | null;
  appointment_start_iso: string | null;
  appointment_location: string | null;
  is_read: boolean;
  created_at: string;
  updated_at: string;
};

export function listUnreadPhoneCalls(): Promise<PhoneCallRecord[]> {
  return request("/api/phone/records?unread_only=true", {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

export function markPhoneCallRead(id: number): Promise<PhoneCallRecord> {
  return request(`/api/phone/records/${id}/read`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
}

