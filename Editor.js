// Editor.js — pure data/logic helpers for the Bar Editor plugin.
// `.pragma library` so the module is loaded once and shared.
.pragma library

var POSITIONS = ["top", "bottom", "left", "right"]
var SECTIONS = ["left", "center", "right"]
var SECTION_LABELS = { left: "Left", center: "Center", right: "Right" }

var CAT_COLORS = {
  "Audio": "#f7768e", "Media": "#9ece6a", "Network": "#7dcfff",
  "System": "#7aa2f7", "Hardware": "#ff9e64", "Utilities": "#bb9af7",
  "Desktop": "#c0caf5", "Compositor": "#e0af68", "Info": "#73daca",
  "AI": "#ff007c", "Status": "#9ece6a", "Appearance": "#e0af68",
  "Files": "#73daca", "Layout": "#bb9af7", "Time": "#7dcfff",
}

function clone(obj) {
  return JSON.parse(JSON.stringify(obj))
}

function ensureCfg(cfg) {
  if (!cfg || typeof cfg !== "object") cfg = {}
  if (!cfg.bar) cfg.bar = {}
  if (!cfg.bar.layout) cfg.bar.layout = {}
  for (var i = 0; i < SECTIONS.length; i++) {
    if (!Array.isArray(cfg.bar.layout[SECTIONS[i]])) cfg.bar.layout[SECTIONS[i]] = []
  }
  cfg.version = 1
  return cfg
}

function widgetInfo(catalog, id) {
  return catalog && catalog[id] ? catalog[id] : {}
}

function displayName(catalog, id) {
  var info = widgetInfo(catalog, id)
  var bw = info.barWidget || {}
  return bw.displayName || info.name || id
}

function categoryOf(catalog, id) {
  var bw = widgetInfo(catalog, id).barWidget || {}
  return bw.category || "Utilities"
}

function catColor(catalog, id) {
  var color = CAT_COLORS[categoryOf(catalog, id)]
  return color || "#7aa2f7"
}

function allowMultiple(catalog, id) {
  var bw = widgetInfo(catalog, id).barWidget || {}
  var value = bw.allowMultiple
  return value === undefined ? true : !!value
}

function isBarWidget(info) {
  if (!info || !info.id) return false
  var kinds = info.kinds || []
  return kinds.indexOf("bar-widget") !== -1
}

// Searchable host list: the default bar plus any catalog bar hosts.
function hosts(catalog) {
  var result = ["omarchy.bar"]
  var ids = []
  for (var id in catalog) {
    var info = catalog[id]
    if (id === "omarchy.bar") continue
    if (!Array.isArray(info.kinds) || info.kinds.indexOf("bar") === -1) continue
    if (!info.barPath) continue
    ids.push(id)
  }
  ids.sort()
  return result.concat(ids)
}

// Match a widget row against a free-text filter (id + name + description).
function rowMatches(catalog, id, filter) {
  var term = String(filter || "").toLowerCase().trim()
  if (!term) return true
  var info = widgetInfo(catalog, id)
  var bw = info.barWidget || {}
  var hay = (id + " " + (bw.displayName || info.name || "") + " " + (bw.description || info.description || "")).toLowerCase()
  return hay.indexOf(term) !== -1
}

// How many of the catalog's bar widgets are already in the layout.
function placedCount(cfg) {
  var seen = {}
  for (var i = 0; i < SECTIONS.length; i++) {
    var list = cfg.bar.layout[SECTIONS[i]]
    for (var j = 0; j < list.length; j++) {
      var id = list[j] && list[j].id
      if (id) seen[id] = (seen[id] || 0) + 1
    }
  }
  return seen
}