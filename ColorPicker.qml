import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Compact color picker for the Bar Editor. A labeled trigger shows the current
// swatch + hex value and opens a popup with an SV square, a hue slider, and a
// set of preset swatches. `value` is always a normalized "#rrggbb" string;
// `changed(string)` fires on every user edit.
//
// The SV square is built from three layers: a base Rectangle painted with the
// pure hue, a horizontal white→transparent gradient that fades saturation left
// to right, and a vertical transparent→black gradient that fades value bottom
// to top. This avoids any per-pixel painting and matches what most hardware
// pickers show for the hue's SV plane.
Item {
  id: root

  property string label: ""
  property string value: "#1a1b26"
  property color foreground: Color.popups.text
  property color accent: Color.accent

  property string fontFamily: Style.font.family
  property int rowHeight: Style.spacing.controlHeight
  property int popupWidth: 236

  signal changed(string value)

  readonly property var popupBorderSpec: Border.localOrSurfaceSpec("popups", "border", Color.popups.border, Color.popups.border, Style.normalBorderWidth)

  implicitWidth: Style.spacing.dropdownWidth
  implicitHeight: root.rowHeight

  function normalized(v) {
    var s = String(v || "").replace(/^#/, "").trim()
    if (/^[0-9a-fA-F]{6}$/.test(s)) return "#" + s.toLowerCase()
    if (/^[0-9a-fA-F]{3}$/.test(s)) {
      return "#" + s.split("").map(function(c) { return c + c }).join("").toLowerCase()
    }
    return "#1a1b26"
  }

  // Hex → {h, s, v} (h in 0..1). Falls back to a neutral gray on bad input.
  function hexToHsv(hex) {
    var s = normalized(hex).replace(/^#/, "")
    var r = parseInt(s.substr(0, 2), 16) / 255
    var g = parseInt(s.substr(2, 2), 16) / 255
    var b = parseInt(s.substr(4, 2), 16) / 255
    var max = Math.max(r, g, b), min = Math.min(r, g, b)
    var d = max - min, h = 0
    if (d !== 0) {
      if (max === r) h = ((g - b) / d) % 6
      else if (max === g) h = (b - r) / d + 2
      else h = (r - g) / d + 4
      h /= 6
      if (h < 0) h += 1
    }
    return { h: h, s: max === 0 ? 0 : d / max, v: max }
  }

  function hsvToHex(h, s, v) {
    var i = Math.floor(h * 6), f = h * 6 - i
    var p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s)
    var r, g, b
    switch (i % 6) {
      case 0: r = v; g = t; b = p; break
      case 1: r = q; g = v; b = p; break
      case 2: r = p; g = v; b = t; break
      case 3: r = p; g = q; b = v; break
      case 4: r = t; g = p; b = v; break
      default: r = v; g = p; b = q
    }
    return normalized(
      "#" + [r, g, b].map(function(c) {
        return Math.round(Math.min(1, Math.max(0, c)) * 255).toString(16).padStart(2, "0")
      }).join(""))
  }

  function setNewValue(v) {
    v = normalized(typeof v === "string" ? v : String(v))
    if (v === root.value) return
    root.value = v
    root.changed(v)
  }

  RowLayout {
    anchors.fill: parent
    spacing: Style.spacing.controlGap

    Text {
      textFormat: Text.PlainText
      visible: root.label !== ""
      text: root.label
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      elide: Text.ElideRight
      Layout.preferredWidth: Math.max(Style.space(56), implicitWidth)
    }

    BorderSurface {
      id: trigger
      Layout.fillWidth: true
      Layout.fillHeight: true
      radius: Style.cornerRadius

      readonly property bool _focused: trigger.activeFocus
      readonly property bool _hot: triggerHover.hovered
      readonly property var _borderSpec: Border.controlSpec(trigger._focused ? "focus" : (trigger._hot ? "hover-cursor" : "normal"), root.foreground, root.accent)

      color: Style.controlFill(trigger._focused, trigger._hot, root.foreground, root.accent)
      borderSpec: _borderSpec

      activeFocusOnTab: true

      HoverHandler { id: triggerHover }

      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
            || event.key === Qt.Key_Space || event.key === Qt.Key_Down) {
          popup.opened ? popup.close() : popup.open()
          event.accepted = true
        } else if (event.key === Qt.Key_Escape && popup.opened) {
          popup.close(); event.accepted = true
        }
      }

      Rectangle {
        id: swatch
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: trigger.borderLeft + Style.spacing.controlGap
        width: root.rowHeight - 2 * Style.spacing.controlPaddingY
        height: root.rowHeight - 2 * Style.spacing.controlPaddingY
        radius: Style.cornerRadius * 0.5
        color: root.value
        border.width: Math.max(1, Style.normalBorderWidth)
        border.color: Qt.darker(root.foreground, 1.4)
      }

      Text {
        textFormat: Text.PlainText
        anchors.left: swatch.right
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.spacing.controlGap
        anchors.rightMargin: Style.spacing.md
        text: root.value
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
      }

      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: {
          trigger.forceActiveFocus()
          popup.opened ? popup.close() : popup.open()
        }
      }
    }
  }

  Popup {
    id: popup
    x: Math.max(0, Math.min(parent.width - width, parent.width - root.popupWidth))
    y: parent.height + Style.spacing.xxs
    width: root.popupWidth
    padding: Style.spacing.hairline
    leftPadding: Border.left(root.popupBorderSpec) + Style.spacing.md
    rightPadding: Border.right(root.popupBorderSpec) + Style.spacing.md
    topPadding: Border.top(root.popupBorderSpec) + Style.spacing.md
    bottomPadding: Border.bottom(root.popupBorderSpec) + Style.spacing.md
    focus: true

    background: BorderSurface {
      color: Color.popups.background
      borderSpec: root.popupBorderSpec
      radius: Style.cornerRadius
    }

    // Shared SV square + hue slider state, initialized from the current value.
    property var hs: (function() {
      var c = root.hexToHsv(root.value)
      return { h: c.h, s: c.s, v: c.v }
    })()

    onOpened: {
      var c = root.hexToHsv(root.value)
      popup.hs = { h: c.h, s: c.s, v: c.v }
      hexField.text = root.value
      hexField.forceActiveFocus()
    }

    function commit() {
      root.setNewValue(root.hsvToHex(popup.hs.h, popup.hs.s, popup.hs.v))
    }

    ColumnLayout {
      width: popup.availableWidth
      spacing: Style.spacing.md

      TextField {
        id: hexField
        Layout.fillWidth: true
        font.pixelSize: Style.font.bodySmall
        onTextChanged: root.setNewValue(root.normalized(text))
      }

      // ---- SV square -------------------------------------------------
      Item {
        Layout.preferredWidth: popup.availableWidth
        Layout.preferredHeight: 132
        clip: true

        Rectangle {
          anchors.fill: parent
          color: root.hsvToHex(popup.hs.h, 1, 1)
        }
        Rectangle {
          anchors.fill: parent
          gradient: Gradient {
            GradientStop { position: 0.0; color: "#ffffff" }
            GradientStop { position: 1.0; color: "#00000000" }
          }
        }
        Rectangle {
          anchors.fill: parent
          gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: "#00000000" }
            GradientStop { position: 1.0; color: "#ff000000" }
          }
        }

        // Crosshair indicator.
        Rectangle {
          property real hx: popup.hs.s
          property real hy: -popup.hs.v + 1
          x: hx * parent.width - width / 2
          y: hy * parent.height - height / 2
          width: 14
          height: 14
          radius: 7
          color: "transparent"
          border.width: 2
          border.color: "white"
          Behavior on x { NumberAnimation { duration: 60 } }
          Behavior on y { NumberAnimation { duration: 60 } }
        }

        MouseArea {
          anchors.fill: parent
          onPressed: {
            popup.hs.s = Math.min(1, Math.max(0, mouse.x / width))
            popup.hs.v = Math.min(1, Math.max(0, 1 - mouse.y / height))
            popup.commit()
            mouse.accepted = true
          }
          onPositionChanged: {
            if (pressed) {
              popup.hs.s = Math.min(1, Math.max(0, mouse.x / width))
              popup.hs.v = Math.min(1, Math.max(0, 1 - mouse.y / height))
              popup.commit()
            }
          }
        }
      }

      // ---- hue slider ------------------------------------------------
      Item {
        Layout.preferredWidth: popup.availableWidth
        Layout.preferredHeight: 12

        Rectangle {
          anchors.fill: parent
          radius: Math.ceil(6)
          gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.00; color: "#ff0000" }
            GradientStop { position: 0.16; color: "#ffff00" }
            GradientStop { position: 0.33; color: "#00ff00" }
            GradientStop { position: 0.50; color: "#00ffff" }
            GradientStop { position: 0.66; color: "#0000ff" }
            GradientStop { position: 0.83; color: "#ff00ff" }
            GradientStop { position: 1.00; color: "#ff0000" }
          }
        }

        Rectangle {
          property real hx: popup.hs.h
          x: hx * parent.width - width / 2
          anchors.verticalCenter: parent.verticalCenter
          width: 5
          height: parent.height + 4
          radius: 3
          color: "white"
          border.width: 1
          border.color: "#000000aa"
          Behavior on x { NumberAnimation { duration: 60 } }
        }

        MouseArea {
          anchors.fill: parent
          onPressed: {
            popup.hs.h = Math.min(1, Math.max(0, mouse.x / width))
            popup.commit()
            mouse.accepted = true
          }
          onPositionChanged: {
            if (pressed) {
              popup.hs.h = Math.min(1, Math.max(0, mouse.x / width))
              popup.commit()
            }
          }
        }
      }

      // ---- preset swatches -------------------------------------------
      Grid {
        Layout.fillWidth: true
        columns: 8
        columnSpacing: Style.spacing.controlGap
        rowSpacing: Style.spacing.controlGap

        Repeater {
          model: [Color.background, Color.foreground, Color.accent, Color.urgent,
                  "#1a1b26", "#c0caf5", "#f7768e", "#7dcfff",
                  "#e0af68", "#73daca", "#bb9af7", "#f38ba8",
                  "#a6e3a1", "#89b4fa", "#f9e2af", "#f5c2e7"]

          Rectangle {
            width: (popup.availableWidth - 7 * Style.spacing.controlGap) / 8
            height: (popup.availableWidth - 7 * Style.spacing.controlGap) / 8
            radius: 3
            color: modelData
            border.width: 1
            border.color: Qt.darker(root.foreground, 1.6)

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                root.setNewValue(modelData)
                popup.close()
              }
            }
          }
        }
      }
    }
  }
}