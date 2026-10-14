import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

// RunneOS Agents · DMS-виджет.
// Данные берутся только из CLI runneos-agents (agent list --json, log --json):
// виджет не открывает SQLite и ничего не пишет в базу, не хранит секретов
// и не запускает задачи агентов. Клик по кнопке открывает панель
// runneos-agents-panel тем же способом, что и остальные виджеты RunneOS
// (Quickshell.execDetached), а не отдельным механизмом.
PluginComponent {
    id: root
    pluginId: "runneosAgents"
    layerNamespacePlugin: "runneos-agents"

    property var agents: []
    property var logEvents: []
    property string logSessionId: ""
    property string selectedAgentId: ""
    property string updated: ""
    property string errorText: ""
    property string logError: ""
    property bool loading: false
    property bool listOk: false

    readonly property string cliName: "runneos-agents"
    readonly property string panelName: "runneos-agents-panel"
    readonly property int runningCount: countState("running")
    readonly property int problemCount: countState("error") + countState("unavailable")
    readonly property string lastError: lastWithKinds(["error", "stderr"])
    readonly property string lastAnswer: lastWithKinds(["agent_message", "message", "turn.completed"])
    readonly property color statusColor: errorText ? Theme.error : (problemCount ? Theme.warning : Theme.primary)

    readonly property string summaryText: {
        if (errorText) return "Агенты · недоступны";
        if (!updated && loading) return "Агенты · проверка";
        if (!agents.length) return "Агенты · нет агентов";
        if (runningCount) return "Агенты · " + runningCount + " " + plural(runningCount, "агент работает", "агента работают", "агентов работают");
        if (problemCount) return "Агенты · " + problemCount + " " + plural(problemCount, "без провайдера", "без провайдера", "без провайдера");
        return "Агенты · " + agents.length + " " + plural(agents.length, "агент", "агента", "агентов");
    }
    // Последнее сообщение или ошибка. При недоступном CLI — причина, а не пустота.
    readonly property string detailText: {
        if (errorText) return errorText;
        if (lastError) return "Ошибка: " + shorten(lastError, 90);
        if (lastAnswer) return shorten(lastAnswer, 90);
        if (logError) return logError;
        return "";
    }

    function plural(count, one, few, many) {
        const last = count % 10;
        const rest = count % 100;
        if (last === 1 && rest !== 11) return one;
        if (last >= 2 && last <= 4 && (rest < 12 || rest > 14)) return few;
        return many;
    }
    function shorten(text, limit) {
        const clean = String(text || "").replace(/\s+/g, " ").trim();
        return clean.length > limit ? clean.slice(0, limit - 1) + "…" : clean;
    }
    function stateOf(agent) { return String((agent && (agent.state || agent.status)) || "idle"); }
    function agentId(agent) { return String((agent && (agent.id || agent.agent_id || agent.slug)) || ""); }
    function countState(state) {
        let count = 0;
        for (let i = 0; i < root.agents.length; i++) if (stateOf(root.agents[i]) === state) count++;
        return count;
    }
    function activityOf(agent) { return String((agent && (agent.updated_at || agent.last_activity || agent.last_message_at || agent.created_at)) || ""); }
    function sessionIdOf(agent) {
        if (!agent) return "";
        const direct = agent.session_id || agent.last_session_id || agent.latest_session_id || agent.current_session_id;
        if (direct) return String(direct);
        if (agent.last_session && (agent.last_session.id || agent.last_session.session_id)) return String(agent.last_session.id || agent.last_session.session_id);
        const list = agent.sessions || agent.last_sessions;
        if (Array.isArray(list) && list.length) return String(list[0].id || list[0].session_id || "");
        return "";
    }
    function pickAgent() {
        if (!root.agents.length) return null;
        // agent list --json не содержит времени активности, зато содержит число сессий:
        // сначала работающие агенты, затем те, у кого есть что показать в журнале.
        const running = root.agents.filter(agent => stateOf(agent) === "running");
        const withSessions = root.agents.filter(agent => Number(agent.sessions || 0) > 0);
        const pool = running.length ? running : (withSessions.length ? withSessions : root.agents);
        let best = pool[0];
        let bestAt = activityOf(best);
        for (let i = 1; i < pool.length; i++) {
            const at = activityOf(pool[i]);
            if (at && at > bestAt) { best = pool[i]; bestAt = at; }
        }
        return best;
    }
    // Форма --json у CLI не заморожена по полям: принимаем и массив, и JSON Lines,
    // и обёртки {agents|sessions|events|messages: [...]}, и одиночный объект.
    function parseRows(text) {
        const raw = String(text || "").trim();
        if (!raw) return [];
        const unwrap = row => {
            if (row && Array.isArray(row.agents)) return row.agents;
            if (row && Array.isArray(row.sessions)) return row.sessions;
            if (row && Array.isArray(row.events)) return row.events;
            return null;
        };
        try {
            const single = JSON.parse(raw);
            if (Array.isArray(single)) return single;
            const rows = unwrap(single);
            if (rows) return rows;
            if (single && typeof single === "object") return [single];
        } catch (_) { /* ниже разбор построчно */ }
        const collected = [];
        raw.split("\n").forEach(line => {
            const value = line.trim();
            if (!value) return;
            try {
                const row = JSON.parse(value);
                const rows = unwrap(row);
                if (rows) rows.forEach(item => collected.push(item));
                else collected.push(row);
            } catch (_) { /* нераспознанную строку пропускаем */ }
        });
        return collected;
    }
    function eventText(event) {
        if (!event) return "";
        if (typeof event.text === "string") return event.text;
        if (typeof event.message === "string") return event.message;
        const payload = event.payload;
        if (payload && typeof payload.text === "string") return payload.text;
        if (payload && typeof payload.message === "string") return payload.message;
        return "";
    }
    function lastWithKinds(kinds) {
        for (let i = root.logEvents.length - 1; i >= 0; i--) {
            const event = root.logEvents[i];
            if (kinds.indexOf(String((event && event.kind) || "")) < 0) continue;
            const text = eventText(event);
            if (text) return text.trim();
        }
        return "";
    }
    function findSessionId(rows) {
        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            if (!row) continue;
            const nested = sessionIdOf(row);
            if (nested) return nested;
            const looksLikeSession = row.id && !row.name && !row.role && (row.provider_session_id || row.title !== undefined || row.state !== undefined);
            if (looksLikeSession) return String(row.id);
        }
        return "";
    }
    function setOffline(reason) {
        root.agents = [];
        root.logEvents = [];
        root.logSessionId = "";
        root.selectedAgentId = "";
        root.listOk = false;
        root.logError = "";
        root.errorText = reason;
    }
    function clearLog() {
        root.logEvents = [];
        root.logSessionId = "";
        root.logError = "";
    }
    function startLog(sessionId) {
        if (logProcess.running) return;
        root.logSessionId = String(sessionId);
        root.logEvents = [];
        root.logError = "";
        logProcess.failed = false;
        logProcess.command = [root.cliName, "log", root.logSessionId, "--json", "--limit", "50"];
        logProcess.running = true;
    }
    function startShow(agentId) {
        if (showProcess.running) return;
        root.clearLog();
        showProcess.failed = false;
        showProcess.command = [root.cliName, "agent", "show", agentId, "--json"];
        showProcess.running = true;
    }
    function acceptAgents(text) {
        if (listProcess.failed) return;
        const rows = parseRows(text).filter(row => row && typeof row === "object" && !Array.isArray(row));
        // Пустой вывод при коде 0 — это «агентов нет», а не сбой: CLI печатает по
        // объекту на строку, поэтому ноль агентов даёт ноль строк. Раньше функция
        // выходила здесь, listOk оставался false, и onExited сообщал о недоступности.
        root.listOk = true;
        root.errorText = "";
        root.updated = new Date().toISOString();
        if (!rows.length) {
            root.agents = [];
            root.selectedAgentId = "";
            root.clearLog();
            return;
        }
        root.agents = rows;
        const agent = pickAgent();
        root.selectedAgentId = agent ? agentId(agent) : "";
        const session = sessionIdOf(agent);
        if (session) root.startLog(session);
        else if (root.selectedAgentId) root.startShow(root.selectedAgentId);
        else root.clearLog();
    }
    function acceptAgentShow(text) {
        if (showProcess.failed) return;
        const rows = parseRows(text);
        const session = findSessionId(rows);
        if (session) root.startLog(session);
        else root.clearLog();
    }
    function acceptLog(text) {
        if (logProcess.failed) return;
        const events = [];
        parseRows(text).forEach(row => {
            if (row && Array.isArray(row.messages)) {
                row.messages.forEach(message => events.push({ kind: String(message.role || "") === "agent" ? "agent_message" : "user_message", text: String(message.text || "") }));
                return;
            }
            if (row && typeof row === "object") events.push(row);
        });
        root.logEvents = events;
    }
    function refresh() {
        if (listProcess.running) return;
        root.loading = true;
        root.listOk = false;
        listProcess.failed = false;
        listProcess.command = [root.cliName, "agent", "list", "--json"];
        watchdog.restart();
        listProcess.running = true;
    }
    function openPanel() { Quickshell.execDetached([root.panelName]); }

    Process {
        id: listProcess
        property bool failed: false
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: root.acceptAgents(text)
        }
        onExited: (code) => {
            root.loading = false;
            listProcess.failed = code !== 0;
            watchdog.stop();
            if (code !== 0) root.setOffline(code === 127 ? "runneos-agents не найден" : "runneos-agents вернул код " + code);
            else if (!root.listOk) root.setOffline("runneos-agents не вернул список агентов");
        }
    }
    Process {
        id: showProcess
        property bool failed: false
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: root.acceptAgentShow(text)
        }
        onExited: (code) => {
            showProcess.failed = code !== 0;
            if (code !== 0) { root.clearLog(); root.logError = "журнал недоступен (код " + code + ")"; }
        }
    }
    Process {
        id: logProcess
        property bool failed: false
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: root.acceptLog(text)
        }
        onExited: (code) => {
            logProcess.failed = code !== 0;
            if (code !== 0) { root.clearLog(); root.logError = "журнал недоступен (код " + code + ")"; }
        }
    }
    // Если CLI отсутствует, Quickshell может не получить сигнал о завершении:
    // тогда честно сообщаем о недоступности, а не остаёмся в «проверке» навсегда.
    // Таймером управляем только командами restart()/stop(): биндинг на running
    // сломался бы при первом же останове и сторож не перевооружился.
    Timer {
        id: watchdog
        interval: 8000
        repeat: false
        onTriggered: {
            if (!root.loading) return;
            listProcess.running = false;
            root.loading = false;
            root.setOffline("runneos-agents не ответил");
        }
    }
    Timer {
        interval: root.runningCount > 0 ? 15000 : 60000
        repeat: true
        running: true
        onTriggered: root.refresh()
    }
    Component.onCompleted: root.refresh()
    // status() намеренно отдаёт только счётчики и причину: текст агентов наружу не уходит.
    IpcHandler {
        target: "runneos-agents"
        function refresh(): string { root.refresh(); return "OK"; }
        function open(): string { root.openPanel(); return "OK"; }
        function status(): string {
            return JSON.stringify({ updated: root.updated, loading: root.loading, agents: root.agents.length,
                running: root.runningCount, problems: root.problemCount, error: root.errorText });
        }
    }
    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingS
            DankIcon { name: "smart_toy"; size: root.iconSize; color: root.statusColor; filled: root.runningCount > 0; anchors.verticalCenter: parent.verticalCenter }
            StyledText { text: root.summaryText; color: Theme.surfaceText; font.pixelSize: Theme.fontSizeSmall; anchors.verticalCenter: parent.verticalCenter }
            StyledText {
                text: root.detailText
                visible: text.length > 0
                width: Math.min(implicitWidth, 200)
                elide: Text.ElideRight
                color: root.errorText || root.lastError ? Theme.error : Theme.surfaceVariantText
                font.pixelSize: Theme.fontSizeSmall
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }
    verticalBarPill: Component {
        DankIcon { name: "smart_toy"; size: root.iconSize; color: root.statusColor; filled: root.runningCount > 0 }
    }
    pillClickAction: () => root.openPanel()
    pillRightClickAction: () => root.refresh()
}
