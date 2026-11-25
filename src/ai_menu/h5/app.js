const get = (id) => document.getElementById(id);
const stateLabels = { enough: "充足", low: "少量", empty: "用完" };
const retries = new Map();
let inventory;
let menu;
let historyCursor;
let busy = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notice(text = "", error = false) {
  const node = get("notice");
  node.textContent = text;
  node.hidden = !text;
  node.className = error ? "error" : "";
}

async function api(path, body, day) {
  const headers = { Accept: "application/json" };
  const options = { headers };
  let fingerprint;
  if (body !== undefined) {
    options.method = "POST";
    options.body = JSON.stringify(body);
    fingerprint = JSON.stringify([path, day, options.body]);
    headers["Content-Type"] = "application/json";
    if (path !== "/inventory/parse") {
      if (!retries.has(fingerprint)) retries.set(fingerprint, crypto.randomUUID());
      headers["Idempotency-Key"] = retries.get(fingerprint);
    }
    if (day) headers["X-Menu-Date"] = day;
  }
  const response = await fetch("/api/v1" + path, options);
  const result = await response.json();
  if (response.status < 500 && response.status !== 202) retries.delete(fingerprint);
  if (!response.ok) {
    const error = new Error(result.error?.code || "REQUEST_FAILED");
    error.details = result.error?.details;
    throw error;
  }
  return result;
}

const errors = {
  IMAGE_AI_BUSY: "正在生成另一张食材图片，请稍后重试。",
  IMAGE_AI_UNAVAILABLE: "暂时无法生成食材图片，请稍后重试。",
  INGREDIENT_NOT_FOUND: "找不到这项食材。",
  MENU_JOB_IN_PROGRESS: "已有换菜任务正在处理，请稍后再试。",
  MENU_JOB_NOT_FOUND: "找不到这次换菜任务，请刷新菜单后再试。",
  MENU_JOB_FAILED: "换菜任务处理失败，请重试。",
  IDEMPOTENCY_KEY_REUSED: "提交参数与原请求不同，请刷新后重试。",
  NO_ALTERNATIVE: "当前库存下没有其他合适的组合。",
  STOCK_VERSION_CONFLICT: "库存已更新，请刷新后重新确认。",
  MENU_REVISION_CONFLICT: "菜单已更新，请刷新后重新操作。",
  MENU_DATE_CHANGED: "日期已切换，请刷新页面。",
  MENU_ALREADY_COOKED: "这份菜单已确认，请刷新页面。",
  INVALID_INPUT: "请检查输入和所选食材。",
  INVENTORY_AI_UNAVAILABLE: "暂时无法识别库存，请检查模型配置或稍后重试。",
  RECIPE_AI_UNAVAILABLE: "暂时无法生成 AI 菜谱，请检查模型配置或稍后重试。",
  WRITE_RETRY_EXHAUSTED: "数据库正忙，请重试。",
};

const inventoryAIReasons = {
  not_configured: "库存识别模型尚未配置，请填写模型地址和名称后重启后台。",
  upstream_http: "模型服务返回错误",
  timeout: "库存识别请求超时，请稍后重试。",
  connection_failed: "后台无法连接库存识别模型，请检查模型服务和网络。",
  invalid_response: "模型返回了无法读取的库存结果，请重试。",
  insufficient_quota: "模型服务额度不足，请检查 API 账单。",
};

const recipeAIReasons = {
  ...inventoryAIReasons,
  not_configured: "菜谱模型尚未配置，请填写模型地址和名称后重启后台。",
  timeout: "生成 AI 菜谱超时，请稍后重试。",
  connection_failed: "后台无法连接菜谱模型，请检查模型服务和网络。",
  invalid_response: "菜谱模型返回的结果格式有误，请重试。",
  invalid_candidates: "模型返回的菜谱未通过用料或格式校验，请重试。",
};

async function run(action, trigger) {
  if (busy) return;
  busy = true;
  if (trigger) trigger.disabled = true;
  notice();
  try {
    await action();
  } catch (error) {
    if (error.message === "IMAGE_AI_UNAVAILABLE") {
      const { reason, upstream_status: status } = error.details || {};
      const details = {
        not_configured: "请先在 image_ai 中配置图片模型并重启后台。",
        timeout: "生成图片超时，请稍后重试。",
        storage_failed: "后台无法保存生成的图片。",
        connection_failed: "后台无法连接图片模型服务。",
        invalid_response: "图片模型返回的数据无法读取。",
      };
      notice(status ? `图片模型返回 HTTP ${status}。`
        : details[reason] || errors.IMAGE_AI_UNAVAILABLE, true);
      return;
    }
    if (error.message === "INVENTORY_AI_UNAVAILABLE" || error.message === "RECIPE_AI_UNAVAILABLE") {
      const reasons = error.message === "RECIPE_AI_UNAVAILABLE" ? recipeAIReasons : inventoryAIReasons;
      const { reason, upstream_status: status } = error.details || {};
      const detail = reason === "upstream_http" && Number.isInteger(status)
        ? `模型服务返回 HTTP ${status}，请检查模型配置或稍后重试。`
        : reasons[reason];
      notice(detail || errors[error.message], true);
      return;
    }
    notice(errors[error.message] || "操作未完成，请检查后台是否运行，然后重试。", true);
  } finally {
    if (trigger) trigger.disabled = false;
    busy = false;
  }
}

function button(text, action, className) {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", () => run(action, node));
  return node;
}

function field(text, control) {
  const label = element("label", text);
  label.append(control);
  return label;
}

function select(options, selected) {
  const node = element("select");
  for (const [value, label] of Object.entries(options)) {
    const option = element("option", label);
    option.value = value;
    node.append(option);
  }
  node.value = selected;
  return node;
}

function textInput(value = "") {
  const node = element("input");
  node.type = "text";
  node.maxLength = 200;
  node.value = value;
  return node;
}

function checkbox(checked = false) {
  const node = element("input");
  node.type = "checkbox";
  node.checked = checked;
  return node;
}

function stockEditor(container, initial = {}) {
  const status = select(stateLabels, initial.state || "enough");
  const note = textInput(initial.note || "");
  const prioritize = checkbox(initial.prioritize);
  const noteLabel = field("备注", note);
  const prioritizeLabel = field("优先使用 ", prioritize);
  function update() {
    noteLabel.hidden = prioritizeLabel.hidden = status.value === "empty";
  }
  status.addEventListener("change", update);
  update();
  container.append(field("库存状态", status), noteLabel, prioritizeLabel);
  return () => ({
    state: status.value,
    note: status.value === "empty" ? null : note.value.trim() || null,
    prioritize: status.value !== "empty" && prioritize.checked,
  });
}

const stockPhotos = new Map([
  ["番茄", 0], ["土豆", 1], ["生菜", 2], ["牛肉", 3], ["羊肉", 4],
  ["鸡肉", 5], ["平菇", 6], ["虾仁", 7], ["西兰花", 8],
]);
const stockPageSize = 12;
let stockLimit = stockPageSize;

const generatedImageIds = new Set();

function ingredientThumbnail(item) {
  const id = item.ingredient_id ?? item.id;
  const photoIndex = stockPhotos.get(item.name);
  const thumbnail = element("span", undefined, "stock-thumbnail");
  thumbnail.dataset.ingredientImage = id;
  thumbnail.setAttribute("aria-hidden", "true");
  if (generatedImageIds.has(id)) {
    const photo = element("img");
    photo.alt = "";
    photo.loading = "lazy";
    photo.src = `/api/v1/ingredient-images/${id}`;
    const fallback = ingredientIcon(item.food_group);
    photo.addEventListener("error", () => { thumbnail.textContent = fallback; }, { once: true });
    thumbnail.append(photo);
  } else if (photoIndex !== undefined) {
    thumbnail.classList.add("stock-photo");
    thumbnail.style.backgroundPosition = `${(photoIndex % 3) * 50}% ${Math.floor(photoIndex / 3) * 50}%`;
  } else {
    thumbnail.textContent = ingredientIcon(item.food_group);
  }
  return thumbnail;
}

function ingredientIcon(group) {
  return ({ plant: "🥬", meat: "🍖", egg: "🥚" })[group] || "🧺";
}

function ingredientStrip(items, limit) {
  const unique = [...new Map(items.map((item) => [item.ingredient_id, item])).values()];
  const list = element("ul", undefined, "ingredient-strip");
  for (const item of unique.slice(0, limit)) {
    const chip = element("li");
    chip.append(ingredientThumbnail(item), element("span", item.name));
    list.append(chip);
  }
  return list;
}

async function generateIngredientImage(item) {
  notice(`正在生成「${item.name}」的图片…`);
  await api(`/ingredient-images/${item.id}`, {});
  generatedImageIds.add(item.id);
  for (const thumbnail of document.querySelectorAll(`[data-ingredient-image="${item.id}"]`)) {
    thumbnail.replaceWith(ingredientThumbnail(item));
  }
  renderStockCards();
  notice(`已添加「${item.name}」的图片。`);
}

function renderStockCards() {
  const query = get("stock-search").value.trim().normalize("NFKC").toLowerCase();
  const filter = get("stock-filter").value;
  const matches = inventory.items.filter(({ ingredient, state }) =>
    ingredient.name.normalize("NFKC").toLowerCase().includes(query)
    && (filter === "all" || (state || "unrecorded") === filter));
  const list = get("inventory");
  list.replaceChildren();
  for (const item of matches.slice(0, stockLimit)) {
    const row = element("li", undefined, "stock-card");
    const thumbnail = ingredientThumbnail(item.ingredient);
    const details = element("div", undefined, "stock-details");
    const status = item.state || "unrecorded";
    details.append(element("strong", item.ingredient.name),
      element("span", stateLabels[status] || "未录入", `stock-state stock-state-${status}`));
    if (item.note) details.append(element("div", item.note, "stock-note muted"));
    if (item.prioritize) details.append(element("span", "优先使用", "stock-priority"));
    if (!stockPhotos.has(item.ingredient.name) && !generatedImageIds.has(item.ingredient.id)) {
      details.append(button("AI 生成图片", () => generateIngredientImage(item.ingredient), "secondary image-action"));
    }
    const deleteButton = button("×", async () => {
      if (!window.confirm(`确定删除「${item.ingredient.name}」？再次录入可恢复。`)) return;
      inventory = await api(`/ingredients/${item.ingredient.id}/delete`, {
        stock_version: inventory.stock_version,
      });
      get("preview").replaceChildren();
      renderInventory();
      notice("食材已删除。");
      await refreshMenu();
    }, "delete-action");
    deleteButton.setAttribute("aria-label", `删除${item.ingredient.name}`);
    deleteButton.title = `删除${item.ingredient.name}`;
    row.append(thumbnail, details, deleteButton);
    list.append(row);
  }
  get("stock-count").textContent = `共 ${matches.length} 项食材`;
  get("stock-empty").hidden = matches.length !== 0;
  get("stock-more").hidden = matches.length <= stockLimit;
}

function resetStockFilter() {
  stockLimit = stockPageSize;
  if (inventory) renderStockCards();
}

function renderInventory() {
  renderStockCards();
  const form = get("manual-form");
  form.replaceChildren();
  if (!inventory.items.length) return;
  const choice = select(Object.fromEntries(inventory.items.map(
    (item) => [item.ingredient.id, item.ingredient.name],
  ).concat([["new", "新增食材…"]])), inventory.items[0].ingredient.id);
  const editor = element("div");
  let read;
  function choose() {
    editor.replaceChildren();
    if (choice.value === "new") {
      const name = textInput();
      name.maxLength = 40;
      editor.append(field("新食材名称", name));
      const readStock = stockEditor(editor);
      read = () => ({ ingredient_name: name.value.trim(), ...readStock() });
    } else {
      const item = inventory.items.find((item) => item.ingredient.id === Number(choice.value));
      const readStock = stockEditor(editor, item);
      read = () => ({ ingredient_id: item.ingredient.id, ...readStock() });
    }
  }
  choice.addEventListener("change", choose);
  choose();
  form.append(field("食材", choice), editor, button("保存库存", () => saveStock(
    inventory.stock_version, [read()],
  )));
}

async function saveStock(stockVersion, items) {
  inventory = await api("/inventory/confirm", { stock_version: stockVersion, items });
  get("preview").replaceChildren();
  renderInventory();
  notice("库存已更新。");
  await refreshMenu();
}

function renderPreview(preview) {
  const panel = element("div", undefined, "panel");
  panel.append(element("h3", "确认库存变更"));
  const names = new Map(inventory.items.map((item) => [item.ingredient.id, item.ingredient.name]));
  const readers = preview.items.map((item) => {
    const row = element("fieldset");
    const newName = item.ingredient_name ? textInput(item.ingredient_name) : null;
    if (newName) newName.maxLength = 40;
    row.append(element("legend", newName ? "新增食材" : names.get(item.ingredient_id) || item.ingredient_id));
    if (newName) row.append(field("食材名称", newName));
    const read = stockEditor(row, item);
    panel.append(row);
    return () => ({
      ...(newName ? { ingredient_name: newName.value.trim() } : { ingredient_id: item.ingredient_id }),
      ...read(),
    });
  });
  let ignore;
  if (preview.unresolved.length) {
    panel.append(element("p", "以下内容需要手动处理："));
    const unresolved = element("ul");
    for (const item of preview.unresolved) unresolved.append(element("li", item.text + " · " + item.reason));
    panel.append(unresolved);
    if (readers.length) {
      ignore = checkbox();
      panel.append(field("只保存已识别项 ", ignore));
    } else {
      const manual = element("a", "手动调整库存");
      manual.href = "#manual";
      manual.addEventListener("click", () => { get("manual").open = true; });
      panel.append(manual);
    }
  }
  if (readers.length) {
    const save = button("确认录入", async () => {
      await saveStock(preview.stock_version, readers.map((read) => read()));
      get("stock-text").value = "";
    });
    if (ignore) {
      save.disabled = true;
      ignore.addEventListener("change", () => { save.disabled = !ignore.checked; });
    }
    panel.append(save);
  }
  get("preview").replaceChildren(panel);
}

async function refreshMenu() {
  menu = await api("/menus/today");
  if (menu.status === "not_generated" || menu.stale) {
    menu = await api("/menus/today/generate", {}, menu.menu_date);
  }
  renderMenu();
}

function revision() {
  return { revision: menu.revision, stock_version: menu.current_stock_version };
}

async function replace(scope, slot) {
  const path = "/menus/today/replace";
  const body = { ...revision(), scope, slot };
  const day = menu.menu_date;
  const fingerprint = JSON.stringify([path, day, JSON.stringify(body)]);
  notice("正在后台换菜…");
  let job = await api(path, body, day);
  while (job.status === "queued" || job.status === "running") {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    try {
      job = await api(path + "/" + job.job_id);
    } catch (error) {
      if (error.message === "MENU_JOB_NOT_FOUND") retries.delete(fingerprint);
      throw error;
    }
  }
  retries.delete(fingerprint);
  if (job.status === "failed") {
    const error = new Error(job.error.code);
    error.details = job.error.details;
    throw error;
  }
  menu = job.menu;
  renderMenu();
  notice(scope === "one" ? "已换一道菜。" : "已更换整套菜单。");
}

function dishCard(dish) {
  const card = element("article", undefined, "card");
  card.append(element("h3", dish.name), element("p", `约 ${dish.minutes} 分钟`, "muted"));
  const details = element("details");
  details.append(element("summary", "食材和做法"));
  const ingredients = element("ul");
  for (const item of dish.ingredients) ingredients.append(element("li", `${item.name} ${item.amount_hint}`));
  const steps = element("ol");
  for (const step of dish.instructions) steps.append(element("li", step));
  details.append(ingredients, steps);
  if (dish.ingredients.length) card.append(ingredientStrip(dish.ingredients, 3));
  card.append(details);
  if (menu.status === "ready") card.append(button("换这道", () => replace("one", dish.slot), "secondary"));
  return card;
}

function renderMenu() {
  const target = get("menu");
  get("menu-date").textContent = menu.menu_date;
  target.replaceChildren();
  if (menu.status === "insufficient") {
    const names = new Map(inventory.items.map((item) => [item.ingredient.id, item.ingredient.name]));
    const missing = menu.diagnostics?.unavailable_ingredient_ids.map((id) => names.get(id)).filter(Boolean);
    const panel = element("div", undefined, "panel");
    panel.append(element("h3", "当前库存凑不齐三道菜"),
                 element("p", missing?.length ? "可补录：" + missing.join("、") : "更新库存后可重新生成。", "muted"));
    const link = element("a", "录入库存");
    link.href = "#stock";
    panel.append(link);
    target.append(panel);
    return;
  }

  const dishes = element("div", undefined, "dishes");
  for (const dish of menu.dishes) dishes.append(dishCard(dish));
  target.append(dishes);
  if (menu.status === "cooked") {
    target.append(element("p", "已确认今日做菜记录。", "panel"));
    return;
  }
  const actions = element("div", undefined, "actions");
  actions.append(button("换整套", () => replace("all"), "secondary"),
                 button("记录做过的菜", renderCooking));
  target.append(actions, element("div"));
}

function renderCooking() {
  const container = get("menu").lastElementChild;
  const panel = element("div", undefined, "panel");
  panel.append(element("h3", "选择实际做过的菜"));
  const checks = menu.dishes.map((dish) => {
    const check = checkbox();
    panel.append(field(dish.name + " ", check));
    return [dish, check];
  });
  const remaining = element("div");
  const editors = new Map();
  const save = button("确认已做", async () => {
    const slots = checks.filter(([, check]) => check.checked).map(([dish]) => dish.slot);
    const updates = Array.from(editors, ([ingredient_id, entry]) => ({
      ingredient_id,
      outcome: entry.status.value,
      ...(entry.status.value === "low" ? { note: entry.note.value.trim() || null } : {}),
    }));
    const result = await api("/menus/today/confirm-cooked", {
      ...revision(), cooked_slots: slots, remaining: updates,
    }, menu.menu_date);
    menu = result.menu;
    renderMenu();
    notice("已保存做菜记录。");
    inventory = await api("/inventory");
    renderInventory();
    await loadHistory();
  });

  function update() {
    const tracked = new Map();
    for (const [dish, check] of checks) {
      if (!check.checked) continue;
      for (const item of dish.ingredients) {
        if (item.stock_policy === "tracked") tracked.set(item.ingredient_id, item.name);
      }
    }
    for (const id of editors.keys()) {
      if (!tracked.has(id)) editors.delete(id);
    }
    remaining.replaceChildren();
    if (tracked.size) remaining.append(element("h3", "确认食材剩余状态"));
    for (const [id, name] of tracked) {
      if (!editors.has(id)) {
        const row = element("fieldset");
        row.append(element("legend", name));
        const status = select({ unchanged: "保留当前记录", low: "还剩一些", empty: "用完了" }, "unchanged");
        const note = textInput();
        const label = field("剩余备注", note);
        label.hidden = true;
        status.addEventListener("change", () => { label.hidden = status.value !== "low"; });
        row.append(field("剩余状态", status), label);
        editors.set(id, { row, status, note });
      }
      remaining.append(editors.get(id).row);
    }
    save.disabled = !checks.some(([, check]) => check.checked);
  }
  for (const [, check] of checks) check.addEventListener("change", update);
  update();
  const actions = element("div", undefined, "actions");
  actions.append(save, button("取消", () => container.replaceChildren(), "secondary"));
  panel.append(remaining, actions);
  container.replaceChildren(panel);
  panel.scrollIntoView({ block: "nearest" });
}

async function loadHistory(append = false) {
  const history = await api("/cooking-history" + (append ? "?before=" + encodeURIComponent(historyCursor) : ""));
  const target = get("history-list");
  if (!append) target.replaceChildren();
  if (!append && !history.items.length) target.append(element("p", "还没有做菜记录。", "muted"));
  for (const entry of history.items) {
    const row = element("article", undefined, "card history-entry");
    row.append(element("h3", entry.cooking.menu_date),
               element("p", entry.dishes.map((dish) => dish.name).join("、")));
    const ingredients = entry.dishes.flatMap((dish) => dish.ingredients);
    if (ingredients.length) row.append(ingredientStrip(ingredients, 4));
    target.append(row);
  }
  historyCursor = history.next_before;
  get("more-history").hidden = !historyCursor;
}

function showView() {
  const destination = location.hash.slice(1);
  const views = ["today", "stock", "history"];
  const active = destination === "manual" ? "stock"
    : views.includes(destination) ? destination : "today";
  for (const id of views) get(id).hidden = id !== active;
  for (const link of get("main-nav").querySelectorAll("a")) {
    if (link.hash === "#" + active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (destination === "manual") {
    get("manual").open = true;
    get("manual").scrollIntoView({ block: "nearest" });
  } else {
    window.scrollTo(0, 0);
  }
}

showView();
window.addEventListener("hashchange", showView);

get("stock-search").addEventListener("input", resetStockFilter);
get("stock-filter").addEventListener("change", resetStockFilter);
get("stock-more").addEventListener("click", () => {
  stockLimit += stockPageSize;
  if (inventory) renderStockCards();
});

get("intake").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const label = get("intake-label");
    const loading = get("intake-loading");
    const previewLoading = get("preview-loading");
    const preview = get("preview");
    label.hidden = true;
    loading.hidden = false;
    previewLoading.hidden = false;
    preview.hidden = true;
    preview.setAttribute("aria-busy", "true");
    const minimumLoading = new Promise((resolve) => setTimeout(resolve, 350));
    try {
      renderPreview(await api("/inventory/parse", { text: get("stock-text").value }));
    } finally {
      await minimumLoading;
      label.hidden = false;
      loading.hidden = true;
      previewLoading.hidden = true;
      preview.hidden = false;
      preview.removeAttribute("aria-busy");
    }
  }, get("intake-submit"));
});
get("manual-form").addEventListener("submit", (event) => event.preventDefault());
get("more-history").addEventListener("click", (event) => run(() => loadHistory(true), event.currentTarget));

run(async () => {
  const images = await api("/ingredient-images").catch(() => ({ ids: [] }));
  for (const id of images.ids) generatedImageIds.add(id);
  inventory = await api("/inventory");
  renderInventory();
  await loadHistory();
  await refreshMenu();
});
