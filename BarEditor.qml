import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Editor.js" as Editor

// Omarchy Bar Editor — a native bar-widget editor for the Omarchy bar.
//
// One bar button (gear glyph) opens a full-screen editor overlay built from
// Omarchy's own UI components, so it inherits the shell's themes, fonts, and
// keyboard conventions. Changes are written atomically to shell.json and
// shell.toml through shell_io.py (the plugin's hardened config-write
// boundary) and hot-reloaded by the shell after a successful save.
BarWidget {
  id: root

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Absolute path to this plugin's folder (with trailing slash), resolved from
  // the QML file itself so shell_io.py is found wherever the plugin lives.
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")

  property bool editorOpen: false
  property var cfg: ({})
  property var catalog: ({})
  property var widgetStates: ({})
  property var toml: ({"values": {}, "exists": false})
  property var profiles: []
  property string statusText: ""
  property bool dirty: false
  property string globalFilter: ""

  property var undoStack: []
  property var redoStack: []
  readonly property int undoLimit: 100

  // Per-section display lists of { id, realIndex, section } — rebuilt on every
  // change. These are what the section ListViews bind to; the real arrays live
  // in root.cfg.bar.layout and are mutated through the helper functions below.
  property var leftList: []
  property var centerList: []
  property var rightList: []

  // Two-way-friendly state for toggles.
  property bool transparentValue: false
  property bool scaleFontValue: true

  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function ensureCfg() {
    return Editor.ensureCfg(root.cfg)
  }

  function secList(sec) {
    if (sec === "left") return root.leftList
    if (sec === "center") return root.centerList
    return root.rightList
  }

  function setSecList(sec, list) {
    if (sec === "left") root.leftList = list
    else if (sec === "center") root.centerList = list
    else root.rightList = list
  }

  function refreshLayout() {
    var cfg = root.ensureCfg()
    for (var i = 0; i < Editor.SECTIONS.length; i++) {
      var sec = Editor.SECTIONS[i]
      var all = cfg.bar.layout[sec]
      var out = []
      for (var j = 0; j < all.length; j++) {
        var id = all[j] && all[j].id ? all[j].id : ""
        if (id && Editor.rowMatches(root.catalog, id, root.globalFilter)) {
          out.push({id: id, realIndex: j, section: sec})
        }
      }
      root.setSecList(sec, out)
    }
  }

  // Widgets eligible for the "add" list: bar-widgets from the catalog, minus
  // anything already placed once when the widget doesn't allow multiples.
  function addOptions() {
    var placed = Editor.placedCount(root.ensureCfg())
    var out = []
    for (var id in root.catalog) {
      if (!Editor.isBarWidget(root.catalog[id])) continue
      if (!Editor.allowMultiple(root.catalog[id], id) && (placed[id] || 0) > 0) continue
      out.push({value: id, label: Editor.displayName(root.catalog, id)})
    }
    out.sort(function(a, b) { return a.label.localeCompare(b.label) })
    return out
  }

  // --- history -------------------------------------------------------------

  function pushHistory() {
    root.undoStack.push(JSON.stringify(root.cfg))
    if (root.undoStack.length > root.undoLimit) root.undoStack.shift()
    root.redoStack = []
  }

  function undo() {
    if (root.undoStack.length === 0) { root.statusText = "Nothing to undo"; return }
    root.redoStack.push(JSON.stringify(root.cfg))
    root.cfg = JSON.parse(root.undoStack.pop())
    root.markDirty()
    root.refreshLayout()
    root.syncFromCfgCore()
    root.statusText = "Undid change"
  }

  function redo() {
    if (root.redoStack.length === 0) { root.statusText = "Nothing to redo"; return }
    root.undoStack.push(JSON.stringify(root.cfg))
    root.cfg = JSON.parse(root.redoStack.pop())
    root.markDirty()
    root.refreshLayout()
    root.syncFromCfgCore()
    root.statusText = "Redid change"
  }

  function markDirty() {
    root.dirty = true
    root.statusText = "Unsaved changes"
  }

  function markClean() {
    root.dirty = false
  }

  // --- load ----------------------------------------------------------------

  function loadConfig() {
    root.statusText = "Loading…"
    readProcess.running = true
    catalogProcess.running = true
    statesProcess.running = true
  }

  function applyFromCfg() {
    var cfg = root.ensureCfg()
    var bar = cfg.bar || {}
    positionDropdown.value = bar.position || "top"
    root.transparentValue = !!bar.transparent
    anchorField.text = bar.centerAnchor || ""
    hostDropdown.value = bar.id || "omarchy.bar"

    var idle = cfg.idle || {}
    screensaverField.value = idle.screensaver || 0
    lockField.value = idle.lock || 0

    var tv = root.toml.values || {}
    bgColorField.value = tv.background || "#1a1b26"
    textColorField.value = tv.text || "#c0caf5"
    activeColorField.value = tv.active || "#f7768e"
    alphaField.value = Math.round(parseFloat(tv.background_alpha || "1.0") * 100)
    sizeHField.value = parseInt(tv.size_horizontal || "26", 10)
    sizeVField.value = parseInt(tv.size_vertical || "28", 10)
    root.scaleFontValue = String(tv.scale_with_font || "true") === "true"

    root.markClean()
    root.refreshLayout()
    root.statusText = "Loaded from disk"
  }

  function syncFromCfgCore() {
    var cfg = root.ensureCfg()
    var bar = cfg.bar || {}
    positionDropdown.value = bar.position || "top"
    root.transparentValue = !!bar.transparent
    anchorField.text = bar.centerAnchor || ""
    hostDropdown.value = bar.id || "omarchy.bar"
    var idle = cfg.idle || {}
    screensaverField.value = idle.screensaver || 0
    lockField.value = idle.lock || 0
    root.refreshLayout()
  }

  // --- collect / save ------------------------------------------------------

  function gatherCfg() {
    var cfg = Editor.clone(root.cfg)
    var bar = cfg.bar = cfg.bar || {}

    bar.position = positionDropdown.value || "top"
    bar.transparent = root.transparentValue
    var anchor = anchorField.text.trim()
    if (anchor) bar.centerAnchor = anchor
    else delete bar.centerAnchor

    var host = hostDropdown.value
    if (host === "omarchy.bar" || host === "") delete bar.id
    else bar.id = host

    var ss = screensaverField.value
    var lk = lockField.value
    if (ss > 0 || lk > 0) cfg.idle = {screensaver: ss, lock: lk}
    else delete cfg.idle

    cfg.version = 1
    return cfg
  }

  function gatherToml() {
    return {
      background: bgColorField.value,
      text: textColorField.value,
      active: activeColorField.value,
      background_alpha: String(Math.round(alphaField.value) / 100),
      size_horizontal: String(sizeHField.value),
      size_vertical: String(sizeVField.value),
      scale_with_font: root.scaleFontValue ? "true" : "false"
    }
  }

  function save() {
    root.statusText = "Saving…"
    writeProcess.command = ["python3", root.pluginDir + "shell_io.py", "write"]
    writeProcess.running = true
    writeProcess.write(JSON.stringify({shell: root.gatherCfg(), toml: {values: root.gatherToml()}}) + "\n")
  }

  // --- layout mutations ----------------------------------------------------

  function addWidget(sec, widgetId) {
    if (!sec || !widgetId) return
    var cfg = root.ensureCfg()
    root.pushHistory()
    cfg.bar.layout[sec].push({id: widgetId})
    root.markDirty()
    root.refreshLayout()
    root.statusText = "Added to " + Editor.SECTION_LABELS[sec].toLowerCase() + " section"
    if (!root.widgetStates[widgetId]) {
      pluginEnableProcess.command = ["omarchy", "plugin", "enable", widgetId]
      pluginEnableProcess.running = true
    }
  }

  function removeWidget(sec, realIndex) {
    var cfg = root.ensureCfg()
    root.pushHistory()
    cfg.bar.layout[sec].splice(realIndex, 1)
    root.markDirty()
    root.refreshLayout()
    root.statusText = "Removed from " + Editor.SECTION_LABELS[sec].toLowerCase() + " section"
  }

  function moveWidget(sec, realIndex, delta) {
    var list = root.ensureCfg().bar.layout[sec]
    var to = realIndex + delta
    if (to < 0 || to >= list.length) return
    root.pushHistory()
    var temp = list[realIndex]
    list[realIndex] = list[to]
    list[to] = temp
    root.markDirty()
    root.refreshLayout()
  }

  function moveSection(fromSec, realIndex, toSec) {
    if (!toSec || fromSec === toSec) return
    var cfg = root.ensureCfg()
    root.pushHistory()
    var entry = cfg.bar.layout[fromSec].splice(realIndex, 1)[0]
    cfg.bar.layout[toSec].push(entry)
    root.markDirty()
    root.refreshLayout()
    root.statusText = "Moved to " + Editor.SECTION_LABELS[toSec].toLowerCase() + " section"
  }

  function neighborSection(sec, direction) {
    var i = Editor.SECTIONS.indexOf(sec)
    var j = i + direction
    if (j < 0 || j >= Editor.SECTIONS.length) return ""
    return Editor.SECTIONS[j]
  }

  // --- reset / bar toggle --------------------------------------------------

  function resetBar() {
    resetConfirm.opened = true
  }

  function confirmReset() {
    resetProcess.running = true
  }

  function toggleBarVisible() {
    toggleBarProcess.running = true
  }

  function closeEditor() {
    root.editorOpen = false
  }

  // --- bar button ----------------------------------------------------------

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf013"
    tooltipText: "Bar Editor — edit bar layout, settings, and styling"
    horizontalMargin: 8.25
    verticalPadding: 7.5
    onPressed: function(mouseButton) {
      root.editorOpen = !root.editorOpen
    }
  }

  // --- the editor overlay --------------------------------------------------

  PanelWindow {
    id: editorWindow
    visible: root.editorOpen
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "omarchy-bar-editor"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

    onVisibleChanged: {
      if (visible) Qt.callLater(function() {
        if (!root.editorOpen) return
        keysCatch.forceActiveFocus()
        root.loadConfig()
      })
    }

    Rectangle {
      anchors.fill: parent
      color: Util.alpha(Color.background, 0.78)

      // click on the scrim closes
      MouseArea {
        anchors.fill: parent
        onClicked: root.closeEditor()
      }
    }

    // Keyboard dispatch. Keys.BeforeItem lets Ctrl+S/Ctrl+R work even while a
    // TextField has focus; plain key handling is left to whichever control
    // has focus so typing is never intercepted.
    Item {
      id: keysCatch
      anchors.fill: parent
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) {
        if (resetConfirm.opened) {
          if (resetConfirm.handleKey(event)) event.accepted = true
          return
        }
        if (event.key === Qt.Key_Escape) {
          root.closeEditor(); event.accepted = true
        } else if (event.modifiers === Qt.ControlModifier && event.key === Qt.Key_S) {
          root.save(); event.accepted = true
        } else if (event.modifiers === Qt.ControlModifier && event.key === Qt.Key_R) {
          root.loadConfig(); event.accepted = true
        } else if (event.modifiers === Qt.ControlModifier && !(event.modifiers & Qt.ShiftModifier) && event.key === Qt.Key_Z) {
          root.undo(); event.accepted = true
        } else if (event.modifiers === (Qt.ControlModifier | Qt.ShiftModifier) && event.key === Qt.Key_Z) {
          root.redo(); event.accepted = true
        }
      }

      Rectangle {
        id: card
        anchors.centerIn: parent
        width: parent.width - 2 * Style.space(80)
        height: parent.height - 2 * Style.space(64)
        radius: Style.cornerRadius
        color: Color.popups.background
        border.width: Math.max(1, Style.space(2))
        border.color: Color.popups.border
        clip: true

        ColumnLayout {
          anchors.fill: parent
          anchors.margins: Style.space(16)
          spacing: Style.space(12)

          // ---- header --------------------------------------------------
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            Text {
              text: "BAR  EDITOR"
              color: Color.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              font.letterSpacing: 2
              Layout.fillWidth: true
            }

            Button {
              text: "Undo"
              tooltipText: "Undo last change (Ctrl+Z)"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(3)
              onClicked: root.undo()
            }
            Button {
              text: "Redo"
              tooltipText: "Redo (Ctrl+Shift+Z)"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(3)
              onClicked: root.redo()
            }
            Button {
              text: "Reload"
              tooltipText: "Reload config from disk (Ctrl+R)"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(3)
              onClicked: root.loadConfig()
            }
            Button {
              text: "Reset"
              tooltipText: "Reset bar to Omarchy defaults"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(3)
              onClicked: root.resetBar()
            }
            Button {
              text: "Save"
              tooltipText: "Save changes (Ctrl+S)"
              bordered: true
              foreground: Color.accent
              accent: Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(14)
              verticalPadding: Style.space(3)
              onClicked: root.save()
            }
            Button {
              text: "\uf00d"
              tooltipText: "Close (Esc)"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(3)
              onClicked: root.closeEditor()
            }
          }

          // ---- body ----------------------------------------------------
          RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Style.space(16)

            // ---- left: settings column -------------------------------
            ColumnLayout {
              Layout.preferredWidth: Style.space(290)
              Layout.fillHeight: true
              spacing: Style.space(10)

              // BAR SETTINGS
              Rectangle {
                Layout.fillWidth: true
                radius: Style.cornerRadius
                color: Util.alpha(Color.foreground, 0.05)
                implicitHeight: barSettingsCol.implicitHeight + 2 * Style.space(12)

                ColumnLayout {
                  id: barSettingsCol
                  anchors.fill: parent
                  anchors.margins: Style.space(12)
                  spacing: Style.space(10)

                  PanelSectionHeader { text: "BAR SETTINGS" }

                  Dropdown {
                    id: positionDropdown
                    Layout.fillWidth: true
                    label: "Position"
                    options: Editor.POSITIONS
                    value: "top"
                    onChanged: root.markDirty()
                  }

                  Toggle {
                    Layout.fillWidth: true
                    label: "Transparent bar"
                    description: "Transparent bar background"
                    checked: root.transparentValue
                    onClicked: { root.transparentValue = !root.transparentValue; root.markDirty() }
                  }

                  ColumnLayout {
                    spacing: Style.spacing.labelGap
                    Layout.fillWidth: true
                    Text {
                      textFormat: Text.PlainText
                      text: "Center anchor widget"
                      color: Qt.darker(Color.foreground, 1.4)
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }
                    TextField {
                      id: anchorField
                      Layout.fillWidth: true
                      font.pixelSize: Style.font.bodySmall
                      placeholderText: "e.g. omarchy.clock"
                      selectByMouse: true
                      onTextChanged: root.markDirty()
                    }
                  }

                  Dropdown {
                    id: hostDropdown
                    Layout.fillWidth: true
                    label: "Bar host"
                    options: []
                    value: "omarchy.bar"
                    onChanged: root.markDirty()
                  }
                }
              }

              // IDLE
              Rectangle {
                Layout.fillWidth: true
                radius: Style.cornerRadius
                color: Util.alpha(Color.foreground, 0.05)
                implicitHeight: idleCol.implicitHeight + 2 * Style.space(12)

                ColumnLayout {
                  id: idleCol
                  anchors.fill: parent
                  anchors.margins: Style.space(12)
                  spacing: Style.space(10)

                  PanelSectionHeader { text: "IDLE / LOCK" }

                  NumberField {
                    id: screensaverField
                    label: "Screensaver (s)"
                    from: 0; to: 86400; stepSize: 10
                    onModified: root.markDirty()
                  }
                  NumberField {
                    id: lockField
                    label: "Lock (s)"
                    from: 0; to: 86400; stepSize: 10
                    onModified: root.markDirty()
                  }
                }
              }

              // STYLING
              Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: Style.cornerRadius
                color: Util.alpha(Color.foreground, 0.05)

                ColumnLayout {
                  anchors.fill: parent
                  anchors.margins: Style.space(12)
                  spacing: Style.space(10)

                  PanelSectionHeader { text: "BAR STYLING (shell.toml)" }

                  ColorPicker {
                    id: bgColorField
                    Layout.fillWidth: true
                    label: "Background"
                    value: "#1a1b26"
                    onChanged: root.markDirty()
                  }
                  ColorPicker {
                    id: textColorField
                    Layout.fillWidth: true
                    label: "Text"
                    value: "#c0caf5"
                    onChanged: root.markDirty()
                  }
                  ColorPicker {
                    id: activeColorField
                    Layout.fillWidth: true
                    label: "Active"
                    value: "#f7768e"
                    onChanged: root.markDirty()
                  }
                  NumberField {
                    id: alphaField
                    label: "Background alpha (%)"
                    from: 0; to: 100; stepSize: 5
                    onModified: root.markDirty()
                  }
                  NumberField {
                    id: sizeHField
                    label: "Horizontal size (px)"
                    from: 1; to: 200; stepSize: 1
                    onModified: root.markDirty()
                  }
                  NumberField {
                    id: sizeVField
                    label: "Vertical size (px)"
                    from: 1; to: 200; stepSize: 1
                    onModified: root.markDirty()
                  }
                  Toggle {
                    Layout.fillWidth: true
                    label: "Scale with font"
                    checked: root.scaleFontValue
                    onClicked: { root.scaleFontValue = !root.scaleFontValue; root.markDirty() }
                  }

                  Item { Layout.fillHeight: true }

                  Button {
                    Layout.fillWidth: true
                    text: "Toggle bar on/off"
                    tooltipText: "Show/hide the bar"
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    horizontalPadding: Style.space(10)
                    verticalPadding: Style.space(3)
                    onClicked: root.toggleBarVisible()
                  }
                }
              }
            }

            // ---- right: layout editor ---------------------------------
            ColumnLayout {
              Layout.fillWidth: true
              Layout.fillHeight: true
              spacing: Style.space(10)

              PanelSectionHeader { text: "BAR  LAYOUT" }

              TextField {
                id: globalSearchField
                Layout.fillWidth: true
                font.pixelSize: Style.font.bodySmall
                placeholderText: "Search across all sections (Ctrl+F)…"
                selectByMouse: true
                onTextChanged: { root.globalFilter = text; root.refreshLayout() }
              }

              RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Style.space(10)

                Repeater {
                  model: Editor.SECTIONS

                  Rectangle {
                    Layout.preferredWidth: 1
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: Style.cornerRadius
                    color: Util.alpha(Color.foreground, 0.05)
                    clip: true

                    ColumnLayout {
                      anchors.fill: parent
                      anchors.margins: Style.space(10)
                      spacing: Style.space(8)

                      // section header: label + count
                      RowLayout {
                        Layout.fillWidth: true
                        Text {
                          Layout.fillWidth: true
                          text: Editor.SECTION_LABELS[modelData].toUpperCase()
                          color: Color.foreground
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          font.bold: true
                          elide: Text.ElideRight
                        }
                        Text {
                          text: root.secList(modelData).length
                          color: Qt.darker(Color.foreground, 1.4)
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                        }
                      }

                      ListView {
                        id: sectionList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: Style.space(4)
                        model: root.secList(modelData)

                        delegate: Rectangle {
                          required property var modelData
                          required property int index
                          width: sectionList.width
                          height: rowBody.implicitHeight + Style.space(8)
                          radius: Style.cornerRadius
                          color: Util.alpha(Color.foreground, 0.03)

                          readonly property var entry: modelData || {}
                          readonly property var wid: entry.id || ""
                          readonly property string sec: entry.section || ""
                          readonly property int realIndex: entry.realIndex

                          RowLayout {
                            id: rowBody
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.leftMargin: Style.space(8)
                            anchors.rightMargin: Style.space(8)
                            spacing: Style.space(6)

                            Rectangle {
                              Layout.preferredWidth: Style.space(6)
                              Layout.preferredHeight: Style.space(6)
                              radius: Math.ceil(Style.space(3))
                              color: Editor.catColor(root.catalog, wid || "")
                            }

                            ColumnLayout {
                              spacing: 0
                              Layout.fillWidth: true
                              Layout.fillHeight: true
                              Text {
                                text: Editor.displayName(root.catalog, wid)
                                color: Color.foreground
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.bodySmall
                                font.bold: true
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                              }
                              Text {
                                text: wid
                                color: Qt.darker(Color.foreground, 1.4)
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.caption
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                              }
                            }

                            Button {
                              text: "\uf077"
                              tooltipText: "Move up"
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              horizontalPadding: Style.space(5)
                              verticalPadding: Style.space(2)
                              onClicked: root.moveWidget(sec, realIndex, -1)
                            }
                            Button {
                              text: "\uf078"
                              tooltipText: "Move down"
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              horizontalPadding: Style.space(5)
                              verticalPadding: Style.space(2)
                              onClicked: root.moveWidget(sec, realIndex, 1)
                            }
                            Button {
                              text: "\uf053"
                              tooltipText: "To previous section"
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              horizontalPadding: Style.space(5)
                              verticalPadding: Style.space(2)
                              onClicked: root.moveSection(sec, realIndex, root.neighborSection(sec, -1))
                            }
                            Button {
                              text: "\uf054"
                              tooltipText: "To next section"
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              horizontalPadding: Style.space(5)
                              verticalPadding: Style.space(2)
                              onClicked: root.moveSection(sec, realIndex, root.neighborSection(sec, 1))
                            }
                            Button {
                              text: "\uf1f8"
                              tooltipText: "Remove"
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              horizontalPadding: Style.space(5)
                              verticalPadding: Style.space(2)
                              onClicked: root.removeWidget(sec, realIndex)
                            }
                          }
                        }
                      }

                      // add widget
                      SearchableDropdown {
                        id: addDrop
                        Layout.fillWidth: true
                        label: "Add widget"
                        triggerLabel: "+ Add widget"
                        placeholderText: "Search widgets…"
                        emptyText: "No matching widgets"
                        options: root.addOptions()
                        onChanged: {
                          if (!value) return
                          root.addWidget(modelData, value)
                          addDrop.value = ""
                        }
                      }
                    }
                  }
                }
              }
            }
          }

          // ---- footer / status -----------------------------------------
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            Rectangle {
              Layout.preferredWidth: Style.space(8)
              Layout.preferredHeight: Style.space(8)
              radius: Math.ceil(Style.space(4))
              color: root.dirty ? Color.urgent : "#3fb950"
            }

            Text {
              Layout.fillWidth: true
              text: root.statusText
              color: Qt.darker(Color.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Text {
              text: "Ctrl+S save · Ctrl+R reload · Esc close"
              color: Qt.darker(Color.foreground, 1.6)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      // reset confirmation (rendered above the card)
      ConfirmDialog {
        id: resetConfirm
        anchors.fill: parent
        opened: false
        message: "Reset the bar to Omarchy defaults?"
        confirmText: "Reset"
        onConfirmed: root.confirmReset()
        onCanceled: resetConfirm.opened = false
      }
    }
  }

  // ---- processes ----------------------------------------------------------

  Process {
    id: readProcess
    running: false
    command: ["python3", root.pluginDir + "shell_io.py", "read"]
    stdout: StdioCollector {
      id: readOutput
      waitForEnd: true
    }
    onExited: function(exitCode) {
      var text = readOutput.text.trim()
      if (exitCode !== 0 || !text) {
        root.statusText = "Could not read config"
        return
      }
      try {
        var parsed = JSON.parse(text)
        root.cfg = Editor.ensureCfg(parsed.cfg || {})
        root.toml = parsed.toml || {values: {}, exists: false}
        root.profiles = parsed.profiles || []
        root.applyFromCfg()
      } catch (e) {
        root.statusText = "Invalid config state"
      }
    }
  }

  Process {
    id: catalogProcess
    running: false
    command: ["omarchy-plugin-catalog"]
    stdout: StdioCollector {
      id: catalogOutput
      waitForEnd: true
    }
    onExited: function(exitCode) {
      var text = catalogOutput.text.trim()
      if (exitCode !== 0 || !text) return
      try {
        var entries = JSON.parse(text)
        var cat = {}
        for (var i = 0; i < entries.length; i++) {
          var e = entries[i]
          if (e && e.id) cat[e.id] = e
        }
        root.catalog = cat
        hostDropdown.options = Editor.hosts(cat).map(function(h) {
          return {value: h, label: h === "omarchy.bar" ? "Omarchy (default)" : h}
        })
        root.refreshLayout()
      } catch (e) {}
    }
  }

  Process {
    id: statesProcess
    running: false
    command: ["omarchy", "plugin", "list", "--json"]
    stdout: StdioCollector {
      id: statesOutput
      waitForEnd: true
    }
    onExited: function(exitCode) {
      var text = statesOutput.text.trim()
      if (exitCode !== 0 || !text) return
      try {
        var entries = JSON.parse(text)
        var st = {}
        for (var i = 0; i < entries.length; i++) {
          var e = entries[i]
          if (e && e.id && e.enabled !== undefined) st[e.id] = e.enabled
        }
        root.widgetStates = st
      } catch (e) {}
    }
  }

  Process {
    id: writeProcess
    running: false
    stdinEnabled: true
    onExited: function(exitCode) {
      if (exitCode === 0) {
        root.markClean()
        root.refreshLayout()
        root.statusText = "Saved — the shell reloads automatically"
      } else {
        root.statusText = "Save failed — check the logs"
      }
    }
  }

  Process {
    id: pluginEnableProcess
    running: false
  }

  Process {
    id: resetProcess
    running: false
    command: ["omarchy", "bar", "defaults"]
    onExited: function(exitCode) {
      resetConfirm.opened = false
      if (exitCode === 0) {
        root.statusText = "Reset to default Omarchy bar"
        root.loadConfig()
      } else {
        root.statusText = "Reset failed"
      }
    }
  }

  Process {
    id: toggleBarProcess
    running: false
    command: ["omarchy", "toggle", "bar"]
    onExited: function(exitCode) {
      if (exitCode === 0) root.statusText = "Bar visibility toggled"
    }
  }
}