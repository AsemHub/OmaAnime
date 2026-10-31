// Pure row and grid logic for the Browse overlay. Qt-free on purpose: `import
// "BrowseModel.js" as Model` from Browse.qml, and node's test runner runs the same source
// against fixtures taken from live CLI output.
//
// Field names below are the ones the CLI actually emits; every one was read off
// `omarchy-anime watching|top|airing|search|media` on 2026-09-15, not assumed:
//   media:  id, title{romaji,english,native}, coverImage{large,medium} (URLs, never read here),
//           coverPath{large,medium} (local files from `covers warm`, "" until warmed),
//           episodes (may be null),
//           status (FINISHED|RELEASING|NOT_YET_RELEASED|...), averageScore (may be null),
//           seasonYear, nextAiringEpisode{episode,airingAt,timeUntilAiring} (may be null)
//   watching entry: {listId, progress, updatedAt, media, next{episode,aired,airsAt,rewatch}}
//   media <id> adds: mediaListEntry{id,status,progress} (null when the show is not on the list)
//   top adds: rank        airing: {top{id,rank,title,coverImage,averageScore,status}, airing, chain[]}
//
// `top`, `airing` and `search` carry NO mediaListEntry unless the CLI query asks for it; every
// reader here treats an absent one as "not on your list" rather than failing.

var STATUS_WORDS = {
  FINISHED: "Finished",
  RELEASING: "Airing",
  NOT_YET_RELEASED: "Upcoming",
  CANCELLED: "Cancelled",
  HIATUS: "On hiatus"
}

var LIST_STATE_WORDS = {
  CURRENT: "Watching",
  PLANNING: "Planned",
  COMPLETED: "Completed",
  DROPPED: "Dropped",
  PAUSED: "Paused",
  REPEATING: "Rewatching"
}

function titleOf(media) {
  if (!media || !media.title) return ""
  var t = media.title
  return String(t.english || t.romaji || t.native || "")
}

// The overlay must never hand Qt an http(s) URL (the shell once crashed in Qt's TLS init, inside
// QQuickPixmapReader, on the process's first https request — a cover). `coverPath` is a local file
// written by `omarchy-anime covers warm`; "" means "not downloaded yet", which draws the row's
// placeholder. The `coverImage` URLs are still in the payload and are deliberately not read here.
function coverOf(media) {
  if (!media || !media.coverPath) return ""
  return String(media.coverPath.medium || media.coverPath.large || "")
}

function statusWord(media) {
  var s = media && media.status ? String(media.status) : ""
  if (STATUS_WORDS[s]) return STATUS_WORDS[s]
  if (!s) return ""
  return s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ")
}

function scoreText(media) {
  var n = media ? Number(media.averageScore) : NaN
  return isFinite(n) && n > 0 ? n + "%" : ""
}

function listStateWord(media) {
  var e = media && media.mediaListEntry ? media.mediaListEntry : null
  if (!e || !e.status) return ""
  var s = String(e.status)
  return LIST_STATE_WORDS[s] || s
}

// AniList timestamps are epoch seconds; `nowMs` is Date.now() so the caller can freeze the clock.
function airsIn(airingAt, nowMs) {
  var ts = Number(airingAt)
  if (!isFinite(ts) || ts <= 0) return ""
  var now = isFinite(Number(nowMs)) ? Number(nowMs) : Date.now()
  var left = ts - now / 1000
  if (left <= 0) return "soon"
  var d = Math.floor(left / 86400)
  var h = Math.floor((left % 86400) / 3600)
  var m = Math.floor((left % 3600) / 60)
  if (d > 0) return d + "d " + h + "h"
  if (h > 0) return h + "h " + m + "m"
  return Math.max(1, m) + "m"
}

// --------------------------------------------------------------- Continue Watching

// One row of `service.watching`. `episode` is what Enter plays: the next episode when it has
// aired, otherwise the last watched one (a rewatch), matching Service.playNext().
function rowText(entry, nowMs) {
  var media = entry && entry.media ? entry.media : null
  var next = entry && entry.next ? entry.next : {}
  var progress = Number(entry && entry.progress) || 0
  var total = media && media.episodes ? media.episodes : null
  var row = {
    mediaId: media && media.id ? media.id : null,
    title: titleOf(media),
    cover: coverOf(media),
    detail: "",
    episode: null,
    rewatch: false,
    media: media
  }
  // Read off the media, not `next`: a list saved before `next` knew the rule still says "aired".
  if (notOut(media)) {
    row.detail = notOutText(media)
    return row
  }
  if (next.episode && next.aired) {
    row.detail = "Ep " + next.episode + " of " + (total ? total : "?")
    row.episode = next.episode
    return row
  }
  if (next.episode && !next.aired) {
    var watched = next.rewatch || progress
    row.detail = "Ep " + watched + " watched"
    var eta = airsIn(next.airsAt, nowMs)
    if (eta) row.detail += " · airs in " + eta
    row.episode = watched || null
    row.rewatch = true
    return row
  }
  if (next.rewatch) {
    row.detail = "All " + next.rewatch + " watched · rewatch"
    row.episode = next.rewatch
    row.rewatch = true
    return row
  }
  if (media) row.detail = "Ep " + (progress + 1) + (total ? " of " + total : "")
  row.episode = media ? progress + 1 : null
  return row
}

// --------------------------------------------------------------- Top 100 / Search

function mediaRow(media, rank) {
  return {
    id: media && media.id ? media.id : null,
    mediaId: media && media.id ? media.id : null,
    rankText: rank ? "#" + rank : "",
    title: titleOf(media),
    cover: coverOf(media),
    scoreText: scoreText(media),
    statusWord: statusWord(media),
    listState: listStateWord(media),
    media: media
  }
}

// Completed entries hide unless the user asks for them.
function topRows(list, showCompleted) {
  if (!isList(list)) return []
  var out = []
  for (var i = 0; i < list.length; i++) {
    var media = list[i]
    if (!media) continue
    var state = media.mediaListEntry ? String(media.mediaListEntry.status || "") : ""
    if (state === "COMPLETED" && !showCompleted) continue
    out.push(mediaRow(media, media.rank || i + 1))
  }
  return out
}

function searchRows(list) {
  if (!isList(list)) return []
  var out = []
  for (var i = 0; i < list.length; i++) {
    var media = list[i]
    if (!media) continue
    var row = mediaRow(media, 0)
    var parts = []
    var word = statusWord(media)
    if (word) parts.push(word)
    if (media.seasonYear) parts.push(String(media.seasonYear))
    if (media.episodes) parts.push(media.episodes + " episode" + (media.episodes === 1 ? "" : "s"))
    row.detail = parts.join(" · ")
    out.push(row)
  }
  return out
}

// --------------------------------------------------------------- Airing from Top

// One `omarchy-anime airing` item. The row is the airing season; the caption credits the
// top-100 entry it was reached from, which is a different show whenever `chain` is non-empty.
function airingRow(item, nowMs) {
  var airing = item && item.airing ? item.airing : null
  var top = item && item.top ? item.top : null
  var next = airing && airing.nextAiringEpisode ? airing.nextAiringEpisode : null
  var caption = ""
  if (top && top.rank) caption = "#" + top.rank + " in the top 100"
  var topTitle = titleOf(top)
  if (caption && topTitle && topTitle !== titleOf(airing)) caption += " · " + topTitle
  var detail = statusWord(airing) || ""
  if (next && next.episode) {
    var eta = airsIn(next.airingAt, nowMs)
    // A saved schedule outlives its time (a day-old Airing list, or the copy an AniList outage answered with):
    // once the time has passed (`airsIn`'s "soon"), that episode has aired, and what came after it is not in the copy.
    // No countdown.
    detail = "Ep " + next.episode + (eta === "soon" ? " aired" : eta ? " airs in " + eta : " next")
  }
  return {
    mediaId: airing && airing.id ? airing.id : null,
    title: titleOf(airing),
    cover: coverOf(airing) || coverOf(top),
    detail: detail,
    caption: caption,
    scoreText: scoreText(airing) || scoreText(top),
    media: airing
  }
}

// --------------------------------------------------------------- detail view

// AniList lists the entry as not yet released and gives no airing schedule: no episode of it is out. A scheduled
// NOT_YET_RELEASED entry is not this (the episodes before the scheduled one aired), nor is any other status. The
// Python side is `anilist.not_out`, which the play's own refusal reads.
function notOut(media) {
  if (!media || String(media.status || "") !== "NOT_YET_RELEASED") return false
  return scheduledEpisode(media) === 0
}

// The episode AniList's airing schedule names next (`nextAiringEpisode.episode`, the first one not yet aired): a
// whole number above 0, or 0 for no schedule (a string, a bool or a fraction is none). `anilist.scheduled_episode` is
// the same test (the shared cells cases hold them equal).
function scheduledEpisode(media) {
  var next = media ? media.nextAiringEpisode : null
  var ep = next ? next.episode : null
  return typeof ep === "number" && isFinite(ep) && Math.floor(ep) === ep && ep > 0 ? ep : 0
}

// The viewer's one date style: `ServiceLogic.savedWhen`'s ("Oct 20, 2025").
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

// AniList's `startDate` (year, month, day, each optional) as the viewer reads it: "Mar 14, 2027", "Mar 2027",
// "2027", or "" without a year. `say.start_date` is the same table (a shared cases file holds them equal).
function startDateText(date) {
  if (!date || typeof date !== "object") return ""
  function part(key, low, high) {
    var v = date[key]
    return typeof v === "number" && Math.floor(v) === v && v >= low && v <= high ? v : null
  }
  var year = part("year", 1, 9999), month = part("month", 1, 12), day = part("day", 1, 31)
  if (year === null) return ""
  if (month === null) return String(year)
  if (day === null) return MONTHS[month - 1] + " " + year
  return MONTHS[month - 1] + " " + day + ", " + year
}

// What a row or the detail says for an entry that is not out, or "" for one that is.
function notOutText(media) {
  if (!notOut(media)) return ""
  var when = startDateText(media.startDate)
  return "Not out yet" + (when ? " · expected " + when : "")
}

// Highest episode number that is out. `nextAiringEpisode.episode` is the one still to come, so
// the last aired is one below it; without a schedule a finished show's `episodes` is the answer,
// and an entry that is not out has none (0).
function lastAiredEpisode(media) {
  if (!media) return null
  var scheduled = scheduledEpisode(media)
  if (scheduled > 0) return scheduled - 1
  if (notOut(media)) return 0
  return media.episodes ? Number(media.episodes) : null
}

function progressOf(media) {
  var e = media && media.mediaListEntry ? media.mediaListEntry : null
  return Math.max(0, Number(e && e.progress) || 0)
}

// The most cells a grid builds. The count is AniList's number, and 2^31 - 1 cells froze the shell; the longest
// recorded show had 1,181 episodes (its next airing one), and building 10,000 cells took node 0-6 ms.
// `player.EPISODE_CELLS_MAX` is the same number.
var EPISODE_CELLS_MAX = 10000

// The episodes the cells cover: { first, last, count }. Above the bound, the window of EPISODE_CELLS_MAX around
// the next episode (progress + 1), kept inside 1..count.
function episodeWindow(media) {
  if (!media) return { first: 1, last: 0, count: 0 }
  var progress = progressOf(media)
  var lastAired = lastAiredEpisode(media)
  // Not out with no count: no guess at one (24 unaired cells said nothing); the watched ones only.
  var count = Number(media.episodes) || lastAired || (notOut(media) ? progress : progress + 24)
  if (!(count > 0)) return { first: 1, last: 0, count: 0 }
  if (!isFinite(count)) count = Number.MAX_SAFE_INTEGER
  count = Math.floor(count)
  if (count <= EPISODE_CELLS_MAX) return { first: 1, last: count, count: count }
  var first = Math.max(1, Math.min(progress + 1 - Math.floor(EPISODE_CELLS_MAX / 2), count - EPISODE_CELLS_MAX + 1))
  return { first: first, last: first + EPISODE_CELLS_MAX - 1, count: count }
}

// "" when every episode has a cell; else the line saying which are shown and how many are not.
function episodeCellsNote(media) {
  var w = episodeWindow(media)
  if (w.count <= EPISODE_CELLS_MAX) return ""
  return "Episodes " + w.first + "–" + w.last + " of " + w.count + " shown · " + (w.count - (w.last - w.first + 1))
         + " more not listed"
}

// One cell per episode: watched (≤ progress), next (progress + 1, if it is out), aired
// (playable), unaired (past the last aired episode, not playable). At most EPISODE_CELLS_MAX (`episodeWindow`).
function episodeCells(media) {
  if (!media) return []
  var progress = progressOf(media)
  var lastAired = lastAiredEpisode(media)
  var w = episodeWindow(media)
  if (w.last < w.first) return []
  var cells = []
  for (var ep = w.first; ep <= w.last; ep++) {
    var state
    if (ep <= progress) state = "watched"
    else if (lastAired !== null && ep > lastAired) state = "unaired"
    else if (ep === progress + 1) state = "next"
    else state = "aired"
    cells.push({ episode: ep, state: state })
  }
  return cells
}

// Where the grid cursor starts: the next episode, else the last playable cell.
function initialCellIndex(cells) {
  if (!isList(cells) || cells.length === 0) return 0
  for (var i = 0; i < cells.length; i++)
    if (cells[i].state === "next") return i
  for (var j = cells.length - 1; j >= 0; j--)
    if (cells[j].state !== "unaired") return j
  return 0
}

function detailHeader(media) {
  var lastAired = lastAiredEpisode(media)
  var episodesText = ""
  if (media && media.episodes) episodesText = media.episodes + " episode" + (Number(media.episodes) === 1 ? "" : "s")
  else if (lastAired) episodesText = lastAired + " aired"
  var entry = media && media.mediaListEntry ? media.mediaListEntry : null
  var listState = "Not on your list"
  if (entry) {
    listState = listStateWord(media)
    var p = progressOf(media)
    if (p > 0) listState += " · " + p + " watched"
  }
  return {
    mediaId: media && media.id ? media.id : null,
    title: titleOf(media),
    cover: coverOf(media),
    scoreText: scoreText(media),
    statusWord: statusWord(media),
    episodesText: episodesText,
    // "Not out yet", with AniList's date when it has one; "" for an entry that is out.
    notOutText: notOutText(media),
    // "" unless the episodes are more than the grid builds (`episodeCellsNote`).
    cellsNote: episodeCellsNote(media),
    listState: listState,
    progress: progressOf(media),
    media: media
  }
}


// ----------------------------------------------------------------------------------- Lists tab
//
// The other five AniList lists. The entry shape is `watching`'s plus `score` and the entry's own
// `status`, so the Continue Watching row renderer is reused and only the right-hand caption and the
// Enter action change.
//
// `REPEATING` is labelled "Rewatching" because that is what anilist.co calls it; the API value
// stays REPEATING everywhere else.

var LIST_STATUSES = ["PLANNING", "PAUSED", "COMPLETED", "DROPPED", "REPEATING"]

var LIST_STATUS_LABELS = {
  PLANNING: "Planning",
  PAUSED: "Paused",
  COMPLETED: "Completed",
  DROPPED: "Dropped",
  REPEATING: "Rewatching",
  CURRENT: "Watching"
}

function listStatusLabel(status) {
  var s = String(status || "")
  return LIST_STATUS_LABELS[s] || s
}

// What an empty Continue Watching says once its list has loaded: ServiceLogic.js `WATCHING_EMPTY`, which the popout
// uses (Browse reaches ServiceLogic.js only through the bridge; a test holds them
// equal).
var WATCHING_EMPTY = "Nothing on your AniList Watching list"

function listEmptyText(status) {
  return "Nothing on your " + listStatusLabel(status) + " list"
}

// One row. `action` is what Enter does:
//   "start" — the show is not on CURRENT yet: write progress 0 with status CURRENT, then play ep 1
//   "play"  — resume at progress + 1
//   "grid"  — open the episode grid instead of playing, because Enter on a 1100-episode completed
//             show must not silently start episode 1
function listRow(entry, status) {
  var media = entry && entry.media ? entry.media : null
  var state = String((entry && entry.status) || status || "")
  var progress = Number(entry && entry.progress) || 0
  var total = media && media.episodes ? media.episodes : null
  var row = {
    mediaId: media && media.id ? media.id : null,
    title: titleOf(media),
    cover: coverOf(media),
    statusWord: statusWord(media),
    scoreText: scoreText(media),
    listState: listStatusLabel(state),
    detail: "",
    episode: null,
    action: "grid",
    media: media
  }
  // Whatever its list: nothing to start or resume. "start" would write progress 0 to AniList before its play.
  if (notOut(media)) {
    row.detail = notOutText(media)
    return row
  }
  if (state === "PLANNING") {
    row.detail = "Not started · " + (total ? total : "?") + " episodes"
    row.episode = 1
    row.action = "start"
    return row
  }
  if (state === "PAUSED") {
    row.detail = "Ep " + progress + " of " + (total ? total : "?") + " · paused"
    row.episode = progress + 1
    row.action = "play"
    return row
  }
  if (state === "REPEATING") {
    row.detail = "Ep " + progress + " of " + (total ? total : "?") + " · rewatching"
    row.episode = progress + 1
    row.action = "play"
    return row
  }
  if (state === "COMPLETED") {
    row.detail = "Watched all " + (total ? total : progress)
    row.episode = null
    row.action = "grid"
    return row
  }
  if (state === "DROPPED") {
    row.detail = "Stopped at Ep " + progress
    row.episode = null
    row.action = "grid"
    return row
  }
  // CURRENT, or anything AniList adds later: the Continue Watching wording, which already handles
  // an unaired next episode and the rewatch case.
  var base = rowText(entry, Date.now())
  row.detail = base.detail
  row.episode = base.episode
  row.action = "play"
  return row
}

function listRows(list, status) {
  if (!isList(list)) return []
  var out = []
  for (var i = 0; i < list.length; i++) {
    if (!list[i]) continue
    out.push(listRow(list[i], status))
  }
  return out
}


// --------------------------------------------------------------------------- settings sheet
//
// The pure halves of the Advanced disclosure and of OrderedList.qml, here rather than in QML so
// node's test runner can reach them (same arrangement as the row logic above).

// Schema rows are split by the manifest's own `advanced` flag: the sheet's first screen stays the
// thirteen keys a user turns while watching, and the other ten live behind a disclosure. Twenty-two
// controls in one Flickable is a wall, and an earlier layout already found that thirteen did not fit a screen.
// A language setting's choices as the Dropdown's `{value, label}` rows, labelled from the CLI's
// `languageNames` (one table, `core.LANGUAGE_NAMES`). Without names (a Service not restarted yet) the codes
// stay as they are.
function languageOptions(options, names) {
  if (!isList(options)) return []
  var codes = listCopy(options)
  if (!names || typeof names !== "object") return codes
  return codes.map(function (code) {
    var c = String(code)
    if (c === "off") return { value: c, label: "Off" }
    return names[c] ? { value: c, label: String(names[c]) + " (" + c + ")" } : c
  })
}

// A settings menu that comes out empty although its row lists options: the language menus once did on a new Qt, and
// nothing in the log said so. Null when the menu has choices, the row has none, or `warned` (the keys reported since
// the sheet opened) holds its key; else what the warning names: the key, how many options the row has, and the type
// tags of the row's options and of the choices made from them.
function emptyMenuReport(row, choices, warned) {
  if (!row || typeof row !== "object" || !row.options || !(Number(row.options.length) > 0)) return null
  if (isList(choices) && choices.length > 0) return null
  var key = String(row.key || "")
  if (warned && warned[key]) return null
  return { key: key, options: Number(row.options.length),
           type: Object.prototype.toString.call(row.options), choices: Object.prototype.toString.call(choices) }
}

function sheetRows(schema) {
  if (!isList(schema)) return []
  return schema.filter(function (r) { return r && r.advanced !== true })
}

function advancedRows(schema) {
  if (!isList(schema)) return []
  return schema.filter(function (r) { return r && r.advanced === true })
}

// Whether `value` is a list. On Qt 6.12 an array that crossed a QML boundary reaches JS as a sequence
// (`[object V4Sequence]`): a Repeater's `modelData` and every array inside it, a `signal(var)` argument, a `list<>`
// property, and any `property var` such a value was stored in. It has `length`, indexes and Array's methods, and
// `instanceof Array` is true for it, but `Array.isArray` is false, which emptied the Settings language menus. A
// string, or an object that merely has a `length` key, is not a list.
// BrowseModel.js and ServiceLogic.js each carry this pair, word for word (a node test holds them equal).
// An AniList media id as one `covers warm --media` word: a whole number in 1..2^31-1 (GraphQL `Int`, the CLI's
// `valid_media_id`), from a number or a digit string; else "". The word is built from the number, so an answer's
// id can never be read as an option (`--url`) or carry anything else into the argv.
var MEDIA_ID_MAX = 2147483647
function mediaIdArg(value) {
  if (typeof value === "string" && !/^[0-9]{1,10}$/.test(value)) return ""
  if (typeof value !== "number" && typeof value !== "string") return ""
  var n = Number(value)
  return Math.floor(n) === n && n > 0 && n <= MEDIA_ID_MAX ? String(n) : ""
}

function isList(value) {
  return Array.isArray(value) || value instanceof Array
}

// A real Array of a list's items, or [] for anything that is not a list: a defensive copy, so code that keeps or
// changes the result never holds the engine's sequence.
function listCopy(value) {
  return isList(value) ? Array.prototype.slice.call(value) : []
}

// The JS twin of `core.as_list`. A list value reaches the sheet as a comma-joined **string**,
// because a JSON array does not survive Omarchy's shell.json write path (measured 2026-09-16: a
// top-level array lands as its comma join, while objects and numbers survive). A hand-edited
// config.json may still hold a real array, so both shapes are accepted.
function asList(value) {
  var parts
  if (isList(value)) parts = listCopy(value)
  else if (typeof value === "string") {
    var text = value.replace(/^\s+|\s+$/g, "")
    if (text === "") return []
    if (text.charAt(0) === "[") {
      try {
        var parsed = JSON.parse(text)
        if (isList(parsed)) parts = parsed
      } catch (e) { /* fall through to the comma split */ }
    }
    if (!parts) parts = text.split(",")
  } else return []
  var out = []
  for (var i = 0; i < parts.length; i++) {
    var v = String(parts[i]).replace(/^\s+|\s+$/g, "")
    if (v !== "") out.push(v)
  }
  return out
}

// What the sheet writes back. The comma join is the storage format, not a display choice.
function joinList(values) { return asList(values).join(",") }

// Move one entry of an ordered list. Out-of-range moves are a no-op rather than an error: the
// ▲ on the first row and the ▼ on the last are disabled, and this is the second guard.
function reorder(values, index, direction) {
  var out = asList(values)
  var to = index + direction
  if (index < 0 || index >= out.length || to < 0 || to >= out.length) return out
  var moved = out[index]
  out[index] = out[to]
  out[to] = moved
  return out
}

// Toggle one option of a fixed set on or off, keeping the order of what is already enabled and
// appending a newly enabled one at the end. `all` is the option list in its canonical order, used
// only to reject an option that is not one of them.
function toggleIn(values, option, all) {
  var out = asList(values)
  var name = String(option)
  if (isList(all) && listCopy(all).indexOf(name) === -1) return out
  var at = out.indexOf(name)
  if (at === -1) out.push(name)
  else out.splice(at, 1)
  return out
}

// Any free-text list entry the stored value can hold: not empty, no comma (the storage separator), no
// control character, at most 300 characters. The fallback for a list key without a shape of its own.
function isListEntry(value) {
  var s = String(value === undefined || value === null ? "" : value).replace(/^\s+|\s+$/g, "")
  return s !== "" && s.length <= 300 && s.indexOf(",") === -1 && !/[\x00-\x1f]/.test(s)
}

// An `indexers` entry: an add-on's file name without `.py`. The loader skips files
// starting with `_` or `.`, so those are refused here too; a comma is the storage separator.
function isAddonName(value) {
  var s = String(value === undefined || value === null ? "" : value).replace(/^\s+|\s+$/g, "")
  return /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(s)
}

// A `preferredGroups` entry: a fansub group's name, optionally `=points` from -20 to 20
// (`core.GROUP_POINTS_BOUNDS`; a bare name is worth `core.PREFERRED_GROUP_POINTS`). The same shape
// `core.parse_group_entry` accepts; points outside the bounds are refused here rather than clamped,
// so what the sheet stores is what ranks. A comma is the storage separator.
var GROUP_POINTS_MIN = -20
var GROUP_POINTS_MAX = 20

function isGroupEntry(value) {
  var s = String(value === undefined || value === null ? "" : value).replace(/^\s+|\s+$/g, "")
  var m = s.match(/^([^,=\x00-\x1f]{1,64}?)\s*(?:=\s*(-?\d{1,4}))?$/)
  if (!m || m[1].replace(/\s+/g, "") === "") return false
  if (m[2] === undefined) return true
  var points = parseInt(m[2], 10)
  return points >= GROUP_POINTS_MIN && points <= GROUP_POINTS_MAX
}

// An `englishOnlyGroups` entry: a name only.
function isGroupName(value) {
  var s = String(value === undefined || value === null ? "" : value)
  return s.indexOf("=") === -1 && isGroupEntry(s)
}

// What a free-text list row shows and checks, per key. A list key with no shape here gets a plain one.
function listRowShape(key) {
  if (key === "indexers")
    return { placeholder: "add-on file name, without .py", addLabel: "Add indexer", validator: isAddonName }
  if (key === "preferredGroups")
    return { placeholder: "group name, or name=points", addLabel: "Add group", validator: isGroupEntry }
  if (key === "englishOnlyGroups")
    return { placeholder: "group name", addLabel: "Add group", validator: isGroupName }
  return { placeholder: "value", addLabel: "Add", validator: isListEntry }
}

// The keys whose control unit is not their stored unit (today only the subtitle cache): 16777216 is
// not a number anybody wants to type. Stored in bytes (that is what the CLI reads), shown and
// stepped in the unit below, schema min/max converted the same way rather than duplicated.
var BYTE_UNITS = {
  subsCacheMaxBytes: { label: "MiB", factor: 1048576 }
}

function byteUnit(key) {
  var u = BYTE_UNITS[String(key)]
  return u ? u : null
}

function bytesToUnit(value, factor) {
  var f = Number(factor) > 0 ? Number(factor) : 1048576
  var n = Number(value)
  if (!isFinite(n) || n <= 0) return 0
  return Math.max(1, Math.round(n / f))
}

function unitToBytes(value, factor) {
  var f = Number(factor) > 0 ? Number(factor) : 1048576
  var n = Number(value)
  if (!isFinite(n) || n <= 0) return f
  return Math.round(n) * f
}

function bytesToMib(value) { return bytesToUnit(value, 1048576) }

function mibToBytes(value) { return unitToBytes(value, 1048576) }


// ------------------------------------------------------------------------- episode cards
// The picture rule. Blur is the user's choice for every unwatched episode: TVDB stills
// are uploaded frames, not curated thumbnails. A watched episode shows the crisp still. An episode
// with no still shows the show's blurred fanart, so a 1005-card show is never 983 grey boxes.
function cardPicture(cell, show) {
  var still = cell && cell.still ? String(cell.still) : ""
  var blur = cell && cell.blur ? String(cell.blur) : ""
  var watched = !!cell && cell.state === "watched"
  if (watched && still) return { source: still, kind: "still" }
  if (blur) return { source: blur, kind: "blur" }
  var fan = show && show.fanartBlur ? String(show.fanartBlur) : ""
  if (fan) return { source: fan, kind: "fanart" }
  return { source: "", kind: "none" }
}

// The resume bar: only an unwatched episode with a fraction strictly inside (0, 1). The CLI already
// guards `dur > 0`; this guards the shape once more because the payload is a file.
function resumeFraction(cell) {
  if (!cell || cell.state === "watched") return 0
  var r = Number(cell.resume)
  return isFinite(r) && r > 0 && r < 1 ? r : 0
}

function gridColumns(width, minCard) {
  if (!(width > 0) || !(minCard > 0)) return 2
  return Math.max(2, Math.min(6, Math.floor(width / minCard)))
}

// ----------------------------------------------------------------------------- release picker
// The `releases` JSON becomes one flat list of items for a ListView: the releases that passed, in
// the ranker's order and under no header (the first one is what auto-play would choose), then the
// rejected ones grouped under one header per reason. `kind` is "row" or "header"; the cursor only
// ever lands on rows (`pickerStep`).

// Order of the rejected groups: the ones that say "wrong show" first, because under a franchise
// refusal those are the answer; the ones that say "right show, can't stream it" after.
var PICKER_REASONS = ["other_show", "series", "season", "seeders", "pack_range", "pack_files", "size", "batch"]

function sizeText(bytes) {
  var n = Number(bytes)
  if (!isFinite(n) || n <= 0) return ""
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GiB"
  return Math.round(n / 1048576) + " MiB"
}

function _uniqueLangs(langs) {
  var out = []
  for (var i = 0; i < (langs || []).length; i++) {
    var l = String(langs[i])
    if (l && out.indexOf(l) < 0) out.push(l)
  }
  return out
}

// A multi-sub release carries 30 languages; listed in full they fill the row and push the note (the
// other show it belongs to) off the edge. Name the viewer's language when it is there, count the rest.
// `want` is `{code, aliases}`; the aliases are the CLI's (`subtitle_aliases`, from the language table), so
// `ms` is Malay and `zh` Chinese. A payload without them falls back to the code and its first two letters.
// A region or script is its language (`ar-SA`, mpv's tag in `tracks.json`, is Arabic): the whole tag, then
// its first subtag, as `core.fold_language` reads it.
function _subsText(langs, want) {
  if (langs.length <= 3) return "subs " + langs.join(" ") + " ✓"
  var w = String(want.code || "")
  var aliases = want.aliases
  var hit = null
  for (var i = 0; i < langs.length; i++) {
    var l = langs[i]
    var tag = String(l).toLowerCase().replace(/_/g, "-")
    var first = tag.split("-")[0]
    var named = aliases ? (aliases.indexOf(tag) >= 0 || aliases.indexOf(first) >= 0)
                        : (first === w || (w.length === 3 && first === w.substring(0, 2))
                           || (w === "ara" && first === "arb"))
    if (named) { hit = l; break }
  }
  return hit ? "subs " + hit + " +" + (langs.length - 1) + " ✓" : "subs " + langs.length + ", no " + (w || "preferred") + " ✓"
}

function _pickerLine(r, want) {
  var parts = [r.group ? String(r.group) : "no group"]
  if (r.resolution) parts.push(r.resolution + "p")
  var size = sizeText(r.total_size)
  if (size) parts.push(size)
  var s = Number(r.seeders) || 0
  parts.push(s + (s === 1 ? " seeder" : " seeders"))
  var langs = _uniqueLangs(r.verified_langs)
  if (langs.length > 0) parts.push(_subsText(langs, want))
  if (r.rd_cached === true) parts.push("cached")
  if (r.pack) parts.push(r.pack_range ? "pack " + r.pack_range[0] + "–" + r.pack_range[1] : "pack")
  return parts.join(" · ")
}

function _pickerRow(r, failed, pin, want) {
  var f = failed && failed[r.info_hash]
  var note = ""
  if (f) note = "failed: " + String(f.error || f.stage || "")
  else if (r.reason === "other_show") note = String(r.show || r.why || "")
  else if (r.show && r.why) note = r.show + " · " + r.why
  else note = String(r.show || r.why || "")
  return {
    kind: "row", hash: String(r.info_hash || ""), line: _pickerLine(r, want), title: String(r.title || ""),
    note: note, dim: !!r.reason, failed: !!f,
    pinned: !!(pin && pin.pack && pin.pack.info_hash === r.info_hash),
    // Why it was rejected (`searches.row`: a code, the viewer's words, the other show its title names): the ask.
    reason: r.reason ? String(r.reason) : "", why: String(r.why || ""), show: String(r.show || "")
  }
}

function _capital(text) {
  var t = String(text || "")
  return t.length ? t.charAt(0).toUpperCase() + t.substring(1) : t
}

function pickerItems(data, pin) {
  if (!data || typeof data !== "object") return []
  var failed = data.failed || {}
  var want = { code: data.subtitle_language || "",
               aliases: isList(data.subtitle_aliases)
                 ? data.subtitle_aliases.map(function (a) { return String(a).toLowerCase() }) : null }
  var items = []
  var passed = isList(data.releases) ? data.releases : []
  for (var i = 0; i < passed.length; i++) items.push(_pickerRow(passed[i], failed, pin, want))
  var rejected = isList(data.rejected) ? data.rejected : []
  var groups = {}
  var order = PICKER_REASONS.slice()
  for (var j = 0; j < rejected.length; j++) {
    var reason = String(rejected[j].reason || "other")
    if (!groups[reason]) groups[reason] = []
    groups[reason].push(rejected[j])
    if (order.indexOf(reason) < 0) order.push(reason)
  }
  for (var k = 0; k < order.length; k++) {
    var rows = groups[order[k]]
    if (!rows || rows.length === 0) continue
    var label = order[k] === "other_show" ? "Other shows" : _capital(rows[0].why || order[k])
    items.push({ kind: "header", text: label + " · " + rows.length })
    for (var m = 0; m < rows.length; m++) items.push(_pickerRow(rows[m], failed, pin, want))
  }
  return items
}

// A rejected release plays at once only when it was rejected for its availability or a preference: nobody sharing it,
// its size, a whole season with packs turned off. It is this episode of this show. Every other reason says it is not
// (another show, another season, another episode, a pack that does not hold the episode or whose files do not), and so
// does a code this list does not know (an add-on's own): those ask first.
var PICKER_AT_ONCE = ["seeders", "size", "batch"]

// Whether a row rejected for `PICKER_AT_ONCE` counts as another show by its title (`show`, `score.sibling_name`). Alone
// that name is noisy: over the measurement corpus it is set on 276 of the 1,391 names the release filter keeps as the
// entry's own, about half of them format words ("pack", "vol", "raw"). It counts only when the ambiguity check found
// other shows in the same search (an `other_show` row), as under the "Idol" refusal, where the 6 GB Love Live! movies
// were rejected for their size.
function _othersFound(data) {
  var rejected = data && isList(data.rejected) ? data.rejected : []
  for (var i = 0; i < rejected.length; i++) if (rejected[i].reason === "other_show") return true
  return false
}

// A release's name as the ask says it: without its group tags in front ("[Grp]"), its tags behind ("[BD 1080p]",
// "(…)"), its extension, and what follows its resolution ("1080p BluRay x265 …"), then shortened in the middle to about
// `PICKER_ASK_TITLE` characters at word ends: "Starlight Chorus! … Lantern Night Festival". The whole name when that
// would leave nothing.
var PICKER_ASK_TITLE = 50

function pickerShortTitle(title) {
  var whole = String(title || "").replace(/\s+/g, " ").trim()
  var t = whole.replace(/\.(mkv|mp4|avi|webm|ts)$/i, "")
  t = t.replace(/^(?:\s*[\[(【][^\])】]*[\])】])+\s*/, "")
  var prev
  do {
    prev = t
    t = t.replace(/\s*[\[(【][^\[\]()【】]*[\])】]\s*$/, "")
  } while (t !== prev)
  t = t.replace(/\s+\(?\b(?:2160|1080|720|576|540|480)[pi]\b.*$/i, "").trim()
  if (!t) t = whole
  if (t.length <= PICKER_ASK_TITLE) return t
  var half = Math.floor((PICKER_ASK_TITLE - 3) / 2)
  var words = t.split(" ")
  var head = "", tail = ""
  for (var i = 0; i < words.length; i++) {
    var h = head ? head + " " + words[i] : words[i]
    if (h.length > half) break
    head = h
  }
  for (var j = words.length - 1; j >= 0; j--) {
    var w = tail ? words[j] + " " + tail : words[j]
    if (w.length > half) break
    tail = w
  }
  head = head.replace(/[\s\-–—:·,]+$/, "")
  tail = tail.replace(/^[\s\-–—:·,]+/, "")
  if (!head) head = t.substring(0, half)
  if (!tail) tail = t.substring(t.length - half)
  return head + " … " + tail
}

// The question a pick of this row asks before it plays, or "" when it plays at once (a passed row, or `PICKER_AT_ONCE`
// with no other show named). `data` is the `releases` answer (`show_title`), `episode` the picker's. It names the
// release (`pickerShortTitle`), not the other show its title was read as (`show`: normalized words, "Starlight Chorus
// Club Vostfr Grp Raws …"), which only decides whether a `PICKER_AT_ONCE` row asks.
function pickerAsk(item, data, episode) {
  if (!item || item.kind !== "row" || !item.dim) return ""
  var reason = String(item.reason || "")
  var show = String(item.show || "")
  var atOnce = PICKER_AT_ONCE.indexOf(reason) >= 0
  if (atOnce) {
    if (!show || !_othersFound(data)) return ""
  }
  var title = data && data.show_title ? String(data.show_title) : "this show"
  var ep = Number(episode) > 0 ? " episode " + Number(episode) : ""
  var why = String(item.why || "")
  var name = pickerShortTitle(item.title)
  var it = name ? "“" + name + "”" : ""
  if (reason === "other_show" || reason === "series" || atOnce) {
    var also = atOnce && why ? " It was also ruled out: " + why + "." : ""
    return (it ? "This is " + it + ", another show" : "This release names another show") + ", not " + title + "."
           + also + " Play it anyway?"
  }
  if (reason === "season")
    return (it ? "This is " + it + ", another season" : "This release is another season") + ", not " + title + ep
           + ". Play it anyway?"
  if (reason === "episode")
    return (it || "This release's title") + " does not name" + (ep || " this episode") + " of " + title
           + ". Play it anyway?"
  if (reason === "pack_range")
    return (it ? "The pack " + it : "This pack") + " does not hold" + (ep || " this episode") + ". Play it anyway?"
  if (reason === "pack_files")
    return (ep ? "Episode " + Number(episode) : "This episode") + " is not among the files of "
           + (it || "this pack") + ". Play it anyway?"
  return (it || "This release") + " was ruled out" + (why ? ": " + why : "") + ". Play it anyway?"
}

// The ask's keys, under its question and on the status line while it asks.
var PICKER_ASK_KEYS = "Enter or a click plays it · Escape cancels"

// What Enter (or a click) on row `index` does: `{play, reason, confirmed}` plays that release, `{ask: {hash, text,
// reason}}` asks first (`pickerAsk`), null does nothing (a header, no row). `confirmHash` is the row the viewer
// confirmed: the ask's own row, by its hash, never whatever the cursor is on now (a mouse drift moves the cursor).
function pickerDecide(items, index, data, episode, confirmHash) {
  var item = isList(items) ? items[index] : null
  if (!item || item.kind !== "row" || !item.hash) return null
  var text = pickerAsk(item, data, episode)
  if (!text) return { play: item.hash, reason: item.reason || null, confirmed: false }
  if (confirmHash && confirmHash === item.hash) return { play: item.hash, reason: item.reason || null, confirmed: true }
  return { ask: { hash: item.hash, text: text, reason: item.reason || null } }
}

// The index of the row with this hash, -1 when the list no longer has it.
function pickerIndexOf(items, hash) {
  if (!isList(items) || !hash) return -1
  for (var i = 0; i < items.length; i++) if (items[i].kind === "row" && items[i].hash === hash) return i
  return -1
}

// Before the list, when no release passed and some were rejected: that none of them is the show's own, plainly, in
// the words of what is there. A row rejected for `PICKER_AT_ONCE` may be the show's own (ruled out, not another show:
// `_othersFound` says when its title's other show counts), so with any such row the line says "passed", not "was
// found", and calls it ruled out. "" otherwise.
function pickerNoneOwn(data) {
  if (!data || typeof data !== "object") return ""
  var passed = isList(data.releases) ? data.releases.length : 0
  var rejected = isList(data.rejected) ? data.rejected : []
  if (passed > 0 || rejected.length === 0) return ""
  var title = data.show_title ? String(data.show_title) : "this show"
  var kinds = [], ruled = 0, whys = [], others = _othersFound(data)
  function kind(k) { if (kinds.indexOf(k) < 0) kinds.push(k) }
  for (var i = 0; i < rejected.length; i++) {
    var r = rejected[i], reason = String(r.reason || "")
    var atOnce = PICKER_AT_ONCE.indexOf(reason) >= 0
    if (reason === "other_show" || reason === "series" || (r.show && (others || !atOnce))) kind("other shows")
    else if (reason === "season") kind("other seasons")
    else if (reason === "episode" || reason === "pack_range" || reason === "pack_files") kind("other episodes")
    else if (atOnce) {
      ruled++
      if (r.why && whys.indexOf(String(r.why)) < 0) whys.push(String(r.why))
    } else kind("releases ruled out for another reason")
  }
  function joined(list) {
    return list.length <= 1 ? list.join("") : list.slice(0, -1).join(", ") + " and " + list[list.length - 1]
  }
  var because = whys.length ? " (" + whys.slice(0, 2).join(", ") + ")" : ""
  if (ruled === 0) return "No release of " + title + " was found. These are " + joined(kinds) + "."
  if (kinds.length === 0) return "No release of " + title + " passed: every one here was ruled out" + because + "."
  return "No release of " + title + " passed. " + ruled
         + (ruled === 1 ? " of these was ruled out" : " of these were ruled out") + because + "; the rest are "
         + joined(kinds) + "."
}

// `/` (own-words search) only when `releases` says a loaded indexer add-on offers text
// search. No list yet (loading, or it failed) means no `/` either.
function pickerOffersWords(data) {
  return !!data && data.text_search === true
}

function pickerKeys(data) {
  return "Enter plays this release · j/k moves"
         + (pickerOffersWords(data) ? " · / searches your words" : "") + " · r searches again"
         + (data && data.pinned ? " · x forgets the pin" : "") + " · Escape goes back"
}

function pickerSummary(data) {
  if (!data || typeof data !== "object") return ""
  var n = (data.releases || []).length
  var r = (data.rejected || []).length
  var counts = n + (n === 1 ? " release found" : " releases found") + " · " + r + " rejected"
  if (data.query) return "Your search “" + data.query + "” · " + counts + " · r goes back to the automatic search"
  if (data.refusal && data.refusal.error) return "Auto-play stopped: " + data.refusal.error
  return counts
}

// The sheet's line under the field, never blank: searching, the failure, the list, or (no list and
// nothing running, which a dropped request used to leave) what to press.
var PICKER_NO_LIST = "No list for this episode yet · r searches again"

function pickerLine(loading, error, data) {
  if (loading) return "Searching the indexes…"
  if (error) return "No list: " + error
  return pickerSummary(data) || PICKER_NO_LIST
}

// Under a paste field whose Save was refused while another sign-in was being checked.
var SIGN_IN_WAITING = "Another sign-in is still being checked. Your token is still in the field: "
                      + "press Save again once that one is done."

function pickerFirst(items) {
  for (var i = 0; i < (items || []).length; i++) if (items[i].kind === "row") return i
  return -1
}

// One step of `delta` rows (not items) from `index`, never past the first or last row.
function pickerStep(items, index, delta) {
  var i = index
  var left = Math.abs(delta)
  var dir = delta < 0 ? -1 : 1
  var last = index
  while (left > 0) {
    i += dir
    if (i < 0 || i >= items.length) break
    if (items[i].kind !== "row") continue
    last = i
    left--
  }
  return last
}

// ------------------------------------------------------------- the waiting play's choices
// A play waiting for TorBox to download the original-audio release (TorBox had only a dub cached), or one that
// stopped waiting and holds the choices, carries `choice` in its play-status entry: a sentence and three actions,
// each with its key and the CLI command it runs (`cli.wait_choice`). The player's script shows the same entry with
// the same keys; both run the same commands. A running entry nobody has written for this long is gone (the number is
// `playstate.ABANDON_AFTER_S`); a held one has no process behind it and stays until a new play claims.
var CHOICE_ABANDON_S = 20     // `playstate.ABANDON_AFTER_S` (also `ServiceLogic.PLAY_ABANDON_S`)
// A held play's choices belong to their moment: gone this long after `finishedAt` (`playstate.HELD_SHOW_S`).
var HELD_SHOW_S = 3600

// The choices to show, or null: `{held, mediaId, line, headline, buttons: [{id, key, label, args, runs, ack}]}`.
// `line` is the play's own sentence (the CLI renders it, with TorBox's numbers once it reports progress). Every
// choice runs a command for the whole wait: during it "Keep waiting" (`confirm`) tells the waiting play the
// viewer wants it, and once pressed the entry carries no sentence and no "Keep waiting" (`headline` ""). A button
// whose action has no command (an entry an older version wrote) runs nothing.
function playChoice(play, nowSec) {
  if (!play || typeof play !== "object" || !play.choice || !isList(play.choice.actions)) return null
  var held = play.state === "held"
  var now = Number(nowSec)
  if (!isFinite(now) || now <= 0) now = Date.now() / 1000
  if (held && now - (Number(play.finishedAt) || 0) > HELD_SHOW_S) return null
  if (!held) {
    if (play.state !== "running") return null
    if (now - (Number(play.updatedAt) || 0) > CHOICE_ABANDON_S) return null
  }
  var buttons = []
  for (var i = 0; i < play.choice.actions.length; i++) {
    var a = play.choice.actions[i] || {}
    var args = listCopy(a.args).map(String)
    var key = typeof a.key === "string" && /^[a-z]$/.test(a.key) && args.length > 0 ? a.key : ""
    buttons.push({ id: String(a.id || ""), key: key, args: args, runs: args.length > 0,
                   label: String(a.label || "") + (a.note ? " (" + String(a.note) + ")" : ""),
                   ack: typeof a.ack === "string" ? a.ack : "" })
  }
  var line = play.text ? String(play.text) : "Episode " + play.episode + " — " + String(play.message || "")
  return { held: held, mediaId: Number(play.mediaId) || 0, line: line, headline: String(play.choice.headline || ""),
           buttons: buttons }
}

// The button a key runs (the key's character, case folded), or null: only a button with a command has a key.
function choiceForKey(choice, text) {
  if (!choice || typeof text !== "string" || text.length !== 1) return null
  var key = text.toLowerCase()
  for (var i = 0; i < choice.buttons.length; i++)
    if (choice.buttons[i].runs && choice.buttons[i].key === key) return choice.buttons[i]
  return null
}

// What a button reads: "d  Watch the English dub now (with Arabic subtitles)". `keysHere` false (another show's detail
// view, whose own keys win): no key prefix.
function choiceButtonText(button, keysHere) {
  if (!button) return ""
  return (button.key && keysHere !== false ? button.key + "  " : "") + button.label
}

// What Browse does with `later`'s answer (`Service.laterAnswer`): say its words; close a moment later only when the
// request was recorded (otherwise the play keeps waiting here, and the viewer can choose again).
function laterAnswerView(answer) {
  if (!answer || typeof answer !== "object") return { text: "", dismiss: false }
  return { text: String(answer.message || ""), dismiss: answer.later === true }
}

// Whether choosing closes the overlay: the dub plays in the player, and "Tell me when it's ready" hides it; keeping on
// waiting stays here, where the wait is shown.
function choiceDismisses(button) {
  return !!button && (button.id === "dub" || button.id === "later")
}

// Whether choosing starts a play the overlay then follows (`playPending`): keeping on waiting from a hold does (a new
// `--wait` play). Keeping on waiting during the wait (`confirm`) starts none: the waiting play goes on.
function choiceStartsPlay(button) {
  return !!button && button.id === "wait"
}

// The status line under the buttons: the play's own line, then the choice's sentence when there is one (none once
// the viewer chose to keep waiting).
function choiceStatusText(choice) {
  if (!choice) return ""
  return choice.headline ? choice.line + "\n" + choice.headline : choice.line
}

// The detail view's reminder that a pin is steering this show's plays.
function pinLine(pin) {
  if (!pin || typeof pin !== "object") return ""
  var parts = []
  if (pin.group) parts.push(String(pin.group))
  var pack = pin.pack
  if (pack) {
    var r = isList(pack.range) && pack.range.length > 0 ? pack.range[0] : null
    parts.push(r ? "pack " + r[0] + "–" + r[1] : "pack")
  }
  return parts.length ? "Pinned: " + parts.join(" · ") + " · x in the release list forgets it" : ""
}

// ------------------------------------------------------------ Accounts rows
// One line per provider, built from what `omarchy-anime accounts` reports (`a` is its block for `provider`). It
// carries a state, a plan and at most the last four characters of the token — never the token.
// TorBox's `plan` is a number ("1 · renews …" was its row); its name comes as `planName`
// (`cli.verify_token`), and a plan that is only a number is never shown. Real-Debrid's is a word (`type`), shown as
// it is, and so is Subdl's (`extsubs.verify_key`).
function accountPlan(a) {
  if (!a || typeof a !== "object") return ""
  if (typeof a.planName === "string" && a.planName.trim() !== "") return a.planName.trim()
  if (typeof a.plan !== "string") return ""
  var plan = a.plan.trim()
  return plan === "" || /^[0-9]+$/.test(plan) ? "" : plan
}

function accountLine(provider, a) {
  a = a && typeof a === "object" ? a : {}
  if (!a.present) return "not connected"
  var parts = []
  if (a.error) return "signed in · " + String(a.error)
  if (a.user) parts.push(String(a.user))
  var plan = accountPlan(a)
  if (plan) parts.push(plan)
  if (provider === "anilist" && a.daysLeft !== undefined && a.daysLeft !== null)
    parts.push(a.expired ? "token expired" : "expires in " + a.daysLeft + " days")
  else if (a.expiresAt) parts.push("renews " + String(a.expiresAt).slice(0, 10))
  // The subtitle databases' allowance for today, as each service last reported it.
  if (a.quota) {
    if (a.quota.exhausted) parts.push("no downloads left today")
    else if (a.quota.remaining !== undefined && a.quota.remaining !== null)
      parts.push(a.quota.remaining + " of " + a.quota.limit + " " + a.quota.kind + " left today")
    else parts.push(a.quota.limit + " " + a.quota.kind + " a day")
  }
  if (a.masked) parts.push("token " + String(a.masked))
  return parts.join(" · ")
}

// The settings sheet's Indexers section, from the `indexers` block of `omarchy-anime
// accounts` ({dir, problem, loaded:[{name,file}], refused:[{file,reason}]}). Why a file did not load
// is the point of its row, so the reason is carried whole; the sheet wraps it rather than eliding it.
// The row says "not loaded", not "refused": a Refusal is auto-play declining a release.
// The empty state (`say.NOTHING_SET_UP` holds the same two
// sentences, a test keeps them equal), then where add-ons go when the folder is known.
var NO_INDEXER_TEXT = "No indexer is set up. Add your Prowlarr or Jackett in Accounts, or add an indexer add-on."

function indexerSection(status) {
  if (!status || typeof status !== "object" || isList(status)) return { caption: "", rows: [] }
  var rows = []
  if (status.problem) rows.push({ kind: "problem", title: "Indexer folder", detail: String(status.problem) })
  var loaded = isList(status.loaded) ? status.loaded : []
  var loadedCount = 0
  for (var i = 0; i < loaded.length; i++) {
    var addon = loaded[i]
    if (!addon || typeof addon !== "object") continue
    var file = String(addon.file || "")
    var name = String(addon.name || file.replace(/\.py$/, "") || "?")
    // The built-in Torznab add-on is no file; it says how much it searches and where that is set.
    var detail = addon.builtin === true ? "built in: " + _count(addon.servers, "server") + ", "
                                          + _count(addon.feeds, "feed") + ", from Accounts" : file
    rows.push({ kind: "loaded", title: (++loadedCount) + ". " + name, detail: detail })
  }
  var refused = isList(status.refused) ? status.refused : []
  for (var j = 0; j < refused.length; j++) {
    var r = refused[j]
    if (!r || typeof r !== "object") continue
    var title = String(r.file || "?") === TORZNAB_FILE ? "Torznab (" + TORZNAB_FILE + ")" : String(r.file || "?")
    rows.push({ kind: "refused", title: title, detail: "not loaded: " + String(r.reason || "no reason given") })
  }
  // The folder as the viewer reads it (`~/…`, the CLI's `shown`) wherever it is shown.
  var shown = status.shown || status.dir
  var where = shown ? " Add-ons live in " + String(shown) + "." : ""
  var caption
  if (loadedCount > 0) caption = "Asked in this order: the indexers setting under Advanced first, then any other file by name." + where
  else if (rows.length > 0) caption = "None loaded, so no release can be found until one does." + where
  else caption = NO_INDEXER_TEXT + (shown ? " Indexer add-ons go in " + String(shown) + "." : "")
  return { caption: caption, rows: rows }
}

// The two buttons under the Indexers rows that hand the viewer's own AI agent a ready prompt (the Service's
// `askAgent`), each with what it does and, after a click, the Service's line for it (`agentNotes`).
// Once: what pressing does to an agent not chosen yet (Omarchy's chooser, then press again) is the row's own line after
// the press (`ServiceLogic.AGENT_OPENED`), where the viewer is looking when the chooser opens.
var AGENT_CAPTION = "Or let your own AI agent help: each button opens your default agent with a ready prompt."
var AGENT_ROWS = [
  { kind: "build", label: "Build an add-on with your AI agent",
    detail: "It reads the add-on interface, asks which indexer you are entitled to use, shows you the file before "
            + "saving it, and checks it." },
  { kind: "prowlarr", label: "Help me set up Prowlarr",
    detail: "It helps you install and run Prowlarr or Jackett; you add the indexers and the key yourself." },
]

function agentRows(notes) {
  var rows = []
  for (var i = 0; i < AGENT_ROWS.length; i++) {
    var r = AGENT_ROWS[i]
    var note = notes && typeof notes === "object" && typeof notes[r.kind] === "string" ? notes[r.kind] : ""
    rows.push({ kind: r.kind, label: r.label, detail: r.detail, note: note })
  }
  return rows
}

function _count(n, noun) {
  var k = Number(n) || 0
  return k + " " + noun + (k === 1 ? "" : "s")
}

// ------------------------------------------------------------------ Torznab servers
// The Accounts sheet's Torznab block, from `omarchy-anime accounts` (its `torznab` block: {path, problem, skipped,
// caption, checkBudget, servers:[{id, row, url, name, key, hasKey, label, kind, feeds, problem}]}) and the last
// `torznab add|check` answer per row (`checks[row]`: {ok, transient, words:{headline, action, lines}} or
// {ok:false, error}). A row is named by its `id` (the CLI resolves it; an address is never on a command line). The
// caption and every sentence are the CLI's (say.py); `key` is already masked there. Nothing here sees more of it.
var TORZNAB_FILE = "torznab-servers.json"

function _sentenceOf(text) {
  var t = String(text || "").trim()
  if (t === "") return ""
  return /[.!?]$/.test(t) ? t : t + "."
}

function _checkText(r) {
  if (!r || typeof r !== "object") return ""
  var w = r.words && typeof r.words === "object" ? r.words : null
  if (!w) return r.ok === false && r.error ? _sentenceOf(r.error) : ""
  return [String(w.headline || ""), String(w.action || "")].filter(function (x) { return x !== "" }).join(" ")
}

function torznabSection(block, checks) {
  if (!block || typeof block !== "object" || isList(block)) return { caption: "", rows: [] }
  var results = checks && typeof checks === "object" ? checks : {}
  var rows = []
  var servers = isList(block.servers) ? block.servers : []
  for (var i = 0; i < servers.length; i++) {
    var s = servers[i]
    if (!s || typeof s !== "object") continue
    var line
    var bad = false
    if (s.problem) {
      line = "not used: " + String(s.problem)
      bad = true
    } else {
      var what = s.kind === "prowlarr" ? "Prowlarr · " + _count(s.feeds, "indexer")
               : s.kind === "torznab" ? "Torznab feed" : "not checked yet"
      line = what + " · " + (s.hasKey && s.key ? "key " + String(s.key) : "no key")
    }
    var row = String(s.row || "")
    var r = row ? results[row] : null
    var w = r && r.words && typeof r.words === "object" ? r.words : null
    rows.push({ id: String(s.id || ""), row: row, title: String(s.name || s.label || s.url || ""), line: line, bad: bad,
                check: _checkText(r), checkLines: w && isList(w.lines) ? w.lines.map(String) : [],
                checkBad: !!r && r.ok === false })
  }
  return { caption: String(block.caption || ""), rows: rows }
}

// One line for an add's (or a remove's, or a failed command's) answer: the CLI's words, or its error.
function torznabResultText(r) {
  if (!r || typeof r !== "object") return ""
  if (r.words && typeof r.words === "object") return _checkText(r)
  return r.error ? _sentenceOf(r.error) : ""
}

// How long a check may take, from the CLI's own budget (`checkBudget`, seconds), for "Checking … (up to N s)".
function torznabBudgetText(block) {
  var n = block && typeof block === "object" ? Number(block.checkBudget) : NaN
  return n > 0 ? "up to " + Math.round(n) + " s" : "a few seconds"
}

// The line under TorBox's device code, from the Service's `torboxCodeClip`: the code was put on the
// clipboard, or that could not be done; "" before the copy has answered (and with a Service older than this file).
function deviceClipText(clip) {
  if (clip === "copied") return "Copied to the clipboard: paste it on that page."
  if (clip === "failed") return "Could not copy it to the clipboard (wl-copy failed); type it on that page."
  return ""
}

// The Settings window's status line: {text, alert}, in the order the sheet's line had on the overlay. A play
// starting (`busy`, with a play refused meanwhile under it), then a settings write that failed (it outranks the play's
// error), then the play's error (`errorHeadline` over `errorDetail`, while `lastError` is set), a play refused before
// it started (`playRefusal`), the restart notice of a Service older than this file, a standing notice (token expiry,
// AniList budget), else the keys. `alert` as the overlay colours it: an error or a notice is up. `back`: Escape goes
// back to the overlay rather than closing.
function settingsStatus(o) {
  var s = o && typeof o === "object" ? o : {}
  if (!s.loaded) return { text: "Service not loaded", alert: false }
  var alert = !!(s.lastError || s.notice || s.restartNotice || s.settingsError)
  var text
  if (s.busy) text = String(s.busy) + (s.playRefusal ? "\n" + String(s.playRefusal) : "")
  else if (s.settingsError) text = String(s.settingsError)
  else if (s.lastError) text = String(s.errorHeadline || "") + "\n" + String(s.errorDetail || "")
  else if (s.playRefusal) text = String(s.playRefusal)
  else if (s.restartNotice) text = String(s.restartNotice)
  else if (s.notice) text = String(s.notice)
  else text = "Tab moves between settings · j/k scrolls · every change is saved to shell.json · reset gives a key "
              + "back to config.json · Escape " + (s.back ? "goes back" : "closes")
  return { text: text, alert: alert }
}

// One server's address as "host:port", for "is this found server already listed?", or "" when it is no
// http(s) address. Only the authority counts: a path (a Prowlarr indexer's `/1/api`, Jackett's
// `/api/v2.0/indexers/all/results/torznab`), a query and a trailing `/` do not make another server; user info (the
// listing's `<redacted>@`) is dropped; the port defaults by scheme; every name for this computer (`localhost`,
// `*.localhost`, 127.0.0.0/8, `::1`) reads as one. A regex rather than `URL`, which throws on a bad address.
function torznabHostPort(url) {
  var m = /^\s*(https?):\/\/([^\/?#]*)/i.exec(String(url || ""))
  if (!m) return ""
  var auth = m[2]
  var at = auth.lastIndexOf("@")
  if (at >= 0) auth = auth.slice(at + 1)
  var host = auth
  var port = ""
  if (auth.charAt(0) === "[") {
    var end = auth.indexOf("]")
    if (end < 0) return ""
    host = auth.slice(1, end)
    var rest = auth.slice(end + 1)
    if (rest !== "" && rest.charAt(0) !== ":") return ""
    port = rest.slice(1)
  } else {
    var colon = auth.lastIndexOf(":")
    if (colon >= 0) {
      host = auth.slice(0, colon)
      port = auth.slice(colon + 1)
    }
  }
  host = host.toLowerCase().replace(/\.$/, "")
  if (host === "") return ""
  if (port === "") port = m[1].toLowerCase() === "https" ? "443" : "80"
  if (!/^[0-9]{1,5}$/.test(port)) return ""
  if (host === "localhost" || /\.localhost$/.test(host) || host === "::1" || host === "0:0:0:0:0:0:0:1"
      || /^127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$/.test(host)) host = "this-computer"
  return host + ":" + parseInt(port, 10)
}

// What the sheet does with `torznab find-local`'s answer (`local`: {found:[{product, address, hostport,
// words:{headline, action}}]}) against the listed servers (`block`, the `accounts` answer's `torznab`): `offers` one
// per server found and not already listed (`torznabHostPort`), `found`/`listed` the products found and those of them
// already listed, for the log. Nothing is offered unless `usable` (ServiceLogic.js `torznabListUsable`, the rule the
// Service asks by). `address` pre-fills the add row; the key is the user's to paste.
function torznabOfferDecision(usable, block, local) {
  var out = { offers: [], found: [], listed: [] }
  if (!local || typeof local !== "object") return out
  var found = isList(local.found) ? local.found : []
  var servers = block && typeof block === "object" && isList(block.servers) ? block.servers : []
  var taken = {}
  for (var j = 0; j < servers.length; j++) {
    var hp = servers[j] && typeof servers[j] === "object" ? torznabHostPort(servers[j].url) : ""
    if (hp) taken[hp] = true
  }
  for (var i = 0; i < found.length; i++) {
    var f = found[i]
    if (!f || typeof f !== "object" || !f.address) continue
    var product = String(f.product || "")
    out.found.push(product)
    if (taken[torznabHostPort(f.address)]) {
      out.listed.push(product)
      continue
    }
    if (usable !== true) continue
    var w = f.words && typeof f.words === "object" ? f.words : {}
    out.offers.push({ product: product, address: String(f.address),
                      text: String(w.headline || ""), where: String(w.action || "") })
  }
  return out
}

// The offers alone (`torznabOfferDecision`), for the sheet's rows.
function torznabOffers(usable, block, local) {
  return torznabOfferDecision(usable, block, local).offers
}

// The Service and ServiceLogic.js Browse.qml talks to can be older than Browse.qml. A plugin update
// without `omarchy restart shell` rebuilds the Service from the shell's cached compile and hands a newly compiled
// Browse the cached ServiceLogic.js, so a function added since is simply absent, and calling it throws: Settings drew
// nothing, Accounts showed "Sign in". Browse therefore calls both only through `serviceBridge`. This file is imported
// by Browse.qml and OrderedList.qml alone, so it compiles with Browse rather than with the Service.
//
// Every Service function Browse calls, with what Browse gets when the running copy lacks it (a function is called
// with the call's arguments). Adding a Service call to Browse.qml means adding its row here;
// a test fails until it is.
var SERVICE_FUNCTIONS = {
  addTorznab: false,
  askAgent: false,                 // a Service without it has no AI agent buttons to answer
  cancelTorBoxDevice: undefined,
  checkTorznab: false,
  clearClipboard: undefined,
  copyTrace: "",
  errorDetail: "",
  errorHeadline: "",
  loadAccounts: undefined,
  loadReleases: undefined,
  logEvent: undefined,
  newTrace: "",
  play: "",
  refreshWatching: undefined,
  runChoice: "",                   // a Service without it runs no choice: the player's keys still do
  removeTorznab: false,
  resetSetting: false,
  resetSettings: false,
  signIn: undefined,
  signInTorBoxDevice: undefined,
  signOut: undefined,
  updateSetting: false,
  wantLocalTorznab: undefined,     // a Service without it offers no local Prowlarr/Jackett
}

// Every Service property Browse reads. An absent one reads as undefined (no throw); it is listed so the log names it.
var SERVICE_PROPERTIES = [
  "accounts", "accountsError", "accountsLoading", "agentNotes", "bin", "busy", "configPath", "languageNames",
  "laterAnswer", "lastError", "lastErrorPicker",
  "notice", "pickerData", "pickerError", "pickerLoading", "playEntry", "playEpisode", "playHeld", "playMediaId",
  "playRefusal", "settings",
  "settingsError",
  "settingsSource",
  "torboxCodeClip", "torboxDevice", "torznabAnswer", "torznabBusy", "torznabChecks", "torznabLocal", "watching",
  "watchingFailure",
  "watchingLoaded", "watchingSaved",
]

// ServiceLogic.js `cliLaunch`, word for word, for the two stand-ins below (a test compares them).
function _cliLaunch(head, words) {
  var env = {}
  env.OMAANIME_ARGS = JSON.stringify(listCopy(words).map(String))
  return { command: listCopy(head).map(String), environment: env }
}

// Every ServiceLogic.js function Browse calls, with its stand-in: the real function's answer for an idle or empty
// input, in the real shape (a test compares them).
var LOGIC_FUNCTIONS = {
  // The real one, so a stored "true" reads as on before the restart too.
  asBool: function (value, fallback) {
    if (value === true || value === false) return value
    if (value === null || value === undefined) return fallback === true
    if (typeof value === "string") {
      var v = value.trim().toLowerCase()
      if (v === "true" || v === "1" || v === "yes" || v === "on") return true
      if (v === "false" || v === "0" || v === "no" || v === "off" || v === "") return false
      return fallback === true
    }
    return !!value
  },
  // Idle can be cancelled, so the TorBox row keeps Cancel and "Paste a token instead" (the real one-liner).
  deviceCancellable: function (sign) { return !sign || String(sign.state) !== "granted" },
  deviceStatusText: "",
  listFailure: function (r, status) {
    if (LOGIC_FUNCTIONS.listAnswerRows(r) !== null) return null
    return { state: "failed", error: r && r.error ? String(r.error) : "the list command printed no JSON", stage: "",
             reason: "", trace: "", status: String(status || "") }
  },
  listFailureDropsRows: false,
  listFailureFor: null,
  // The real unwrap: the saved list an AniList outage answered with still shows its rows.
  listAnswerRows: function (r) {
    if (isList(r)) return r
    if (r && typeof r === "object" && r.ok === true && isList(r.entries) && r.stale
        && typeof r.stale === "object") return r.entries
    return null
  },
  listAnswerStale: null,                // no marker: the rows show without the line
  listStaleText: "",
  // Browse reads `.text`; without the real rules the view says it is loading.
  listStateText: function (o) {
    return { state: "loading", text: "Loading " + String((o && o.what) || "your list") + "…" }
  },
  torznabListUsable: false,        // "not usable", so no offer is shown
  // The real two (`docs/privacy.md`), so a launch from Browse keeps the show off the command line however old the
  // running ServiceLogic.js is: the words in `OMAANIME_ARGS`, which the CLI on disk reads.
  cliOn: function (proc, head, words) {
    var launch = _cliLaunch(head, words)
    proc.environment = launch.environment
    proc.command = launch.command
    return launch
  },
  logEventLaunch: function (bin, trace, component, stage, data, level) {
    return _cliLaunch([bin, "--trace", trace, "log-event"],
                      [String(component), String(stage), "--level", String(level || "info"),
                       "--data", JSON.stringify(data || {})])
  },
}

var SERVICE_RESTART = "omarchy restart shell"

function _bridgeCall(target, name) {
  return function () { return target[name].apply(target, arguments) }
}

function _bridgeStandIn(value) {
  return function () { return typeof value === "function" ? value.apply(null, arguments) : value }
}

// {svc, logic, missing, line, notice}: `svc` and `logic` carry every listed function, the running one's or its
// stand-in; `missing` names what the running copies lack (ServiceLogic.js names as `Logic.x`), "" `line`/`notice`
// when nothing is. A null `service` is "not loaded yet", not stale: everything stands in and nothing is reported.
function serviceBridge(service, logic) {
  var svc = {}
  var lg = {}
  var missing = []
  var name
  for (name in SERVICE_FUNCTIONS) {
    if (service && typeof service[name] === "function") svc[name] = _bridgeCall(service, name)
    else {
      svc[name] = _bridgeStandIn(SERVICE_FUNCTIONS[name])
      if (service) missing.push(name)
    }
  }
  if (service)
    for (var i = 0; i < SERVICE_PROPERTIES.length; i++)
      if (!(SERVICE_PROPERTIES[i] in service)) missing.push(SERVICE_PROPERTIES[i])
  for (name in LOGIC_FUNCTIONS) {
    if (logic && typeof logic[name] === "function") lg[name] = _bridgeCall(logic, name)
    else {
      lg[name] = _bridgeStandIn(LOGIC_FUNCTIONS[name])
      if (service) missing.push("Logic." + name)
    }
  }
  var stale = missing.length > 0
  return {
    svc: svc,
    logic: lg,
    missing: missing,
    line: stale ? "the running Service is older than Browse.qml, missing " + missing.join(", ")
                  + "; run `" + SERVICE_RESTART + "` to load the updated plugin" : "",
    notice: stale ? "The shell is still running this plugin's previous version. Run " + SERVICE_RESTART
                    + " to finish the update." : "",
  }
}
