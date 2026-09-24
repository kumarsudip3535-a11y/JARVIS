"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  clearToken,
  sendChatMessage,
  getCurrentUser,
  addUser,
  createTallyBill,
  createCalendarEvent,
  getAgent,
  executeCode,
  listUnreadPhoneCalls,
  markPhoneCallRead,
  placeOutboundCall,
  type OutboundCallDraft,
  type PhoneCallRecord,
  type TallyBillDraft,
  type CalendarEventDraft,
  type CodeExecuteResult,
} from "@/lib/api";

// Custom Tally billing feature: a chat reply can carry a drafted bill
// (see sendChatMessage's response / chat_routes.py). "idle" means the
// review card is showing and awaiting Sudeep's decision; "sending" while
// the create-bill call is in flight; "success"/"error" once it resolves.
type TallyCardStatus = "idle" | "sending" | "success" | "error";

// Phase 19 "Email + Calendar": same review-card pattern as the Tally draft
// above, for a [CALENDAR_EVENT_DRAFT] block — "idle" while awaiting Sudeep's
// decision, "sending" while the create-event call is in flight,
// "success"/"error" once it resolves. Nothing is added to his real calendar
// until he presses the button.
type CalendarCardStatus = "idle" | "sending" | "success" | "error";

type OutboundCallCardStatus = "idle" | "sending" | "success" | "error";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  tallyDraft?: TallyBillDraft;
  tallyStatus?: TallyCardStatus;
  tallyResultMessage?: string;
  calendarDraft?: CalendarEventDraft;
  calendarStatus?: CalendarCardStatus;
  calendarResultMessage?: string;
  outboundCallDraft?: OutboundCallDraft;
  outboundCallStatus?: OutboundCallCardStatus;
  outboundCallResultMessage?: string;
};

function tallyDraftTotal(draft: TallyBillDraft): number {
  const itemsTotal = draft.items.reduce((sum, item) => sum + item.amount, 0);
  const taxTotal = draft.tax_lines.reduce((sum, t) => sum + t.amount, 0);
  return itemsTotal + taxTotal;
}

function formatRupees(amount: number): string {
  return `₹${amount.toLocaleString("en-IN")}`;
}

// Phase 19 "Email + Calendar": start_iso/end_iso are real RFC3339 timestamps
// (e.g. "2026-09-25T15:00:00+05:30") — parsed and displayed in the browser's
// own locale/timezone rather than shown as raw ISO text, same reasoning as
// formatRupees above (never make Sudeep read a machine format). Falls back
// to the raw string if it's somehow unparseable, rather than showing "Invalid Date".
function formatEventTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// Code execution (Phase 12 "Coding Agent" — added 2026-09-20). The backend
// /code/execute endpoint and the system prompt telling JARVIS to write
// fenced code blocks already existed before this, but no client rendered a
// "Run Code" button or called it — the feature was completely invisible.
// This parses ```language / ``` fences out of a reply so each one can be
// shown as a real code block with a Run Code button beneath it, instead of
// literal backtick text inline in the bubble. Only python/javascript/sql
// are runnable (the only languages /code/execute supports, per
// code_executor.py); any other fenced language still renders as a
// formatted code block, just without a Run button.
const RUNNABLE_LANGUAGES = new Set(["python", "javascript", "js", "sql"]);

type MessageSegment =
  | { type: "text"; text: string }
  | { type: "code"; language: string; code: string };

function parseMessageSegments(content: string): MessageSegment[] {
  const segments: MessageSegment[] = [];
  const fenceRe = /```(\w+)?\n?([\s\S]*?)```/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = fenceRe.exec(content)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: "text", text: content.slice(lastIndex, match.index) });
    }
    const language = (match[1] || "").toLowerCase().trim();
    const code = match[2].replace(/\n$/, "");
    segments.push({ type: "code", language, code });
    lastIndex = fenceRe.lastIndex;
  }
  if (lastIndex < content.length) {
    segments.push({ type: "text", text: content.slice(lastIndex) });
  }
  return segments;
}

// /code/execute only accepts "python"/"javascript"/"sql" (code_executor.py)
// — normalize the common "js" fence alias so it's still runnable.
function normalizeRunnableLanguage(language: string): string {
  return language === "js" ? "javascript" : language;
}

type CodeRunState = {
  status: "idle" | "running" | "done";
  result?: CodeExecuteResult;
  error?: string;
};

type VoiceStatus = "idle" | "listening" | "thinking" | "speaking";

// Minimal ambient typings so TypeScript doesn't complain about the
// browser-only, non-standard Web Speech API.
type SpeechRecognitionAlternativeLike = { transcript: string };
type SpeechRecognitionResultLike = {
  [index: number]: SpeechRecognitionAlternativeLike;
  length: number;
  isFinal: boolean;
};
type SpeechRecognitionEventLike = {
  resultIndex: number;
  results: {
    [index: number]: SpeechRecognitionResultLike;
    length: number;
  };
};
type SpeechRecognitionLike = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  start: () => void;
  stop: () => void;
};

export default function ChatPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [userEmail, setUserEmail] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  // Code execution (Phase 12) — keyed by "<messageIndex>-<blockIndex>" since
  // a single reply can contain more than one fenced code block, each run
  // independently rather than sharing one status like tallyStatus above.
  const [codeRuns, setCodeRuns] = useState<Record<string, CodeRunState>>({});
  // Phase 20: unread incoming calls/callback requests, refreshed while
  // JARVIS is open so a completed phone call appears without asking in chat.
  const [phoneRecords, setPhoneRecords] = useState<PhoneCallRecord[]>([]);

  // Agent Factory v1 (custom feature, see progress-tracker.md): chatting via
  // /chat?agent=<id> starts (or continues) a conversation with a named
  // agent's persona/restricted toolbox instead of general JARVIS. Read
  // directly from window.location rather than useSearchParams() so this
  // page doesn't need a Suspense boundary. agentId is only actually sent to
  // the backend when starting a brand-new conversation (see sendChatMessage
  // in lib/api.ts) — once a conversation exists its agent is fixed
  // server-side, and agentName/agentId below are kept in sync from each
  // reply's agent_id/agent_name instead.
  const [agentId, setAgentId] = useState<number | null>(null);
  const [agentName, setAgentName] = useState<string | null>(null);

  // Voice mode: a hands-free loop of listen -> send -> speak -> listen again.
  // Recognition runs continuously for the whole voice-chat session (not just
  // per-turn) so that if you start talking while JARVIS is still speaking,
  // we can hear it immediately and cut JARVIS off (barge-in).
  const [voiceSupported, setVoiceSupported] = useState(false);
  const [voiceBlockedReason, setVoiceBlockedReason] = useState("");
  const [voiceMode, setVoiceMode] = useState(false);
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>("idle");
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // Refs mirroring state so callbacks registered once (onresult/onend) always
  // see the latest value instead of a stale closure from when they were set.
  const voiceModeRef = useRef(false);
  const voiceStatusRef = useRef<VoiceStatus>("idle");
  const conversationIdRef = useRef<number | null>(null);
  // The browser's own "isFinal" flag is unreliable in continuous mode — it can
  // simply never arrive. pendingTranscriptRef + silenceTimerRef let us decide
  // for ourselves that you've stopped talking (no new words for ~1.2s) instead
  // of waiting forever for a signal that might not come.
  const pendingTranscriptRef = useRef("");
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Tracks the last time recognition actually reported anything, so a watchdog
  // can notice if it has silently died (a known Chrome quirk in continuous
  // mode: no error, no "end" event, it just stops picking up audio) and force
  // a restart instead of leaving the UI stuck on "Listening...".
  const lastActivityRef = useRef(Date.now());
  // Whether the mic stays live while JARVIS is talking, so you can interrupt
  // it mid-sentence. OFF by default: on a laptop/phone speaker (no headphones),
  // the mic picks up JARVIS's own voice through the speaker and mistakes it
  // for you talking — which makes JARVIS reply to itself in an endless loop.
  // Only turn this on if you're using headphones/earbuds, which remove that
  // feedback path entirely.
  const [allowBargeIn, setAllowBargeInState] = useState(false);
  const allowBargeInRef = useRef(false);
  function setAllowBargeIn(value: boolean) {
    allowBargeInRef.current = value;
    setAllowBargeInState(value);
  }

  // Add user panel
  const [showAddUser, setShowAddUser] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newFullName, setNewFullName] = useState("");
  const [addUserBusy, setAddUserBusy] = useState(false);
  const [addUserError, setAddUserError] = useState("");
  const [addUserSuccess, setAddUserSuccess] = useState("");

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    getCurrentUser()
      .then((user) => {
        setUserEmail(user.email);
        setChecking(false);
      })
      .catch(() => {
        clearToken();
        router.replace("/login");
      });
  }, [router]);

  useEffect(() => {
    if (checking || !getToken()) return;
    let active = true;

    const refreshPhoneRecords = () => {
      listUnreadPhoneCalls()
        .then((records) => {
          if (active) setPhoneRecords(records);
        })
        .catch(() => {
          // Phone notifications are helpful but must never block chat.
        });
    };

    refreshPhoneRecords();
    const timer = window.setInterval(refreshPhoneRecords, 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [checking]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const raw = new URLSearchParams(window.location.search).get("agent");
    if (!raw) return;
    const id = parseInt(raw, 10);
    if (Number.isNaN(id)) return;
    setAgentId(id);
    getAgent(id)
      .then((agent) => setAgentName(agent.name))
      .catch(() => {
        // Agent doesn't exist / isn't yours anymore — fall back to a normal
        // general-JARVIS chat rather than failing the whole page.
        setAgentId(null);
        setAgentName(null);
      });
  }, []);

  function leaveAgentChat() {
    setAgentId(null);
    setAgentName(null);
    setConversationId(null);
    setMessages([]);
    router.replace("/chat");
  }

  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    voiceStatusRef.current = voiceStatus;
  }, [voiceStatus]);

  // Sets React state (for the UI) and the ref (for the recognition callbacks,
  // which are registered once and would otherwise see a stale value) together.
  function updateVoiceStatus(status: VoiceStatus) {
    voiceStatusRef.current = status;
    setVoiceStatus(status);
  }

  useEffect(() => {
    // Set up voice recognition if this browser supports it (Chrome/Edge do; Firefox/Safari mostly don't).
    type WindowWithSpeech = Window & {
      SpeechRecognition?: new () => SpeechRecognitionLike;
      webkitSpeechRecognition?: new () => SpeechRecognitionLike;
    };
    const w = window as WindowWithSpeech;

    // Electron's Chromium engine defines webkitSpeechRecognition, but it
    // never returns any results — real speech recognition needs a private
    // Google API key that only official Chrome ships with, so this would
    // just hang on "Listening..." forever. Detect the desktop app and say
    // so plainly instead of pretending voice chat works there.
    const isElectron = typeof navigator !== "undefined" && /electron/i.test(navigator.userAgent);
    if (isElectron) {
      setVoiceBlockedReason("Voice chat isn't available in the desktop app yet — use Chrome or the mobile app for voice.");
      return;
    }

    const SpeechRecognitionCtor = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!SpeechRecognitionCtor) return;

    const recognition = new SpeechRecognitionCtor();
    recognition.lang = "en-US";
    // Interim results + continuous mode let us "hear" you while JARVIS is
    // still mid-sentence, instead of only finding out what you said after
    // you've finished and a fresh listening session has started.
    recognition.interimResults = true;
    recognition.continuous = true;

    recognition.onresult = (e: SpeechRecognitionEventLike) => {
      lastActivityRef.current = Date.now();
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        const transcript = result[0]?.transcript?.trim();
        if (!transcript) continue;

        // You started talking while JARVIS was speaking — stop it immediately
        // and switch to listening, the same as a person going quiet when
        // you interrupt them. Only relevant when barge-in is turned on
        // (headphones); otherwise the mic isn't even running during "speaking".
        if (allowBargeInRef.current && voiceStatusRef.current === "speaking") {
          window.speechSynthesis?.cancel();
          updateVoiceStatus("listening");
        }

        // Only act on speech when it's actually your turn (not while a
        // request is already in flight or JARVIS is still mid-reply).
        if (voiceStatusRef.current !== "listening") continue;

        pendingTranscriptRef.current = transcript;
        if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current);

        if (result.isFinal) {
          finalizeTurn();
        } else {
          // The browser sometimes never marks a continuous-mode result as
          // final. Treat ~1.2s with no new words as "you've stopped talking"
          // so the conversation can't get stuck waiting for a signal that
          // may never arrive.
          silenceTimerRef.current = setTimeout(finalizeTurn, 1200);
        }
      }
    };
    recognition.onerror = () => {
      // A no-speech timeout, mic glitch, etc. — onend fires right after this
      // and restarts the session if we're still in voice mode, so there's
      // nothing to do here.
    };
    recognition.onend = () => {
      // The recognizer stops itself periodically (silence timeouts, some
      // browsers cap continuous sessions). Restart it so the hands-free
      // conversation doesn't quietly die.
      if (voiceModeRef.current) {
        try {
          recognition.start();
          lastActivityRef.current = Date.now();
        } catch {
          // Already running or a transient error — the next onend will retry.
        }
      }
    };

    recognitionRef.current = recognition;
    setVoiceSupported(true);

    // Watchdog: continuous recognition can occasionally go silent in Chrome —
    // no error, no "end" event — and just stop picking up audio. If we're
    // supposed to be listening (or listening-while-speaking) and haven't
    // heard anything from the recognizer in a while, force a restart instead
    // of leaving the UI stuck.
    const watchdog = setInterval(() => {
      if (!voiceModeRef.current) return;
      const status = voiceStatusRef.current;
      const shouldBeListening = status === "listening" || (status === "speaking" && allowBargeInRef.current);
      if (!shouldBeListening) return;
      if (Date.now() - lastActivityRef.current < 12000) return;
      lastActivityRef.current = Date.now();
      try {
        recognition.stop();
      } catch {
        // ignore
      }
      setTimeout(() => {
        if (!voiceModeRef.current) return;
        try {
          recognition.start();
        } catch {
          // ignore — onend (if it eventually fires) or the next watchdog tick will retry
        }
      }, 300);
    }, 5000);

    return () => clearInterval(watchdog);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function finalizeTurn() {
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current);
      silenceTimerRef.current = null;
    }
    const transcript = pendingTranscriptRef.current.trim();
    pendingTranscriptRef.current = "";
    if (transcript && voiceStatusRef.current === "listening") {
      sendVoiceMessage(transcript);
    }
  }

  function startListening() {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    try {
      recognition.start();
    } catch {
      // start() throws if already listening — safe to ignore, it's already running.
    }
  }

  function stopListening() {
    recognitionRef.current?.stop();
  }

  function speak(text: string, onDone?: () => void) {
    if (typeof window === "undefined" || !window.speechSynthesis) {
      onDone?.();
      return;
    }
    window.speechSynthesis.cancel(); // stop anything already playing
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.onend = () => onDone?.();
    utterance.onerror = () => onDone?.();
    window.speechSynthesis.speak(utterance);
  }

  function toggleVoiceMode() {
    if (voiceMode) {
      // Turning voice mode off
      voiceModeRef.current = false;
      setVoiceMode(false);
      updateVoiceStatus("idle");
      stopListening();
      window.speechSynthesis?.cancel();
      if (silenceTimerRef.current) {
        clearTimeout(silenceTimerRef.current);
        silenceTimerRef.current = null;
      }
      pendingTranscriptRef.current = "";
    } else {
      // Turning voice mode on — recognition now runs continuously for the
      // whole session (see the effect above), so we only need to start it once.
      voiceModeRef.current = true;
      setVoiceMode(true);
      updateVoiceStatus("listening");
      lastActivityRef.current = Date.now();
      startListening();
    }
  }

  async function sendVoiceMessage(text: string) {
    updateVoiceStatus("thinking");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setError("");
    try {
      const result = await sendChatMessage(text, conversationIdRef.current, agentId);
      setConversationId(result.conversation_id);
      setAgentId(result.agent_id ?? null);
      setAgentName(result.agent_name ?? null);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: result.reply,
          tallyDraft: result.tally_draft || undefined,
          tallyStatus: result.tally_draft ? "idle" : undefined,
          calendarDraft: result.calendar_draft || undefined,
          calendarStatus: result.calendar_draft ? "idle" : undefined,
          outboundCallDraft: result.outbound_call_draft || undefined,
          outboundCallStatus: result.outbound_call_draft ? "idle" : undefined,
        },
      ]);
      updateVoiceStatus("speaking");
      lastActivityRef.current = Date.now();
      // Unless you've opted into barge-in (headphones), mute the mic while
      // JARVIS talks — otherwise it hears its own voice through the speaker
      // and mistakes it for you, replying to itself forever.
      if (!allowBargeInRef.current) stopListening();
      speak(result.reply, () => {
        if (!voiceModeRef.current) {
          updateVoiceStatus("idle");
          return;
        }
        // If you already interrupted (barge-in already flipped status back to
        // "listening"), don't stomp on that.
        if (voiceStatusRef.current === "speaking") {
          updateVoiceStatus("listening");
        }
        lastActivityRef.current = Date.now();
        if (!allowBargeInRef.current) startListening();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "JARVIS couldn't reply. Try again.");
      if (voiceModeRef.current) {
        updateVoiceStatus("listening");
        lastActivityRef.current = Date.now();
      } else {
        updateVoiceStatus("idle");
      }
    }
  }

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() || sending) return;

    const userMessage = input.trim();
    setInput("");
    setError("");
    setMessages((prev) => [...prev, { role: "user", content: userMessage }]);
    setSending(true);

    try {
      const result = await sendChatMessage(userMessage, conversationId, agentId);
      setConversationId(result.conversation_id);
      setAgentId(result.agent_id ?? null);
      setAgentName(result.agent_name ?? null);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: result.reply,
          tallyDraft: result.tally_draft || undefined,
          tallyStatus: result.tally_draft ? "idle" : undefined,
          calendarDraft: result.calendar_draft || undefined,
          calendarStatus: result.calendar_draft ? "idle" : undefined,
          outboundCallDraft: result.outbound_call_draft || undefined,
          outboundCallStatus: result.outbound_call_draft ? "idle" : undefined,
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "JARVIS couldn't reply. Try again.");
    } finally {
      setSending(false);
    }
  }

  async function handleSendToTally(index: number) {
    const target = messages[index];
    if (!target?.tallyDraft) return;
    setMessages((prev) =>
      prev.map((m, i) => (i === index ? { ...m, tallyStatus: "sending" as TallyCardStatus } : m))
    );
    try {
      const result = await createTallyBill(target.tallyDraft);
      setMessages((prev) =>
        prev.map((m, i) =>
          i === index
            ? {
                ...m,
                tallyStatus: "success" as TallyCardStatus,
                tallyResultMessage: `Created in Tally — total ${formatRupees(result.total_amount)}.`,
              }
            : m
        )
      );
    } catch (err) {
      setMessages((prev) =>
        prev.map((m, i) =>
          i === index
            ? {
                ...m,
                tallyStatus: "error" as TallyCardStatus,
                tallyResultMessage:
                  err instanceof Error ? err.message : "Couldn't reach Tally. Try again.",
              }
            : m
        )
      );
    }
  }

  // Phase 19 "Email + Calendar": same two-step review pattern as
  // handleSendToTally above — nothing is added to Sudeep's real Google
  // Calendar until this button is actually clicked.
  async function handleCreateEvent(index: number) {
    const target = messages[index];
    if (!target?.calendarDraft) return;
    setMessages((prev) =>
      prev.map((m, i) => (i === index ? { ...m, calendarStatus: "sending" as CalendarCardStatus } : m))
    );
    try {
      const result = await createCalendarEvent(target.calendarDraft);
      setMessages((prev) =>
        prev.map((m, i) =>
          i === index
            ? {
                ...m,
                calendarStatus: "success" as CalendarCardStatus,
                calendarResultMessage: result.message,
              }
            : m
        )
      );
    } catch (err) {
      setMessages((prev) =>
        prev.map((m, i) =>
          i === index
            ? {
                ...m,
                calendarStatus: "error" as CalendarCardStatus,
                calendarResultMessage:
                  err instanceof Error ? err.message : "Couldn't reach Google Calendar. Try again.",
              }
            : m
        )
      );
    }
  }

  // Phase 22: this is the explicit approval action for a real outbound
  // Twilio call. Drafting a card in chat never calls anyone by itself.
  async function handlePlaceOutboundCall(index: number) {
    const target = messages[index];
    if (!target?.outboundCallDraft) return;
    setMessages((prev) =>
      prev.map((m, i) => i === index ? { ...m, outboundCallStatus: "sending" as OutboundCallCardStatus } : m)
    );
    try {
      const result = await placeOutboundCall(target.outboundCallDraft);
      setMessages((prev) =>
        prev.map((m, i) => i === index ? {
          ...m,
          outboundCallStatus: "success" as OutboundCallCardStatus,
          outboundCallResultMessage: result.message,
        } : m)
      );
    } catch (err) {
      setMessages((prev) =>
        prev.map((m, i) => i === index ? {
          ...m,
          outboundCallStatus: "error" as OutboundCallCardStatus,
          outboundCallResultMessage: err instanceof Error ? err.message : "Couldn't start the call.",
        } : m)
      );
    }
  }

  // Code execution (Phase 12) — runs one fenced code block, identified by
  // its own "<messageIndex>-<blockIndex>" key, against the real backend
  // sandbox (see code_executor.py). Never auto-runs anything: this is only
  // ever called from the "Run Code" button's onClick, so nothing executes
  // without Sudeep explicitly choosing to run that specific block.
  async function handleRunCode(key: string, language: string, code: string) {
    setCodeRuns((prev) => ({ ...prev, [key]: { status: "running" } }));
    try {
      const result = await executeCode(normalizeRunnableLanguage(language), code);
      setCodeRuns((prev) => ({ ...prev, [key]: { status: "done", result } }));
    } catch (err) {
      setCodeRuns((prev) => ({
        ...prev,
        [key]: {
          status: "done",
          error: err instanceof Error ? err.message : "Couldn't run this code. Try again.",
        },
      }));
    }
  }

  async function handleAddUser(e: React.FormEvent) {
    e.preventDefault();
    setAddUserError("");
    setAddUserSuccess("");
    setAddUserBusy(true);
    try {
      await addUser(newEmail, newPassword, newFullName);
      setAddUserSuccess(`Account created for ${newEmail}.`);
      setNewEmail("");
      setNewPassword("");
      setNewFullName("");
    } catch (err) {
      setAddUserError(err instanceof Error ? err.message : "Couldn't create that account.");
    } finally {
      setAddUserBusy(false);
    }
  }

  async function dismissPhoneRecord(id: number) {
    try {
      await markPhoneCallRead(id);
      setPhoneRecords((records) => records.filter((record) => record.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't mark that call as read.");
    }
  }

  function handleLogout() {
    voiceModeRef.current = false;
    clearToken();
    router.replace("/login");
  }

  if (checking) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-500">
        Loading...
      </div>
    );
  }

  const statusLabel: Record<VoiceStatus, string> = {
    idle: "",
    listening: "🎤 Listening...",
    thinking: "💭 JARVIS is thinking...",
    speaking: allowBargeIn
      ? "🔊 JARVIS is speaking... (just talk to interrupt)"
      : "🔊 JARVIS is speaking...",
  };

  return (
    <div className="flex-1 flex flex-col max-w-3xl w-full mx-auto p-4">
      <header className="flex items-center justify-between border-b pb-3 mb-4">
        <div>
          <h1 className="text-lg font-semibold">JARVIS</h1>
          <p className="text-xs text-gray-500">
            Signed in as {userEmail}
            {agentName && (
              <>
                {" · "}
                <span className="font-medium text-gray-700">🏭 {agentName}</span>
                <button
                  onClick={leaveAgentChat}
                  title="Leave this agent and start a general JARVIS chat"
                  className="ml-1 text-gray-400 hover:text-gray-700 underline"
                >
                  switch to general chat
                </button>
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {voiceBlockedReason && (
            <span className="text-xs text-gray-400" title={voiceBlockedReason}>
              🎙️ Voice unavailable here
            </span>
          )}
          {voiceSupported && (
            <label
              className="flex items-center gap-1 text-xs text-gray-500"
              title="Only works well with headphones/earbuds — otherwise the mic hears JARVIS's own voice and replies to itself."
            >
              <input
                type="checkbox"
                checked={allowBargeIn}
                onChange={(e) => setAllowBargeIn(e.target.checked)}
              />
              Interrupt (headphones)
            </label>
          )}
          {voiceSupported && (
            <button
              onClick={toggleVoiceMode}
              className={`text-sm rounded-lg px-3 py-1.5 font-medium ${
                voiceMode
                  ? "bg-red-600 text-white"
                  : "bg-black text-white"
              }`}
            >
              {voiceMode ? "End voice chat" : "🎙️ Voice chat"}
            </button>
          )}
          <Link href="/memory" className="text-sm text-gray-500 underline">
            🧠 Memory
          </Link>
          <Link href="/knowledge" className="text-sm text-gray-500 underline">
            📚 Knowledge
          </Link>
          <Link href="/skills" className="text-sm text-gray-500 underline">
            🎓 Skills
          </Link>
          <Link href="/agents" className="text-sm text-gray-500 underline">
            🏭 Agents
          </Link>
          <Link href="/tools" className="text-sm text-gray-500 underline">
            🧰 Tools
          </Link>
          <Link href="/settings/google" className="text-sm text-gray-500 underline">
            📧 Email + Calendar
          </Link>
          <button
            onClick={() => setShowAddUser((v) => !v)}
            className="text-sm text-gray-500 underline"
          >
            Add user
          </button>
          <button onClick={handleLogout} className="text-sm text-gray-500 underline">
            Sign out
          </button>
        </div>
      </header>

      {phoneRecords.length > 0 && (
        <section className="mb-4 space-y-2" aria-label="Unread incoming calls">
          {phoneRecords.map((record) => (
            <div
              key={record.id}
              className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950"
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="font-semibold">
                    {record.callback_requested ? "📞 New callback request" : "📞 New incoming call"}
                  </p>
                  <p className="mt-1">
                    {record.caller_name ? `${record.caller_name} · ` : ""}
                    {record.caller_number}
                  </p>
                  <p className="text-xs text-amber-800">
                    {new Date(record.created_at).toLocaleString("en-IN")}
                  </p>
                  {record.reason && (
                    <p className="mt-2">
                      <span className="font-medium">Reason:</span> {record.reason}
                    </p>
                  )}
                  {record.preferred_callback_time && (
                    <p>
                      <span className="font-medium">Preferred time:</span>{" "}
                      {record.preferred_callback_time}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => dismissPhoneRecord(record.id)}
                  className="shrink-0 rounded-lg border border-amber-400 bg-white px-3 py-1.5 text-xs font-medium hover:bg-amber-100"
                >
                  Mark read
                </button>
              </div>
            </div>
          ))}
        </section>
      )}

      {showAddUser && (
        <form
          onSubmit={handleAddUser}
          className="border rounded-lg p-3 mb-4 space-y-2 bg-gray-50"
        >
          <p className="text-sm font-medium">Add a new user</p>
          <input
            type="text"
            placeholder="Full name"
            value={newFullName}
            onChange={(e) => setNewFullName(e.target.value)}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          <input
            type="email"
            required
            placeholder="Email"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          <input
            type="password"
            required
            placeholder="Password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          {addUserError && <p className="text-xs text-red-600">{addUserError}</p>}
          {addUserSuccess && <p className="text-xs text-green-600">{addUserSuccess}</p>}
          <button
            type="submit"
            disabled={addUserBusy}
            className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            {addUserBusy ? "Creating..." : "Create account"}
          </button>
        </form>
      )}

      <div className="flex-1 overflow-y-auto space-y-3 mb-4">
        {messages.length === 0 && (
          <p className="text-sm text-gray-400 text-center mt-8">
            Say hello to JARVIS to start a conversation.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`group relative rounded-2xl px-4 py-2 max-w-[80%] whitespace-pre-wrap ${
                m.role === "user" ? "bg-black text-white" : "bg-gray-100 text-gray-900"
              }`}
            >
              {m.role === "assistant" ? (
                parseMessageSegments(m.content).map((seg, segIdx) => {
                  if (seg.type === "text") {
                    return seg.text ? <span key={segIdx}>{seg.text}</span> : null;
                  }
                  const key = `${i}-${segIdx}`;
                  const runnable = RUNNABLE_LANGUAGES.has(seg.language);
                  const run = codeRuns[key];
                  return (
                    <div key={segIdx} className="block my-2 rounded-lg overflow-hidden border border-gray-200">
                      <pre className="bg-gray-900 text-gray-100 text-xs p-3 overflow-x-auto whitespace-pre">
                        <code>{seg.code}</code>
                      </pre>
                      {runnable && (
                        <div className="bg-white px-3 py-2 text-xs space-y-2">
                          <button
                            onClick={() => handleRunCode(key, seg.language, seg.code)}
                            disabled={run?.status === "running"}
                            className="bg-black text-white rounded-lg px-3 py-1 text-xs font-medium disabled:opacity-50"
                          >
                            {run?.status === "running" ? "Running..." : "▶ Run Code"}
                          </button>
                          {run?.status === "done" && run.result && (
                            <pre
                              className={`whitespace-pre-wrap rounded-md p-2 ${
                                run.result.success
                                  ? "bg-green-50 text-green-900"
                                  : "bg-red-50 text-red-900"
                              }`}
                            >
                              {run.result.success
                                ? run.result.output
                                : run.result.error}
                            </pre>
                          )}
                          {run?.status === "done" && run.error && (
                            <pre className="whitespace-pre-wrap rounded-md p-2 bg-red-50 text-red-900">
                              {run.error}
                            </pre>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })
              ) : (
                m.content
              )}
              {m.role === "assistant" && (
                <button
                  onClick={() => speak(m.content)}
                  title="Play this reply"
                  className="ml-2 align-middle text-gray-400 hover:text-gray-700"
                >
                  🔊
                </button>
              )}
              {m.role === "assistant" && m.tallyDraft && (
                <div className="mt-3 border rounded-xl bg-white text-gray-900 p-3 text-sm space-y-2">
                  <p className="font-semibold">🧾 Bill ready for Tally</p>
                  <p>
                    <span className="text-gray-500">Customer: </span>
                    {m.tallyDraft.party_name}
                  </p>
                  <ul className="space-y-0.5">
                    {m.tallyDraft.items.map((item, idx) => (
                      <li key={idx} className="flex justify-between gap-4">
                        <span>{item.description}</span>
                        <span>{formatRupees(item.amount)}</span>
                      </li>
                    ))}
                  </ul>
                  {m.tallyDraft.tax_lines.length > 0 && (
                    <ul className="space-y-0.5 text-gray-500">
                      {m.tallyDraft.tax_lines.map((t, idx) => (
                        <li key={idx} className="flex justify-between gap-4">
                          <span>
                            {t.ledger}
                            {m.tallyDraft?.gst_rate != null ? ` (${m.tallyDraft.gst_rate}%)` : ""}
                          </span>
                          <span>{formatRupees(t.amount)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="flex justify-between gap-4 font-medium border-t pt-1">
                    <span>Total</span>
                    <span>{formatRupees(tallyDraftTotal(m.tallyDraft))}</span>
                  </div>
                  <p className="text-xs text-gray-500">{m.tallyDraft.voucher_date}</p>
                  {(m.tallyDraft.buyer_order_no || m.tallyDraft.other_reference_no || m.tallyDraft.destination) && (
                    <p className="text-xs text-gray-500">
                      {m.tallyDraft.buyer_order_no && <>Work Order No: {m.tallyDraft.buyer_order_no}<br /></>}
                      {m.tallyDraft.other_reference_no && <>Complaint No: {m.tallyDraft.other_reference_no}<br /></>}
                      {m.tallyDraft.destination && <>Destination: {m.tallyDraft.destination}</>}
                    </p>
                  )}
                  {m.tallyDraft.narration && (
                    <p className="text-xs text-gray-500">{m.tallyDraft.narration}</p>
                  )}

                  {m.tallyStatus === "success" ? (
                    <p className="text-xs text-green-600">✅ {m.tallyResultMessage}</p>
                  ) : (
                    <>
                      {m.tallyStatus === "error" && (
                        <p className="text-xs text-red-600">⚠️ {m.tallyResultMessage}</p>
                      )}
                      <button
                        onClick={() => handleSendToTally(i)}
                        disabled={m.tallyStatus === "sending"}
                        className="bg-black text-white rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
                      >
                        {m.tallyStatus === "sending"
                          ? "Sending to Tally..."
                          : m.tallyStatus === "error"
                            ? "Retry"
                            : "Send to Tally"}
                      </button>
                    </>
                  )}
                </div>
              )}
              {m.role === "assistant" && m.outboundCallDraft && (
                <div className="mt-3 border rounded-xl bg-white text-gray-900 p-3 text-sm space-y-2">
                  <p className="font-semibold">📞 Outbound call ready for your review</p>
                  <p><span className="font-medium">To:</span> {m.outboundCallDraft.to_number}</p>
                  <p><span className="font-medium">Purpose:</span> {m.outboundCallDraft.purpose}</p>
                  <p className="text-xs text-gray-600">{m.outboundCallDraft.opening_message}</p>
                  <p className="text-xs text-amber-700">Pressing Place Call starts a real phone call. Check the number and request first.</p>
                  {m.outboundCallStatus === "success" ? (
                    <p className="text-xs text-green-600">✅ {m.outboundCallResultMessage}</p>
                  ) : (
                    <>
                      {m.outboundCallStatus === "error" && (
                        <p className="text-xs text-red-600">⚠️ {m.outboundCallResultMessage}</p>
                      )}
                      <button
                        onClick={() => handlePlaceOutboundCall(i)}
                        disabled={m.outboundCallStatus === "sending" || m.outboundCallStatus === "error"}
                        className="bg-black text-white rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
                      >
                        {m.outboundCallStatus === "sending" ? "Starting call..." : "Place Call"}
                      </button>
                    </>
                  )}
                </div>
              )}
              {m.role === "assistant" && m.calendarDraft && (
                <div className="mt-3 border rounded-xl bg-white text-gray-900 p-3 text-sm space-y-2">
                  <p className="font-semibold">📅 Event ready for your calendar</p>
                  <p className="font-medium">{m.calendarDraft.summary}</p>
                  <p className="text-gray-500">
                    {formatEventTime(m.calendarDraft.start_iso)} – {formatEventTime(m.calendarDraft.end_iso)}
                  </p>
                  {m.calendarDraft.location && (
                    <p className="text-xs text-gray-500">📍 {m.calendarDraft.location}</p>
                  )}
                  {m.calendarDraft.description && (
                    <p className="text-xs text-gray-500">{m.calendarDraft.description}</p>
                  )}

                  {m.calendarStatus === "success" ? (
                    <p className="text-xs text-green-600">✅ {m.calendarResultMessage}</p>
                  ) : (
                    <>
                      {m.calendarStatus === "error" && (
                        <p className="text-xs text-red-600">⚠️ {m.calendarResultMessage}</p>
                      )}
                      <button
                        onClick={() => handleCreateEvent(i)}
                        disabled={m.calendarStatus === "sending"}
                        className="bg-black text-white rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
                      >
                        {m.calendarStatus === "sending"
                          ? "Adding to Calendar..."
                          : m.calendarStatus === "error"
                            ? "Retry"
                            : "Create Event"}
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
        {sending && (
          <div className="flex justify-start">
            <div className="rounded-2xl px-4 py-2 bg-gray-100 text-gray-500">
              JARVIS is thinking...
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {voiceMode ? (
        <div className="border-t pt-4 flex flex-col items-center gap-3 pb-2">
          <div
            className={`w-16 h-16 rounded-full flex items-center justify-center text-2xl ${
              voiceStatus === "listening"
                ? "bg-red-100 animate-pulse"
                : voiceStatus === "speaking"
                  ? "bg-blue-100 animate-pulse"
                  : "bg-gray-100"
            }`}
          >
            {voiceStatus === "listening" ? "🎤" : voiceStatus === "speaking" ? "🔊" : "💭"}
          </div>
          <p className="text-sm text-gray-600">{statusLabel[voiceStatus]}</p>
          <p className="text-xs text-gray-400">
            {allowBargeIn
              ? "Just speak — even to cut JARVIS off mid-reply"
              : "Just speak — JARVIS will listen again once it's done talking"}
          </p>
        </div>
      ) : (
        <form onSubmit={handleSend} className="flex gap-2 border-t pt-3">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Message JARVIS..."
            className="flex-1 border rounded-lg px-3 py-2"
          />
          <button
            type="submit"
            disabled={sending}
            className="bg-black text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50"
          >
            Send
          </button>
        </form>
      )}
    </div>
  );
}
