"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { loginUser, setToken } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await loginUser(email, password);
      setToken(result.access_token);
      router.push("/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex-1 flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold mb-1 text-center">JARVIS</h1>
        <p className="text-sm text-gray-500 mb-6 text-center">Sign in to continue</p>

        <form onSubmit={handleSubmit} className="space-y-3">
          <input
            type="email"
            required
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full border rounded-lg px-3 py-2"
          />
          <input
            type="password"
            required
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full border rounded-lg px-3 py-2"
          />

          {error && <p className="text-sm text-red-600">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-black text-white rounded-lg py-2 font-medium disabled:opacity-50"
          >
            {loading ? "Please wait..." : "Sign in"}
          </button>
        </form>

        <p className="text-xs text-center mt-6 text-gray-400">
          JARVIS is a personal assistant — accounts are added by the owner from
          inside the app, not created here.
        </p>
      </div>
    </div>
  );
}
