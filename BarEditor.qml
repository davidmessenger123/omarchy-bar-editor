import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Omarchy Bar Editor — bar-widget gear button.
//
// This widget lives in the Omarchy bar. Clicking the gear icon launches the
// standalone Bar Editor application via a launcher script that sets up a
// quickshell config directory with the required qs.Commons / qs.Ui symlinks.
BarWidget {
  id: root

  function localScriptPath(url) {
    var text = String(url || "")
    if (text.length > 4096 || text.indexOf("file://") !== 0) return ""
    try {
      text = decodeURIComponent(text.slice(7))
    } catch (error) {
      return ""
    }
    if (!text || text.length > 4096 || text.charAt(0) !== "/" || /[\u0000-\u001f\u007f]/.test(text) ||
        text.split("/").some(function(part) { return part === ".." })) return ""
    return text
  }

  readonly property string pluginDir: localScriptPath(String(Qt.resolvedUrl(".")))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf013"
    tooltipText: "Bar Editor — edit bar layout, settings, and styling"
    horizontalMargin: 8.25
    verticalPadding: 7.5
    onPressed: function(mouseButton) {
      if (root.pluginDir === "") return
      Quickshell.execDetached([
        "/usr/share/omarchy/bin/omarchy-launch-tui",
        "--app-id=org.omarchy.bar-editor",
        root.pluginDir + "bin/omarchy-bar-editor"
      ])
    }
  }
}
