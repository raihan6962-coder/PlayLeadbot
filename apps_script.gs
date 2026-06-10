// ============================================================
// PlayLead Engine — Google Apps Script  (Fixed Edition)
// ============================================================
// Deploy as: Extensions → Apps Script → Deploy → Web App
//   Execute as: Me
//   Who has access: Anyone
// Copy the Web App URL → paste into dashboard as "Sheet URL"
// ============================================================

var SHEET_NAME = "PlayLeads";   // Your Google Spreadsheet name

// Tab names — must match exactly what main.py sends
var TAB_ALL_LEADS       = "All Leads";
var TAB_QUALIFIED       = "Qualified Leads";
var TAB_EMAIL_SENT      = "Email Sent";
var TAB_KEYWORD_LOG     = "Keyword Log";
var TAB_UNSUBSCRIBES    = "Unsubscribes";

// Column headers for each tab
var HEADERS = {
  "All Leads":       ["App Name","Developer","Email","Category","Installs","Score","URL","Keyword","Scraped At","Email Sent","App ID"],
  "Qualified Leads": ["App Name","Developer","Email","Category","Installs","Score","URL","Keyword","Scraped At","Email Sent","App ID"],
  "Email Sent":      ["App ID","App Name","Email","Sent At"],
  "Keyword Log":     ["Keyword","Leads Found","Logged At"],
  "Unsubscribes":    ["Email","At"],
};

// ── Entry point ───────────────────────────────────────────────────────────────
function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);
    var action  = payload.action || "";
    var result  = {};

    if      (action === "append")      result = handleAppend(payload);
    else if (action === "mark_sent")   result = handleMarkSent(payload);
    else if (action === "get_all")     result = handleGetAll(payload);
    else if (action === "get_pending") result = handleGetPending();
    else if (action === "get_analytics") result = handleAnalytics();
    else                               result = { error: "unknown action: " + action };

    return respond(result);
  } catch (err) {
    return respond({ error: err.message });
  }
}

function doGet(e) {
  return respond({ ok: true, msg: "PlayLead Apps Script is live" });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function respond(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function getOrCreateSheet(tabName) {
  var ss    = SpreadsheetApp.openById(getSpreadsheetId());
  var sheet = ss.getSheetByName(tabName);
  if (!sheet) {
    sheet = ss.insertSheet(tabName);
    var headers = HEADERS[tabName];
    if (headers) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      sheet.getRange(1, 1, 1, headers.length)
           .setBackground("#1a1a2e")
           .setFontColor("#ffffff")
           .setFontWeight("bold");
      sheet.setFrozenRows(1);
    }
  }
  return sheet;
}

function getSpreadsheetId() {
  // Reads from Script Properties → set via:
  //   Project Settings → Script Properties → SPREADSHEET_ID
  var id = PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID");
  if (!id) {
    // Fallback: use the spreadsheet this script is bound to (if bound)
    id = SpreadsheetApp.getActiveSpreadsheet().getId();
  }
  return id;
}

function colIndex(tabName, colName) {
  var headers = HEADERS[tabName] || [];
  var idx     = headers.indexOf(colName);
  return idx === -1 ? -1 : idx + 1;  // 1-based for Sheets API
}

// ── ACTION: append ────────────────────────────────────────────────────────────
function handleAppend(payload) {
  var tabName = payload.tab;
  var rowData = payload.row || {};
  if (!tabName || !rowData) return { error: "tab and row required" };

  var sheet   = getOrCreateSheet(tabName);
  var headers = HEADERS[tabName];
  if (!headers) return { error: "unknown tab: " + tabName };

  var rowArr = headers.map(function(h) {
    var val = rowData[h];
    return (val === undefined || val === null) ? "" : val;
  });

  sheet.appendRow(rowArr);
  return { ok: true, tab: tabName };
}

// ── ACTION: mark_sent ─────────────────────────────────────────────────────────
function handleMarkSent(payload) {
  var appId = (payload.app_id || "").trim();
  if (!appId) return { error: "app_id required" };

  // Update "Email Sent" column in All Leads
  var updated = _updateColumnWhere(TAB_ALL_LEADS, "App ID", appId, "Email Sent", "Yes");
  // Update "Email Sent" column in Qualified Leads
  _updateColumnWhere(TAB_QUALIFIED, "App ID", appId, "Email Sent", "Sent");

  return { ok: true, updated: updated };
}

function _updateColumnWhere(tabName, keyCol, keyVal, updateCol, updateVal) {
  var sheet = getOrCreateSheet(tabName);
  var data  = sheet.getDataRange().getValues();
  if (data.length < 2) return 0;

  var headers    = data[0];
  var keyColIdx  = headers.indexOf(keyCol);
  var updColIdx  = headers.indexOf(updateCol);
  if (keyColIdx === -1 || updColIdx === -1) return 0;

  var updated = 0;
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][keyColIdx]).trim() === String(keyVal).trim()) {
      sheet.getRange(i + 1, updColIdx + 1).setValue(updateVal);
      updated++;
    }
  }
  return updated;
}

// ── ACTION: get_all ───────────────────────────────────────────────────────────
function handleGetAll(payload) {
  var tabName = payload.tab || TAB_ALL_LEADS;
  var sheet   = getOrCreateSheet(tabName);
  var data    = sheet.getDataRange().getValues();

  if (data.length < 2) return { records: [] };

  var headers = data[0];
  var records = [];
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var obj = {};
    for (var j = 0; j < headers.length; j++) {
      obj[headers[j]] = row[j] !== undefined ? String(row[j]) : "";
    }
    records.push(obj);
  }
  return { records: records, count: records.length };
}

// ── ACTION: get_pending ───────────────────────────────────────────────────────
// Returns leads from Qualified Leads where Email Sent = "Pending"
function handleGetPending() {
  var sheet = getOrCreateSheet(TAB_QUALIFIED);
  var data  = sheet.getDataRange().getValues();
  if (data.length < 2) return { leads: [], count: 0 };

  var headers      = data[0];
  var sentColIdx   = headers.indexOf("Email Sent");
  var leads        = [];

  for (var i = 1; i < data.length; i++) {
    var sentVal = String(data[i][sentColIdx] || "").trim().toLowerCase();
    if (sentVal === "pending" || sentVal === "no" || sentVal === "") {
      var obj = {};
      for (var j = 0; j < headers.length; j++) {
        obj[headers[j]] = data[i][j] !== undefined ? String(data[i][j]) : "";
      }
      // Remap to what main.py expects
      leads.push({
        app_id:     obj["App ID"]    || "",
        app_name:   obj["App Name"]  || "",
        developer:  obj["Developer"] || "",
        email:      obj["Email"]     || "",
        category:   obj["Category"]  || "",
        installs:   parseInt(obj["Installs"]) || 0,
        score:      parseFloat(obj["Score"])  || null,
        url:        obj["URL"]       || "",
        keyword:    obj["Keyword"]   || "",
        scraped_at: obj["Scraped At"]|| "",
        email_sent: false,
      });
    }
  }
  return { leads: leads, count: leads.length };
}

// ── ACTION: get_analytics ─────────────────────────────────────────────────────
function handleAnalytics() {
  var ss     = SpreadsheetApp.openById(getSpreadsheetId());
  var events = [];

  // Total leads
  var allSheet = ss.getSheetByName(TAB_ALL_LEADS);
  if (allSheet) {
    var totalRows = Math.max(0, allSheet.getLastRow() - 1);
    events.push({ label: "Total Leads", value: totalRows });
  }

  // Emails sent
  var sentSheet = ss.getSheetByName(TAB_EMAIL_SENT);
  if (sentSheet) {
    var sentRows = Math.max(0, sentSheet.getLastRow() - 1);
    events.push({ label: "Emails Sent", value: sentRows });
  }

  // Keywords used
  var kwSheet = ss.getSheetByName(TAB_KEYWORD_LOG);
  if (kwSheet) {
    var kwRows = Math.max(0, kwSheet.getLastRow() - 1);
    events.push({ label: "Keywords Used", value: kwRows });
  }

  // Unsubscribes
  var unsubSheet = ss.getSheetByName(TAB_UNSUBSCRIBES);
  if (unsubSheet) {
    var unsubRows = Math.max(0, unsubSheet.getLastRow() - 1);
    events.push({ label: "Unsubscribes", value: unsubRows });
  }

  return { ok: true, events: events };
}
