const DEFAULT_WS_URL = "ws://127.0.0.1:8765";
const RECONNECT_DELAY_MS = 3000;

let socket = null;
let reconnectTimer = null;
let lastAutoSentMessageId = null;

function log(...args) {
  console.log("[ObsidianGPT background]", ...args);
}

function sendToServer(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    log("WebSocket is not open; dropping payload", payload);
    return;
  }

  socket.send(JSON.stringify(payload));
}

async function getChatGptTabs() {
  return chrome.tabs.query({
    url: [
      "https://chatgpt.com/*",
      "https://*.chatgpt.com/*"
    ]
  });
}

async function requestLatestAssistantMessage() {
  const tabs = await getChatGptTabs();

  if (!tabs.length) {
    sendToServer({
      type: "capture_error",
      error: "No ChatGPT tab is open."
    });
    return;
  }

  const [tab] = tabs;

  try {
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "capture_latest_assistant_message"
    });

    if (!response?.ok) {
      sendToServer({
        type: "capture_error",
        error: response?.error || "Unknown capture failure."
      });
      return;
    }

    sendToServer({
      type: "assistant_message",
      text: response.text,
      metadata: response.metadata || {}
    });
  } catch (error) {
    sendToServer({
      type: "capture_error",
      error: error.message
    });
  }
}

function handleAutoCapturedMessage(message) {
  if (!message?.text) {
    return;
  }

  const messageId = message.messageId || message.text;

  if (messageId === lastAutoSentMessageId) {
    return;
  }

  lastAutoSentMessageId = messageId;
  sendToServer({
    type: "assistant_message",
    text: message.text,
    metadata: {
      ...(message.metadata || {}),
      autoCaptured: true
    }
  });
}

async function handleServerMessage(rawMessage) {
  let message;

  try {
    message = JSON.parse(rawMessage.data);
  } catch (error) {
    log("Received invalid JSON from server", error);
    return;
  }

  switch (message.type) {
    case "ping":
      sendToServer({ type: "pong" });
      break;
    case "capture_latest_assistant_message":
      await requestLatestAssistantMessage();
      break;
    default:
      log("Ignoring unknown message type", message.type);
  }
}

function scheduleReconnect() {
  if (reconnectTimer) {
    return;
  }

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_DELAY_MS);
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  log("Connecting to", DEFAULT_WS_URL);
  socket = new WebSocket(DEFAULT_WS_URL);

  socket.addEventListener("open", () => {
    log("Connected to websocket bridge");
    sendToServer({
      type: "extension_hello",
      source: "chrome-extension"
    });
  });

  socket.addEventListener("message", handleServerMessage);

  socket.addEventListener("close", () => {
    log("WebSocket closed; scheduling reconnect");
    scheduleReconnect();
  });

  socket.addEventListener("error", (error) => {
    log("WebSocket error", error);
    socket?.close();
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message?.type) {
    case "auto_capture_assistant_message":
      handleAutoCapturedMessage(message);
      sendResponse({ ok: true });
      return false;
    case "ensure_websocket_connection":
      connect();
      sendResponse({ ok: true });
      return false;
    default:
      return false;
  }
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
