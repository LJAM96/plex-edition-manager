const statusEls = {
  state: document.querySelector("[data-state]"),
  flag: document.querySelector("[data-flag]"),
  exit: document.querySelector("[data-exit]"),
  progressLabel: document.querySelector("[data-progress-label]"),
  progressBar: document.getElementById("progressBar"),
  logWindow: document.getElementById("logWindow"),
};

const runButtons = document.querySelectorAll("[data-run]");
const cancelButton = document.querySelector("[data-cancel]");
const settingsForm = document.getElementById("settingsForm");
const moduleList = document.getElementById("moduleList");
const moduleOrderInput = document.getElementById("moduleOrderInput");
const serverSchemeField = document.querySelector("select[name='server_scheme']");
const serverHostField = document.querySelector("input[name='server_host']");
const serverPortField = document.querySelector("input[name='server_port']");
const serverTokenField = document.querySelector("input[name='server_token']");
const connectionBtn = document.getElementById("testConnectionBtn");
const connectionStatus = document.getElementById("connectionStatus");

let pollTimer = null;

function updateUI(data) {
  if (!data) return;

  const running = Boolean(data.running);
  const progress = Number(data.progress || 0);
  const counts = data.progress_counts || {};
  const done = Number(counts.done ?? NaN);
  const total = Number(counts.total ?? NaN);

  if (statusEls.state) statusEls.state.textContent = running ? "Running" : "Idle";
  if (statusEls.flag) statusEls.flag.textContent = data.current_flag || "None";
  if (statusEls.exit) statusEls.exit.textContent = data.exit_code ?? "—";
  if (statusEls.progressLabel) {
    const precision = progress >= 10 ? 1 : 2;
    let label = `${progress.toFixed(precision)}%`;
    if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
      const doneFmt = Math.floor(done).toLocaleString();
      const totalFmt = Math.floor(total).toLocaleString();
      label += ` (${doneFmt}/${totalFmt})`;
    }
    statusEls.progressLabel.textContent = label;
  }
  if (statusEls.progressBar) statusEls.progressBar.value = progress;

  const logWindow = statusEls.logWindow;
  if (logWindow) {
    const shouldStick =
      logWindow.scrollTop + logWindow.clientHeight >= logWindow.scrollHeight - 10;
    logWindow.textContent = (data.logs || []).join("\n");
    if (shouldStick) {
      logWindow.scrollTop = logWindow.scrollHeight;
    }
  }

  runButtons.forEach((btn) => {
    btn.disabled = running;
  });
  if (cancelButton) {
    cancelButton.disabled = !running;
  }
}

async function fetchStatus() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error("Status request failed");
    const payload = await response.json();
    updateUI(payload);
  } catch (err) {
    console.error(err);
  }
}

async function triggerAction(action) {
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || "Failed to start task");
    }
    setTimeout(fetchStatus, 500);
  } catch (err) {
    alert(err.message);
  }
}

async function cancelAction() {
  try {
    const response = await fetch("/api/cancel", { method: "POST" });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || "Unable to stop task");
    }
    setTimeout(fetchStatus, 500);
  } catch (err) {
    alert(err.message);
  }
}

function syncModuleOrder() {
  if (!moduleList || !moduleOrderInput) return;
  const order = [];
  moduleList.querySelectorAll("li").forEach((item) => {
    const checkbox = item.querySelector("input[type='checkbox']");
    if (checkbox?.checked) {
      order.push(item.dataset.module);
      item.classList.add("is-enabled");
    } else {
      item.classList.remove("is-enabled");
    }
  });
  moduleOrderInput.value = order.join(";");
}

function moveModule(li, direction) {
  if (!moduleList) return;
  const sibling = direction === "up" ? li.previousElementSibling : li.nextElementSibling;
  if (!sibling) return;
  if (direction === "up") {
    moduleList.insertBefore(li, sibling);
  } else {
    moduleList.insertBefore(sibling, li);
  }
  syncModuleOrder();
}

function setupModuleBuilder() {
  if (!moduleList) return;
  moduleList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-move]");
    if (!button) return;
    const li = button.closest("li");
    if (!li) return;
    moveModule(li, button.dataset.move);
  });
  moduleList.addEventListener("change", (event) => {
    if (event.target.matches("input[type='checkbox']")) {
      syncModuleOrder();
    }
  });
  syncModuleOrder();
}

function setConnectionStatus(message, state = "idle") {
  if (!connectionStatus) return;
  connectionStatus.textContent = message;
  connectionStatus.dataset.state = state;
}

async function testServerConnection() {
  if (!connectionBtn) return;
  const scheme = serverSchemeField?.value || "http";
  const host = (serverHostField?.value || "").trim();
  const port = (serverPortField?.value || "").trim();
  const token = (serverTokenField?.value || "").trim();
  if (!host || !token) {
    setConnectionStatus("Enter host/IP and token first.", "error");
    return;
  }
  setConnectionStatus("Testing connection…", "pending");
  connectionBtn.disabled = true;
  try {
    const response = await fetch("/api/server/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scheme, host, port, token }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "Connection failed");
    const name = data.server_name || host;
    setConnectionStatus(`Connected to ${name}`, "success");
  } catch (err) {
    setConnectionStatus(err.message, "error");
  } finally {
    connectionBtn.disabled = false;
  }
}

function init() {
  runButtons.forEach((btn) => {
    btn.addEventListener("click", () => triggerAction(btn.dataset.run));
  });
  cancelButton?.addEventListener("click", cancelAction);
  settingsForm?.addEventListener("submit", () => syncModuleOrder());
  setupModuleBuilder();
  connectionBtn?.addEventListener("click", (event) => {
    event.preventDefault();
    testServerConnection();
  });

  updateUI(window.__INITIAL_STATUS__ || {});
  pollTimer = setInterval(fetchStatus, 2000);
}

window.addEventListener("DOMContentLoaded", init);
