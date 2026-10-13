import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root
    layerNamespacePlugin: "runneos-software"
    function launch() { Quickshell.execDetached(["runneos-software"]); }
    pillClickAction: () => root.launch()
    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingS
            DankIcon { name: "apps"; size: root.iconSize; color: Theme.primary; anchors.verticalCenter: parent.verticalCenter }
            StyledText { text: "Программы"; color: Theme.surfaceText; font.pixelSize: Theme.fontSizeSmall; anchors.verticalCenter: parent.verticalCenter }
        }
    }
    verticalBarPill: Component {
        DankIcon { name: "apps"; size: root.iconSize; color: Theme.primary }
    }
    IpcHandler {
        target: "runneos-software"
        function open(): string { root.launch(); return "OK"; }
    }
}
