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

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, ""))

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
      Quickshell.execDetached([
        "omarchy-launch-tui",
        "--app-id=org.omarchy.bar-editor",
        root.pluginDir + "bin/omarchy-bar-editor"
      ])
    }
  }
}
