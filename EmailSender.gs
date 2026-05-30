// ═══════════════════════════════════════════════════════════════════════════
// PlayLead Engine — Email Sender Script (EmailSender.gs)  v2.2
// ═══════════════════════════════════════════════════════════════════════════
//
// DEPLOYMENT STEPS:
// 1. Google Apps Script project খোলো (script.google.com → New Project)
// 2. সব code delete করে এই পুরো file paste করো
// 3. Deploy → New Deployment → Web App
//    - Execute as: Me
//    - Who has access: Anyone
// 4. Deploy → Authorize → URL copy করো
// 5. PlayLead Dashboard → Settings → Email Script URLs এ paste করো
// ═══════════════════════════════════════════════════════════════════════════


function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);
    var action  = payload.action || "send_email";
    if (action === "send_email" || action === "send") return respond(handleSendEmail(payload));
    if (action === "ping" || action === "health")    return respond({ status: "ok", ts: new Date().toISOString() });
    return respond({ status: "error", msg: "Unknown action: " + action });
  } catch (err) {
    return respond({ status: "error", msg: err.toString() });
  }
}

function doGet(e) {
  return respond({ status: "ok", service: "PlayLead Email Sender", ts: new Date().toISOString() });
}

function respond(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}


// ─────────────────────────────────────────────────────────────────────────────
// Send Email
// ─────────────────────────────────────────────────────────────────────────────
function handleSendEmail(payload) {
  var to              = payload.to              || "";
  var subject         = payload.subject         || "";
  var body            = payload.body            || "";
  var fromEmail       = payload.from_email      || "";   // Sending email address from Settings
  var senderName      = payload.sender_name     || "";   // Sender name from Settings
  var trackingPixelUrl = payload.tracking_pixel_url || ""; // Open tracking pixel URL

  if (!to || !subject || !body) {
    return { status: "error", msg: "Missing required fields: to, subject, body" };
  }
  if (!to.match(/^[^\s@]+@[^\s@]+\.[^\s@]+$/)) {
    return { status: "error", msg: "Invalid email address: " + to };
  }

  try {
    var options = { noReply: false };

    // Set sender name if provided
    if (senderName && senderName.trim() !== "") {
      options.name = senderName;
    }

    // Set sending address if provided
    // This must be the primary Gmail address OR a configured "Send mail as" alias
    if (fromEmail && fromEmail.trim() !== "") {
      options.from = fromEmail.trim();
    }

    // Build styled HTML email with tracking pixel + unsubscribe footer
    var actualSender = fromEmail || Session.getActiveUser().getEmail();
    options.htmlBody = buildHtmlEmail_(body, actualSender, senderName, trackingPixelUrl);

    GmailApp.sendEmail(to, subject, body, options);

    return {
      status:      "ok",
      to:          to,
      sender_used: options.from || Session.getActiveUser().getEmail()
    };

  } catch (err) {
    return { status: "error", msg: err.toString() };
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// Build HTML Email — professional card + unsubscribe + tracking pixel
// ─────────────────────────────────────────────────────────────────────────────
function buildHtmlEmail_(plainText, senderEmail, senderName, trackingPixelUrl) {
  var escaped = plainText
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;");

  var htmlRows = escaped.split("\n").map(function(line) {
    var t = line.trim();
    if (t === "") return '<tr><td style="height:10px;font-size:0;line-height:0">&nbsp;</td></tr>';
    return '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.75;color:#2c2c2c;padding:0 0 2px 0">' + t + '</td></tr>';
  }).join("\n");

  var displayName = senderName || senderEmail;
  var unsubLink   = "mailto:" + senderEmail
    + "?subject=Unsubscribe"
    + "&body=Hi%2C%20please%20remove%20me%20from%20your%20mailing%20list.%20Thank%20you.";

  // Tracking pixel (1x1 transparent image — loads when email is opened)
  var pixelHtml = "";
  if (trackingPixelUrl && trackingPixelUrl !== "") {
    pixelHtml = '<img src="' + trackingPixelUrl + '" width="1" height="1" alt="" border="0"'
      + ' style="display:block;width:1px;height:1px;overflow:hidden;mso-hide:all">';
  }

  return '<!DOCTYPE html>'
    + '<html lang="en"><head><meta charset="UTF-8">'
    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + '<meta name="color-scheme" content="light"></head>'
    + '<body style="margin:0;padding:0;background-color:#f0f0f0">'
    // Outer wrapper
    + '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f0f0f0;padding:32px 16px">'
    + '<tr><td align="center">'
    // Card
    + '<table width="600" cellpadding="0" cellspacing="0" border="0"'
    + ' style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08)">'
    // Top gold bar
    + '<tr><td height="5" style="background:linear-gradient(90deg,#c9a84c,#e8c76a,#c9a84c);font-size:0;line-height:0">&nbsp;</td></tr>'
    // Body
    + '<tr><td style="padding:36px 44px 28px 44px">'
    + '<table width="100%" cellpadding="0" cellspacing="0" border="0">' + htmlRows + '</table>'
    + '</td></tr>'
    // Divider
    + '<tr><td style="padding:0 44px"><div style="border-top:1px solid #ececec"></div></td></tr>'
    // Unsubscribe footer
    + '<tr><td style="padding:22px 44px 30px 44px;text-align:center">'
    + '<p style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#aaaaaa;margin:0 0 14px 0;line-height:1.6">'
    + 'You received this message because your app was discovered on Google Play Store.<br>'
    + 'To opt out of future outreach from <strong>' + displayName + '</strong>, click below.'
    + '</p>'
    + '<a href="' + unsubLink + '" style="display:inline-block;padding:9px 28px;background:#f7f7f7;color:#777777;'
    + 'text-decoration:none;border-radius:6px;border:1px solid #dddddd;'
    + 'font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:500;letter-spacing:0.4px">'
    + 'Unsubscribe</a>'
    + '</td></tr>'
    // Bottom gold bar
    + '<tr><td height="4" style="background:linear-gradient(90deg,#c9a84c,#e8c76a,#c9a84c);font-size:0;line-height:0">&nbsp;</td></tr>'
    + '</table>'
    + '</td></tr></table>'
    // Tracking pixel at very end of body
    + pixelHtml
    + '</body></html>';
}


// ─────────────────────────────────────────────────────────────────────────────
// Test function — run manually in Apps Script editor to verify sending works
// ─────────────────────────────────────────────────────────────────────────────
function testSendEmail() {
  var result = handleSendEmail({
    to:               "your-test-email@gmail.com",  // ← তোমার email দাও
    subject:          "PlayLead Engine — Test Email",
    body:             "Hi,\n\nThis is a test email from PlayLead Engine v2.\n\nBest,\nYour Name",
    sender_name:      "Your Name",
    from_email:       "your@gmail.com",             // ← তোমার Gmail address দাও
    tracking_pixel_url: ""
  });
  console.log(JSON.stringify(result, null, 2));
}
