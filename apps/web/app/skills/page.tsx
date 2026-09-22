"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  listSkills,
  learnSkill,
  approveSkill,
  deleteSkill,
} from "@/lib/api";

type Skill = {
  id: number;
  name: string;
  description: string | null;
  content: string;
  status: string; // "pending_review" | "active"
  version: number;
  created_at: string;
  approved_at: string | null;
};

export default function SkillsPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [topic, setTopic] = useState("");
  const [learning, setLearning] = useState(false);

  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

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
    listSkills()
      .then((data) => setSkills(data))
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Couldn't load skills.")
      )
      .finally(() => setLoading(false));
  }

  async function handleLearn(e: React.FormEvent) {
    e.preventDefault();
    if (!topic.trim() || learning) return;
    setLearning(true);
    setError("");
    try {
      const skill = await learnSkill(topic.trim());
      setTopic("");
      refresh();
      setExpandedId(skill.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "JARVIS couldn't research that topic.");
    } finally {
      setLearning(false);
    }
  }

  async function handleApprove(id: number) {
    setBusyId(id);
    try {
      await approveSkill(id);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't approve that skill.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(id: number) {
    setBusyId(id);
    try {
      await deleteSkill(id);
      setSkills((prev) => prev.filter((s) => s.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove that skill.");
    } finally {
      setBusyId(null);
    }
  }

  if (checking) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-500">
        Loading...
      </div>
    );
  }

  const pending = skills.filter((s) => s.status === "pending_review");
  const active = skills.filter((s) => s.status === "active");

  return (
    <div className="flex-1 flex flex-col max-w-3xl w-full mx-auto p-4">
      <header className="flex items-center justify-between border-b pb-3 mb-4">
        <div>
          <h1 className="text-lg font-semibold">🎓 Skills</h1>
          <p className="text-xs text-gray-500">
            Ask JARVIS to learn a topic, then review and approve before it's used in chat
          </p>
        </div>
        <Link href="/chat" className="text-sm text-gray-500 underline">
          ← Back to chat
        </Link>
      </header>

      <form onSubmit={handleLearn} className="border rounded-lg p-3 mb-4 space-y-2 bg-gray-50">
        <p className="text-sm font-medium">Learn a new skill</p>
        <input
          type="text"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="e.g. GST e-invoicing rules for contractors"
          className="w-full border rounded-lg px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={learning || !topic.trim()}
          className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
        >
          {learning ? "Researching... (this can take a bit)" : "Learn this skill"}
        </button>
      </form>

      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {loading ? (
        <p className="text-sm text-gray-400">Loading skills...</p>
      ) : (
        <>
          {pending.length > 0 && (
            <div className="mb-4">
              <p className="text-sm font-medium mb-2">Pending review</p>
              <div className="space-y-2">
                {pending.map((s) => (
                  <SkillCard
                    key={s.id}
                    skill={s}
                    expanded={expandedId === s.id}
                    busy={busyId === s.id}
                    onToggle={() => setExpandedId(expandedId === s.id ? null : s.id)}
                    onApprove={() => handleApprove(s.id)}
                    onDelete={() => handleDelete(s.id)}
                  />
                ))}
              </div>
            </div>
          )}

          <div>
            <p className="text-sm font-medium mb-2">Active skills</p>
            {active.length === 0 ? (
              <p className="text-sm text-gray-400 text-center mt-4">
                Nothing active yet — learn a topic above, then approve it here
                once you've reviewed what JARVIS found.
              </p>
            ) : (
              <div className="space-y-2">
                {active.map((s) => (
                  <SkillCard
                    key={s.id}
                    skill={s}
                    expanded={expandedId === s.id}
                    busy={busyId === s.id}
                    onToggle={() => setExpandedId(expandedId === s.id ? null : s.id)}
                    onDelete={() => handleDelete(s.id)}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SkillCard({
  skill,
  expanded,
  busy,
  onToggle,
  onApprove,
  onDelete,
}: {
  skill: Skill;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  onApprove?: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="border rounded-lg p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">{skill.name}</p>
          {skill.description && (
            <p className="text-xs text-gray-500 mt-0.5">{skill.description}</p>
          )}
          <p className="text-xs text-gray-400 mt-1">
            {skill.status === "active" ? "Active — used in chat" : "Pending your review"}
            {" · v"}
            {skill.version}
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <button onClick={onToggle} className="text-xs text-gray-500 underline">
            {expanded ? "Hide" : "View"}
          </button>
          {onApprove && (
            <button
              onClick={onApprove}
              disabled={busy}
              className="text-xs bg-black text-white rounded px-2 py-1 disabled:opacity-50"
            >
              {busy ? "..." : "Approve"}
            </button>
          )}
          <button
            onClick={onDelete}
            disabled={busy}
            className="text-xs text-red-600 underline disabled:opacity-50"
          >
            {skill.status === "active" ? "Forget" : "Discard"}
          </button>
        </div>
      </div>
      {expanded && (
        <div className="mt-3 pt-3 border-t text-sm whitespace-pre-wrap text-gray-700">
          {skill.content}
        </div>
      )}
    </div>
  );
}
