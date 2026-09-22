"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  listMemories,
  createMemory,
  updateMemory,
  deleteMemory,
} from "@/lib/api";

type Memory = {
  id: number;
  content: string;
  category: string | null;
  source: string;
  created_at: string;
  updated_at: string;
};

const CATEGORIES = ["general", "business", "family", "preference", "project"];

export default function MemoryPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [newContent, setNewContent] = useState("");
  const [newCategory, setNewCategory] = useState("general");
  const [adding, setAdding] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editCategory, setEditCategory] = useState("general");

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setChecking(false);
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  function refresh() {
    setLoading(true);
    listMemories()
      .then((data) => setMemories(data))
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Couldn't load memories.")
      )
      .finally(() => setLoading(false));
  }

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!newContent.trim() || adding) return;
    setAdding(true);
    setError("");
    try {
      await createMemory(newContent.trim(), newCategory);
      setNewContent("");
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save that.");
    } finally {
      setAdding(false);
    }
  }

  function startEdit(m: Memory) {
    setEditingId(m.id);
    setEditContent(m.content);
    setEditCategory(m.category || "general");
  }

  async function saveEdit(id: number) {
    if (!editContent.trim()) return;
    try {
      await updateMemory(id, editContent.trim(), editCategory);
      setEditingId(null);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save that edit.");
    }
  }

  async function handleDelete(id: number) {
    try {
      await deleteMemory(id);
      setMemories((prev) => prev.filter((m) => m.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't delete that.");
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
    <div className="flex-1 flex flex-col max-w-3xl w-full mx-auto p-4">
      <header className="flex items-center justify-between border-b pb-3 mb-4">
        <div>
          <h1 className="text-lg font-semibold">🧠 Memory</h1>
          <p className="text-xs text-gray-500">
            What JARVIS remembers about you, across every conversation
          </p>
        </div>
        <Link href="/chat" className="text-sm text-gray-500 underline">
          ← Back to chat
        </Link>
      </header>

      <form
        onSubmit={handleAdd}
        className="border rounded-lg p-3 mb-4 space-y-2 bg-gray-50"
      >
        <p className="text-sm font-medium">Add a memory manually</p>
        <textarea
          value={newContent}
          onChange={(e) => setNewContent(e.target.value)}
          placeholder="e.g. I prefer short, direct answers"
          className="w-full border rounded-lg px-3 py-2 text-sm"
          rows={2}
        />
        <div className="flex items-center gap-2">
          <select
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
            className="border rounded-lg px-2 py-1.5 text-sm"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={adding}
            className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            {adding ? "Saving..." : "Save memory"}
          </button>
        </div>
      </form>

      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {loading ? (
        <p className="text-sm text-gray-400">Loading memories...</p>
      ) : memories.length === 0 ? (
        <p className="text-sm text-gray-400 text-center mt-8">
          Nothing remembered yet — JARVIS will save durable facts here as you
          chat, or you can add one yourself above.
        </p>
      ) : (
        <div className="flex-1 overflow-y-auto space-y-2">
          {memories.map((m) => (
            <div key={m.id} className="border rounded-lg p-3">
              {editingId === m.id ? (
                <div className="space-y-2">
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    className="w-full border rounded-lg px-3 py-2 text-sm"
                    rows={2}
                  />
                  <div className="flex items-center gap-2">
                    <select
                      value={editCategory}
                      onChange={(e) => setEditCategory(e.target.value)}
                      className="border rounded-lg px-2 py-1.5 text-sm"
                    >
                      {CATEGORIES.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={() => saveEdit(m.id)}
                      className="bg-black text-white rounded-lg px-3 py-1 text-sm font-medium"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => setEditingId(null)}
                      className="text-sm text-gray-500 underline"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-sm whitespace-pre-wrap">{m.content}</p>
                    <p className="text-xs text-gray-400 mt-1">
                      {m.category || "general"} ·{" "}
                      {m.source === "auto" ? "learned automatically" : "added manually"}
                    </p>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => startEdit(m)}
                      className="text-xs text-gray-500 underline"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => handleDelete(m.id)}
                      className="text-xs text-red-600 underline"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
