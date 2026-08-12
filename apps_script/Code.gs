/**
 * Brainboard Apps Script bindings.
 *
 * Wires the Master Log V2 spreadsheet (Master Log V2 tab + 9 macro silo
 * tabs) to the FastAPI backend's /webhooks/* routes.
 *
 * IMPORTANT -- installation:
 *   The reserved simple-trigger function `onEdit(e)` runs in a
 *   restricted, unauthorized execution context and CANNOT call
 *   UrlFetchApp against an external host. This file therefore does NOT
 *   define `onEdit`. Instead run `setupTriggers()` once from the Apps
 *   Script editor (select it in the function dropdown and click Run) to
 *   install `installableOnEdit` as an installable "On edit" trigger,
 *   which runs with full authorization.
 *
 * Script Properties required (Project Settings -> Script Properties):
 *   BACKEND_BASE_URL       e.g. https://your-vps-host/  (no trailing slash)
 *   WEBHOOK_SHARED_SECRET  must match the backend's WEBHOOK_SHARED_SECRET
 */

const MASTER_LOG_SHEET_NAME = "Master Log V2";

const SILO_TAB_NAMES = [
  "CME Macro Funnel",
  "Tariff Silo",
  "Food Processing & Fabrication Silo",
  "Oil & Gas / Refining Silo",
  "Heavy Machinery & Industrial Equipment Silo",
  "Agriculture & Grain Handling Silo",
  "Healthcare & Pharma Silo",
  "E-Commerce & Fulfillment Silo",
  "HigherGov Funnel",
];

const CO_BROKERS = [
  "Nick F", "Mike F", "Vinny", "Victor", "Gallo", "Shaun", "Kris", "DanStol",
  "Roman", "James", "Alfred", "Marcus", "Ricky", "Zack", "Emilio", "Jorge", "Tony", "Seb",
];

const MASTER_LOG_STATUSES = [
  "New lead", "App Sent", "Docs Owed", "Chase Docs", "Docs in", "In negotiation",
  "Offer Made Not Sold", "Sold Deal Killed", "Deal Stalled Proxy Pass", "Funded",
  "Ghosted", "Loss to Competitor", "Dog Shit",
];

// Master Log V2 column indices (1-based, A=1)
const ML_COL_UID = 1; // A
const ML_COL_COBROKER = 6; // F
const ML_COL_STATUS = 7; // G
const ML_COL_FOLLOWUP = 11; // K
const ML_COL_NOTES = 12; // L
const ML_COL_DOSSIER = 24; // X

// Silo tab column indices (1-based, A=1) -- 8-column schema
const SILO_COL_UID = 1; // A
const SILO_COL_PHONE = 3; // C
const SILO_COL_DOSSIER = 6; // F
const SILO_COL_STATUS = 8; // H

function getConfig_() {
  const props = PropertiesService.getScriptProperties();
  const baseUrl = props.getProperty("BACKEND_BASE_URL");
  const secret = props.getProperty("WEBHOOK_SHARED_SECRET");
  if (!baseUrl || !secret) {
    throw new Error("Set BACKEND_BASE_URL and WEBHOOK_SHARED_SECRET in Script Properties first.");
  }
  return { baseUrl: baseUrl.replace(/\/+$/, ""), secret };
}

function postToBackend_(path, payload) {
  const { baseUrl, secret } = getConfig_();
  const response = UrlFetchApp.fetch(baseUrl + path, {
    method: "post",
    contentType: "application/json",
    headers: { "X-Webhook-Secret": secret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  if (code >= 400) {
    console.error(`POST ${path} failed (${code}): ${response.getContentText()}`);
  }
  return response;
}

/** Run once from the Apps Script editor to install the trigger. */
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach((t) => {
    if (t.getHandlerFunction() === "installableOnEdit") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("installableOnEdit").forSpreadsheet(SpreadsheetApp.getActive()).onEdit().create();
}

function installableOnEdit(e) {
  if (!e || !e.range) return;
  const sheetName = e.range.getSheet().getName();

  if (sheetName === MASTER_LOG_SHEET_NAME) {
    onMasterLogEdit(e);
  } else if (SILO_TAB_NAMES.indexOf(sheetName) !== -1) {
    onSiloStatusEdit(e);
  }
}

/**
 * Column K (follow-up date) or Column L (notes) mutated -> push to
 * Postgres + sync the linked Calendar event via lead_uid.
 */
function onMasterLogEdit(e) {
  const col = e.range.getColumn();
  if (col !== ML_COL_FOLLOWUP && col !== ML_COL_NOTES) return;

  const sheet = e.range.getSheet();
  const row = e.range.getRow();
  if (row === 1) return; // header row

  let leadUid = sheet.getRange(row, ML_COL_UID).getValue();
  if (!leadUid) {
    leadUid = Utilities.getUuid();
    sheet.getRange(row, ML_COL_UID).setValue(leadUid);
  }

  const followUpRaw = sheet.getRange(row, ML_COL_FOLLOWUP).getValue();
  const notes = sheet.getRange(row, ML_COL_NOTES).getValue();

  const payload = {
    lead_uid: String(leadUid),
    notes: notes ? String(notes) : null,
  };
  if (followUpRaw instanceof Date) {
    payload.follow_up_date = followUpRaw.toISOString();
  }

  postToBackend_("/webhooks/master-log-edit", payload);
}

/**
 * Column H (status) mutated on a silo tab -> convert/dismiss the
 * candidate in Postgres. Converting requires a co-broker assignment
 * (the 8-column silo schema has no co-broker column), so the user is
 * prompted for one at the moment of conversion.
 */
function onSiloStatusEdit(e) {
  const col = e.range.getColumn();
  if (col !== SILO_COL_STATUS) return;

  const sheet = e.range.getSheet();
  const row = e.range.getRow();
  if (row === 1) return;

  const status = String(e.range.getValue()).trim().toLowerCase();
  if (["pending", "converted", "dismissed"].indexOf(status) === -1) {
    SpreadsheetApp.getUi().alert(`Invalid status "${status}". Use: pending, converted, dismissed.`);
    return;
  }

  let candidateUid = sheet.getRange(row, SILO_COL_UID).getValue();
  if (!candidateUid) {
    candidateUid = Utilities.getUuid();
    sheet.getRange(row, SILO_COL_UID).setValue(candidateUid);
  }

  const payload = { candidate_uid: String(candidateUid), status: status };

  if (status === "converted") {
    const ui = SpreadsheetApp.getUi();
    const result = ui.prompt(
      "Convert candidate",
      `Assign a co-broker (one of: ${CO_BROKERS.join(", ")})`,
      ui.ButtonSet.OK_CANCEL
    );
    if (result.getSelectedButton() !== ui.Button.OK) return;
    const coBroker = result.getResponseText().trim();
    if (CO_BROKERS.indexOf(coBroker) === -1) {
      ui.alert(`"${coBroker}" is not a valid co-broker. Conversion aborted.`);
      return;
    }
    payload.co_broker = coBroker;
  }

  postToBackend_("/webhooks/silo-status-edit", payload);
}

/**
 * doPost webhook -- inbound lead intake (e.g. a web form). Appends a row
 * to Master Log V2 and forwards the lead to the backend.
 *
 * Expected JSON body: { business_name, co_broker, contact_name?, phone?,
 * email?, loan_amount_requested? }
 */
function doPost(e) {
  const respond = (statusCode, body) =>
    ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);

  let data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return respond(400, { error: "invalid JSON body" });
  }

  if (!data.business_name || !data.co_broker) {
    return respond(422, { error: "business_name and co_broker are required" });
  }
  if (CO_BROKERS.indexOf(data.co_broker) === -1) {
    return respond(422, { error: `invalid co_broker: ${data.co_broker}` });
  }

  const leadUid = Utilities.getUuid();
  const sheet = SpreadsheetApp.getActive().getSheetByName(MASTER_LOG_SHEET_NAME);
  const row = new Array(24).fill("");
  row[ML_COL_UID - 1] = leadUid;
  row[1] = data.business_name;
  row[2] = data.contact_name || "";
  row[3] = data.phone || "";
  row[4] = data.email || "";
  row[ML_COL_COBROKER - 1] = data.co_broker;
  row[ML_COL_STATUS - 1] = "New lead";
  sheet.appendRow(row);

  postToBackend_("/webhooks/lead-intake", {
    lead_uid: leadUid, // keep Sheets Column A and Postgres keyed on the same UUID
    business_name: data.business_name,
    co_broker: data.co_broker,
    contact_name: data.contact_name || null,
    phone: data.phone || null,
    email: data.email || null,
  });

  return respond(201, { lead_uid: leadUid });
}
