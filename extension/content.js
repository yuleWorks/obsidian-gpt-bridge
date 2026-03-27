const AUTO_SEND_SENTINEL = "!!!DONE!!!";
const CONNECTION_CHECK_INTERVAL_MS = 15000;

function normalizeText(text) {
  return text
    .replace(/\u00a0/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function collectTextFromNode(node) {
  if (!node) {
    return "";
  }

  const text = node.innerText || node.textContent || "";
  return normalizeText(text);
}

function findLatestAssistantMessage() {
  const selectors = [
    "[data-message-author-role='assistant']",
    "[data-testid^='conversation-turn-'] [data-message-author-role='assistant']",
    "article[data-testid^='conversation-turn-']",
    "main article"
  ];

  for (const selector of selectors) {
    const nodes = Array.from(document.querySelectorAll(selector));

    for (let index = nodes.length - 1; index >= 0; index -= 1) {
      const node = nodes[index];
      const text = collectTextFromNode(node);

      if (text) {
        return {
          text,
          selector,
          node
        };
      }
    }
  }

  return null;
}

function buildMessageId(node, text) {
  const container = node?.closest("[data-testid^='conversation-turn-'], article, [data-message-id]");
  const attrs = [
    container?.getAttribute("data-message-id"),
    container?.getAttribute("data-testid"),
    node?.getAttribute("data-message-id"),
    node?.id
  ].filter(Boolean);

  if (attrs.length) {
    return attrs.join(":");
  }

  return text;
}

function buildCaptureResponse(result) {
  return {
    ok: true,
    text: result.text,
    messageId: buildMessageId(result.node, result.text),
    metadata: {
      capturedAt: new Date().toISOString(),
      selector: result.selector,
      pageTitle: document.title,
      pageUrl: window.location.href
    }
  };
}

let autoSendScheduled = false;
let lastObservedCompletedMessageId = null;
let connectionIntervalStarted = false;

async function ensureWebSocketConnection() {
  try {
    await chrome.runtime.sendMessage({
      type: "ensure_websocket_connection"
    });
  } catch (error) {
    console.error("[ObsidianGPT content] Could not wake background connection", error);
  }
}

function startConnectionKeepalive() {
  if (connectionIntervalStarted) {
    return;
  }

  connectionIntervalStarted = true;
  void ensureWebSocketConnection();
  setInterval(() => {
    void ensureWebSocketConnection();
  }, CONNECTION_CHECK_INTERVAL_MS);
}

async function maybeAutoSendLatestAssistantMessage() {
  autoSendScheduled = false;

  const result = findLatestAssistantMessage();

  if (!result) {
    return;
  }

  const trimmedText = result.text.trim();
  const messageId = buildMessageId(result.node, trimmedText);

  if (!trimmedText.endsWith(AUTO_SEND_SENTINEL)) {
    return;
  }

  if (messageId === lastObservedCompletedMessageId) {
    return;
  }

  lastObservedCompletedMessageId = messageId;

  try {
    await chrome.runtime.sendMessage({
      type: "auto_capture_assistant_message",
      text: result.text,
      messageId,
      metadata: {
        capturedAt: new Date().toISOString(),
        selector: result.selector,
        pageTitle: document.title,
        pageUrl: window.location.href
      }
    });
  } catch (error) {
    console.error("[ObsidianGPT content] Auto-send failed", error);
  }
}

function scheduleAutoSendCheck() {
  if (autoSendScheduled) {
    return;
  }

  autoSendScheduled = true;
  setTimeout(() => {
    void maybeAutoSendLatestAssistantMessage();
  }, 300);
}

const observer = new MutationObserver(() => {
  scheduleAutoSendCheck();
});

function startObserver() {
  if (!document.body) {
    return;
  }

  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true
  });

  startConnectionKeepalive();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      void ensureWebSocketConnection();
    }
  });
  window.addEventListener("focus", () => {
    void ensureWebSocketConnection();
  });

  scheduleAutoSendCheck();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "capture_latest_assistant_message") {
    return false;
  }

  const result = findLatestAssistantMessage();

  if (!result) {
    sendResponse({
      ok: false,
      error: "Could not find a non-empty assistant message on the page."
    });
    return false;
  }

  sendResponse(buildCaptureResponse(result));
  return false;
});

if (document.body) {
  startObserver();
} else {
  window.addEventListener(
    "DOMContentLoaded",
    () => {
      startObserver();
    },
    { once: true }
  );
}
