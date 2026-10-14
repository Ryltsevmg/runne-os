"use strict";
// Fixtures only. No credentials, storage, account requests or OS commands.
const $ = (selector) => document.querySelector(selector);
const state = {view: "overview", scenario: "ready", space: "Работа", account: "codex"};
const scenarios = new Set(["ready", "stale", "auth", "low", "empty", "loading", "unknown", "zero"]);
const views = new Set(["overview", "accounts", "details", "programs", "settings"]);
let toastTimer;
function notify(text) {
  document.querySelector(".toast")?.remove();
  const node = document.createElement("div"); node.className = "toast";
  node.setAttribute("role", "status"); node.textContent = text; document.body.append(node);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.remove(), 5000);
}
function condition() {
  if (state.scenario === "stale") return `<div class="callout warning"><strong>Нет сети. Показываем сохранённые данные.</strong><p>Последний успешный ответ — 42 минуты назад. Остаток мог измениться.</p></div>`;
  if (state.scenario === "auth") return `<div class="callout error"><strong>Codex: нужно войти снова</strong><p>Сохранённые значения не являются текущими. Другие аккаунты продолжают работать.</p><button class="quiet-button" data-connect>Переподключить →</button></div>`;
  if (state.scenario === "low") return `<div class="callout warning"><strong>Мало ресурса в окне Codex</strong><p>Осталось 8% пятитчасового окна. Это не недельная квота и не баланс API.</p></div>`;
  return "";
}
function empty(loading = false) {
  return `<div class="empty-state"><div class="empty-symbol" aria-hidden="true">◈</div><h2>${loading ? "Получаем первые данные" : "Подключите свой AI"}</h2><p>${loading ? "Не подменяем ожидание нулевым балансом. В макете выберите другое состояние сверху." : "Ваши лимиты и балансы — в одном месте. Только те аккаунты, которыми вы пользуетесь."}</p>${loading ? "" : `<button class="primary" data-connect>Добавить аккаунт</button>`}</div>`;
}
function quota() { return state.scenario === "low" ? 8 : 62; }
function codexMetric() {
  if (state.scenario === "unknown") return `<div class="metric-value">Неизвестно</div><p class="metric-caption">Данные окна 5 часов не получены</p>`;
  return `<div class="metric-value">${quota()}% <span class="muted" style="font-size:14px">осталось</span></div><div class="meter" role="meter" aria-label="Остаток пятитчасового окна" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${quota()}"><div class="meter-fill ${quota() < 10 ? "low" : ""}" style="width:${quota()}%"></div></div><p class="metric-caption">Окно 5 часов · сброс через 2ч 18м</p>`;
}
function statusText() { return state.scenario === "stale" ? "Сохранено 42 мин назад" : state.scenario === "auth" ? "Нужен вход" : "Обновлено минуту назад"; }
function identity(name, product, glyph) { return `<div class="account-identity"><span class="provider-glyph" aria-hidden="true">${glyph}</span><div><h3>${name}</h3><p>${product}</p></div></div>`; }
function accounts() {
  if (["empty", "loading"].includes(state.scenario)) return empty(state.scenario === "loading");
  const amount = state.scenario === "zero" ? "0,00" : "12,40";
  return `${condition()}<div class="account-list"><section class="account-row">${identity("Codex", "Личный · подписка", "C")}<div>${codexMetric()}</div><button class="quiet-button" data-details>Подробнее →</button></section><section class="account-row">${identity("DeepSeek", "Проект · API", "D")}<div><div class="metric-value">$${amount}</div><p class="metric-caption">Баланс API · USD, не лимит подписки</p></div><button class="quiet-button" data-details>Источники →</button></section></div><p class="muted" style="margin-top:16px;font-size:12px">${statusText()} · Демонстрационные значения. Неподключённые сервисы находятся в «Добавить аккаунт».</p>`;
}
function overview() {
  return `<div class="page-heading"><div><span class="eyebrow">ВАШ РАБОЧИЙ СТОЛ</span><h1 style="margin-top:8px">Меньше шума. Больше дела.</h1><p>Рабочие контексты, приложения и AI-ресурс — в одном спокойном пространстве.</p></div><button class="primary" data-open-accounts>Мой AI</button></div><div class="section-head"><h3>Рабочие контексты</h3><small>Концепция Runne Spaces</small></div><div class="grid">${[["Разработка", ">_", "Редактор, проект и терминал в нужной папке"], ["Общение", "↗", "Разговоры и встречи без лишних окон"], ["Личное", "◉", "Ваши файлы и привычные приложения"]].map(([name, symbol, description]) => `<section class="space-card"><span class="space-icon" aria-hidden="true">${symbol}</span><strong>${name}</strong><p>${description}</p><button class="quiet-button" data-demo="${name}">Открыть контекст →</button></section>`).join("")}</div><section class="section"><div class="section-head"><h3>AI-ресурс</h3><button class="quiet-button" data-open-accounts>Все аккаунты →</button></div>${accounts()}</section><section class="section"><div class="activity-row"><div><h3>Ваша система под контролем</h3><p>Обновления, снимки и диагностика — отдельные проверяемые сценарии.</p></div><button class="quiet-button" data-demo="Диагностика">Подробнее →</button></div></section>`;
}
function details() {
  if (state.account === "deepseek") return apiDetails();
  return `<button class="quiet-button" data-open-accounts style="padding-left:0;margin-bottom:20px">← AI-аккаунты</button><div class="page-heading"><div><span class="eyebrow">CODEX / ЛИЧНЫЙ / ПОДПИСКА</span><h1 style="margin-top:8px">Ресурс и использование</h1><p>Лимиты аккаунта и локальная история показаны раздельно.</p></div><button class="quiet-button" data-demo="Обновление данных">Обновить</button></div>${condition()}<div class="detail-grid"><section class="metric-card"><span class="eyebrow">ПЯТИЧАСОВОЕ ОКНО</span>${codexMetric()}<small>Источник: ответ аккаунта · ДЕМО</small></section><section class="metric-card"><span class="eyebrow">НЕДЕЛЯ</span><div class="metric-value">81%</div><p class="metric-caption">Осталось · сброс в понедельник</p><small>Отдельное окно, не сумма с 5ч</small></section><section class="metric-card"><span class="eyebrow">ЛОКАЛЬНАЯ ИСТОРИЯ</span><div class="metric-value">1,24M</div><p class="metric-caption">Токенов за 7 дней</p><small>Только этот компьютер · не остаток квоты</small></section></div><section class="section"><div class="section-head"><h3>Источники и свежесть</h3><span class="demo-tag">ДЕМО</span></div><div class="setting-row"><div><strong>Лимиты подписки</strong><p>Окна и проценты из источника аккаунта</p></div><span class="status ${state.scenario === "stale" ? "warning" : ""}">${statusText()}</span></div><div class="setting-row"><div><strong>История CLI</strong><p>Известные этому компьютеру события; не смешивается с историей OpenCode</p></div><span class="status">Локально</span></div><div class="setting-row"><div><strong>Стоимость подписки</strong><p>Не вычисляем из стоимости токенов</p></div><span class="muted">Неизвестно</span></div></section><section class="section"><h3>Локальная активность · пример</h3><div class="table-scroll"><table><thead><tr><th>День</th><th>Токены</th><th>Запросы</th><th>Источник</th></tr></thead><tbody><tr><td>Сегодня</td><td>412 300</td><td>84</td><td>CLI · демо</td></tr><tr><td>Вчера</td><td>827 700</td><td>126</td><td>CLI · демо</td></tr></tbody></table></div></section>`;
}
function apiDetails() {
  const amount = state.scenario === "zero" ? "0,00" : "12,40";
  return `<button class="quiet-button" data-open-accounts style="padding-left:0;margin-bottom:20px">← AI-аккаунты</button><div class="page-heading"><div><span class="eyebrow">DEEPSEEK / ПРОЕКТ / API</span><h1 style="margin-top:8px">Баланс API</h1><p>Баланс аккаунта и использование на этом компьютере — разные источники.</p></div><span class="demo-tag">ДЕМО</span></div><div class="detail-grid"><section class="metric-card"><span class="eyebrow">ДОСТУПНО · USD</span><div class="metric-value">$${amount}</div><p class="metric-caption">Баланс API-аккаунта</p><small>${state.scenario === "stale" ? "Сохранено 42 минуты назад" : "Обновлено минуту назад"}</small></section><section class="metric-card"><span class="eyebrow">ПОДПИСОЧНАЯ КВОТА</span><div class="metric-value">Не применимо</div><small>У этого продукта баланс API, не окно подписки</small></section><section class="metric-card"><span class="eyebrow">ДРУГИЕ ВАЛЮТЫ</span><div class="metric-value">Неизвестно</div><small>Не складываем USD и CNY без курса</small></section></div><section class="section"><h3>Происхождение данных</h3><div class="setting-row"><div><strong>Источник баланса</strong><p>Account balance API · демонстрационный пример ответа</p></div><span class="status">ДЕМО</span></div><div class="setting-row"><div><strong>Ключ подключения</strong><p>В рабочей версии — ссылка на GNOME Keyring, не значение ключа</p></div><span class="muted">Прототип</span></div><div class="setting-row"><div><strong>Глобальный расход</strong><p>Локальная история не охватывает другие устройства</p></div><span class="muted">Неизвестно</span></div></section>`;
}
function programs() {
  return `<div class="page-heading"><div><h1>Программы</h1><p>Тот же удобный центр приложений — в визуальном языке Runne.</p></div><button class="quiet-button" data-demo="Поиск программ">Поиск</button></div><div class="section-head"><h3>Ваши инструменты</h3><small>Дизайн каталога · не установка</small></div><div class="grid">${[["Ghostty", ">_", "Свободный терминал, быстрый и предсказуемый"], ["Bitwarden", "◈", "Ваш сейф на компьютере и телефоне"], ["Firefox", "◎", "Работа с вебом и привычными расширениями"], ["Runne", "R", "Будущий AI-runtime. Пока не компонент ОС"], ["Файлы", "▱", "Папки, проекты и быстрый доступ"], ["AI-центр", "◉", "Аккаунты, лимиты и происхождение данных"]].map(([name, glyph, description]) => `<section class="program-card"><span class="program-icon" aria-hidden="true">${glyph}</span><h3>${name}</h3><p>${description}</p><button class="quiet-button" data-demo="${name}" style="align-self:start;padding-left:0">Подробнее →</button></section>`).join("")}</div>`;
}
function settings() {
  return `<div class="page-heading"><div><h1>Спокойно. По-своему.</h1><p>Настройки RunneOS остаются у своих владельцев. Hub объединяет доступ, а не дублирует состояние.</p></div></div>${[["Оформление", "Carbon / Cloud · общие токены", "Тему можно переключить сверху"], ["Панель", "Главные действия на виду, диагностика в раскрытии", "Концепция"], ["Уведомления AI", "Порог ресурса и интервал повторения", "Концепция"], ["Секреты", "GNOME Keyring — локально, Bitwarden — личный сейф", "Без доступа из макета"], ["Восстановление", "Снимки системы и отдельный backup личных файлов", "Не выполняется в макете"], ["Runne runtime", "Подключится позже через поддерживаемый API", "Отдельный этап"]].map(([name, description, status]) => `<div class="setting-row"><div><h3>${name}</h3><p>${description}</p></div><span class="muted" style="font-size:12px">${status}</span></div>`).join("")}`;
}
function popup() {
  if (["empty", "loading"].includes(state.scenario)) { $("#popup-body").innerHTML = empty(state.scenario === "loading"); return; }
  $("#popup-body").innerHTML = `${condition()}<section class="popup-account">${identity("Codex", "Личный · подписка", "C")}${codexMetric()}<p class="metric-caption">Неделя: осталось 81% · отдельное окно</p><p class="metric-caption">${statusText()}</p></section><section class="popup-account">${identity("DeepSeek", "Проект · API", "D")}<div class="metric-value">$${state.scenario === "zero" ? "0,00" : "12,40"}</div><p class="metric-caption">Баланс API · USD</p><p class="metric-caption">Не бюджет ключа и не подписка</p></section>`;
}
function render() {
  $("#account-count").textContent = state.scenario === "empty" ? "0" : "2";
  $("#bar-summary").textContent = state.scenario === "empty" ? "AI · подключить" : state.scenario === "loading" ? "AI · обновляется…" : state.scenario === "unknown" ? "AI · квота неизвестна" : state.scenario === "auth" ? "AI · нужен вход" : `AI · 5ч осталось ${quota()}%${state.scenario === "stale" ? " · старое" : ""}`;
  const content = {overview, accounts: () => `<div class="page-heading"><div><h1>Ваш AI-ресурс</h1><p>Подписки, балансы API и локальная история — без смешения понятий.</p></div><button class="primary" data-connect>Добавить аккаунт</button></div>${accounts()}`, details, programs, settings};
  $("#main").innerHTML = content[state.view]();
  document.querySelectorAll(".account-row").forEach(row => {
    row.querySelector("button").dataset.account = row.querySelector("h3").textContent === "DeepSeek" ? "deepseek" : "codex";
  });
  document.querySelectorAll("[data-view]").forEach(button => { const selected = button.dataset.view === (state.view === "details" ? "accounts" : state.view); button.classList.toggle("active", selected); if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
  popup();
}
function navigate(view) { if (!views.has(view)) return; state.view = view; render(); }
function showConnect() { $("#ai-popup").close(); $("#connect-dialog").showModal(); }
document.addEventListener("click", event => {
  const button = event.target.closest("button"); if (!button) return;
  if (button.dataset.view) navigate(button.dataset.view);
  if (button.hasAttribute("data-open-accounts")) navigate("accounts");
  if (button.hasAttribute("data-details")) { state.account = button.dataset.account || "codex"; navigate("details"); }
  if (button.hasAttribute("data-connect")) showConnect();
  if (button.dataset.demo) notify(`${button.dataset.demo}: это макет, действие ОС не выполняется.`);
  if (button.dataset.space) { state.space = button.dataset.space; document.querySelectorAll("[data-space]").forEach(b => b.classList.toggle("active", b.dataset.space === state.space)); notify(`Контекст «${state.space}» выбран в макете.`); }
});
$("#theme").addEventListener("change", event => { document.documentElement.dataset.theme = event.target.value; });
$("#scenario").addEventListener("change", event => { if (scenarios.has(event.target.value)) state.scenario = event.target.value; render(); });
$("#home").addEventListener("click", () => navigate("overview"));
$("#settings-shortcut").addEventListener("click", () => navigate("settings"));
$("#ai-toggle").addEventListener("click", () => { popup(); $("#ai-popup").showModal(); $("#ai-toggle").setAttribute("aria-expanded", "true"); });
$("#popup-close").addEventListener("click", () => $("#ai-popup").close());
$("#ai-popup").addEventListener("close", () => $("#ai-toggle").setAttribute("aria-expanded", "false"));
$("#popup-details").addEventListener("click", () => { $("#ai-popup").close(); navigate("accounts"); });
for (const id of ["connect-close", "connect-done"]) $("#"+id).addEventListener("click", () => $("#connect-dialog").close());
render();
