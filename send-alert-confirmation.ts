// Courtready: send the confirmation email the moment someone signs up.
//
// Triggered by a Supabase Database Webhook on INSERT into court_alerts.
// Paste this into the Supabase dashboard: Edge Functions, Deploy a new
// function, Via Editor. Name it exactly: send-alert-confirmation
//
// Secrets it needs (Edge Functions, Secrets tab):
//   POSTMARK_TOKEN    server API token for the Winnipeg Court Dates server
//   POSTMARK_FROM     admin@courtdates.ca
//   POSTMARK_STREAM   optional, defaults to "outbound"
//   WEBHOOK_SECRET    any long random string, also set on the webhook
//
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are provided automatically.
//
// A copy of this file lives in the repo because the dashboard editor has
// no version history. If you change it in one place, change it in both.

const POSTMARK_TOKEN = Deno.env.get("POSTMARK_TOKEN") ?? "";
const POSTMARK_FROM = Deno.env.get("POSTMARK_FROM") ?? "";
const POSTMARK_STREAM = Deno.env.get("POSTMARK_STREAM") ?? "outbound";
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const SENDER_NAME = "Courtready";
const REPLY_TO = "tom@courtready.ca";
const TOOL_URL = "https://courtready.ca/winnipeg-court-dates-finder/";

const KEEP = new Set(["ISO", "I.S.O.", "MVA", "AM", "PM", "KB", "BDO", "MNP"]);
const MINOR = new Set([
  "OF", "TO", "THE", "AND", "OR", "FOR", "IN", "ON", "AT", "A", "AN",
  "BY", "WITH", "FROM", "INTO", "PER",
]);

// Same rules as the website, so the email reads the way the page did.
function tidy(name: string): string {
  const out = String(name).replace(/[A-Za-z0-9.]+/g, (tok, at: number) => {
    const ord = tok.match(/^(\d+)(ST|ND|RD|TH)$/i);
    if (ord) return ord[1] + ord[2].toLowerCase();
    const up = tok.toUpperCase();
    if (KEEP.has(up)) return up;
    if (/\d/.test(tok)) return tok;
    if (at > 0 && MINOR.has(up)) return tok.toLowerCase();
    return tok.charAt(0).toUpperCase() + tok.slice(1).toLowerCase();
  });
  return out.replace(/'S\b/g, "'s");
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// "Small Claims Winnipeg - 1st App (9:00AM), Option B"
function fullLabel(row: Record<string, unknown>): string {
  const name = tidy(String(row.hearing_name ?? "your hearing type"));
  const opt = String(row.option_label ?? "").trim();
  if (opt) return `${name}, ${opt}`;
  return name;
}

function buildEmail(label: string) {
  const safe = escapeHtml(label);

  const subject = `Alert confirmed: ${label}`;

  const text =
    `This confirms you have signed up for an alert for ${label} at the ` +
    `Winnipeg Court of King's Bench.\n\n` +
    `We will email you once, from this address, when that category next ` +
    `has available dates. This is not a subscription and you will not ` +
    `hear from us about anything else.\n\n` +
    `If you did not sign up for this, reply to this email and we will ` +
    `remove you.\n\n` +
    `Questions: Tom Macintosh Zheng, ${REPLY_TO}\n\n` +
    `---\n` +
    `Courtready.ca's Winnipeg Court Dates Finder\n${TOOL_URL}\n`;

  const html =
    `<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;` +
    `line-height:1.6;color:#2f2f2f;max-width:520px">` +
    `<p style="margin:0 0 16px">This confirms you have signed up for an ` +
    `alert for <strong>${safe}</strong> at the Winnipeg Court of King's ` +
    `Bench.</p>` +
    `<p style="margin:0 0 16px">We will email you once, from this address, ` +
    `when that category next has available dates. This is not a ` +
    `subscription and you will not hear from us about anything else.</p>` +
    `<p style="margin:0 0 16px">If you did not sign up for this, reply to ` +
    `this email and we will remove you.</p>` +
    `<p style="margin:0 0 16px">Questions: Tom Macintosh Zheng, ` +
    `<a href="mailto:${REPLY_TO}" style="color:#c87040">${REPLY_TO}</a></p>` +
    `<hr style="border:none;border-top:1px solid #e5e1db;margin:20px 0">` +
    `<p style="margin:0;font-size:12.5px;color:#857a72">` +
    `<a href="${TOOL_URL}" style="color:#857a72">Courtready.ca's Winnipeg ` +
    `Court Dates Finder</a></p></div>`;

  return { subject, text, html };
}

async function markConfirmed(id: number) {
  await fetch(`${SUPABASE_URL}/rest/v1/court_alerts?id=eq.${id}`, {
    method: "PATCH",
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=minimal",
    },
    body: JSON.stringify({ confirmation_sent_at: new Date().toISOString() }),
  });
}

Deno.serve(async (req) => {
  // Only the database webhook should be able to call this.
  if (WEBHOOK_SECRET) {
    if (req.headers.get("x-webhook-secret") !== WEBHOOK_SECRET) {
      return new Response("no", { status: 401 });
    }
  }

  if (!POSTMARK_TOKEN || !POSTMARK_FROM) {
    console.error("Postmark is not configured. Set POSTMARK_TOKEN and POSTMARK_FROM.");
    return new Response("not configured", { status: 200 });
  }

  let payload: Record<string, unknown>;
  try {
    payload = await req.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }

  const row = (payload.record ?? payload) as Record<string, unknown>;
  const email = String(row.email ?? "").trim();
  const id = Number(row.id ?? 0);

  if (!email || !id) {
    console.error("Payload had no email or id.", JSON.stringify(payload).slice(0, 300));
    return new Response("nothing to do", { status: 200 });
  }

  const { subject, text, html } = buildEmail(fullLabel(row));

  const res = await fetch("https://api.postmarkapp.com/email", {
    method: "POST",
    headers: {
      "X-Postmark-Server-Token": POSTMARK_TOKEN,
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      From: `${SENDER_NAME} <${POSTMARK_FROM}>`,
      To: email,
      ReplyTo: REPLY_TO,
      Subject: subject,
      TextBody: text,
      HtmlBody: html,
      MessageStream: POSTMARK_STREAM,
      Headers: [{
        Name: "List-Unsubscribe",
        Value: `<mailto:${REPLY_TO}?subject=unsubscribe>`,
      }],
    }),
  });

  if (res.ok) {
    await markConfirmed(id);
    console.log(`Confirmation sent to ${email} for row ${id}.`);
    return new Response("sent", { status: 200 });
  }

  // Leave confirmation_sent_at empty. The scraper picks up stragglers,
  // so a failure here is recoverable rather than lost.
  const body = await res.text();
  console.error(`Postmark ${res.status} for row ${id}: ${body.slice(0, 300)}`);
  return new Response("send failed", { status: 200 });
});
