// Preload script — runs before the JARVIS web page loads, with access to
// Node APIs even though the page itself doesn't get any (contextIsolation
// is on, nodeIntegration is off, for safety).
//
// Nothing is exposed to the page yet. This is a placeholder for future
// desktop-only features (system tray controls, native notifications,
// local file access) that the web app doesn't have in a normal browser tab.
// Anything added here should go through contextBridge.exposeInMainWorld,
// never by turning nodeIntegration on.

window.addEventListener("DOMContentLoaded", () => {
  // no-op for now
});
