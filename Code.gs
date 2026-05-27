// ═══════════════════════════════════════════════════════════════════════════
// PlayLead Engine — Google Apps Script (Code.gs)  v2.1
// ═══════════════════════════════════════════════════════════════════════════
//
// DEPLOYMENT STEPS:
// 1. Open Google Sheets → Extensions → Apps Script
// 2. Paste this entire file as Code.gs
// 3. Click Deploy → New Deployment → Web App
//    - Execute as: Me
//    - Who has access: Anyone
// 4. Copy the Web App URL → paste into PlayLead Settings (Sheet URLs field)
// 5. For sender alias: add the alias in Gmail → Settings → "Send mail as"
//    then put that address in PlayLead Settings → Sender Alias field.
//
// TABS CREATED AUTOMATICALLY:
//   All Leads | Qualified Leads | Email Sent | Keyword Log
// ═══════════════════════════════════════════════════════════════════════════

const SPREADSHEET_ID = ""; // Leave blank to use the sheet this script is attached to

// ── Tab schemas ───────────────────────────────────────────────────────────────
const TAB_SCHEMAS = {
  "All Leads": [
    "App Name","Developer","Email","Category","Installs","Score",
    "Ratings","URL","Keyword","Mode","Scraped At","Email Sent",
    "App ID","Rating Confidence"
  ],
  "Qualified Leads": [
    "App Name","Developer","Email","Category","Installs","Score",
    "URL","Keyword","Mode","Scraped At","Email Sent","App ID"
  ],
  "Email Sent": [
    "App ID","App Name","Email","Sent At"
  ],
  "Keyword Log": [
    "Keyword","Leads Found","Mode","Logged At"
  ]
};

// ─────────────────────────────────────────────────────────────────────────────
// Entry point
// ─────────────────────────────────────────────────────────────────────────────
function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const action  = payload.action || "";

    if (action === "append")      return respond(handleAppend(payload));
    if (action === "mark_sent")   return respond(handleMarkSent(payload));
    if (action === "get_all")     return respond(handleGetAll(payload));
    if (action === "get_pending") return respond(handleGetPending());
    if (action === "send_email")  return respond(handleSendEmail(payload));
    if (action === "init_tabs")   return respond(handleInitTabs());

    return respond({ status: "error", msg: "Unknown action: " + action });
  } catch (err) {
    return respond({ status: "error", msg: err.toString() });
  }
}

function doGet(e) {
  return respond({ status: "ok", ts: new Date().toISOString() });
}

function respond(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}

// ─────────────────────────────────────────────────────────────────────────────
// Sheet helpers
// ─────────────────────────────────────────────────────────────────────────────
function getSpreadsheet() {
  if (SPREADSHEET_ID) return SpreadsheetApp.openById(SPREADSHEET_ID);
  return SpreadsheetApp.getActiveSpreadsheet();
}

function getOrCreateTab(ss, tabName) {
  let sheet = ss.getSheetByName(tabName);
  if (!sheet) {
    sheet = ss.insertSheet(tabName);
    const headers = TAB_SCHEMAS[tabName];
    if (headers) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      sheet.getRange(1, 1, 1, headers.length)
        .setBackground("#1a1a2e")
        .setFontColor("#e8c76a")
        .setFontWeight("bold");
      sheet.setFrozenRows(1);
    }
  }
  return sheet;
}

function getHeaders(sheet) {
  const lastCol = sheet.getLastColumn();
  if (lastCol === 0) return [];
  return sheet.getRange(1, 1, 1, lastCol).getValues()[0];
}

function rowToObj(headers, rowValues) {
  const obj = {};
  headers.forEach((h, i) => { obj[h] = rowValues[i] || ""; });
  return obj;
}

// ─────────────────────────────────────────────────────────────────────────────
// HTML email builder with professional unsubscribe footer
// ─────────────────────────────────────────────────────────────────────────────
function buildHtmlEmail(plainText, senderEmail, senderName) {
  // Escape HTML entities in the body
  const escaped = plainText
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Convert plain-text lines → HTML paragraphs (blank lines become spacers)
  const htmlBody = escaped
    .split("\n")
    .map(function(line) {
      const trimmed = line.trim();
      if (trimmed === "") return '<tr><td style="height:10px"></td></tr>';
      return '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
           + 'line-height:1.75;color:#2c2c2c;padding:0 0 2px 0">'
           + trimmed + "</td></tr>";
    })
    .join("\n");

  // Unsubscribe mailto using the actual sending address
  const unsubLink =
    "mailto:" + senderEmail +
    "?subject=Unsubscribe&body=Hi%2C%20please%20remove%20me%20from%20your%20mailing%20list.%20Thank%20you.";

  return '<!DOCTYPE html>\n'
    + '<html lang="en">\n'
    + '<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>\n'
    + '<body style="margin:0;padding:0;background-color:#f0f0f0">\n'

    // ── Outer wrapper
    + '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
    +   'style="background-color:#f0f0f0;padding:32px 16px">\n'
    + '<tr><td align="center">\n'

    // ── Card
    + '<table width="600" cellpadding="0" cellspacing="0" border="0" '
    +   'style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;'
    +   'overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08)">\n'

    // ── Top accent bar
    + '<tr><td height="5" style="background:linear-gradient(90deg,#c9a84c,#e8c76a,#c9a84c);'
    +   'font-size:0;line-height:0">&nbsp;</td></tr>\n'

    // ── Body content
    + '<tr><td style="padding:36px 44px 28px 44px">\n'
    + '<table width="100%" cellpadding="0" cellspacing="0" border="0">\n'
    + htmlBody + "\n"
    + '</table>\n'
    + '</td></tr>\n'

    // ── Divider
    + '<tr><td style="padding:0 44px">'
    +   '<div style="border-top:1px solid #ececec"></div>'
    + '</td></tr>\n'

    // ── Unsubscribe footer
    + '<tr><td style="padding:24px 44px 32px 44px;text-align:center">\n'
    +   '<p style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
    +     'color:#aaaaaa;margin:0 0 16px 0;line-height:1.6">\n'
    +     'You received this message because your app was discovered on Google Play Store.<br>\n'
    +     'To opt out of future outreach from ' + (senderName || senderEmail) + ', click below.\n'
    +   '</p>\n'
    +   '<a href="' + unsubLink + '" '
    +     'style="display:inline-block;padding:9px 28px;'
    +     'background:#f7f7f7;color:#777777;'
    +     'text-decoration:none;border-radius:6px;'
    +     'border:1px solid #dddddd;'
    +     'font-family:Arial,Helvetica,sans-serif;'
    +     'font-size:12px;font-weight:500;letter-spacing:0.4px;'
    +     'transition:background 0.2s">\n'
    +     'Unsubscribe\n'
    +   '</a>\n'
    + '</td></tr>\n'

    // ── Bottom accent bar
    + '<tr><td height="4" style="background:linear-gradient(90deg,#c9a84c,#e8c76a,#c9a84c);'
    +   'font-size:0;line-height:0">&nbsp;</td></tr>\n'

    + '</table>\n'   // end card
    + '</td></tr>\n'
    + '</table>\n'   // end outer
    + '</body>\n</html>';
}

// ─────────────────────────────────────────────────────────────────────────────
// Action: send_email
// ─────────────────────────────────────────────────────────────────────────────
function handleSendEmail(payload) {
  const to        = payload.to;
  const subject   = payload.subject;
  const body      = payload.body;
  const fromAlias = payload.from_alias || "";
  const senderName = payload.sender_name || "";

  if (!to || !subject || !body) {
    return { status: "error", msg: "to, subject, body required" };
  }

  try {
    const options = { name: senderName, noReply: false };

    // Resolve the actual sending address for the unsubscribe link
    let actualSender = Session.getActiveUser().getEmail();

    if (fromAlias) {
      const aliases = getAvailableAliases_();
      if (aliases.includes(fromAlias.toLowerCase())) {
        options.from = fromAlias;
        actualSender = fromAlias;   // unsubscribe link uses the alias
      } else {
        console.log("Alias not available, falling back to primary: " + fromAlias);
      }
    }

    // Build styled HTML email with unsubscribe footer
    options.htmlBody = buildHtmlEmail(body, actualSender, senderName);

    GmailApp.sendEmail(to, subject, body, options);

    return { status: "ok", to: to, alias_used: options.from || "primary" };
  } catch (err) {
    return { status: "error", msg: err.toString() };
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Action: append
// ─────────────────────────────────────────────────────────────────────────────
function handleAppend(payload) {
  const ss      = getSpreadsheet();
  const tabName = payload.tab || "All Leads";
  const sheet   = getOrCreateTab(ss, tabName);
  const headers = getHeaders(sheet);
  const row     = payload.row || {};

  const rowArr = headers.map(function(h) {
    const v = row[h];
    return (v !== undefined && v !== null) ? v : "";
  });

  sheet.appendRow(rowArr);
  return { status: "ok", tab: tabName };
}

// ─────────────────────────────────────────────────────────────────────────────
// Action: mark_sent
// ─────────────────────────────────────────────────────────────────────────────
function handleMarkSent(payload) {
  const ss    = getSpreadsheet();
  const appId = String(payload.app_id || "").trim();
  if (!appId) return { status: "error", msg: "app_id required" };

  ["All Leads", "Qualified Leads"].forEach(function(tabName) {
    const sheet = ss.getSheetByName(tabName);
    if (!sheet) return;
    const headers = getHeaders(sheet);
    const idCol   = headers.indexOf("App ID") + 1;
    const sentCol = headers.indexOf("Email Sent") + 1;
    if (idCol < 1 || sentCol < 1) return;

    const lastRow = sheet.getLastRow();
    if (lastRow < 2) return;
    const idValues = sheet.getRange(2, idCol, lastRow - 1, 1).getValues();
    for (var i = 0; i < idValues.length; i++) {
      if (String(idValues[i][0]).trim() === appId) {
        sheet.getRange(i + 2, sentCol).setValue("Yes");
      }
    }
  });

  return { status: "ok" };
}

// ─────────────────────────────────────────────────────────────────────────────
// Action: get_all
// ─────────────────────────────────────────────────────────────────────────────
function handleGetAll(payload) {
  const ss      = getSpreadsheet();
  const tabName = payload.tab || "All Leads";
  const sheet   = ss.getSheetByName(tabName);
  if (!sheet || sheet.getLastRow() < 2) return { records: [] };

  const headers   = getHeaders(sheet);
  const lastRow   = sheet.getLastRow();
  const values    = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();
  const records   = values.map(function(row) { return rowToObj(headers, row); });
  return { status: "ok", records: records };
}

// ─────────────────────────────────────────────────────────────────────────────
// Action: get_pending
// ─────────────────────────────────────────────────────────────────────────────
function handleGetPending() {
  const ss    = getSpreadsheet();
  const sheet = ss.getSheetByName("Qualified Leads");
  if (!sheet || sheet.getLastRow() < 2) return { leads: [] };

  const headers = getHeaders(sheet);
  const lastRow = sheet.getLastRow();
  const values  = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();

  const leads = values
    .map(function(row) { return rowToObj(headers, row); })
    .filter(function(obj) {
      const s = String(obj["Email Sent"] || "").trim().toLowerCase();
      return s === "pending" || s === "no" || s === "";
    });

  return { status: "ok", leads: leads };
}

// ─────────────────────────────────────────────────────────────────────────────
// Action: init_tabs
// ─────────────────────────────────────────────────────────────────────────────
function handleInitTabs() {
  const ss      = getSpreadsheet();
  const created = [];
  Object.keys(TAB_SCHEMAS).forEach(function(tabName) {
    const existed = !!ss.getSheetByName(tabName);
    getOrCreateTab(ss, tabName);
    if (!existed) created.push(tabName);
  });
  return { status: "ok", created: created };
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper: available Gmail aliases
// ─────────────────────────────────────────────────────────────────────────────
function getAvailableAliases_() {
  try {
    return [];
  } catch(e) {
    return [];
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Spreadsheet open trigger
// ─────────────────────────────────────────────────────────────────────────────
function onOpen() {
  handleInitTabs();
  const ui = SpreadsheetApp.getUi();
  ui.createMenu("PlayLead Engine")
    .addItem("Initialize Tabs", "handleInitTabs")
    .addItem("Clear Test Data", "clearTestData_")
    .addToUi();
}

function clearTestData_() {
  const ss = getSpreadsheet();
  ["All Leads","Qualified Leads","Email Sent","Keyword Log"].forEach(function(name) {
    const sheet = ss.getSheetByName(name);
    if (sheet && sheet.getLastRow() > 1) {
      sheet.deleteRows(2, sheet.getLastRow() - 1);
    }
  });
  SpreadsheetApp.getUi().alert("Test data cleared.");
}
