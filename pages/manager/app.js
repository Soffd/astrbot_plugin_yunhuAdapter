const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const state = {
  instance: "",
  instances: [],
  files: [],
  offset: 0,
  limit: 20,
  total: 0,
  fileRequest: 0,
  settings: null,
  preview: null,
  busy: 0,
};
const icons = {
  download:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/></svg>',
  delete:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></svg>',
};
const field = (
  key,
  label,
  type = "text",
  value = "",
  options = null,
  required = true,
) => ({ key, label, type, value, options, required });
const contentType = () =>
  field("content_type", "内容格式", "select", "text", [
    "text",
    "markdown",
    "html",
  ]);
const messageId = (required = true) =>
  field("message_id", "消息 ID", "text", "", null, required);
const OPERATIONS = [
  {
    key: "send",
    title: "发送消息",
    description: "向指定会话发送文本、Markdown 或 HTML。",
    fields: [field("text", "消息内容", "textarea"), contentType()],
  },
  {
    key: "buttons",
    title: "发送按钮消息",
    description: "按钮按行组织，支持 URL、复制和点击汇报。",
    fields: [
      field("text", "消息内容", "textarea"),
      field(
        "buttons",
        "按钮 JSON",
        "json",
        '[[{"text":"确认","type":"callback","value":"yes"}]]',
      ),
    ],
  },
  {
    key: "history",
    title: "查询消息历史",
    read: true,
    description: "在当前会话中按消息 ID 或最近条数查询。",
    fields: [
      field("before", "向前条数", "number", 20),
      messageId(false),
      field("after", "向后条数", "number", 0),
    ],
  },
  {
    key: "mute",
    title: "禁言 / 解禁成员",
    group: true,
    description: "时长以秒填写，0 解禁，-1 永久禁言。",
    fields: [
      field("user_id", "成员 ID"),
      field("duration", "时长 · 秒", "number", 600),
    ],
  },
  {
    key: "remove",
    title: "移除群成员",
    group: true,
    danger: true,
    description: "将明确指定的成员移出当前群。",
    fields: [field("user_id", "成员 ID")],
  },
  {
    key: "message_types",
    title: "设置群消息类型",
    group: true,
    description: "填写逗号分隔的类型，例如 text,image；留空恢复全部类型。",
    fields: [field("types", "允许的消息类型", "text", "", null, false)],
  },
  {
    key: "tags",
    title: "查询群标签",
    group: true,
    read: true,
    description: "查询当前群已存在的标签。",
    fields: [],
  },
  {
    key: "create_tag",
    title: "创建群标签",
    group: true,
    description: "创建标签，可设置颜色、说明和排序。",
    fields: [
      field("tag", "标签名"),
      field("color", "颜色 · #RRGGBB", "text", "#3566DC", null, false),
      field("description", "说明", "text", "", null, false),
      field("sort", "排序", "number", 0),
    ],
  },
  {
    key: "edit_tag",
    title: "修改群标签",
    group: true,
    description: "changes 支持 new_tag、color、description、sort。",
    fields: [
      field("tag", "现有标签名"),
      field("changes", "修改字段 JSON", "json", '{"description":"新的说明"}'),
    ],
  },
  {
    key: "delete_tag",
    title: "删除群标签",
    group: true,
    danger: true,
    description: "删除指定标签，请先确认标签名。",
    fields: [field("tag", "标签名")],
  },
  {
    key: "user_tag",
    title: "修改成员标签",
    group: true,
    description: "为成员添加或移除一个已有标签。",
    fields: [
      field("action", "操作", "select", "add", ["add", "remove"]),
      field("user_id", "成员 ID"),
      field("tag", "标签名"),
    ],
  },
  {
    key: "recall",
    title: "撤回消息",
    danger: true,
    description: "按明确的消息 ID 撤回当前会话消息。",
    fields: [messageId()],
  },
  {
    key: "edit_message",
    title: "编辑机器人消息",
    description: "修改当前会话中机器人发出的消息。",
    fields: [messageId(), field("text", "新内容", "textarea"), contentType()],
  },
  {
    key: "board",
    title: "设置会话看板",
    description: "到期时间是秒级 Unix 时间戳，0 表示不过期。",
    fields: [
      field("text", "看板内容", "textarea"),
      contentType(),
      field("member_id", "群成员 ID · 可选", "text", "", null, false),
      field("expire_time", "到期时间戳", "number", 0),
    ],
  },
  {
    key: "dismiss_board",
    title: "取消会话看板",
    description: "取消当前会话或指定群成员的看板。",
    fields: [field("member_id", "群成员 ID · 可选", "text", "", null, false)],
  },
  {
    key: "global_board",
    global: true,
    title: "管理全局看板",
    danger: true,
    description: "此操作面向机器人的所有用户，请确认操作范围。",
    fields: [
      field("action", "操作", "select", "set", ["set", "dismiss"]),
      field("text", "看板内容 · 取消时可留空", "textarea", "", null, false),
      contentType(),
      field("expire_time", "到期时间戳", "number", 0),
    ],
  },
  {
    key: "batch_send",
    global: true,
    title: "批量发送消息",
    danger: true,
    description: "仅向明确列出的用户或群发送，最多 100 个 ID。",
    fields: [
      field("receiver_type", "接收类型", "select", "user", ["user", "group"]),
      field("receiver_ids", "接收者 ID · 逗号或换行分隔", "ids"),
      field("text", "消息内容", "textarea"),
      contentType(),
    ],
  },
];

function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(value ?? "");
  node.className = className;
  return node;
}
function button(label, className, handler, icon = null) {
  const node = text("button", label, className);
  node.type = "button";
  if (icon) {
    node.innerHTML = icons[icon];
    node.setAttribute("aria-label", label);
    node.title = label;
  }
  node.addEventListener("click", handler);
  return node;
}
function bytes(value) {
  let size = Number(value) || 0;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index++;
  }
  return `${size.toLocaleString("zh-CN", { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
}
function date(value) {
  const d = new Date(Number(value) * 1000);
  return Number.isFinite(d.getTime())
    ? d.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
}
function toast(message, error = false) {
  clearTimeout(toast.timer);
  $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  toast.timer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = `notice${error ? " error" : ""}`;
  $("notice").hidden = !message;
}
async function attempt(work) {
  try {
    await work();
  } catch (error) {
    toast(error.message || "操作失败，请重试", true);
  }
}
async function mutation(work) {
  state.busy++;
  $("instance").disabled = true;
  $("save-settings").disabled = true;
  $("run-operation").disabled = true;
  try {
    return await work();
  } finally {
    state.busy--;
    $("instance").disabled = !state.instances.length || Boolean(state.busy);
    $("save-settings").disabled = !state.instance || Boolean(state.busy);
    $("run-operation").disabled = !state.instance || Boolean(state.busy);
  }
}
function confirmAction(title, description, detail = "", danger = false) {
  const dialog = $("confirm-dialog");
  if (dialog.open) return Promise.resolve(false);
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  $("confirm-detail").textContent = detail;
  $("confirm-detail").hidden = !detail;
  $("confirm-submit").className = `button ${danger ? "danger" : "primary"}`;
  dialog.returnValue = "cancel";
  dialog.showModal();
  $("confirm-cancel").focus();
  return new Promise((resolve) =>
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "confirm"),
      { once: true },
    ),
  );
}
function chooseTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(`panel-${tab.dataset.tab}`).hidden = !selected;
  });
}
function renderUsage(usage) {
  $("file-count").textContent = usage.file_count.toLocaleString();
  $("file-limit").textContent =
    `上限 ${usage.max_files.toLocaleString()} 个文件`;
  $("used-space").textContent = bytes(usage.used_bytes);
  $("pinned-count").textContent = usage.pinned_count.toLocaleString();
  $("pinned-size").textContent =
    `占用 ${bytes(usage.pinned_bytes)}，不参与自动淘汰`;
  $("retention-days").textContent = usage.retention_days
    ? `${usage.retention_days} 天`
    : "已关闭";
  const percent = Math.min(100, (usage.used_bytes / usage.max_bytes) * 100);
  $("space-bar").style.width = `${percent}%`;
  $("space-bar").parentElement.setAttribute(
    "aria-valuenow",
    String(Math.round(percent)),
  );
  $("space-limit").textContent =
    `共 ${bytes(usage.max_bytes)} · 已用 ${percent.toFixed(1)}%`;
}
function renderConversations(items) {
  const selected = $("conversation").value;
  const select = $("conversation");
  select.replaceChildren(new Option("所有会话", ""));
  $("known-chats").replaceChildren();
  for (const chat of items) {
    const value = `${chat.chat_type}:${chat.chat_id}`;
    select.append(
      new Option(
        `${chat.chat_type === "group" ? "群聊" : "私聊"} ${chat.chat_id}`,
        value,
      ),
    );
    const option = new Option(
      chat.chat_type === "group" ? "群聊" : "私聊",
      chat.chat_id,
    );
    $("known-chats").append(option);
  }
  if (selected && !Array.from(select.options).some((o) => o.value === selected))
    select.append(new Option(selected, selected));
  select.value = selected;
}
function renderFiles(data) {
  state.files = data.items;
  state.total = data.total;
  renderUsage(data.usage);
  renderConversations(data.conversations);
  const body = $("files-body");
  body.replaceChildren();
  for (const file of data.items) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const nameWrap = text("div", "", "file-name-cell");
    const extension = file.file_name.split(".").pop().toLowerCase();
    const kind = ["zip", "rar", "7z", "gz", "tar"].includes(extension)
      ? " archive"
      : ["doc", "docx", "pdf", "txt", "md"].includes(extension)
        ? " document"
        : "";
    nameWrap.append(
      text("span", extension.slice(0, 5).toUpperCase(), `file-type${kind}`),
    );
    const names = document.createElement("div");
    const title = text("span", file.file_name, "file-title");
    title.title = file.file_name;
    names.append(
      title,
      text("span", `上传者 ${file.sender_id}`, "file-subtitle"),
    );
    nameWrap.append(names);
    nameCell.append(nameWrap);
    row.append(nameCell);
    const chatCell = document.createElement("td");
    chatCell.append(
      text("span", file.chat_type === "group" ? "群聊" : "私聊", "chat-name"),
      text("span", file.chat_id, "chat-subtitle"),
    );
    row.append(chatCell);
    row.append(text("td", bytes(file.size)));
    const policyCell = document.createElement("td");
    policyCell.append(
      text(
        "span",
        file.pinned ? "长期保留" : "按期清理",
        `badge${file.pinned ? " pinned" : ""}`,
      ),
    );
    policyCell.append(
      text(
        "span",
        file.pinned ? "不会自动淘汰" : `${date(file.expires_at)} 到期`,
        "expiry-detail",
      ),
    );
    row.append(policyCell);
    row.append(text("td", date(file.created_at)));
    const actionCell = document.createElement("td");
    const actions = text("div", "", "row-actions");
    actions.append(
      button("预览", "text-button", () => attempt(() => preview(file))),
      button(file.pinned ? "取消保留" : "保留", "text-button", () =>
        attempt(() => manageFile(file, file.pinned ? "unpin" : "pin")),
      ),
      button(
        `下载 ${file.file_name}`,
        "icon-button",
        () => attempt(() => download(file)),
        "download",
      ),
      button(
        `删除 ${file.file_name}`,
        "icon-button delete",
        () => attempt(() => manageFile(file, "delete")),
        "delete",
      ),
    );
    actionCell.append(actions);
    row.append(actionCell);
    body.append(row);
  }
  $("file-empty").hidden = Boolean(data.items.length);
  $("pagination-label").textContent = data.total
    ? `共 ${data.total} 个文件 · 显示 ${state.offset + 1}–${Math.min(state.offset + state.limit, data.total)}`
    : "共 0 个文件";
  $("prev").disabled = state.offset === 0;
  $("next").disabled = state.offset + state.limit >= data.total;
  const filtered =
    $("search").value ||
    $("conversation").value ||
    $("file-status").value !== "all";
  $("file-empty").querySelector("h3").textContent = filtered
    ? "没有符合条件的文件"
    : "还没有保留文件";
  $("file-empty").querySelector("p").textContent = filtered
    ? "试试其他关键词、会话或保留状态。"
    : "向机器人发送文件后，会按当前保存策略加入这里。";
}
async function loadFiles(reset = false) {
  if (!state.instance) return;
  if (reset) state.offset = 0;
  const instance = state.instance;
  const sequence = ++state.fileRequest;
  const [chat_type = "", ...chatIdParts] = $("conversation").value.split(":");
  const data = await bridge.apiGet("files", {
    instance_id: instance,
    search: $("search").value.trim(),
    chat_type,
    chat_id: chatIdParts.join(":"),
    status: $("file-status").value,
    limit: state.limit,
    offset: state.offset,
  });
  if (instance !== state.instance || sequence !== state.fileRequest) return;
  if (data.total && state.offset >= data.total) {
    state.offset = Math.floor((data.total - 1) / state.limit) * state.limit;
    return loadFiles();
  }
  renderFiles(data);
}
function renderSettings(settings) {
  state.settings = settings;
  for (const key of [
    "bot_id",
    "file_retention_days",
    "file_store_max_mb",
    "file_store_max_count",
    "media_ttl",
  ])
    $("settings-form").elements.namedItem(key).value = settings[key];
  $("settings-form").elements.namedItem("reply_in_thread").checked =
    settings.reply_in_thread;
  $("storage-path").textContent = settings.storage_path;
  $("settings-state").textContent = "设置按平台实例分别保存";
}
async function selectInstance() {
  state.instance = $("instance").value;
  state.offset = 0;
  state.fileRequest++;
  state.settings = null;
  state.files = [];
  $("files-body").replaceChildren();
  $("conversation").replaceChildren(new Option("所有会话", ""));
  const instance = state.instances.find((item) => item.id === state.instance);
  const available = Boolean(instance);
  for (const id of ["save-settings", "run-operation", "cleanup"])
    $(id).disabled = !available;
  if (!available) {
    $("file-empty").hidden = false;
    $("pagination-label").textContent = "没有可管理的实例";
    $("connection").textContent = "尚未载入实例";
    $("connection").classList.remove("online");
    $("bot-label").textContent = "—";
    for (const id of [
      "file-count",
      "used-space",
      "pinned-count",
      "retention-days",
    ])
      $(id).textContent = "—";
    $("space-bar").style.width = "0%";
    $("settings-form").reset();
    $("storage-path").textContent = "—";
    notice(
      "尚未发现已载入的云湖实例。请先在 AstrBot 机器人管理中创建并启用 yunhu 实例。",
    );
    return;
  }
  notice("");
  $("connection").textContent = instance.connected
    ? instance.mode === "webhook"
      ? "Webhook 监听中"
      : "WebSocket 已连接"
    : "实例已载入 · 等待连接";
  $("connection").classList.toggle("online", instance.connected);
  $("bot-label").textContent = instance.bot_id
    ? `机器人 ID ${instance.bot_id}`
    : "尚未识别机器人 ID";
  const current = state.instance;
  const settings = bridge.apiGet("settings", { instance_id: current });
  const results = await Promise.allSettled([loadFiles(), settings]);
  if (current !== state.instance) return;
  if (results[1].status === "fulfilled") renderSettings(results[1].value);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) throw failed.reason;
}
async function refresh() {
  const previous = state.instance;
  const data = await bridge.apiGet("instances");
  state.instances = data.instances;
  $("version").textContent = data.version;
  $("instance").replaceChildren(
    ...data.instances.map((item) => new Option(item.id, item.id)),
  );
  if (!data.instances.length)
    $("instance").append(new Option("暂无云湖实例", ""));
  $("instance").disabled = !data.instances.length;
  if (data.instances.some((item) => item.id === previous))
    $("instance").value = previous;
  await selectInstance();
}
async function download(file) {
  await mutation(async () => {
    await bridge.download(
      `files/${file.file_id}/download`,
      { instance_id: state.instance },
      file.file_name,
    );
    toast("已开始下载");
  });
}
async function preview(file) {
  state.preview = { ...file, instance_id: state.instance };
  $("preview-title").textContent = file.file_name;
  $("preview-meta").textContent =
    `文件 ID ${file.file_id} · ${bytes(file.size)}`;
  $("preview-content").textContent = "正在读取…";
  $("preview-note").textContent = "";
  $("preview-dialog").showModal();
  try {
    const result = await bridge.apiGet(`files/${file.file_id}/preview`, {
      instance_id: state.preview.instance_id,
    });
    if (state.preview?.file_id !== file.file_id) return;
    $("preview-content").textContent = result.text || "（空文件）";
    $("preview-note").textContent = result.truncated
      ? "已显示前 32,000 字符，下载可查看完整文件。"
      : "以纯文本显示，不执行文件中的代码或 HTML。";
  } catch (error) {
    $("preview-content").textContent = error.message;
    $("preview-note").textContent = "可下载原文件后使用对应应用查看。";
  }
}
async function manageFile(file, action) {
  const instance = state.instance;
  const titles = {
    pin: "长期保留文件",
    unpin: "取消长期保留",
    delete: "删除保留文件",
  };
  const descriptions = {
    pin: "此文件将不再按时间或容量自动淘汰，仍占文件库容量。",
    unpin: "文件将按当前默认保留天数重新计时；若设置为 0 天，将进入过期清理。",
    delete: "将永久删除文件库中的保留副本。此操作无法撤销。",
  };
  if (
    !(await confirmAction(
      titles[action],
      `${file.file_name}：${descriptions[action]}`,
      `实例：${instance}\n文件 ID：${file.file_id}`,
      action === "delete",
    ))
  )
    return;
  await mutation(async () => {
    await bridge.apiPost(`files/${file.file_id}/manage`, {
      instance_id: instance,
      action,
    });
    toast("操作已完成");
    if (instance === state.instance) await loadFiles();
  });
}
function renderOperation() {
  const operation = OPERATIONS.find(
    (item) => item.key === $("operation-name").value,
  );
  $("operation-description").textContent = operation.description;
  $("operation-fields").replaceChildren();
  for (const item of operation.fields) {
    const label = text("label", item.label, "field");
    const input = document.createElement(
      ["textarea", "json", "ids"].includes(item.type)
        ? "textarea"
        : item.type === "select"
          ? "select"
          : "input",
    );
    input.name = item.key;
    if (item.options)
      input.append(...item.options.map((o) => new Option(o, o)));
    if (input.tagName === "INPUT") {
      input.type = item.type;
      if (item.type === "number") input.step = "1";
    }
    input.value = item.value;
    input.required = item.required;
    label.append(input);
    $("operation-fields").append(label);
  }
  if (operation.group) $("operation-chat-type").value = "group";
  $("operation-chat-id").closest(".form-row").hidden = Boolean(
    operation.global,
  );
  $("operation-chat-id").required = !operation.global;
}
function operationArguments(operation) {
  const args = {};
  for (const item of operation.fields) {
    let value = $("operation-fields").querySelector(
      `[name="${item.key}"]`,
    ).value;
    if (item.type === "number") {
      value = Number(value);
      if (!Number.isSafeInteger(value))
        throw new Error(`${item.label}必须为整数`);
    } else if (item.type === "json") {
      try {
        value = JSON.parse(value);
      } catch {
        throw new Error(`${item.label}不是有效 JSON`);
      }
    } else if (item.type === "ids")
      value = value.split(/[\s,，]+/).filter(Boolean);
    args[item.key] = value;
  }
  return args;
}
async function runOperation(event) {
  event.preventDefault();
  const operation = OPERATIONS.find(
    (item) => item.key === $("operation-name").value,
  );
  const chatType = $("operation-chat-type").value;
  const chatId = $("operation-chat-id").value.trim();
  if (operation.group && chatType !== "group")
    throw new Error("该功能需要选择群聊");
  const payload = {
    instance_id: state.instance,
    chat_type: chatType,
    chat_id: chatId,
    operation: operation.key,
    arguments: operationArguments(operation),
  };
  const target = operation.global
    ? operation.key === "global_board"
      ? "全局看板"
      : "列表中的接收者"
    : `${chatType === "group" ? "群聊" : "私聊"} ${chatId}`;
  if (
    !operation.read &&
    !(await confirmAction(
      operation.title,
      `即将在实例 ${state.instance} 的${target} 执行以下操作。`,
      JSON.stringify(payload.arguments, null, 2),
      operation.danger,
    ))
  )
    return;
  await mutation(async () => {
    $("result-status").textContent = "执行中";
    $("result-status").className = "badge";
    try {
      const result = await bridge.apiPost("operations/run", payload);
      $("result-status").textContent = result.ok ? "操作成功" : "操作失败";
      $("result-status").className = `badge ${result.ok ? "success" : "error"}`;
      $("result-message").textContent = result.message || "已收到云湖响应";
      $("operation-result").textContent = JSON.stringify(result, null, 2);
    } catch (error) {
      $("result-status").textContent = "请求失败";
      $("result-status").className = "badge error";
      $("result-message").textContent = error.message;
      $("operation-result").textContent = error.message;
      throw error;
    }
  });
}
async function saveSettings(event) {
  event.preventDefault();
  const form = $("settings-form");
  const settings = {
    bot_id: form.elements.namedItem("bot_id").value.trim(),
    reply_in_thread: form.elements.namedItem("reply_in_thread").checked,
  };
  for (const key of [
    "file_retention_days",
    "file_store_max_mb",
    "file_store_max_count",
    "media_ttl",
  ])
    settings[key] = Number(form.elements.namedItem(key).value);
  if (!state.settings) throw new Error("请先等待实例设置载入");
  if (
    (settings.file_store_max_mb < state.settings.file_store_max_mb ||
      settings.file_store_max_count < state.settings.file_store_max_count) &&
    !(await confirmAction(
      "调整文件库上限",
      "保存后将按新额度清理最早的非长期保留文件，长期保留文件不自动删除。",
      `容量：${settings.file_store_max_mb} MiB\n数量：${settings.file_store_max_count} 个`,
    ))
  )
    return;
  await mutation(async () => {
    const result = await bridge.apiPost("settings/save", {
      instance_id: state.instance,
      settings,
    });
    renderSettings(result.settings);
    $("settings-state").textContent = "已保存并应用，无需重启";
    toast("设置已保存并应用");
    await loadFiles();
  });
}
function theme(context) {
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
}

$("confirm-cancel").addEventListener("click", () =>
  $("confirm-dialog").close("cancel"),
);
$("confirm-submit").addEventListener("click", () =>
  $("confirm-dialog").close("confirm"),
);
$("close-preview").addEventListener("click", () => $("preview-dialog").close());
$("preview-dialog").addEventListener("close", () => {
  state.preview = null;
});
$("preview-download").addEventListener("click", () =>
  attempt(async () => {
    if (state.preview)
      await bridge.download(
        `files/${state.preview.file_id}/download`,
        { instance_id: state.preview.instance_id },
        state.preview.file_name,
      );
  }),
);
document
  .querySelectorAll(".tab")
  .forEach((tab) =>
    tab.addEventListener("click", () => chooseTab(tab.dataset.tab)),
  );
document.querySelector(".tabs").addEventListener("keydown", (event) => {
  const tabs = Array.from(document.querySelectorAll(".tab"));
  let index = tabs.indexOf(document.activeElement);
  if (
    index < 0 ||
    !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
  )
    return;
  event.preventDefault();
  index =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) %
          tabs.length;
  chooseTab(tabs[index].dataset.tab);
  tabs[index].focus();
});
$("instance").addEventListener("change", () => attempt(selectInstance));
$("refresh").addEventListener("click", () => attempt(refresh));
$("conversation").addEventListener("change", () =>
  attempt(() => loadFiles(true)),
);
$("file-status").addEventListener("change", () =>
  attempt(() => loadFiles(true)),
);
let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => attempt(() => loadFiles(true)), 250);
});
$("prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  attempt(loadFiles);
});
$("next").addEventListener("click", () => {
  state.offset += state.limit;
  attempt(loadFiles);
});
$("settings-form").addEventListener("submit", (event) =>
  attempt(() => saveSettings(event)),
);
$("settings-form").addEventListener("input", () => {
  $("settings-state").textContent = "有未保存的修改";
});
$("operation-name").append(
  ...OPERATIONS.map((item) => new Option(item.title, item.key)),
);
$("operation-name").addEventListener("change", renderOperation);
$("operation-form").addEventListener("submit", (event) =>
  attempt(() => runOperation(event)),
);
$("cleanup").addEventListener("click", () =>
  attempt(async () => {
    if (
      !(await confirmAction(
        "按策略清理",
        "清理已过期文件，并在超出额度时淘汰最早的非长期保留文件。长期保留文件不会自动删除。",
      ))
    )
      return;
    await mutation(async () => {
      await bridge.apiPost("cleanup", { instance_id: state.instance });
      await loadFiles();
      toast("已按当前策略清理");
    });
  }),
);
renderOperation();
if (!bridge) {
  notice(
    "请从 AstrBot WebUI 的插件详情中打开此管理页。当前页面需要 Dashboard bridge。",
    true,
  );
  $("instance").replaceChildren(new Option("请从 AstrBot 打开", ""));
  for (const id of [
    "instance",
    "refresh",
    "cleanup",
    "save-settings",
    "run-operation",
  ])
    $(id).disabled = true;
} else {
  attempt(async () => {
    theme(await bridge.ready());
    bridge.onContext?.(theme);
    await refresh();
  });
}
