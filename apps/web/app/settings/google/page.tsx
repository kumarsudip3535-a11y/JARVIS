"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  getGoogleStatus,
  getGoogleAuthUrl,
  disconnectGoogle,
  type GoogleStatus,
} from "@/lib/api";

// Phase 19 "Email + Calendar" (added 2026-09-22, scoped with Sudeep via 3
// AskUserQuestion questions - see progress-tracker.md and
// app/google_client.py's docstring for the full design). This page exists
// because OAuth genuinely needs a real browser page: "Connect Google
// Account" navigates the whole browser to Google's own consent screen
// (google_routes.oauth_start), which then redirects back here
// (google_routes.oauth_callback) with a ?connected=1 or ?error=... query
// param this page reads on load. Everything else in this feature (reading
// email, viewing/finding calendar slots, drafting replies, and the
// [CALENDAR_EVENT_DRAFT] review card's "Create Event" button) happens
// through chat - this page is only for the one-time connect/disconnect step.
export default function GoogleSettingsPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [banner, setBanner] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setChecking(false);

    // Read the OAuth callback's own query params directly from
    // window.location (not useSearchParams()) so this page doesn't need a
    // Suspense boundary - same convention chat/page.tsx already uses for
    // its ?agent= param.
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) {
      setBanner({ kind: "success", text: "Google account connected." });
      window.history.replaceState({}, "", "/settings/google");
    } else if (params.get("error")) {
      setBanner({
        kind: "error",
        text: `Couldn't connect your Google account (${params.get("error")}). Try again.`,
      });
      window.history.replaceState({}, "", "/settings/google");
    }

    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  function refresh() {
    setLoading(true);
    getGoogleStatus()
      .then(setStatus)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load Google status."))
      .finally(() => setLoading(false));
  }

  async function handleConnect() {
    setConnecting(true);
    setError("");
    try {
      const { auth_url } = await getGoogleAuthUrl();
      // A full browser navigation, not a fetch - Google's consent screen is
      // a real page the person has to interact with (sign in, review the
      // requested permissions, approve), not something an API call can do.
      window.location.href = auth_url;
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Couldn't start the Google connection. Try again."
      );
      setConnecting(false);
    }
  }

  async function handleDisconnect() {
    setDisconnecting(true);
    setError("");
    try {
      await disconnectGoogle();
      setBanner(null);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't disconnect Google.");
    } finally {
      setDisconnecting(false);
    }
  }

  if (checking) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-500">
        Loading...
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col max-w-xl w-full mx-auto p-4">
      <header className="flex items-center justify-between border-b pb-3 mb-4">
        <div>
          <h1 className="text-lg font-semibold">📧 Email + Calendar</h1>
          <p className="text-xs text-gray-500">
            Connect your Gmail so JARVIS can read/search/summarize email and
            draft replies (never sends), and view/find open slots on your
            Google Calendar
          </p>
        </div>
        <Link href="/chat" className="text-sm text-gray-500 underline">
          ← Back to chat
        </Link>
      </header>

      {banner && (
        <p
          className={`text-sm rounded-lg px-3 py-2 mb-3 ${
            banner.kind === "success"
              ? "bg-green-50 text-green-700"
              : "bg-red-50 text-red-700"
          }`}
        >
          {banner.text}
        </p>
      )}
      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {loading ? (
        <p className="text-sm text-gray-400">Loading...</p>
      ) : status?.connected ? (
        <div className="border rounded-lg p-4 space-y-3">
          <p className="text-sm">
            ✅ Connected as <span className="font-medium">{status.google_email}</span>
          </p>
          {status.connected_at && (
            <p className="text-xs text-gray-500">
              Connected {new Date(status.connected_at).toLocaleDateString()}
            </p>
          )}
          <p className="text-xs text-gray-500">
            JARVIS can read/search your Gmail and create drafts (it can never
            send anything - a draft sits in your Gmail Drafts folder until you
            press Send yourself), and view/find open slots on your Google
            Calendar. Creating a real calendar event always shows you a
            review card in chat first - nothing is added until you confirm it.
          </p>
          <button
            onClick={handleDisconnect}
            disabled={disconnecting}
            className="text-sm text-red-600 underline disabled:opacity-50"
          >
            {disconnecting ? "Disconnecting..." : "Disconnect Google"}
          </button>
        </div>
      ) : (
        <div className="border rounded-lg p-4 space-y-3">
          <p className="text-sm text-gray-600">
            Not connected yet. Connecting will open Google's own sign-in and
            consent screen - JARVIS only ever sees what you approve there.
          </p>
          <button
            onClick={handleConnect}
            disabled={connecting}
            className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            {connecting ? "Connecting..." : "Connect Google Account"}
          </button>
        </div>
      )}
    </div>
  );
}
