"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  listDocuments,
  uploadDocument,
  deleteDocument,
  askAboutDocuments,
} from "@/lib/api";

type Document = {
  id: number;
  filename: string;
  source_type: string;
  size_bytes: number | null;
  created_at: string;
};

function formatSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function KnowledgePage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [uploading, setUploading] = useState(false);

  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState("");

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
    listDocuments()
      .then((data) => setDocuments(data))
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Couldn't load your documents.")
      )
      .finally(() => setLoading(false));
  }

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      await uploadDocument(file);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't upload that file.");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(id: number) {
    try {
      await deleteDocument(id);
      setDocuments((prev) => prev.filter((d) => d.id !== id));
      setSelectedIds((prev) => prev.filter((x) => x !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't delete that.");
    }
  }

  function toggleSelected(id: number) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  async function handleAsk(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim() || selectedIds.length === 0 || asking) return;
    setAsking(true);
    setError("");
    setAnswer("");
    try {
      const result = await askAboutDocuments(selectedIds, question.trim());
      setAnswer(result.answer);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't get an answer that time.");
    } finally {
      setAsking(false);
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
          <h1 className="text-lg font-semibold">📚 Knowledge base</h1>
          <p className="text-xs text-gray-500">
            Upload documents, then pick which ones to ask JARVIS about
          </p>
        </div>
        <Link href="/chat" className="text-sm text-gray-500 underline">
          ← Back to chat
        </Link>
      </header>

      <div className="border rounded-lg p-3 mb-4 bg-gray-50">
        <label className="flex items-center gap-3 text-sm">
          <span className="bg-black text-white rounded-lg px-3 py-1.5 font-medium cursor-pointer">
            {uploading ? "Uploading..." : "Upload a document"}
          </span>
          <input
            type="file"
            accept=".pdf,.doc,.docx,.xls,.xlsx,.txt,.csv"
            onChange={handleFileChange}
            disabled={uploading}
            className="hidden"
          />
          <span className="text-xs text-gray-500">
            PDF, Word (.docx), Excel (.xlsx), .txt or .csv
          </span>
        </label>
      </div>

      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {loading ? (
        <p className="text-sm text-gray-400">Loading documents...</p>
      ) : documents.length === 0 ? (
        <p className="text-sm text-gray-400 text-center mt-4 mb-4">
          Nothing uploaded yet — add a document above to start asking JARVIS
          about it.
        </p>
      ) : (
        <div className="space-y-2 mb-4">
          {documents.map((d) => (
            <label
              key={d.id}
              className="flex items-center gap-3 border rounded-lg p-3 cursor-pointer"
            >
              <input
                type="checkbox"
                checked={selectedIds.includes(d.id)}
                onChange={() => toggleSelected(d.id)}
              />
              <div className="flex-1 min-w-0">
                <p className="text-sm truncate">{d.filename}</p>
                <p className="text-xs text-gray-400">
                  {d.source_type.toUpperCase()}
                  {d.size_bytes ? ` · ${formatSize(d.size_bytes)}` : ""}
                </p>
              </div>
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  handleDelete(d.id);
                }}
                className="text-xs text-red-600 underline shrink-0"
              >
                Delete
              </button>
            </label>
          ))}
        </div>
      )}

      <form onSubmit={handleAsk} className="border rounded-lg p-3 space-y-2 bg-gray-50">
        <p className="text-sm font-medium">
          Ask about {selectedIds.length === 0 ? "the selected document(s)" : `${selectedIds.length} selected document${selectedIds.length > 1 ? "s" : ""}`}
        </p>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. What was the total revenue in this report?"
          className="w-full border rounded-lg px-3 py-2 text-sm"
          rows={2}
        />
        <button
          type="submit"
          disabled={asking || selectedIds.length === 0 || !question.trim()}
          className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
        >
          {asking ? "Asking JARVIS..." : "Ask"}
        </button>
      </form>

      {answer && (
        <div className="border rounded-lg p-3 mt-4">
          <p className="text-xs text-gray-400 mb-1">JARVIS:</p>
          <p className="text-sm whitespace-pre-wrap">{answer}</p>
        </div>
      )}
    </div>
  );
}
