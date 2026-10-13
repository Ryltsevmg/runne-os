import QtQuick
import Quickshell
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: root
    pluginId:"dankAIUsage"
    StyledText {text:"AI · RunneOS";font.pixelSize:Theme.fontSizeLarge;color:Theme.surfaceText}
    StyledText {width:parent.width;text:"Настоящие лимиты аккаунтов и API-балансы. Подключения и локальная история управляются в AI-центре; ключи хранятся в GNOME Keyring.";wrapMode:Text.WordWrap;color:Theme.surfaceVariantText}
    Rectangle {
        width:240;height:40;radius:6;color:"#315CFF"
        StyledText {anchors.centerIn:parent;text:"Открыть AI-центр";color:"#FFFFFF"}
        MouseArea {anchors.fill:parent;cursorShape:Qt.PointingHandCursor;onClicked:Quickshell.execDetached(["runneos-ai"])}
    }
    StyledText {width:parent.width;text:"Опрос не чаще трёх минут. Ошибки сохраняют последние доступные данные с явным возрастом. Неподключённые сервисы не показываются пустыми карточками.";wrapMode:Text.WordWrap;color:Theme.surfaceVariantText}
}
