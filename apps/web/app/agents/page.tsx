"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  listAgents,
  createAgent,
  updateAgent,
  pauseAgent,
  resumeAgent,
  deleteAgent,
  listSkills,
  listTools,
  type AgentOut,
  type CustomToolOut,
} from "@/lib/api";

type Skill = {
  id: number;
  name: string;
  description: string | null;
  status: string; // "pending_review" | "active"
};

export default function AgentsPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [agents, setAgents] = useState<AgentOut[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [tools, setTools] = useState<CustomToolOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Create form
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [roleDescription, setRoleDescription] = useState("");
  const [systemInstructions, setSystemInstructions] = useState("");
  const [allowWebSearch, setAllowWebSearch] = useState(true);
  const [allowTallyBilling, setAllowTallyBilling] = useState(false);
  const [allowEmailCalendar, setAllowEmailCalendar] = useState(false);
  const [allowTeamManagement, setAllowTeamManagement] = useState(false);
  const [selectedSkillIds, setSelectedSkillIds] = useState<number[]>([]);
  const [selectedToolIds, setSelectedToolIds] = useState<number[]>([]);
  const [creating, setCreating] = useState(false);

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
    Promise.all([listAgents(), listSkills(), listTools()])
      .then(([agentsData, skillsData, toolsData]) => {
        setAgents(agentsData);
        setSkills(skillsData);
        setTools(toolsData);
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Couldn't load agents.")
      )
      .finally(() => setLoading(false));
  }

  function resetForm() {
    setName("");
    setRoleDescription("");
    setSystemInstructions("");
    setAllowWebSearch(true);
    setAllowTallyBilling(false);
    setAllowEmailCalendar(false);
    setAllowTeamManagement(false);
    setSelectedSkillIds([]);
    setSelectedToolIds([]);
  }

  function toggleSkill(id: number) {
    setSelectedSkillIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function toggleTool(id: number) {
    setSelectedToolIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !roleDescription.trim() || creating) return;
    setCreating(true);
    setError("");
    try {
      await createAgent({
        name: name.trim(),
        role_description: roleDescription.trim(),
        system_instructions: systemInstructions.trim() || null,
        allow_web_search: allowWebSearch,
        allow_tally_billing: allowTallyBilling,
        allow_email_calendar: allowEmailCalendar,
        allow_team_management: allowTeamManagement,
        assigned_skill_ids: selectedSkillIds,
        assigned_custom_tool_ids: selectedToolIds,
      });
      resetForm();
      setShowCreate(false);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't create that agent.");
    } finally {
      setCreating(false);
    }
  }

  async function handlePause(id: number) {
    setBusyId(id);
    try {
      await pauseAgent(id);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't pause that agent.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleResume(id: number) {
    setBusyId(id);
    try {
      await resumeAgent(id);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't resume that agent.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(id: number) {
    setBusyId(id);
    try {
      await deleteAgent(id);
      setAgents((prev) => prev.filter((a) => a.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove that agent.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleTogglePermission(agent: AgentOut, field: "allow_web_search" | "allow_tally_billing" | "allow_email_calendar" | "allow_team_management") {
    setBusyId(agent.id);
    try {
      await updateAgent(agent.id, { [field]: !agent[field] });
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't update that agent.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleToggleAgentTool(agent: AgentOut, toolId: number) {
    setBusyId(agent.id);
    try {
      const next = agent.assigned_custom_tool_ids.includes(toolId)
        ? agent.assigned_custom_tool_ids.filter((id) => id !== toolId)
        : [...agent.assigned_custom_tool_ids, toolId];
      await updateAgent(agent.id, { assigned_custom_tool_ids: next });
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't update that agent's tools.");
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

  const activeSkills = skills.filter((s) => s.status === "active");

  return (
    <div className="flex-1 flex flex-col max-w-3xl w-full mx-auto p-4">
      <header className="flex items-center justify-between border-b pb-3 mb-4">
        <div>
          <h1 className="text-lg font-semibold">🏭 Agents</h1>
          <p className="text-xs text-gray-500">
            Named personas that use JARVIS&apos;s existing chat, memory, skills and
            Tally billing — with a toolbox you restrict per agent
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link href="/tools" className="text-sm text-gray-500 underline">
            🧰 Tools
          </Link>
          <Link href="/settings/google" className="text-sm text-gray-500 underline">
            📧 Email + Calendar
          </Link>
          <Link href="/chat" className="text-sm text-gray-500 underline">
            ← Back to chat
          </Link>
        </div>
      </header>

      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {!showCreate ? (
        <button
          onClick={() => setShowCreate(true)}
          className="mb-4 bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium self-start"
        >
          + New agent
        </button>
      ) : (
        <form onSubmit={handleCreate} className="border rounded-lg p-3 mb-4 space-y-2 bg-gray-50">
          <p className="text-sm font-medium">New agent</p>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name, e.g. SS Retail Billing & Ops"
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          <textarea
            value={roleDescription}
            onChange={(e) => setRoleDescription(e.target.value)}
            placeholder="What is this agent for? e.g. Handles billing and day-to-day ops questions for SS Retail Services."
            rows={2}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          <textarea
            value={systemInstructions}
            onChange={(e) => setSystemInstructions(e.target.value)}
            placeholder="Optional — any extra behavior/persona detail"
            rows={2}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />

          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={allowWebSearch}
                onChange={(e) => setAllowWebSearch(e.target.checked)}
              />
              Can search the web
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={allowTallyBilling}
                onChange={(e) => setAllowTallyBilling(e.target.checked)}
              />
              Can bill in Tally
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={allowEmailCalendar}
                onChange={(e) => setAllowEmailCalendar(e.target.checked)}
              />
              Can use email + calendar
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={allowTeamManagement}
                onChange={(e) => setAllowTeamManagement(e.target.checked)}
              />
              Can manage the team
            </label>
          </div>

          {activeSkills.length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-1">
                Skills this agent draws on (none selected = no skills, not all of them)
              </p>
              <div className="flex flex-wrap gap-2">
                {activeSkills.map((s) => (
                  <label
                    key={s.id}
                    className={`text-xs rounded-full px-2.5 py-1 border cursor-pointer ${
                      selectedSkillIds.includes(s.id)
                        ? "bg-black text-white border-black"
                        : "bg-white text-gray-600 border-gray-300"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="hidden"
                      checked={selectedSkillIds.includes(s.id)}
                      onChange={() => toggleSkill(s.id)}
                    />
                    {s.name}
                  </label>
                ))}
              </div>
            </div>
          )}

          {tools.length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-1">
                Custom tools this agent can use (none selected = none, not all
                of them —{" "}
                <Link href="/tools" className="underline">
                  manage tools
                </Link>
                )
              </p>
              <div className="flex flex-wrap gap-2">
                {tools.map((t) => (
                  <label
                    key={t.id}
                    className={`text-xs rounded-full px-2.5 py-1 border cursor-pointer ${
                      selectedToolIds.includes(t.id)
                        ? "bg-black text-white border-black"
                        : "bg-white text-gray-600 border-gray-300"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="hidden"
                      checked={selectedToolIds.includes(t.id)}
                      onChange={() => toggleTool(t.id)}
                    />
                    {t.name}
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || !name.trim() || !roleDescription.trim()}
              className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
            >
              {creating ? "Creating..." : "Create agent"}
            </button>
            <button
              type="button"
              onClick={() => {
                setShowCreate(false);
                resetForm();
              }}
              className="text-sm text-gray-500 underline"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <p className="text-sm text-gray-400">Loading agents...</p>
      ) : agents.length === 0 ? (
        <p className="text-sm text-gray-400 text-center mt-4">
          No agents yet — create one above to give JARVIS a focused persona
          with its own restricted toolbox.
        </p>
      ) : (
        <div className="space-y-2">
          {agents.map((a) => (
            <AgentCard
              key={a.id}
              agent={a}
              skills={skills}
              tools={tools}
              busy={busyId === a.id}
              onPause={() => handlePause(a.id)}
              onResume={() => handleResume(a.id)}
              onDelete={() => handleDelete(a.id)}
              onToggleWebSearch={() => handleTogglePermission(a, "allow_web_search")}
              onToggleTallyBilling={() => handleTogglePermission(a, "allow_tally_billing")}
              onToggleEmailCalendar={() => handleTogglePermission(a, "allow_email_calendar")}
              onToggleTeamManagement={() => handleTogglePermission(a, "allow_team_management")}
              onToggleTool={(toolId) => handleToggleAgentTool(a, toolId)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function AgentCard({
  agent,
  skills,
  tools,
  busy,
  onPause,
  onResume,
  onDelete,
  onToggleWebSearch,
  onToggleTallyBilling,
  onToggleEmailCalendar,
  onToggleTeamManagement,
  onToggleTool,
}: {
  agent: AgentOut;
  skills: Skill[];
  tools: CustomToolOut[];
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  onDelete: () => void;
  onToggleWebSearch: () => void;
  onToggleTallyBilling: () => void;
  onToggleEmailCalendar: () => void;
  onToggleTeamManagement: () => void;
  onToggleTool: (toolId: number) => void;
}) {
  const assignedSkillNames = agent.assigned_skill_ids
    .map((id) => skills.find((s) => s.id === id)?.name)
    .filter((n): n is string => Boolean(n));
  const assignedToolNames = agent.assigned_custom_tool_ids
    .map((id) => tools.find((t) => t.id === id)?.name)
    .filter((n): n is string => Boolean(n));

  return (
    <div className="border rounded-lg p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {agent.name}
            {agent.status === "paused" && (
              <span className="ml-2 text-xs text-gray-400 font-normal">(paused)</span>
            )}
          </p>
          <p className="text-xs text-gray-500 mt-0.5">{agent.role_description}</p>
          <p className="text-xs text-gray-400 mt-1">
            {agent.allow_web_search ? "🔎 Web search on" : "🔎 Web search off"}
            {" · "}
            {agent.allow_tally_billing ? "🧾 Tally billing on" : "🧾 Tally billing off"}
            {" · "}
            {agent.allow_email_calendar ? "📧 Email + calendar on" : "📧 Email + calendar off"}
            {" · "}
            {agent.allow_team_management ? "🧑‍🤝‍🧑 Team management on" : "🧑‍🤝‍🧑 Team management off"}
            {assignedSkillNames.length > 0 && <> · 🎓 {assignedSkillNames.join(", ")}</>}
            {assignedToolNames.length > 0 && <> · 🧰 {assignedToolNames.join(", ")}</>}
          </p>
        </div>
        <div className="flex gap-2 shrink-0 flex-wrap justify-end">
          <Link
            href={`/chat?agent=${agent.id}`}
            className={`text-xs rounded px-2 py-1 ${
              agent.status === "active"
                ? "bg-black text-white"
                : "bg-gray-200 text-gray-400 pointer-events-none"
            }`}
          >
            Chat
          </Link>
          {agent.status === "active" ? (
            <button
              onClick={onPause}
              disabled={busy}
              className="text-xs text-gray-500 underline disabled:opacity-50"
            >
              Pause
            </button>
          ) : (
            <button
              onClick={onResume}
              disabled={busy}
              className="text-xs text-gray-500 underline disabled:opacity-50"
            >
              Resume
            </button>
          )}
          <button
            onClick={onDelete}
            disabled={busy}
            className="text-xs text-red-600 underline disabled:opacity-50"
          >
            Delete
          </button>
        </div>
      </div>
      <div className="mt-2 pt-2 border-t flex items-center gap-4 text-xs text-gray-500">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={agent.allow_web_search}
            disabled={busy}
            onChange={onToggleWebSearch}
          />
          Web search
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={agent.allow_tally_billing}
            disabled={busy}
            onChange={onToggleTallyBilling}
          />
          Tally billing
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={agent.allow_email_calendar}
            disabled={busy}
            onChange={onToggleEmailCalendar}
          />
          Email + calendar
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={agent.allow_team_management}
            disabled={busy}
            onChange={onToggleTeamManagement}
          />
          Team management
        </label>
      </div>
      {tools.length > 0 && (
        <div className="mt-2 pt-2 border-t flex flex-wrap items-center gap-3 text-xs text-gray-500">
          {tools.map((t) => (
            <label key={t.id} className="flex items-center gap-1.5 cursor-pointer">
              <input
                type="checkbox"
                checked={agent.assigned_custom_tool_ids.includes(t.id)}
                disabled={busy}
                onChange={() => onToggleTool(t.id)}
              />
              {t.name}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
