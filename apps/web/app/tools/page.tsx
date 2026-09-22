"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  getToken,
  listTools,
  createTool,
  updateTool,
  deleteTool,
  type CustomToolOut,
  type CustomToolParam,
} from "@/lib/api";

export default function ToolsPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [tools, setTools] = useState<CustomToolOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [showCreate, setShowCreate] = useState(false);
  // editingId is null while creating a brand-new tool, and the tool's id
  // while the same form is being reused to edit an existing one (added
  // 2026-09-20 - the Tools page originally only had Create/Disable/Delete,
  // no way to fix a mistyped URL short of deleting and recreating the whole
  // tool, which a real user hit almost immediately).
  const [editingId, setEditingId] = useState<number | null>(null);
  const [existingHasAuth, setExistingHasAuth] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [url, setUrl] = useState("");
  const [method, setMethod] = useState("GET");
  const [params, setParams] = useState<CustomToolParam[]>([]);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [authHeaderName, setAuthHeaderName] = useState("Authorization");
  const [authValue, setAuthValue] = useState("");
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
    listTools()
      .then(setTools)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load tools."))
      .finally(() => setLoading(false));
  }

  function resetForm() {
    setEditingId(null);
    setExistingHasAuth(false);
    setName("");
    setDescription("");
    setUrl("");
    setMethod("GET");
    setParams([]);
    setNeedsAuth(false);
    setAuthHeaderName("Authorization");
    setAuthValue("");
  }

  function handleEditClick(tool: CustomToolOut) {
    setEditingId(tool.id);
    setExistingHasAuth(tool.has_auth_value);
    setName(tool.name);
    setDescription(tool.description);
    setUrl(tool.url);
    setMethod(tool.http_method);
    setParams(tool.param_schema.length ? tool.param_schema : []);
    setNeedsAuth(tool.has_auth_value || !!tool.auth_header_name);
    setAuthHeaderName(tool.auth_header_name || "Authorization");
    setAuthValue(""); // the real secret is never sent back by the API - blank means "keep it"
    setError("");
    setShowCreate(true);
  }

  async function handleRemoveKey() {
    if (editingId == null) return;
    setBusyId(editingId);
    try {
      await updateTool(editingId, { clear_auth: true });
      setExistingHasAuth(false);
      setAuthValue("");
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove the API key.");
    } finally {
      setBusyId(null);
    }
  }

  function addParam() {
    setParams((prev) => [...prev, { name: "", description: "", required: false }]);
  }

  function updateParam(i: number, patch: Partial<CustomToolParam>) {
    setParams((prev) => prev.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  }

  function removeParam(i: number) {
    setParams((prev) => prev.filter((_, idx) => idx !== i));
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !description.trim() || !url.trim() || creating) return;
    setCreating(true);
    setError("");
    try {
      if (editingId != null) {
        // Editing: an empty authValue means "leave the existing secret
        // alone" (the real value is never sent back by the API to prefill
        // it, so we can't tell blank-because-untouched apart from
        // blank-on-purpose here - clearing a key is a separate explicit
        // action, the "Remove key" button, not just blanking this field).
        await updateTool(editingId, {
          name: name.trim(),
          description: description.trim(),
          http_method: method,
          url: url.trim(),
          param_schema: params.filter((p) => p.name.trim()),
          auth_header_name: needsAuth ? authHeaderName.trim() || "Authorization" : null,
          ...(needsAuth && authValue ? { auth_value: authValue } : {}),
          is_read_only: method === "GET",
        });
      } else {
        await createTool({
          name: name.trim(),
          description: description.trim(),
          http_method: method,
          url: url.trim(),
          param_schema: params.filter((p) => p.name.trim()),
          auth_header_name: needsAuth ? authHeaderName.trim() || "Authorization" : null,
          auth_value: needsAuth ? authValue : null,
          is_read_only: method === "GET",
        });
      }
      resetForm();
      setShowCreate(false);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Couldn't ${editingId != null ? "update" : "create"} that tool.`);
    } finally {
      setCreating(false);
    }
  }

  async function handleToggleEnabled(tool: CustomToolOut) {
    setBusyId(tool.id);
    try {
      await updateTool(tool.id, { enabled: !tool.enabled });
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't update that tool.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(id: number) {
    setBusyId(id);
    try {
      await deleteTool(id);
      setTools((prev) => prev.filter((t) => t.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove that tool.");
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

  const isReadOnly = method === "GET";

  return (
    <div className="flex-1 flex flex-col max-w-3xl w-full mx-auto p-4">
      <header className="flex items-center justify-between border-b pb-3 mb-4">
        <div>
          <h1 className="text-lg font-semibold">🧰 Tools</h1>
          <p className="text-xs text-gray-500">
            Simple web tools you define yourself — assign them to an agent on
            the Agents page and JARVIS can use them in chat
          </p>
        </div>
        <Link href="/agents" className="text-sm text-gray-500 underline">
          ← Back to agents
        </Link>
      </header>

      {error && <p className="text-sm text-red-600 mb-2">{error}</p>}

      {!showCreate ? (
        <button
          onClick={() => {
            resetForm();
            setShowCreate(true);
          }}
          className="mb-4 bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium self-start"
        >
          + New tool
        </button>
      ) : (
        <form onSubmit={handleCreate} className="border rounded-lg p-3 mb-4 space-y-2 bg-gray-50">
          <p className="text-sm font-medium">{editingId != null ? `Edit tool: ${name || "..."}` : "New tool"}</p>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name, e.g. check_shipment_status"
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What does this do, and when should JARVIS use it? Be specific — this is how JARVIS decides when to call it."
            rows={2}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          />
          <div className="flex gap-2">
            <select
              value={method}
              onChange={(e) => setMethod(e.target.value)}
              className="border rounded-lg px-3 py-2 text-sm"
            >
              <option value="GET">GET (read — called live)</option>
              <option value="POST">POST (action — drafted only)</option>
              <option value="PUT">PUT (action — drafted only)</option>
              <option value="PATCH">PATCH (action — drafted only)</option>
              <option value="DELETE">DELETE (action — drafted only)</option>
            </select>
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://..."
              className="flex-1 border rounded-lg px-3 py-2 text-sm"
            />
          </div>
          {!isReadOnly && (
            <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1.5">
              This is an action call. In v1, JARVIS will always show you exactly
              what it would send, but never sends it automatically — you'd
              need to send it yourself.
            </p>
          )}

          <div>
            <div className="flex items-center justify-between">
              <p className="text-xs text-gray-500">Parameters JARVIS can fill in</p>
              <button type="button" onClick={addParam} className="text-xs text-gray-500 underline">
                + Add parameter
              </button>
            </div>
            {params.map((p, i) => (
              <div key={i} className="flex gap-1.5 mt-1.5 items-center">
                <input
                  type="text"
                  value={p.name}
                  onChange={(e) => updateParam(i, { name: e.target.value })}
                  placeholder="name"
                  className="w-28 border rounded px-2 py-1 text-xs"
                />
                <input
                  type="text"
                  value={p.description}
                  onChange={(e) => updateParam(i, { description: e.target.value })}
                  placeholder="what is this for?"
                  className="flex-1 border rounded px-2 py-1 text-xs"
                />
                <label className="flex items-center gap-1 text-xs text-gray-500 shrink-0">
                  <input
                    type="checkbox"
                    checked={p.required}
                    onChange={(e) => updateParam(i, { required: e.target.checked })}
                  />
                  required
                </label>
                <button type="button" onClick={() => removeParam(i)} className="text-xs text-red-600">
                  ×
                </button>
              </div>
            ))}
          </div>

          <div>
            <label className="flex items-center gap-1.5 text-sm">
              <input type="checkbox" checked={needsAuth} onChange={(e) => setNeedsAuth(e.target.checked)} />
              This tool needs an API key
            </label>
            {needsAuth && (
              <div className="mt-1.5">
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={authHeaderName}
                    onChange={(e) => setAuthHeaderName(e.target.value)}
                    placeholder="Header name, e.g. Authorization"
                    className="w-48 border rounded-lg px-3 py-2 text-sm"
                  />
                  <input
                    type="password"
                    value={authValue}
                    onChange={(e) => setAuthValue(e.target.value)}
                    placeholder={
                      editingId != null && existingHasAuth
                        ? "Leave blank to keep the existing key"
                        : "The API key / secret value"
                    }
                    className="flex-1 border rounded-lg px-3 py-2 text-sm"
                  />
                </div>
                {editingId != null && existingHasAuth && (
                  <p className="text-xs text-gray-400 mt-1">
                    🔑 A key is already saved for this tool — it's never shown again here.
                    Type a new one above to replace it, or{" "}
                    <button
                      type="button"
                      onClick={handleRemoveKey}
                      disabled={busyId === editingId}
                      className="text-red-600 underline disabled:opacity-50"
                    >
                      remove it
                    </button>
                    .
                  </p>
                )}
              </div>
            )}
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || !name.trim() || !description.trim() || !url.trim()}
              className="bg-black text-white rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50"
            >
              {creating
                ? editingId != null
                  ? "Saving..."
                  : "Creating..."
                : editingId != null
                  ? "Save changes"
                  : "Create tool"}
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
        <p className="text-sm text-gray-400">Loading tools...</p>
      ) : tools.length === 0 ? (
        <p className="text-sm text-gray-400 text-center mt-4">
          No tools yet — create one above, then assign it to an agent so
          JARVIS can use it in chat.
        </p>
      ) : (
        <div className="space-y-2">
          {tools.map((t) => (
            <div key={t.id} className="border rounded-lg p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium">
                    {t.name}
                    {!t.enabled && <span className="ml-2 text-xs text-gray-400 font-normal">(disabled)</span>}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">{t.description}</p>
                  <p className="text-xs text-gray-400 mt-1">
                    {t.http_method} {t.url}
                    {" · "}
                    {t.is_read_only ? "called live" : "drafted only"}
                    {t.has_auth_value && " · 🔑 API key set"}
                  </p>
                </div>
                <div className="flex gap-2 shrink-0 flex-wrap justify-end">
                  <button
                    onClick={() => handleEditClick(t)}
                    disabled={busyId === t.id}
                    className="text-xs text-gray-500 underline disabled:opacity-50"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => handleToggleEnabled(t)}
                    disabled={busyId === t.id}
                    className="text-xs text-gray-500 underline disabled:opacity-50"
                  >
                    {t.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    onClick={() => handleDelete(t.id)}
                    disabled={busyId === t.id}
                    className="text-xs text-red-600 underline disabled:opacity-50"
                  >
                    Delete
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
