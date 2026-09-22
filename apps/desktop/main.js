const path = require("path");
const { spawn } = require("child_process");
const treeKill = require("tree-kill");
const { app, BrowserWindow, Menu, shell, Tray, nativeImage } = require("electron");

// --- Paths -------------------------------------------------------------
// apps/desktop is two levels under the JARVIS project root (…/JARVIS/apps/desktop).
const PROJECT_ROOT = path.resolve(__dirname, "..", "..");
const BACKEND_DIR = path.join(PROJECT_ROOT, "apps", "backend");
const WEB_DIR = path.join(PROJECT_ROOT, "apps", "web");
const INFRA_DIR = path.join(PROJECT_ROOT, "infra");
const VENV_PYTHON = path.join(BACKEND_DIR, "venv", "Scripts", "python.exe");

const JARVIS_WEB_URL = process.env.JARVIS_WEB_URL || "http://localhost:3000";

// Set JARVIS_SKIP_AUTOSTART=1 before launching (e.g. `set JARVIS_SKIP_AUTOSTART=1 && npm start`)
// to fall back to the old behavior of just showing the window and expecting
// Docker/backend/web to already be running in their own terminals — useful
// if something below ever needs debugging directly.
const SKIP_AUTOSTART = process.env.JARVIS_SKIP_AUTOSTART === "1";

let mainWindow = null;
let tray = null;
let isQuitting = false;
let cleanedUp = false;
let dockerInterval = null;
let backendProc = null;
let webProc = null;

function log(name, data) {
  const text = data.toString().trim();
  if (text) console.log(`[${name}] ${text}`);
}

// Runs `docker compose up -d` in infra/ on a repeating timer. This is a
// one-shot command (not a long-running server), and it's safe to re-run
// over and over — if the containers are already up it does nothing, and if
// Docker Desktop was still starting (or wasn't open yet) when JARVIS
// launched, this keeps retrying until it succeeds instead of requiring a
// restart of the desktop app.
function ensureDockerCompose() {
  try {
    const child = spawn("docker", ["compose", "up", "-d"], {
      cwd: INFRA_DIR,
      shell: true,
    });
    child.stdout.on("data", (d) => log("docker", d));
    child.stderr.on("data", (d) => log("docker", d));
  } catch (err) {
    log("docker", String(err));
  }
}

// A "supervised" long-running process: if it exits for any reason (crash,
// port conflict, database not ready yet, etc.) it's automatically restarted
// after a short delay, until .stop() is called. This covers the common
// startup race where the backend starts before Postgres is actually ready.
function superviseProcess(name, command, args, options) {
  let child = null;
  let stopped = false;

  function spawnIt() {
    if (stopped) return;
    child = spawn(command, args, options);
    child.stdout.on("data", (d) => log(name, d));
    child.stderr.on("data", (d) => log(name, d));
    child.on("exit", (code, signal) => {
      log(name, `exited (code=${code}, signal=${signal})`);
      if (!stopped) setTimeout(spawnIt, 2000);
    });
    child.on("error", (err) => {
      log(name, `failed to start: ${err.message}`);
    });
  }

  spawnIt();

  return {
    stop: () =>
      new Promise((resolve) => {
        stopped = true;
        if (child && child.pid) {
          treeKill(child.pid, "SIGTERM", () => resolve());
        } else {
          resolve();
        }
      }),
  };
}

function startServices() {
  if (SKIP_AUTOSTART) {
    log("jarvis", "JARVIS_SKIP_AUTOSTART=1 set — not auto-starting Docker/backend/web.");
    return;
  }

  ensureDockerCompose();
  dockerInterval = setInterval(ensureDockerCompose, 20000);

  backendProc = superviseProcess(
    "backend",
    VENV_PYTHON,
    ["-m", "uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"],
    { cwd: BACKEND_DIR }
  );

  webProc = superviseProcess("web", "npm", ["run", "dev"], {
    cwd: WEB_DIR,
    shell: true,
  });
}

async function stopServices() {
  if (dockerInterval) clearInterval(dockerInterval);
  const stops = [];
  if (backendProc) stops.push(backendProc.stop());
  if (webProc) stops.push(webProc.stop());
  await Promise.all(stops);
}

// --- Window / menu / tray ----------------------------------------------

function buildMenu() {
  const template = [
    {
      label: "JARVIS",
      submenu: [
        {
          label: "Reload",
          accelerator: "CmdOrCtrl+R",
          click: () => mainWindow && mainWindow.reload(),
        },
        {
          label: "Toggle DevTools",
          accelerator: "CmdOrCtrl+Shift+I",
          click: () => mainWindow && mainWindow.webContents.toggleDevTools(),
        },
        { type: "separator" },
        {
          label: "Quit",
          accelerator: "CmdOrCtrl+Q",
          click: () => {
            isQuitting = true;
            app.quit();
          },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function loadingHtml(message) {
  return `data:text/html;charset=utf-8,${encodeURIComponent(`
    <html>
      <body style="margin:0;display:flex;align-items:center;justify-content:center;height:100vh;
                   background:#0f172a;color:#e2e8f0;font-family:Segoe UI,Arial,sans-serif;">
        <div style="text-align:center;max-width:480px;padding:24px;">
          <h2 style="margin-bottom:8px;">Starting JARVIS…</h2>
          <p style="color:#94a3b8;line-height:1.5;">${message}</p>
        </div>
      </body>
    </html>
  `)}`;
}

function tryLoad() {
  if (!mainWindow) return;
  mainWindow.loadURL(JARVIS_WEB_URL).catch(() => {
    mainWindow.loadURL(
      loadingHtml(
        SKIP_AUTOSTART
          ? `Waiting for the JARVIS web app at ${JARVIS_WEB_URL}. Make sure Docker Desktop, ` +
              `the backend (uvicorn), and the web app (npm run dev) are all running — this ` +
              `window will connect automatically once they're up.`
          : `Starting Docker, the backend, and the web app for you — this can take up to a ` +
              `minute the first time (longer if Docker Desktop itself still needs to open). ` +
              `This window will connect automatically as soon as everything is ready.`
      )
    );
    setTimeout(tryLoad, 3000);
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "JARVIS",
    backgroundColor: "#0f172a",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Links that try to open a new window (target="_blank", etc.) open in the
  // user's normal browser instead of spawning a second Electron window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // Closing the window minimizes JARVIS to the system tray instead of
  // quitting it, like a normal background assistant app — use the tray
  // menu's "Quit JARVIS" (or Ctrl+Q) to actually exit.
  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  buildMenu();
  tryLoad();

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "tray-icon.png"));
  tray = new Tray(icon);
  tray.setToolTip("JARVIS");

  function buildTrayMenu() {
    const startsAtLogin = app.getLoginItemSettings().openAtLogin;
    return Menu.buildFromTemplate([
      {
        label: "Open JARVIS",
        click: () => {
          if (mainWindow) {
            mainWindow.show();
            mainWindow.focus();
          }
        },
      },
      { type: "separator" },
      {
        label: "Start JARVIS when Windows starts",
        type: "checkbox",
        checked: startsAtLogin,
        click: (menuItem) => {
          app.setLoginItemSettings({ openAtLogin: menuItem.checked });
        },
      },
      { type: "separator" },
      {
        label: "Quit JARVIS",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]);
  }

  tray.setContextMenu(buildTrayMenu());
  tray.on("click", () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

app.whenReady().then(() => {
  startServices();
  createWindow();
  createTray();
});

app.on("window-all-closed", () => {
  // Do nothing — JARVIS lives in the tray. It only fully quits via the tray
  // menu, the app menu's Quit, or Ctrl+Q, all of which set isQuitting first.
});

app.on("activate", () => {
  if (mainWindow) {
    mainWindow.show();
  } else {
    createWindow();
  }
});

// Make sure the backend/web child processes (and their own children — e.g.
// uvicorn's --reload watcher, or the node process npm run dev spawns) are
// actually killed before JARVIS exits, instead of leaving them running in
// the background. before-quit fires first and lets us delay the real quit
// until cleanup finishes.
app.on("before-quit", () => {
  isQuitting = true;
});

app.on("will-quit", (event) => {
  if (cleanedUp) return;
  event.preventDefault();
  cleanedUp = true;
  stopServices().finally(() => app.quit());
});
