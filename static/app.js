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
const plexLoginForm = document.getElementById("plexLoginForm");
const plexStatus = document.getElementById("plexLoginStatus");
const plexPicker = document.getElementById("plexServerPicker");
const plexSelect = document.getElementById("plexServerSelect");
const plexApplyBtn = document.getElementById("applyPlexServer");
const serverSchemeField = document.querySelector("select[name='server_scheme']");
const serverHostField = document.querySelector("input[name='server_host']");
const serverPortField = document.querySelector("input[name='server_port']");
const serverTokenField = document.querySelector("input[name='server_token']");

let pollTimer = null;
let plexAuthToken = "";

function updateUI(data) {
  if (!data) return;

  const running = Boolean(data.running);
  const progress = Number(data.progress || 0);

  if (statusEls.state) statusEls.state.textContent = running ? "Running" : "Idle";
  if (statusEls.flag) statusEls.flag.textContent = data.current_flag || "None";
  if (statusEls.exit) statusEls.exit.textContent = data.exit_code ?? "—";
  if (statusEls.progressLabel) statusEls.progressLabel.textContent = `${progress}%`;
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

async function handlePlexLogin(event) {
  event.preventDefault();
  const formData = new FormData(plexLoginForm);
  const payload = {
    username: formData.get("username"),
    password: formData.get("password"),
  };
  const otp = (formData.get("otp") || "").trim();
  if (otp) {
    payload.otp = otp;
  }
  plexStatus.textContent = "Signing in…";
  plexPicker.classList.add("hidden");
  plexSelect.innerHTML = "";
  try {
    const response = await fetch("/api/plex/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "Login failed");
    plexAuthToken = data.token;
    populatePlexServers(data.servers || []);
    plexStatus.textContent = "Token retrieved. Select a server connection below.";
  } catch (err) {
    plexStatus.textContent = err.message;
  }
}

function populatePlexServers(servers) {
  plexSelect.innerHTML = "";
  const options = [];
  servers.forEach((server) => {
    (server.connections || []).forEach((conn) => {
      if (!conn.uri) return;
      const label = `${server.name || "Unnamed"} — ${conn.protocol}://${conn.address}:${conn.port}${
        conn.local ? " (LAN)" : ""
      }`;
      const option = document.createElement("option");
      option.value = conn.uri;
      option.textContent = label;
      options.push(option);
    });
  });
  if (options.length === 0) {
    plexPicker.classList.add("hidden");
    plexStatus.textContent = "No servers were returned for this account.";
    return;
  }
  options.forEach((opt) => plexSelect.appendChild(opt));
  plexPicker.classList.remove("hidden");
}

async function applyPlexServer() {
  if (!plexAuthToken) {
    plexStatus.textContent = "Please sign in first.";
    return;
  }
  const uri = plexSelect.value;
  if (!uri) {
    plexStatus.textContent = "Select a server connection first.";
    return;
  }
  plexStatus.textContent = "Saving server selection…";
  try {
    const response = await fetch("/api/plex/select-server", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: plexAuthToken, uri }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "Unable to save server");
    plexStatus.textContent = "Server settings updated. Remember to save the form below.";
    if (serverTokenField) serverTokenField.value = plexAuthToken;
    if (serverSchemeField && serverHostField && serverPortField && data.address) {
      try {
        const url = new URL(data.address);
        serverSchemeField.value = url.protocol.replace(":", "") || "http";
        serverHostField.value = url.hostname;
        serverPortField.value = url.port || (url.protocol === "https:" ? "443" : "32400");
      } catch (err) {
        console.warn("Unable to parse address", err);
      }
    }
  } catch (err) {
    plexStatus.textContent = err.message;
  }
}

function init() {
  runButtons.forEach((btn) => {
    btn.addEventListener("click", () => triggerAction(btn.dataset.run));
  });
  cancelButton?.addEventListener("click", cancelAction);
  settingsForm?.addEventListener("submit", () => syncModuleOrder());
  setupModuleBuilder();
  plexLoginForm?.addEventListener("submit", handlePlexLogin);
  plexApplyBtn?.addEventListener("click", applyPlexServer);

  updateUI(window.__INITIAL_STATUS__ || {});
  pollTimer = setInterval(fetchStatus, 2000);
}

window.addEventListener("DOMContentLoaded", init);
