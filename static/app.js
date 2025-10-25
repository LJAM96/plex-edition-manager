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

let pollTimer = null;

function updateUI(data) {
  if (!data) return;

  const running = Boolean(data.running);
  const progress = Number(data.progress || 0);

  statusEls.state.textContent = running ? "Running" : "Idle";
  statusEls.flag.textContent = data.current_flag || "None";
  statusEls.exit.textContent = data.exit_code ?? "—";
  statusEls.progressLabel.textContent = `${progress}%`;
  statusEls.progressBar.value = progress;

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
    if (!response.ok) {
      throw new Error("Status request failed");
    }
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
    if (!response.ok) {
      throw new Error(payload.error || "Failed to start task");
    }
    if (payload.error) {
      throw new Error(payload.error);
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
    if (!response.ok) {
      throw new Error(payload.error || "Unable to stop task");
    }
    setTimeout(fetchStatus, 500);
  } catch (err) {
    alert(err.message);
  }
}

function init() {
  runButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      triggerAction(btn.dataset.run);
    });
  });

  if (cancelButton) {
    cancelButton.addEventListener("click", cancelAction);
  }

  updateUI(window.__INITIAL_STATUS__ || {});
  pollTimer = setInterval(fetchStatus, 2000);
}

window.addEventListener("DOMContentLoaded", init);
