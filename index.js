import express from "express";
import fetch from "node-fetch";
import http from "node:http";
import https from "node:https";

const app = express();
app.use(express.json({ limit: "2mb" }));

// 🔥 your SiteGround endpoint
const TARGET_URL = process.env.TARGET_URL;

const LOG_BASE_URL = process.env.LOG_BASE_URL; 
const LOG_TIMEZONE = process.env.LOG_TIMEZONE || "Asia/Singapore";
const MAX_TRACKED = Number(process.env.MAX_TRACKED || 50);

const httpAgent = new http.Agent({ keepAlive: true });
const httpsAgent = new https.Agent({ keepAlive: true });
const agentFor = (url) => (url?.protocol === "http:" ? httpAgent : httpsAgent);

let nextRequestId = 1;
let inFlight = 0;
/** @type {Array<{id:number, receivedAt:string, updateId?:number, chatId?:number, from?:string, summary?:string, status:'queued'|'forwarding'|'forwarded'|'failed', forwardMs?:number, forwardStatus?:number, error?:string}>} */
const recent = [];

function nowIso() {
  return new Date().toISOString();
}

function safeJson(obj, maxChars = 400) {
  try {
    const s = JSON.stringify(obj);
    return s.length > maxChars ? s.slice(0, maxChars) + "…" : s;
  } catch {
    return "<unserializable>";
  }
}

function pushRecent(entry) {
  recent.unshift(entry);
  if (recent.length > MAX_TRACKED) recent.length = MAX_TRACKED;
}

function getMessageText(update) {
  const text = update?.message?.text;
  return typeof text === "string" ? text.trim() : null;
}

function getChatId(update) {
  const id = update?.message?.chat?.id;
  return typeof id === "number" ? id : null;
}

function getFromLabel(update) {
  const from = update?.message?.from;
  if (!from) return null;
  const parts = [from.first_name, from.last_name].filter(Boolean);
  const name = parts.join(" ") || from.username;
  return name || null;
}

function parseCommand(text) {
  if (!text || !text.startsWith("/")) return null;
  const [cmdRaw, ...rest] = text.split(/\s+/);
  const cmd = cmdRaw.split("@")[0].toLowerCase();
  return { cmd, args: rest.join(" ").trim() };
}

function formatQueue() {
  const pending = recent.filter((r) => r.status === "queued" || r.status === "forwarding");
  const last = recent.slice(0, 10);

  const lines = [];
  lines.push(`Render proxy queue`);
  lines.push(`In-flight: ${inFlight}`);
  lines.push(`Pending: ${pending.length}`);
  lines.push("");

  if (pending.length) {
    lines.push("Pending requests:");
    for (const r of pending.slice(0, 10)) {
      lines.push(
        `#${r.id} ${r.status.toUpperCase()} at ${r.receivedAt}${r.summary ? ` — ${r.summary}` : ""}`
      );
    }
    lines.push("");
  }

  lines.push("Last 10 requests:");
  for (const r of last) {
    const meta = [r.status.toUpperCase()];
    if (typeof r.forwardStatus === "number") meta.push(`HTTP ${r.forwardStatus}`);
    if (typeof r.forwardMs === "number") meta.push(`${r.forwardMs}ms`);
    lines.push(
      `#${r.id} ${meta.join(" • ")} at ${r.receivedAt}${r.summary ? ` — ${r.summary}` : ""}`
    );
  }

  lines.push("");
  lines.push("Tip: cold starts are normal on free tiers.");
  lines.push("If you keep seeing long delays, keep Render warm with a cron ping.");
  return truncateForTelegram(lines.join("\n"));
}

function truncateForTelegram(text, max = 3900) {
  if (!text) return "";
  if (text.length <= max) return text;
  return text.slice(0, max) + "\n…(truncated)";
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function formatDateForTz(date, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type)?.value;
  const y = get("year");
  const m = get("month");
  const d = get("day");
  if (!y || !m || !d) return null;
  return `${y}-${m}-${d}`;
}

function extractLatestLogBlock(logText) {
  const sep = "-----------------------------------";
  const chunks = logText.split(sep).map((c) => c.trim()).filter(Boolean);
  if (chunks.length === 0) return truncateForTelegram(logText);
  const last = chunks[chunks.length - 1];
  return truncateForTelegram(last);
}

async function fetchLogsForDate(dateStr) {
  if (!LOG_BASE_URL) {
    return {
      ok: false,
      message:
        "LOG_BASE_URL is not set on Render. Set it to your domain",
    };
  }

  const url = `${LOG_BASE_URL.replace(/\/$/, "")}/generatepdf-logs/debug-${dateStr}.log`;
  try {
    const resp = await fetchWithTimeout(
      url,
      {
        method: "GET",
        headers: { "User-Agent": "render-link/1.0" },
        agent: agentFor(new URL(url)),
      },
      4000
    );

    const text = await resp.text();
    if (!resp.ok) {
      return {
        ok: false,
        message: `Could not fetch logs (${resp.status}). URL: ${url}\nBody: ${truncateForTelegram(text, 1200)}`,
      };
    }

    return { ok: true, url, text };
  } catch (e) {
    return { ok: false, message: `Log fetch error: ${e?.message || String(e)}` };
  }
}

app.post("/webhook", async (req, res) => {
  const id = nextRequestId++;
  const receivedAt = nowIso();
  const update = req.body;
  const updateId = typeof update?.update_id === "number" ? update.update_id : undefined;
  const chatId = getChatId(update) ?? undefined;
  const from = getFromLabel(update) ?? undefined;
  const text = getMessageText(update);
  const summary = text
    ? text.slice(0, 80)
    : update?.callback_query
      ? "callback_query"
      : "(non-message update)";

  const entry = {
    id,
    receivedAt,
    updateId,
    chatId,
    from,
    summary,
    status: "queued",
  };
  pushRecent(entry);

  let didIncrementInFlight = false;

  try {
    console.log(`[${id}] Incoming update_id=${updateId ?? "?"} chat=${chatId ?? "?"} from=${from ?? "?"}`);
    if (text) console.log(`[${id}] Text: ${text}`);

    const cmd = parseCommand(text);
    if (cmd?.cmd === "/queue") {
      entry.status = "forwarded";
      if (!chatId) return res.sendStatus(200);
      return res.status(200).json({
        method: "sendMessage",
        chat_id: chatId,
        text: formatQueue(),
      });
    }

    if (cmd?.cmd === "/logs") {
      if (!chatId) {
        entry.status = "forwarded";
        return res.sendStatus(200);
      }
      const arg = cmd.args;
      const dateStr =
        arg && /^\d{4}-\d{2}-\d{2}$/.test(arg)
          ? arg
          : formatDateForTz(new Date(), LOG_TIMEZONE);

      if (!dateStr) {
        entry.status = "forwarded";
        return res.status(200).json({
          method: "sendMessage",
          chat_id: chatId,
          text: "Could not compute today’s date for logs. Try `/logs YYYY-MM-DD`.",
        });
      }

      const result = await fetchLogsForDate(dateStr);
      entry.status = "forwarded";

      if (!result.ok) {
        return res.status(200).json({
          method: "sendMessage",
          chat_id: chatId,
          text: truncateForTelegram(`Logs (${dateStr})\n${result.message}`),
        });
      }

      const block = extractLatestLogBlock(result.text);
      const msg = truncateForTelegram(
        `Logs (${dateStr})\nSource: ${result.url}\n\n${block}\n\nIf this looks stuck, try /queue to see if forwarding is still in-flight.`
      );

      return res.status(200).json({
        method: "sendMessage",
        chat_id: chatId,
        text: msg,
      });
    }

    if (!TARGET_URL) {
      entry.status = "failed";
      entry.error = "TARGET_URL not set";
      console.error(`[${id}] Missing TARGET_URL env var`);
      return res.sendStatus(200);
    }

    // forward to your PHP
    entry.status = "forwarding";
    inFlight++;
    didIncrementInFlight = true;
    const start = Date.now();

    const target = new URL(TARGET_URL);
    const response = await fetchWithTimeout(
      TARGET_URL,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Webhook-Secret": process.env.WEBHOOK_SECRET,
        },
        body: JSON.stringify(update),
        agent: agentFor(target),
      },
      12_000
    ); // 12s timeout to avoid hanging too long

    const bodyText = await response.text();
    entry.forwardMs = Date.now() - start;
    entry.forwardStatus = response.status;
    entry.status = response.ok ? "forwarded" : "failed";
    if (!response.ok) entry.error = truncateForTelegram(bodyText, 600);

    console.log(`[${id}] Forward result HTTP ${response.status} in ${entry.forwardMs}ms`);
    if (bodyText) console.log(`[${id}] Response from PHP: ${truncateForTelegram(bodyText, 1200)}`);

    // VERY IMPORTANT: reply 200 to Telegram
    res.sendStatus(200);
  } catch (err) {
    entry.status = "failed";
    entry.error = err?.message || String(err);
    console.error(`[${id}] Error:`, err);
    res.sendStatus(200); // still return 200 so Telegram doesn't retry spam
  } finally {
    if (entry.status === "forwarding") entry.status = "failed";
    if (didIncrementInFlight && inFlight > 0) inFlight--;
  }
});

// health check (optional)
app.get("/", (req, res) => {
  res.json({
    ok: true,
    message: "Webhook proxy running",
    inFlight,
    tracked: recent.length,
    targetUrlSet: Boolean(TARGET_URL),
    logBaseUrlSet: Boolean(LOG_BASE_URL),
    now: nowIso(),
  });
});

// Human-friendly queue view (useful for you in browser)
app.get("/queue", (req, res) => {
  res.json({ inFlight, tracked: recent.length, recent });
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});