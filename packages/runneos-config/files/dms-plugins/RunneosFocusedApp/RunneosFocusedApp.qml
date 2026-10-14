import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Widgets
import qs.Common
import qs.Services
import qs.Widgets
import qs.Modules.Plugins

// RunneOS · виджет активного окна (замена встроенного focusedWindow).
// Встроенный виджет DMS 1.6.3 открывает по любому клику контекстное меню и
// не умеет фокусировать окно (проверено по исходникам v1.6.3 и master).
// Здесь: левый клик — фокус окна через Niri IPC (NiriService.focusWindow),
// правый — выщёлка со сведениями и действиями. Данные — только NiriService,
// ничего не пишется и не запускается само.
PluginComponent {
    id: root
    pluginId: "runneosFocusedApp"
    layerNamespacePlugin: "runneos-focused-app"
    popoutWidth: 340

    readonly property var niriWindow: {
        if (!CompositorService.isNiri)
            return null
        const focused = NiriService.windows.find(w => w.is_focused)
        if (focused)
            return focused
        if (NiriService.lastFocusedWindowId === null)
            return null
        return NiriService.windows.find(w => w.id === NiriService.lastFocusedWindowId) || null
    }
    readonly property string appId: niriWindow?.app_id ?? ""
    readonly property string windowTitle: niriWindow?.title ?? ""
    readonly property string appName: appId ? Paths.getAppName(appId, null) : ""
    readonly property string iconSource: appId ? Paths.getAppIcon(appId, null) : ""

    function focusWindow() {
        if (niriWindow && niriWindow.id !== undefined)
            NiriService.focusWindow(niriWindow.id)
    }

    function closeWindow() {
        if (!niriWindow || niriWindow.id === undefined)
            return
        closeProc.command = ["niri", "msg", "action", "close-window", "--id", String(niriWindow.id)]
        closeProc.running = true
    }

    Process {
        id: closeProc
    }

    pillClickAction: () => root.focusWindow()
    pillRightClickAction: () => root.triggerPopout()

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS

            IconImage {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.iconSource !== ""
                source: root.iconSource
                width: 18
                height: 18
            }

            StyledText {
                anchors.verticalCenter: parent.verticalCenter
                text: root.appName === "" ? "" : (root.windowTitle === "" ? root.appName : root.appName + " — " + root.windowTitle)
                visible: root.appName !== ""
                width: Math.min(implicitWidth, 260)
                elide: Text.ElideRight
            }
        }
    }

    popoutContent: Component {
        PopoutComponent {
            headerText: root.appName !== "" ? root.appName : I18n.tr("No window focused")
            detailsText: root.windowTitle !== ""
                         ? root.windowTitle + (root.niriWindow && root.niriWindow.pid ? "  ·  pid " + root.niriWindow.pid : "")
                         : I18n.tr("Click a window to focus it")

            headerActions: Component {
                Row {
                    spacing: Theme.spacingXS

                    DankActionButton {
                        iconName: "center_focus_strong"
                        iconSize: 18
                        buttonSize: 30
                        visible: root.niriWindow !== null
                        onClicked: {
                            root.focusWindow()
                            root.closePopout()
                        }
                    }
                    DankActionButton {
                        iconName: "close"
                        iconSize: 18
                        buttonSize: 30
                        visible: root.niriWindow !== null
                        onClicked: {
                            root.closeWindow()
                            root.closePopout()
                        }
                    }
                }
            }
        }
    }
}
