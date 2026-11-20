/**
 * GeminiNest — Google Apps Script: Auto-export Google Docs in `Gemini_Nest/inbox/` to `.txt`
 *
 * 用途：
 *   Gemini Web 端聊天点击「导出到 Google 文档 (Export to Docs)」并移动到 `Gemini_Nest/inbox/` 后，
 *   Google Drive for Desktop 在本地仅映射为 `.gdoc` 快捷方式指针（无法直接作为纯文本解析）。
 *   本定时脚本运行于 Google 云端，定期将 `inbox/` 内新增或更新的 Google Docs 导出为同名 `.txt`
 *   纯文本文件，供本地 `sync_manager.py` 在次日 Step 1 自动解析 `[MEMORY_PATCH]` 并归档。
 *
 * 防重复机制（双重幂等校验）：
 *   1. 使用 ScriptProperties 记录每个 Doc ID 最近一次导出的 `lastUpdated` ISO 时间戳；
 *      即使本地 `sync_manager` 已经吸收并把 `.txt` 移入 `inbox_archived/`，只要原 Google Doc 未被再次编辑，
 *      本脚本也不会重复导出同一篇文档。
 *   2. 若 `inbox/` 下已存在同名 `.txt` 且其更新时间不早于 Google Doc，同样直接跳过。
 *
 * 部署步骤：
 *   1. 打开 https://script.google.com/ 新建项目，粘贴本脚本并保存。
 *   2. 如需指定精确文件夹 ID，可填写可选常量 `INBOX_FOLDER_ID`；留空则自动查找 `Gemini_Nest/inbox`。
 *   3. 手动运行一次 `exportInboxGoogleDocsToTxt()` 完成 Google Drive / Docs 权限授权。
 *   4. 运行一次 `installTimeDrivenTrigger()` 安装每 15 分钟执行一次的云端定时触发器。
 */

var CONFIG = {
  // 云端根目录与收件箱目录名（按路径查找时使用）
  NEST_ROOT_FOLDER_NAME: 'Gemini_Nest',
  INBOX_FOLDER_NAME: 'inbox',
  ARCHIVED_FOLDER_NAME: 'inbox_archived',

  // 可选：直接填 `inbox` 文件夹 ID（留空则按名称路径自动查找）
  INBOX_FOLDER_ID: '',

  // 导出后是否将原 Google Doc 移至 `inbox_archived/`（默认 false，靠 ScriptProperties 防重即可）
  MOVE_EXPORTED_GDOC_TO_ARCHIVED: false,

  // 定时触发间隔（分钟；Google Apps Script 支持 1, 5, 10, 15, 30）
  TRIGGER_EVERY_MINUTES: 15,

  // ScriptProperties 键前缀
  STATE_KEY_PREFIX: 'gemininest_exported_v1_'
};

/**
 * 主入口：扫描 `Gemini_Nest/inbox/` 中的 Google Docs，导出新增/更新文档为同名 `.txt`。
 */
function exportInboxGoogleDocsToTxt() {
  var inboxFolder = resolveInboxFolder_();
  if (!inboxFolder) {
    Logger.log('[GeminiNest] 未找到目录 %s/%s，本次跳过。',
      CONFIG.NEST_ROOT_FOLDER_NAME, CONFIG.INBOX_FOLDER_NAME);
    return;
  }

  var archivedFolder = CONFIG.MOVE_EXPORTED_GDOC_TO_ARCHIVED
    ? resolveArchivedFolder_(inboxFolder)
    : null;

  var props = PropertiesService.getScriptProperties();
  var docs = inboxFolder.getFilesByType(MimeType.GOOGLE_DOCS);

  var exportedCount = 0;
  var skippedCount = 0;
  var errorCount = 0;

  while (docs.hasNext()) {
    var docFile = docs.next();
    var docId = docFile.getId();
    var docName = docFile.getName();
    var docUpdatedIso = docFile.getLastUpdated().toISOString();
    var stateKey = CONFIG.STATE_KEY_PREFIX + docId;

    try {
      // 防重复检查 1：已按相同 lastUpdated 导出过（即便 .txt 已被本地吸收移入 inbox_archived）
      var prevUpdatedIso = props.getProperty(stateKey);
      if (prevUpdatedIso === docUpdatedIso) {
        skippedCount++;
        continue;
      }

      var targetTxtName = sanitizeTxtFileName_(docName, docId);

      // 防重复检查 2：inbox 中已存在同名且不旧于原 Doc 的 .txt
      var existingTxt = findFileByName_(inboxFolder, targetTxtName);
      if (existingTxt && existingTxt.getLastUpdated().getTime() >= docFile.getLastUpdated().getTime()) {
        props.setProperty(stateKey, docUpdatedIso);
        skippedCount++;
        continue;
      }

      var bodyText = DocumentApp.openById(docId).getBody().getText();
      var outputText = buildExportedText_(docName, docId, docUpdatedIso, bodyText);

      if (existingTxt) {
        // 原 Doc 被重新编辑后更新已有 .txt（原地覆盖，避免产生同名副本）
        existingTxt.setContent(outputText);
      } else {
        inboxFolder.createFile(targetTxtName, outputText, MimeType.PLAIN_TEXT);
      }

      props.setProperty(stateKey, docUpdatedIso);
      exportedCount++;
      Logger.log('[GeminiNest] 已导出: %s -> %s', docName, targetTxtName);

      if (archivedFolder) {
        docFile.moveTo(archivedFolder);
      }
    } catch (err) {
      errorCount++;
      Logger.log('[GeminiNest] 导出失败 (%s / %s): %s', docName, docId, err);
    }
  }

  Logger.log('[GeminiNest] 本轮完成: exported=%d, skipped=%d, errors=%d',
    exportedCount, skippedCount, errorCount);
}

/**
 * 安装每 15 分钟自动运行的定时触发器（先清理同名旧触发器，防止重复注册）。
 */
function installTimeDrivenTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'exportInboxGoogleDocsToTxt') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('exportInboxGoogleDocsToTxt')
    .timeBased()
    .everyMinutes(CONFIG.TRIGGER_EVERY_MINUTES)
    .create();
  Logger.log('[GeminiNest] 已安装定时触发器：每 %d 分钟执行 exportInboxGoogleDocsToTxt()',
    CONFIG.TRIGGER_EVERY_MINUTES);
}

/**
 * 工具函数：清空已导出状态缓存（仅在手动强制全量重导时运行）。
 */
function resetExportState() {
  var props = PropertiesService.getScriptProperties();
  var all = props.getProperties();
  var removed = 0;
  for (var key in all) {
    if (key.indexOf(CONFIG.STATE_KEY_PREFIX) === 0) {
      props.deleteProperty(key);
      removed++;
    }
  }
  Logger.log('[GeminiNest] 已清理 %d 条历史导出状态记录。', removed);
}

// ---------------------------------------------------------------------------
// Internal Helpers
// ---------------------------------------------------------------------------

function resolveInboxFolder_() {
  if (CONFIG.INBOX_FOLDER_ID && CONFIG.INBOX_FOLDER_ID.trim() !== '') {
    return DriveApp.getFolderById(CONFIG.INBOX_FOLDER_ID.trim());
  }
  var rootIter = DriveApp.getFoldersByName(CONFIG.NEST_ROOT_FOLDER_NAME);
  if (!rootIter.hasNext()) {
    return null;
  }
  var nestRoot = rootIter.next();
  var inboxIter = nestRoot.getFoldersByName(CONFIG.INBOX_FOLDER_NAME);
  if (!inboxIter.hasNext()) {
    return null;
  }
  return inboxIter.next();
}

function resolveArchivedFolder_(inboxFolder) {
  var parents = inboxFolder.getParents();
  if (!parents.hasNext()) {
    return null;
  }
  var nestRoot = parents.next();
  var iter = nestRoot.getFoldersByName(CONFIG.ARCHIVED_FOLDER_NAME);
  if (iter.hasNext()) {
    return iter.next();
  }
  return nestRoot.createFolder(CONFIG.ARCHIVED_FOLDER_NAME);
}

function findFileByName_(folder, fileName) {
  var iter = folder.getFilesByName(fileName);
  return iter.hasNext() ? iter.next() : null;
}

function sanitizeTxtFileName_(docName, docId) {
  var base = (docName || 'web_patch_' + docId.slice(0, 8))
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, ' ')
    .trim();
  if (base.toLowerCase().endsWith('.txt')) {
    return base;
  }
  return base + '.txt';
}

function buildExportedText_(docName, docId, docUpdatedIso, bodyText) {
  var header = [
    '# Exported from Google Docs by GeminiNest Apps Script',
    '# source_doc: ' + docName,
    '# doc_id_prefix: ' + docId.slice(0, 8),
    '# last_updated: ' + docUpdatedIso,
    ''
  ].join('\n');
  return header + (bodyText || '').trim() + '\n';
}
