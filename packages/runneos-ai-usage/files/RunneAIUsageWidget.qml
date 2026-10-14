import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root
    pluginId: "dankAIUsage"
    property var accounts: []
    property var history: []
    property string updated: ""
    property string errorText: ""
    property bool loading: false
    readonly property var selected: accounts.find(a => a.pinned && (a.quotas.length || a.balances.length)) || null
    readonly property string summaryText: {
        if (!selected) return loading ? "AI · обновление" : accounts.length ? "AI · проверить" : "AI · подключить";
        const stale = selected.status === "stale" ? " · старое" : "";
        if (selected.quotas.length) return selected.name.split(" · ")[0] + " · " + selected.quotas[0].label + " " + Math.round(100-selected.quotas[0].usedPercent) + "%" + stale;
        const b=selected.balances[0]; return selected.name.split(" · ")[0] + " · " + Number(b.amount).toFixed(2) + " " + b.currency + stale;
    }
    function openCenter() { Quickshell.execDetached(["runneos-ai"]); }
    function age(value) {
        if (!value) return "Успешных ответов пока нет";
        const mins=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/60000));
        return mins<1 ? "Обновлено только что" : "Последний ответ: " + mins + " мин назад";
    }
    function reset(value) {
        if (!value) return "Время сброса неизвестно";
        const parsed=new Date(value);
        if (isNaN(parsed.getTime())) return "Время сброса неизвестно";
        return "Сброс: " + Qt.formatDateTime(parsed,"dd.MM HH:mm");
    }
    function refresh(force) {
        if (collector.running) return;
        root.loading=true;
        collector.command=force ? ["runneos-ai-collect","--force"] : ["runneos-ai-collect"];
        collector.running=true;
    }
    Process {
        id: collector
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    const data=JSON.parse(text);
                    if (data.error || data.schemaVersion!==2 || !Array.isArray(data.accounts)) throw new Error("invalid");
                    root.accounts=data.accounts;root.history=data.history || [];root.updated=data.generatedAt || "";root.errorText="";
                } catch (_) {root.errorText="Не удалось обновить данные. Откройте AI-центр.";}
            }
        }
        onExited: (code) => {root.loading=false;if(code!==0)root.errorText="Сборщик недоступен. Откройте AI-центр.";}
    }
    Timer {interval:180000;repeat:true;running:true;onTriggered:root.refresh(false)}
    Component.onCompleted: refresh(false)
    IpcHandler {
        target: "runneos-ai-usage"
        function refresh(): string {root.refresh(true);return "Refresh requested";}
        function status(): string {
            return JSON.stringify({version:2,loading:root.loading,updated:root.updated,error:root.errorText,
                accounts:root.accounts.map(a=>({id:a.id,product:a.product,status:a.status,quotas:a.quotas.length,balances:a.balances.length})),historySources:root.history.length});
        }
        function open(): string {root.openCenter();return "OK";}
    }
    horizontalBarPill: Component {
        Row {
            spacing:Theme.spacingS
            DankIcon {name:"monitoring";size:root.iconSize;color:root.errorText ? Theme.error : Theme.primary;anchors.verticalCenter:parent.verticalCenter}
            StyledText {text:root.summaryText;color:Theme.surfaceText;font.pixelSize:Theme.fontSizeSmall;anchors.verticalCenter:parent.verticalCenter}
        }
    }
    verticalBarPill: Component {DankIcon {name:"monitoring";size:root.iconSize;color:Theme.primary}}
    pillRightClickAction: () => root.openCenter()
    popoutWidth:440
    popoutHeight:Math.min(650,220+accounts.length*190)
    popoutContent: Component {
        Item {
            id: panel
            property var parentPopout: null
            implicitHeight:root.popoutHeight
            DankFlickable {
                anchors.fill:parent;anchors.margins:18
                contentWidth:width;contentHeight:content.implicitHeight;clip:true
                Column {
                    id:content
                    width:parent.width;spacing:16
                    Row {
                        width:parent.width
                        StyledText {text:"Ваш AI-ресурс";font.pixelSize:Theme.fontSizeLarge;color:Theme.surfaceText;width:parent.width-40}
                        Rectangle {
                            width:34;height:34;radius:8;color:Theme.surfaceContainerHigh
                            DankIcon {anchors.centerIn:parent;name:"refresh";size:20;color:Theme.primary}
                            MouseArea {anchors.fill:parent;cursorShape:Qt.PointingHandCursor;enabled:!root.loading;onClicked:root.refresh(true)}
                        }
                    }
                    StyledText {width:parent.width;text:root.loading ? "Обновляем источники…" : "Подписки и API-балансы показаны раздельно";wrapMode:Text.WordWrap;color:Theme.surfaceVariantText;font.pixelSize:Theme.fontSizeSmall}
                    StyledText {width:parent.width;text:root.errorText;visible:text.length>0;wrapMode:Text.WordWrap;color:Theme.error}
                    StyledText {width:parent.width;text:"Добавьте нужный аккаунт в AI-центре. Неподключённые провайдеры не занимают панель.";visible:root.accounts.length===0;wrapMode:Text.WordWrap;color:Theme.surfaceVariantText}
                    Repeater {
                        model:root.accounts
                        Column {
                            id:card
                            required property var modelData
                            width:content.width;spacing:8
                            Rectangle {width:parent.width;height:1;color:Theme.outline}
                            StyledText {text:card.modelData.name;wrapMode:Text.WordWrap;width:parent.width;color:Theme.surfaceText;font.pixelSize:Theme.fontSizeMedium}
                            StyledText {text:card.modelData.label;width:parent.width;elide:Text.ElideRight;color:Theme.surfaceVariantText;font.pixelSize:Theme.fontSizeSmall}
                            StyledText {text:card.modelData.message || "Данные недоступны";visible:card.modelData.status!=="live";width:parent.width;wrapMode:Text.WordWrap;color:Theme.warning;font.pixelSize:Theme.fontSizeSmall}
                            Repeater {
                                model:card.modelData.quotas || []
                                Column {
                                    required property var modelData
                                    width:card.width;spacing:5
                                    StyledText {text:Math.round(100-parent.modelData.usedPercent)+"% осталось · "+parent.modelData.label;wrapMode:Text.WordWrap;width:parent.width;color:Theme.surfaceText;font.pixelSize:Theme.fontSizeMedium}
                                    Rectangle {
                                        width:parent.width;height:5;radius:3;color:Theme.surfaceContainerHighest
                                        Rectangle {width:parent.width*Math.max(0,Math.min(1,1-parent.parent.modelData.usedPercent/100));height:5;radius:3;color:parent.parent.modelData.usedPercent>90 ? Theme.error : Theme.primary}
                                    }
                                    StyledText {text:root.reset(parent.modelData.resetsAt);color:Theme.surfaceVariantText;font.pixelSize:Theme.fontSizeSmall}
                                }
                            }
                            Repeater {model:card.modelData.balances || [];StyledText {text:Number(modelData.amount).toFixed(2)+" "+modelData.currency;color:Theme.surfaceText;font.pixelSize:24}}
                            Repeater {model:card.modelData.metrics || [];StyledText {text:modelData.label+": "+Number(modelData.value).toFixed(2)+" "+modelData.unit;wrapMode:Text.WordWrap;width:card.width;color:Theme.surfaceText;font.pixelSize:Theme.fontSizeSmall}}
                            StyledText {text:card.modelData.scope;wrapMode:Text.WordWrap;width:parent.width;color:Theme.surfaceVariantText;font.pixelSize:Theme.fontSizeSmall}
                            StyledText {text:root.age(card.modelData.lastSuccess);color:Theme.surfaceVariantText;font.pixelSize:Theme.fontSizeSmall}
                        }
                    }
                    Rectangle {
                        width:parent.width;height:40;radius:6;color:"#315CFF"
                        StyledText {anchors.centerIn:parent;text:"Аккаунты и подробности →";color:"#FFFFFF";font.pixelSize:Theme.fontSizeMedium}
                        MouseArea {anchors.fill:parent;cursorShape:Qt.PointingHandCursor;onClicked:root.openCenter()}
                    }
                    StyledText {text:"Локальная история доступна отдельно в AI-центре";visible:root.history.length>0;wrapMode:Text.WordWrap;width:parent.width;color:Theme.surfaceVariantText;font.pixelSize:Theme.fontSizeSmall}
                }
            }
        }
    }
}
