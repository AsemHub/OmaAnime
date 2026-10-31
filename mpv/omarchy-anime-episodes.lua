-- omarchy-anime-episodes.lua — the show's episode list, down the right-hand side of the player.
--
-- The user asked to "inject all the unwatched episodes into the player so I don't have to
-- leave to click the next episode". Today the only way to start the next episode from the player is
-- to leave it (open Browse, pick the row, come back); `autoNext` covers exactly one case.
--
-- Data: ~/.cache/omaanime/episodes/<mediaId>.json, written by the CLI on every play and on
-- every watched tick. Nothing here fetches anything; a missing file is an OSD line and the player is
-- untouched.
--
-- Everything below that is marked "measured" was checked on this machine's mpv 0.41 / LuaJIT 2.1.
-- LuaJIT is 5.1: no goto, no //, no bitwise operators, `unpack` not `table.unpack`.
local mp = require 'mp'
local msg = require 'mp.msg'
local utils = require 'mp.utils'
local options = require 'mp.options'

local opts = {
    -- `--script-opt=omarchy_anime_episodes-bin=…` (singular: the plural form *overwrites* the
    -- user's own script-opts list, per the man page). The identifier defaults to the script name.
    bin = "",
    cache = "",
    -- Empty: the panel draws in mpv's `osd-font`, like the rest of the player's interface. The
    -- launcher does not pass the subtitle font here; a user may set one by hand.
    font = "",
    -- Arabic runs are drawn in this family, when fc-match says it is installed. The
    -- fallback for Arabic is Noto Naskh Arabic, whose tall metrics draw it at half the Latin cap
    -- height; the "UI" face is built to sit next to Latin UI text. Empty turns it off.
    arabic_font = "Noto Sans Arabic UI",
    -- The panel's design calls for "roughly six rows visible, each row:
    -- thumbnail, episode number, title, runtime, state" -- 12 dense rows at the old default
    -- left a picture too small to read (a 69x39px swatch at 1080p). Six rows, still configurable
    -- for anyone who wants the old dense list back.
    -- Now five rows of 96 ASS units. Every other line (header, scroll indicators,
    -- hint) keeps the font's own line height, not row_h -- see THUMB_ROW_H and draw()'s `ys` --
    -- so the worst case (both "N above"/"N below" indicators showing) totals 600 of the 630
    -- units above the OSC band, never past it.
    rows = 5,
    width = 32,          -- per cent of the canvas the panel may take
    -- An earlier attempt at bigger pictures doubled this to 40 to grow the row (hence the
    -- picture) — but the title budget below is *inversely* proportional to `size`, so doubling the
    -- font also halved how much title fit, down to an unreadable "Episo…" on every row. Back to
    -- its original 20; `row_h` below no longer rides on `size` at all, so the picture stays big
    -- without shrinking the title. See THUMB_ROW_H.
    size = 20,           -- ASS font size, in units of the 720-high virtual canvas
}
options.read_options(opts)

local PANEL_Z = 1001     -- the OSC draws at z=1000 (osc.lua v0.41 `set_osd(…, 1000)`); every
                         -- third-party list script leaves z at 0 and therefore draws *under* it.
local RES_Y = 720        -- res_x stays 0 so libass derives it and an ASS pixel stays square.

-- A command line is readable by every account on the computer (`/proc/<pid>/cmdline`, 0444), an environment only by
-- its owner (0400) (`docs/privacy.md`). So a launch of the plugin's CLI keeps fixed words in `head` (the binary,
-- `--trace` and the trace, the subcommand) and every word that names the show, the episode or a log line's data in
-- `words`, which travel in `OMAANIME_ARGS`: a JSON list the CLI appends to its argv and drops before it starts
-- anything. mpv's `env` is the child's whole environment, in execle()'s format (man mpv 0.41, `subprocess`: "On Lua,
-- you may use utils.get_env_list() to retrieve the current environment if you e.g. simply want to add a new
-- variable"), so it is this process's own, without any copy of the variable, plus ours. The other fields of `t` go to
-- the `subprocess` command as they are.
local ARGS_ENV = "OMAANIME_ARGS"
local CHOICE_COMMANDS = { play = "play", later = "later", ["keep-waiting"] = "keep-waiting", player = "player" }
local function cli_command(t)
    local env = {}
    for _, kv in ipairs(utils.get_env_list() or {}) do
        if kv:sub(1, #ARGS_ENV + 1) ~= ARGS_ENV .. "=" then env[#env + 1] = kv end
    end
    local words = {}
    for i, w in ipairs(t.words or {}) do words[i] = tostring(w) end
    env[#env + 1] = ARGS_ENV .. "=" .. (#words > 0 and utils.format_json(words) or "[]")
    local cmd = { name = "subprocess", playback_only = false, env = env, args = t.head }
    for k, v in pairs(t) do
        if k ~= "head" and k ~= "words" then cmd[k] = v end
    end
    return cmd
end

-- There used to be a `PENDING_S = 8` here: how long the "finding a release" line stayed up,
-- and how long a second click was refused for. A play measured 28-43 s in the traces, so the player
-- looked dead for the other 20-35 s and the second click was always accepted — and a second play
-- takes the first one's downloader away. Both jobs now come from the CLI's own shared entry,
-- `~/.cache/omaanime/play-status.json`, which is the only thing that knows what is happening.
local STATUS_FILE = "play-status.json"
local POLL_ACTIVE_S = 0.5   -- while a play is in flight: the second counter has to move
local POLL_IDLE_S = 2.0     -- otherwise: one small file read, to notice a play someone else started
local FAIL_HOLD_S = 30      -- how long a failure's reason and advice stay on screen
local LAUNCH_GRACE_S = 2.0  -- from spawning the CLI to its first write; the only local timer left
-- Must not exceed playstate.ABANDON_AFTER_S: this side may believe an entry for *less* time than
-- the CLI does, never more, or the panel would refuse a click the CLI would have accepted.
local ABANDON_AFTER_S = 20
-- A held play's choices belong to their moment: gone this long after `finishedAt`, as `playstate.HELD_SHOW_S`
-- says (no line, no keys, no art).
local HELD_SHOW_S = 3600

local COLOUR = {          -- ASS is &HBBGGRR&
    watched = "&H808080&",
    next    = "&H158BE2&",
    aired   = "&HFFFFFF&",
    unaired = "&H606060&",
}
local MARK = { watched = "\226\156\147", next = "\226\150\182", aired = " ", unaired = "\194\183" }

local LRM = "\226\128\142"   -- U+200E LEFT-TO-RIGHT MARK
local FSI = "\226\129\168"   -- U+2068 FIRST STRONG ISOLATE
local PDI = "\226\129\169"   -- U+2069 POP DIRECTIONAL ISOLATE

local overlay = nil
local hide, show          -- forward declarations: the binding table and the ☰ close over them
local shown = false
local data = nil
local cursor = 1
local top = 1
local bound = false

-- The in-flight play, as the CLI publishes it. `launched` covers only the gap between spawning the
-- CLI and its first write — the one thing the file cannot know yet.
local play_entry = nil
local launched = { at = 0, episode = 0, trace = "" }
local announced_fail = ""   -- trace of the failure already put on screen, so it is shown once
local hidden_fail_trace = ""  -- trace of the failure whose empty window has already been retracted
local status_timer = nil
local last_status = ""   -- the play line as last drawn, so a redraw costs a change

-- Where the panel is, in ASS units, recomputed by every draw(). Rows are separate `\pos`-ed
-- events, so row geometry is known by construction and the hit test is arithmetic rather than a
-- guess at libass's metrics. `compute_bounds` is not used: the manual calls it "very inefficient
-- … it renders the full result … It will flush various caches", and experimental.
-- `thumb_left`: the text column's own left edge, `x`, unless a
-- picture is currently drawn further left this frame -- in which case it's the picture's left
-- edge instead, so the mouse-scoped rectangle and the row hit test both cover the picture too.
-- Defaults to `x` (unchanged from before thumbnails existed) whenever nothing is drawn there.
-- `line_y[li]`: each line's own top, since episode rows and text lines (header,
-- scroll indicators, hint) no longer share one pitch -- `row_h` for the former, `text_h` for the
-- latter. `bottom` is `line_y` of one past the last line, i.e. the panel's own lowest pixel, so
-- `scope_mouse` can clamp to it instead of assuming every line was `row_h` tall.
local geom = { x = 0, right = 0, top = 0, row_h = 0, lines = 0, rows = {}, thumb_left = 0,
               line_y = {}, bottom = 0, text_h = 0 }
local free_top = false    -- the wheel moved the window; stop dragging it back to the cursor
local tab_shown = false   -- the ☰ way-in, drawn only while the pointer is over the window
local tab_bound = false
local tab_timer = nil
local hover_row = nil     -- the row the pointer was last over, so a redraw costs a row change
local redraws = 0         -- draw() calls, so the coalescing can be asserted rather than believed

-- The text panel stays one ass-events overlay; these are the picture layer.
-- `overlay-add` is a completely separate mechanism from ass-events, with its own id space (0-63)
-- and raw BGRA pixels at screen coordinates rather than RES_Y ones — it is drawn *together with*
-- the text overlay, never instead of it. THUMB_W/THUMB_H must stay identical to
-- plugin/lib/omarchy_anime/thumbs.py's THUMB_W/THUMB_H: the buffer on disk has no header, so a
-- mismatch here would draw a corrupted image, not an error.
local THUMB_W, THUMB_H = 320, 180
local THUMB_MAX_SLOTS = 24   -- generous over "roughly six visible rows"; well inside the 0-63 space
local THUMB_BASE_ID = 0      -- overlay ids THUMB_BASE_ID .. THUMB_BASE_ID + THUMB_MAX_SLOTS - 1
-- The loading screen; never a row slot. The logo takes the HIGHER id on purpose:
-- mpv composites overlay elements in ascending id order, so the full-window backdrop at
-- 63 painted straight over a logo at 62 and it never appeared. The manual documents the id only as
-- "an integer between 0 and 63 identifying the overlay element" and says nothing about ordering,
-- so this was measured on the live player, not read. A future third layer picks its id by what has
-- to be on top, not by counting down from 63.
local BACKDROP_ID, LOGO_ID = 62, 63
local THUMB_GAP = 8          -- ASS units between the picture's right edge and the text column
-- A row's height must not ride on the font size -- `size` also drives
-- the title's character budget (inversely: bigger font, fewer characters), so growing `size` to
-- grow the row shrank every title down to an unreadable "Episo…" (an earlier attempt's mistake).
-- This is a fixed ASS-unit floor for the row pitch instead, independent of `size`: 96
-- units, so a fullscreen 4K row is 288 px tall and 512 wide, four times the earlier rows' area. Only
-- episode rows are this tall -- the header, the scroll indicators and the hint keep the font's
-- own line height (`text_row_h` below, `draw()`'s `ys`), so five 96-unit rows plus four 26-unit
-- text lines and TOP_Y come to 600 of the 630 units above the OSC band, never overlapping it.
local THUMB_ROW_H = 96
local thumb_ids = {}         -- slot (1..THUMB_MAX_SLOTS) -> episode number currently drawn there
-- This script's forced-binding section. Measured off a live player's `input-bindings`.
local SECTION = "input_forced_" .. mp.get_script_name()
-- ASS pixels left for the OSC: the panel's mouse rectangle stops above it, so the "whichever
-- section was enabled last wins" race with osc.lua cannot arise.
local OSC_BAND = 90
local TOP_Y = 16
-- The ☰ way-in: its box is TAB_W units wide at the right edge. The play's block keeps TAB_W + TAB_GAP
-- free there whether or not the ☰ is showing, so both read this one number.
local TAB_W = 44
local TAB_GAP = 8
-- The height of one line of mpv's OSD message, as a multiple of its font size, so the play's
-- block hangs just under it. Measured on screen: `show-text` at the defaults and the block at
-- 16 + 30 * 1.3 = 55 sit clear of each other.
local OSD_LINE_H = 1.3

-- The loading screen. `overlay-add` bitmaps are always above ASS (measured, and
-- again on 2026-09-22), so the backdrop covers
-- everything EXCEPT the bottom LOADING_BAND units, and every line of text (the show line, then the
-- play's status line the panel already draws) lives in that band, above the OSC's own strip.
-- 180 = OSC_BAND + four lines of the default 20-unit font. Measured, not chosen: at 90 the status
-- line's own rows fall under the bitmap and the band renders nothing.
local LOADING_BAND = 180
-- While a play waiting for TorBox offers the viewer its choices (the entry's `choice`), the band grows by
-- this many units so the sentence and one line per choice sit above the backdrop too: four lines of the
-- default font, the same 26-unit pitch `text_row_h` gives it.
local CHOICE_BAND = 104
local CHOICE_LINES = 4      -- the extra lines the band's status block may take for them
local art_shown = false        -- ids BACKDROP_ID/LOGO_ID are live
-- The trace of the play whose file mpv has already presented. `file-loaded` clears the
-- art, but the CLI writes `done` several hundred ms later (its covers fetch and now-playing write
-- come first), so without this the 0.5 s tick found a still-`running` entry and put the backdrop
-- back on top of the playing video until that write landed.
local landed_trace = nil

local function home()
    return os.getenv("HOME") or ""
end

local function cache_dir()
    if opts.cache ~= "" then return opts.cache end
    local xdg = os.getenv("XDG_CACHE_HOME")
    return ((xdg and xdg ~= "") and xdg or (home() .. "/.cache")) .. "/omaanime"
end

local function read_json(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local body = f:read("*a")
    f:close()
    if not body or body == "" then return nil end
    local parsed = utils.parse_json(body)
    return parsed
end

local function play_status_path()
    return cache_dir() .. "/" .. STATUS_FILE
end

-- The player stamp, `mediaId:episode:infohash` (`player.PLAYING_KEY`): the CLI sets it just before every `loadfile`
-- it sends (`player.prepare_load`, on a play and on a restart), and mpv keeps it across files. A stamp write that
-- fails does not stop the `loadfile`, so the file then starts under the previous stamp and the menu lists the show
-- before it (left as is: no stamp write has failed in the logs). The CLI's namespace, not this script's
-- `omarchy-anime/`.
local STAMP_KEY = "user-data/omaanime/playing"
local file_stamp = nil   -- the stamp as it stood at the loaded file's `start-file`
local menu_sig = ""      -- the show and what named it, last logged, so the line comes once per change
local menu_from = ""     -- what named the show when the menu last opened (`dump`'s `menuFrom`)

local function media_id_of(t)
    return type(t) == "table" and math.floor(tonumber(t.mediaId) or 0) or 0
end

local function stamp_media_id(value)
    if type(value) ~= "string" then return 0 end
    local id = value:match("^(%d+):%d+:")
    return id and math.floor(tonumber(id) or 0) or 0
end

-- Which show the menu lists: the play on screen.
--   * A file is loaded (`idle-active` false; an episode that ended under --keep-open is still loaded): that file's
--     show, from the stamp its `start-file` saw. now-playing.json is rewritten seconds after the `loadfile` (the
--     AniList and cover reads come first), so in that gap it still names the show before; the stamp is set before the
--     `loadfile`. A play of another show that runs, holds or fails over the file loads nothing and changes nothing.
--     No `start-file` seen (this script loaded after the file): the stamp as it is; no stamp at all, or one that
--     isn't `mediaId:episode:infohash` (refused as `player.parse_stamp` refuses it): now-playing.
--   * Nothing is loaded (idle, or a fresh player): the latest play's show, from play-status.json read now. Never
--     now-playing.json: it names the last file a play loaded, and a failed play of a movie in a fresh player listed
--     the show watched an hour before.
-- Returns the show's id (0 for none), what named it, the reason, the entry that may carry its title, and the stamp
-- that named it (logged, so the line ties to the play's `player.stamp set` in the CLI's log).
local function menu_show()
    local loaded = mp.get_property_native("idle-active") ~= true
    if loaded then
        local id = stamp_media_id(file_stamp)
        if id > 0 then
            return id, "player stamp", "a file is loaded; the stamp at its start names it, stamp=" .. file_stamp,
                   nil, file_stamp
        end
        local live = mp.get_property_native(STAMP_KEY)
        id = stamp_media_id(live)
        if id > 0 then
            return id, "player stamp", "a file is loaded; no stamp was taken at its start (none was set then, or this "
                   .. "script missed it), the player's stamp names it, stamp=" .. live, nil, live
        end
        local np = read_json(cache_dir() .. "/now-playing.json")
        id = media_id_of(np)
        if id > 0 then return id, "now-playing", "a file is loaded and the player has no stamp", np end
        return 0, "none", "a file is loaded, with no stamp and no now-playing"
    end
    local latest = read_json(play_status_path())
    local id = media_id_of(latest)
    if id > 0 then
        return id, "latest play", "nothing is loaded; play-status names the latest play (" .. tostring(latest.state)
               .. " trace=" .. tostring(latest.trace) .. ")", latest
    end
    return 0, "none", "nothing is loaded and no play is recorded"
end

-- The show's name for the plain line: the entry that named it, else the latest play's, else now-playing's, whichever
-- is about this show. "" when none is. A file `entry` already is (`from` says which) is not read again.
local function show_title(id, entry, from)
    local candidates, n = { entry }, 1
    if from ~= "latest play" then n = n + 1; candidates[n] = read_json(play_status_path()) end
    if from ~= "now-playing" then n = n + 1; candidates[n] = read_json(cache_dir() .. "/now-playing.json") end
    for i = 1, n do
        local e = candidates[i]
        if media_id_of(e) == id and type(e.title) == "string" and e.title ~= "" then
            return e.title
        end
    end
    return ""
end

local function load_data()
    local id, from, why, entry, stamp = menu_show()
    menu_from = from
    local listed = id > 0 and read_json(cache_dir() .. "/episodes/" .. id .. ".json") or nil
    if listed and type(listed.episodes) ~= "table" then listed = nil end
    -- The stamp is part of the change: the next episode of the same show is a new file, a new play to tie the line to.
    -- It is constant within a file, so this is still one line per file, never one per tick or opening.
    local sig = from .. "|" .. id .. "|" .. tostring(listed ~= nil) .. "|" .. (stamp or "")
    if sig ~= menu_sig then
        menu_sig = sig
        msg.info("episodes menu: show " .. id .. " named by " .. from .. " (" .. why .. "); "
                 .. (listed and (#listed.episodes .. " episodes listed") or (id > 0 and "no episode list on disk"
                     or "nothing to list")))
    end
    data = listed
    if id == 0 then return false, "Nothing is playing" end
    if not data then
        local title = show_title(id, entry, from)
        if title == "" then return false, "No episode list for this show yet" end
        return false, "No episode list for " .. title .. " yet"
    end
    -- Start on the episode that is playing, else on the next unwatched one.
    cursor = 1
    for i, ep in ipairs(data.episodes) do
        if data.playing and ep.episode == data.playing then cursor = i break end
        if ep.state == "next" then cursor = i end
    end
    return true
end

-- --------------------------------------------------------------------------- the in-flight play
-- One small JSON file, written by the CLI at every stage of a play and read here. This side never
-- writes it: the CLI is the only process that knows whether a release was found, which provider
-- answered, or why it gave up. (`play_status_path` is above, with the menu's show.)

-- mpv's `mp.get_time()` is process-relative — measured 4.93 s on a player 4.93 s old — so it cannot
-- be compared with the CLI's CLOCK_MONOTONIC `startedMono` at all. The entry carries the writer's
-- own monotonic `elapsed` instead, and this adds the wall-clock drift since it last wrote.
local function status_elapsed(e)
    local base = tonumber(e.elapsed) or 0
    local drift = os.time() - (tonumber(e.updatedAt) or 0)
    if drift < 0 then drift = 0 end
    return base + drift
end

local function status_quiet(e)
    local quiet = os.time() - (tonumber(e.updatedAt) or 0)
    if quiet < 0 then quiet = 0 end
    return quiet
end

local function ep_num(e)
    return string.format("%d", math.floor(tonumber(e.episode) or 0))
end

-- A field left out or left empty reads as its default, as Python's and JavaScript's `or` read it ("" is truthy here).
local function said_or(v, default)
    if v == nil or v == "" then return default end
    return tostring(v)
end

-- The same sentence `playstate.render` builds on the Python side, character for character. Two
-- implementations that disagree about the wording would be worse than one that says nothing.
local function status_text(e)
    if not e then return "" end
    if e.state == "failed" then
        return "Episode " .. ep_num(e) .. " failed — " .. said_or(e.error, "no reason given")
    end
    if e.state == "held" then
        if os.time() - (tonumber(e.finishedAt) or 0) > HELD_SHOW_S then return "" end
        return "Episode " .. ep_num(e) .. " — " .. said_or(e.message, "waiting for your choice")
    end
    if e.state ~= "running" then return "" end
    local text = "Episode " .. ep_num(e) .. " — " .. said_or(e.message, "starting")
    local secs = math.floor(status_elapsed(e))
    if secs >= 1 then text = text .. " · " .. secs .. "s" end
    return text
end

-- A subset of `playstate.decide`, and deliberately never a larger one: this side may only refuse a
-- click the CLI would also refuse. Everything else is launched and the CLI decides, because it is
-- the side that can stop the other process.
local function play_pending()
    local e = play_entry
    if e and e.state == "running" and status_quiet(e) <= ABANDON_AFTER_S then return e end
    return nil
end

local function launching()
    return launched.at > 0 and (mp.get_time() - launched.at) < LAUNCH_GRACE_S
end

-- The click's own words, for its two seconds before the CLI's first write: the band's line, or the corner line's.
local function starting_text()
    return "Episode " .. launched.episode .. " — starting"
end

-- The viewer's choices while a play waits for TorBox's download of the original-audio release, or after
-- it held them: the entry's `choice`, written by the CLI on every running entry of the wait and
-- on the held one. Nil otherwise. A held entry has no process behind it and is never "in flight".
local function choice_entry()
    local e = play_entry
    if not (e and type(e.choice) == "table" and type(e.choice.actions) == "table") then return nil end
    if e.state == "held" then
        if os.time() - (tonumber(e.finishedAt) or 0) <= HELD_SHOW_S then return e end
        return nil
    end
    if e.state == "running" and status_quiet(e) <= ABANDON_AFTER_S then return e end
    return nil
end

-- Nothing is being watched: no file is loaded (`idle-active`), or the loaded one has ended (`eof-reached`). The player
-- runs `--keep-open=yes`, which "pause[s] the player" at the end instead of unloading the file (man mpv), so an episode
-- watched to its end — the one auto-next starts from — stays loaded on its last frame with `idle-active` false. Its
-- end is mpv's `eof-reached`; seeking back clears it (measured on mpv 0.41: true at the end, false at once after a
-- seek back, nil while a `loadfile … replace` swaps the file). `pause` is not part of it: at the moment
-- `eof-reached` turns true `pause` was still false (measured), and a paused episode mid-way is still being watched.
local function nothing_watched()
    return mp.get_property_native("idle-active") == true or mp.get_property_native("eof-reached") == true
end

-- A play the CLI runs in the background (the entry's `background`): it started while the viewer watched the
-- episode on this player, and nothing of it is drawn over that episode: no loading screen, no band line, not even a
-- failure's red line. Its one small corner line is all (`background_text`). Once nothing is watched (the episode ended:
-- auto-next's case) it is drawn as any play is, choices, keys and a failure's screen included.
local function behind_watched(e)
    return e ~= nil and e.background == true and not nothing_watched()
end

-- The sentence and one line per choice, in the CLI's own words: "d  Watch the English dub now …".
-- The key is the one bound (`choice_keys`); every choice the CLI writes has one. Once the viewer chose to keep waiting
-- the entry carries no sentence and no "Keep waiting", and the lines are what is still on offer.
local function choice_lines(e)
    local out = {}
    if not e then return out end
    if type(e.choice.headline) == "string" and e.choice.headline ~= "" then out[#out + 1] = e.choice.headline end
    for _, a in ipairs(e.choice.actions) do
        local label = tostring(a.label or "")
        if type(a.note) == "string" and a.note ~= "" then label = label .. " (" .. a.note .. ")" end
        local key = (type(a.key) == "string" and a.key:match("^%l$")) and a.key or nil
        out[#out + 1] = (key and (key .. ": ") or "") .. label
    end
    return out
end

local function poll_play_status()
    play_entry = read_json(play_status_path())
end

-- What to say on screen about the play, and in what colour. Empty when there is nothing to say.
--
-- This is drawn into the panel's **own** overlay rather than sent through `mp.osd_message`. The OSD
-- is sized by `osd-font-size` against the window, and a 28-43 s message at that size covers the
-- panel header and everything the user might want to read while they wait — tolerable for the old
-- 8 s line, not for one that is meant to stay up until the play lands. Our layer is at z=1001 in
-- the same 720-high canvas the rows are measured in, so the line sits where we put it and stays out
-- of the way. `mp.osd_message` is kept for the one-shot answers to a click, which is what it is for.
local function status_line()
    local chosen = choice_entry()
    -- The choices are drawn only while nothing is being watched, where their keys are bound (`sync_choice_keys`):
    -- over an earlier episode still playing the line is the plain one, as before; over one that has ended they are
    -- drawn as on an idle player.
    if chosen and not nothing_watched() then chosen = nil end
    if chosen then
        -- Waiting, or held: never red. A held play is not a failure and keeps its window (no retraction).
        local lines = choice_lines(chosen)
        table.insert(lines, 1, status_text(chosen))
        return table.concat(lines, "\n"), "&HFFFFFF&"
    end
    local inflight = play_pending()
    if inflight and not behind_watched(inflight) then return status_text(inflight), "&HFFFFFF&" end
    -- An episode that was ready while nobody watched loaded paused: its line ("Episode 3 is ready. Space plays it.")
    -- for as long as it stays paused.
    local e = play_entry
    if e and e.state == "done" and type(e.detail) == "table" and e.detail.readyPaused
       and mp.get_property_native("pause") == true then
        return tostring(e.message or ""), "&HFFFFFF&"
    end
    -- Over an episode being watched a click in the episodes menu says "starting" in the corner line instead
    -- (`background_text`).
    if launching() and nothing_watched() then
        return starting_text(), "&HFFFFFF&"
    end
    -- A failure holds the line for half a minute and then clears: it has been read by then, and a
    -- player that permanently advertises an old failure is its own bug. A play in the background that failed draws
    -- no line over the episode being watched (`behind_watched`).
    if play_entry and play_entry.state == "failed" and not behind_watched(play_entry)
       and (os.time() - (tonumber(play_entry.finishedAt) or 0)) < FAIL_HOLD_S then
        local text = status_text(play_entry)
        if play_entry.suggest and play_entry.suggest ~= "" then
            text = text .. " \194\183 " .. play_entry.suggest
        end
        -- With no indexer set up, where add-ons go (the hint) is part of the answer, as on the panel.
        local said = type(play_entry.say) == "table" and play_entry.say or {}
        if play_entry.failedStage == "indexers.none" and type(said.hint) == "string" and said.hint ~= "" then
            text = text .. " " .. said.hint
        end
        return text, "&H5050FF&"      -- ASS is &HBBGGRR&: red
    end
    return "", "&HFFFFFF&"
end

-- ----------------------------------------------------------------------- the loading screen
-- The window is up from claim time, so the 5-43 s resolve is spent looking at the show
-- rather than at a black rectangle. Size from the NAME: the buffer has no header, and the CLI is
-- the one place that decides the size (`thumbs.BACKDROP_W/H`) — so this file never carries a copy
-- of it and a change there cannot desynchronise this side. The byte count is still checked, since
-- `overlay-add` would happily draw a half-written or stale buffer as noise.
local function bgra_size_from_name(path)
    if type(path) ~= "string" then return nil end
    local w, h = path:match("@(%d+)x(%d+)%.bgra$")
    if not w then return nil end
    w, h = tonumber(w), tonumber(h)
    local f = io.open(path, "rb")
    if not f then return nil end
    local size = f:seek("end")
    f:close()
    if size ~= w * h * 4 then return nil end
    return w, h
end

local function clear_art()
    if not art_shown then return end
    pcall(mp.commandv, "overlay-remove", BACKDROP_ID)
    pcall(mp.commandv, "overlay-remove", LOGO_ID)
    art_shown = false
end

-- Draw (or redraw, on a resize) the backdrop and the logo for the play in flight. Reads only the
-- status entry: a never-played show has no episodes JSON at claim time, and draw()'s own early
-- return on `data == nil` must not stop this. Degrades to "no picture" on every failure
-- (nothing in a polish path may cost the user the episode, or even a redraw).
--
-- The positional `overlay-add` form is the verified one: `{"command": ["overlay-add", {…}]}` is
-- rejected by mpv 0.41 with `invalid parameter` every time, while
-- `overlay-add id x y file offset fmt w h stride dw dh` is accepted — including with every
-- argument stringified, which is what `mp.commandv` sends (measured).
local function draw_art()
    local e = play_entry
    -- This play's file is already on screen, whatever the entry still says. mpv's own
    -- event is the earlier truth here and the poll must not be allowed to undo it.
    if e and landed_trace and e.trace == landed_trace then clear_art() return end
    -- A play in the background never covers the episode being watched, failed or not.
    if behind_watched(e) then
        msg.debug("art: nothing to draw (a play in the background, over the episode being watched)")
        clear_art()
        return
    end
    -- A held play keeps its loading screen for as long as it holds the choices: it is not a failure, and
    -- nothing retracts its window. Only while nothing is being watched: never over an episode still playing, and over
    -- one that has ended (auto-next's case) as over an idle player.
    -- The two seconds after a click in the episodes menu count only while nothing is watched: over an episode being
    -- watched, the entry on disk is still another play's, whose picture must not cover it.
    local active = (play_pending() ~= nil or (launching() and nothing_watched())
                    or (choice_entry() ~= nil and nothing_watched()))
        or (e and e.state == "failed" and (os.time() - (tonumber(e.finishedAt) or 0)) < FAIL_HOLD_S)
    local art = active and e and type(e.art) == "table" and e.art or nil
    if not art then
        -- Every arm says what it decided and on what, so "why is there no picture" is
        -- answerable from `--msg-level=omarchy_anime_episodes=debug` alone.
        msg.debug("art: nothing to draw (active=" .. tostring(active)
                  .. " entry=" .. tostring(e ~= nil) .. " art=" .. type(e and e.art)
                  .. " nothing_watched=" .. tostring(nothing_watched()) .. ")")
        clear_art()
        return
    end
    local dim = mp.get_property_native("osd-dimensions") or {}
    if not (dim.w and dim.h and dim.h > 0) then
        msg.debug("art: no osd-dimensions yet (w=" .. tostring(dim.w) .. " h=" .. tostring(dim.h) .. ")")
        clear_art()
        return
    end
    local k = RES_Y / dim.h                        -- ASS units per pixel, same as scale()
    local band = LOADING_BAND + (choice_entry() and CHOICE_BAND or 0)
    local band_px = math.floor(band / k + 0.5)
    local dh = dim.h - band_px
    local any = false
    local bw, bh = bgra_size_from_name(art.backdrop)
    if bw and dh > 0 then
        -- `mp.commandv` returns nil plus an error string when mpv refuses the command, so both the
        -- pcall's own ok and the command's return are checked (the same rule draw_thumbs follows).
        local ok, res = pcall(mp.commandv, "overlay-add", BACKDROP_ID, 0, 0, art.backdrop, "0", "bgra",
                              bw, bh, bw * 4, dim.w, dh)
        if ok and res then
            any = true
        else
            msg.debug("art: backdrop refused (ok=" .. tostring(ok) .. " res=" .. tostring(res)
                      .. " " .. bw .. "x" .. bh .. " -> " .. dim.w .. "x" .. dh .. ")")
            pcall(mp.commandv, "overlay-remove", BACKDROP_ID)
        end
    else
        msg.debug("art: no usable backdrop buffer (path=" .. tostring(art.backdrop)
                  .. " dh=" .. tostring(dh) .. ")")
        pcall(mp.commandv, "overlay-remove", BACKDROP_ID)
    end
    local lw, lh = bgra_size_from_name(art.logo)
    if lw and dh > 0 then
        local dw = math.floor(dim.w / 3)
        local ldh = math.floor(dw * lh / lw)
        local x = math.floor((dim.w - dw) / 2)
        local y = math.floor((dh - ldh) / 2)
        local ok, res = pcall(mp.commandv, "overlay-add", LOGO_ID, x, y, art.logo, "0", "bgra",
                              lw, lh, lw * 4, dw, ldh)
        if ok and res then
            any = true
        else
            msg.debug("art: logo refused (ok=" .. tostring(ok) .. " res=" .. tostring(res)
                      .. " " .. lw .. "x" .. lh .. " -> " .. dw .. "x" .. ldh .. " at " .. x .. "," .. y .. ")")
            pcall(mp.commandv, "overlay-remove", LOGO_ID)
        end
    else
        msg.debug("art: no usable logo buffer (path=" .. tostring(art.logo)
                  .. " dh=" .. tostring(dh) .. ")")
        pcall(mp.commandv, "overlay-remove", LOGO_ID)
    end
    art_shown = any
end

-- The backdrop leaves the bottom band (LOADING_BAND, plus CHOICE_BAND with the choices) to the text, because bitmaps
-- draw above ASS. Over an idle player the band is black. Over a loaded file (an episode that ended under the wait, a
-- hidden window shown again) the file's own picture and subtitles showed through it: the backdrop covered the top
-- 61 % (1 - 284/720) and the old frame the rest. An opaque ASS box under the band's text covers it; it reaches
-- two units under the backdrop, which draws over that.
local band_drawn = false
local function band_wanted()
    return art_shown and mp.get_property_native("idle-active") == false
end

local function band_event(res_x)
    band_drawn = band_wanted()
    if not band_drawn then return nil end
    local band = LOADING_BAND + (choice_entry() and CHOICE_BAND or 0) + 2
    local w = math.ceil(res_x) + 2
    return string.format("{\\an7\\pos(0,%d)\\bord0\\shad0\\1c&H000000&\\1a&H00&\\p1}m 0 0 l %d 0 %d %d 0 %d{\\p0}",
                         RES_Y - band, w, w, band, band)
end

-- The show line for the band: "<title> — Episode N · <episode title>", or "" when nothing is known.
-- The isolate is added by the caller, not here: `sanitize` strips U+2068/U+2069 along with every
-- other bidi control, so an isolate written in at this end would be stripped again on its way to
-- the screen. Same order as draw()'s header — sanitize, truncate, then wrap.
local function art_line()
    local e = play_entry
    local art = e and type(e.art) == "table" and e.art or nil
    if not art then return "" end
    local head = (art.title and art.title ~= "") and art.title or (e.title or "")
    local line = (head ~= "" and (head .. " \226\128\148 ") or "") .. "Episode " .. ep_num(e)
    if art.episodeTitle and art.episodeTitle ~= "" then line = line .. " \194\183 " .. art.episodeTitle end
    return line
end

-- `escape-ass` is mpv's own: "Modify text so that commands and functions that interpret ASS tags …
-- will display it verbatim". A hand-rolled gsub was tried first and mangled braces and backslashes
-- on screen, which is the whole argument for using the command.
--
-- The result also carries the per-run Arabic font, which is why every drawn string goes
-- through here: the tags are added after escaping, or `escape-ass` would turn them into text.
local arabic_runs
local function esc(s)
    local ok, out = pcall(mp.command_native, { "escape-ass", tostring(s or "") })
    if not (ok and type(out) == "string") then out = tostring(s or ""):gsub("[{}\\]", "") end
    return arabic_runs(out)
end

-- `escape-ass` does NOT strip bidi control characters, and neither does mpv's own osd_mangle_ass.
-- A title carrying U+202E could otherwise unbalance our isolate and reorder the whole panel.
local function sanitize(s)
    s = tostring(s or "")
    s = s:gsub("\226\128\142", ""):gsub("\226\128\143", "")              -- U+200E, U+200F
    s = s:gsub("\226\128\170", ""):gsub("\226\128\171", "")              -- U+202A, U+202B
    s = s:gsub("\226\128\172", ""):gsub("\226\128\173", "")              -- U+202C, U+202D
    s = s:gsub("\226\128\174", "")                                        -- U+202E
    s = s:gsub("\226\129\166", ""):gsub("\226\129\167", "")              -- U+2066, U+2067
    s = s:gsub("\226\129\168", ""):gsub("\226\129\169", "")              -- U+2068, U+2069
    return s
end

-- ASS has no eliding, so a title that does not fit has to be cut before it is drawn. Byte-wise
-- truncation would split a UTF-8 sequence and libass would draw a replacement glyph, so the cut is
-- made on code-point boundaries (LuaJIT is 5.1: there is no utf8 library).
local function utf8_truncate(str, max_chars)
    local out, count, i = {}, 0, 1
    local n = #str
    while i <= n do
        local b = str:byte(i)
        local len = 1
        if b >= 240 then len = 4 elseif b >= 224 then len = 3 elseif b >= 192 then len = 2 end
        if count >= max_chars then
            return table.concat(out) .. "\226\128\166"      -- U+2026 HORIZONTAL ELLIPSIS
        end
        out[#out + 1] = str:sub(i, i + len - 1)
        count = count + 1
        i = i + len
    end
    return table.concat(out)
end


-- Code points, not bytes: the same reason utf8_truncate exists (LuaJIT is 5.1, there is no utf8
-- library and a byte count would break a multi-byte sequence).
local function utf8_len(str)
    local count, i, n = 0, 1, #str
    while i <= n do
        local b = str:byte(i)
        i = i + ((b >= 240 and 4) or (b >= 224 and 3) or (b >= 192 and 2) or 1)
        count = count + 1
    end
    return count
end

-- Greedy word wrap for the play's line. A failure carries a reason *and* the CLI's advice, and
-- cutting either at an ellipsis throws away the half that says what to do about it — which is
-- exactly the complaint about a play line that vanished too soon, one level down.
local function wrap_lines(text, budget, max_lines)
    local lines, current = {}, ""
    for word in tostring(text):gmatch("%S+") do
        local candidate = (current == "") and word or (current .. " " .. word)
        if utf8_len(candidate) <= budget or current == "" then
            current = candidate
        else
            lines[#lines + 1] = current
            current = word
            if #lines >= max_lines then break end
        end
    end
    if #lines < max_lines and current ~= "" then lines[#lines + 1] = current end
    -- The last line still has to fit: one very long word (a URL, a release name) is cut.
    lines[#lines] = utf8_truncate(lines[#lines] or "", budget)
    return lines
end

-- `wrap_lines` per line of `text` (the choices put one per line), within one `max_lines` for all of them.
-- Text without a line break wraps exactly as `wrap_lines` wraps it.
local function wrap_block(text, budget, max_lines)
    local out = {}
    for para in (tostring(text) .. "\n"):gmatch("([^\n]*)\n") do
        if #out >= max_lines then break end
        if para:find("%S") then
            for _, line in ipairs(wrap_lines(para, budget, max_lines - #out)) do out[#out + 1] = line end
        end
    end
    return out
end

local function clamp(i, n)
    if n <= 0 then return 1 end
    if i < 1 then return 1 end
    if i > n then return n end
    return i
end

local function window(n)
    local rows = math.max(3, math.floor(tonumber(opts.rows) or 12))
    if n <= rows then return 1, n end
    top = clamp(top, n - rows + 1)
    if not free_top then
        if cursor < top then top = cursor end
        if cursor > top + rows - 1 then top = cursor - rows + 1 end
    end
    top = clamp(top, n - rows + 1)
    return top, math.min(n, top + rows - 1)
end

local function font_tag()
    if opts.font == "" then return "" end
    return "{\\fn" .. opts.font .. "}"
end

-- The family Arabic runs are drawn in: `opts.arabic_font` once `fc-match` has confirmed it
-- is installed, else "" and nothing is wrapped. Decided once, at load (see the end of this file).
local arabic_font = ""

local function is_arabic(cp)
    return (cp >= 0x0600 and cp <= 0x06FF) or (cp >= 0x0750 and cp <= 0x077F)
        or (cp >= 0x0870 and cp <= 0x08FF) or (cp >= 0xFB50 and cp <= 0xFDFF)
        or (cp >= 0xFE70 and cp <= 0xFEFF)
end

-- Allowed inside a run, between two Arabic letters: a space, ZWNJ, ZWJ. A run never stops mid-word,
-- because the font change would break the letters' joining; it ends on its last Arabic code point.
local function joins_run(cp)
    return cp == 0x20 or cp == 0x200C or cp == 0x200D
end

-- Wrap each Arabic run of already-escaped text in `{\fn<arabic_font>}` … and close it back to the
-- panel's own font (`{\fn}` is the style's, i.e. osd-font; a hand-set `opts.font` is restored).
-- Every tag in this file is ASCII, so the walk never lands inside one of ours.
arabic_runs = function(s)
    if arabic_font == "" or not s:find("[\216-\221\224\239]") then return s end
    local out, i, n = {}, 1, #s
    local open_at, last_end = nil, nil   -- byte where the current run starts / its last Arabic byte
    local close = (opts.font ~= "") and font_tag() or "{\\fn}"
    while i <= n do
        local b = s:byte(i)
        local len = (b >= 240 and 4) or (b >= 224 and 3) or (b >= 192 and 2) or 1
        local cp = b
        if len == 2 then
            cp = (b % 32) * 64 + (s:byte(i + 1) or 128) % 64
        elseif len == 3 then
            cp = (b % 16) * 4096 + ((s:byte(i + 1) or 128) % 64) * 64 + (s:byte(i + 2) or 128) % 64
        elseif len == 4 then
            cp = 0x10000   -- outside every range this cares about
        end
        if is_arabic(cp) then
            if not open_at then open_at = i end
            last_end = i + len - 1
        elseif open_at and not joins_run(cp) then
            out[#out + 1] = "{\\fn" .. arabic_font .. "}" .. s:sub(open_at, last_end) .. close
            out[#out + 1] = s:sub(last_end + 1, i - 1)
            open_at = nil
        end
        if not open_at then out[#out + 1] = s:sub(i, i + len - 1) end
        i = i + len
    end
    if open_at then
        out[#out + 1] = "{\\fn" .. arabic_font .. "}" .. s:sub(open_at, last_end) .. close
        out[#out + 1] = s:sub(last_end + 1)
    end
    return table.concat(out)
end

-- The decision, what it rested on and how long the call took, for the load line.
-- One of three copies of the installed-font rule, with bin/omarchy-anime-player (the subtitle font
-- at launch) and player.font_installed (the subtitle font per play); this one is kept apart because
-- a script loaded over IPC gets no script-opts.
local function detect_arabic_font()
    local want = tostring(opts.arabic_font or "")
    if want == "" then return "", "off" end
    local t0 = mp.get_time()
    local ok, res = pcall(mp.command_native, {
        name = "subprocess", playback_only = false, capture_stdout = true, capture_stderr = true,
        args = { "fc-match", "-f", "%{family}", want },
    })
    local ms = string.format("%d ms", math.floor((mp.get_time() - t0) * 1000 + 0.5))
    if not (ok and type(res) == "table" and res.status == 0) then
        -- `error_string` is "" when fc-match ran and exited non-zero, so the status carries the reason.
        local status = (ok and type(res) == "table") and tostring(res.status) or "none"
        local err = (ok and type(res) == "table") and tostring(res.error_string or "") or tostring(res)
        return "", "fc-match failed: status=" .. status .. " error='" .. err .. "', " .. ms
    end
    local got = tostring(res.stdout or "")
    -- fontconfig always answers with SOME family, so installed means the family it returns
    -- contains the one asked for.
    if got:lower():find(want:lower(), 1, true) then return want, "installed, " .. ms end
    return "", "not installed, fc-match gave '" .. got .. "', " .. ms
end

-- mouse-pos is in `osd-dimensions` space; the canvas is 720 high with res_x = 0, which libass
-- derives as 720 * w/h — so res_x/w and res_y/h are the same number and ONE factor converts both
-- axes. Its inverse puts a rectangle back into VO pixels for `set_mouse_area`.
local function scale()
    local dim = mp.get_property_native("osd-dimensions")
    if not dim or not dim.h or dim.h <= 0 then return nil end
    return RES_Y / dim.h
end

-- The pointer in ASS units, or nil: `hover` false means x/y are stale and the manual says to
-- ignore them (measured: false under --vo=null, true on a real window).
local function pointer_ass()
    local k = scale()
    if not k then return nil end
    local pos = mp.get_property_native("mouse-pos")
    if not pos or not pos.hover or not pos.x or not pos.y then return nil end
    return pos.x * k, pos.y * k
end

-- The episode index under an ASS-space point. Episode rows and text lines no longer
-- share one pitch, so this is a scan over each line's own top (`geom.line_y`) rather than one
-- floor() division. `geom.thumb_left`, not `geom.x`: a row's thumbnail sits left of the text
-- column, and a point over the picture must still hit-test to that row --
-- it is `geom.x` itself whenever no picture is drawn there, so a thumbnail-less
-- panel is unaffected.
local function row_under(ax, ay)
    if not shown or geom.row_h <= 0 then return nil end
    if ax < geom.thumb_left or ax > geom.right or ay < geom.top then return nil end
    for li = 1, geom.lines do
        local y0 = geom.line_y[li]
        local h = geom.rows[li] and geom.row_h or geom.text_h
        if ay >= y0 and ay < y0 + h then return geom.rows[li] end
    end
    return nil
end

-- Restrict the forced mouse bindings to the panel (or the ☰) so everything outside stays mpv's:
-- a click on the video still pauses, the wheel over it still changes the volume. Sections are
-- created with a full-screen area (`input/input.c:408-409`), so this must be set, not left alone.
-- This is *why* a thumbnail drawn left of `geom.x` was a wheel and
-- click dead zone before this fix -- anything outside this rectangle falls through to mpv's own
-- default section, and a picture out there was outside it by construction.
local function scope_mouse()
    if not bound and not tab_bound then return end
    local k = scale()
    if not k then return end
    local r
    if shown then
        r = { geom.thumb_left, 0, geom.right,
              math.min(RES_Y - OSC_BAND, geom.bottom) }
    elseif geom.tab then
        r = geom.tab
    end
    if not r then
        mp.set_mouse_area(0, 0, 0, 0, SECTION)
        return
    end
    mp.set_mouse_area(math.floor(r[1] / k), math.floor(r[2] / k),
                      math.ceil(r[3] / k), math.ceil(r[4] / k), SECTION)
end

-- Where the play's block goes. At the bottom, above the OSC band, it sat on the subtitle's
-- first line whenever a file was on screen. `sub-margin-y` cannot make room: it moves only unstyled
-- text subtitles (man mpv), and anime tracks are mostly ASS; `sub-pos` would move their signs too.
-- So the block moves instead: top-left while a file is on screen, just under the line mpv's own OSD
-- messages use (top-left too: "Switching to episode 3", volume, seeks). The bottom band stays for
-- the loading screen (art_shown: the backdrop covers everything above it) and an idle player, where
-- no subtitle is drawn. Both OSD values are in the same 720-high units as this canvas.
local status_placed = ""   -- the last placement logged, so the line is written once per change

-- Returns the y and the OSD values it came from, so the placement log can show its working.
local function status_top_y()
    -- Defaults are man mpv's (16, 30, 1.0), for a property that does not answer.
    local margin = tonumber(mp.get_property_native("osd-margin-y")) or 16
    local font = tonumber(mp.get_property_native("osd-font-size")) or 30
    local sc = tonumber(mp.get_property_native("osd-scale")) or 1
    return math.floor(margin + font * sc * OSD_LINE_H + 0.5), margin, font, sc
end

-- The canvas's width: `res_x = 0` makes libass derive PlayResX from res_y and the window's aspect (`draw`), 16:9
-- until mpv answers `osd-dimensions`.
local function canvas_res_x()
    local dim = mp.get_property_native("osd-dimensions") or {}
    return RES_Y * ((dim.w and dim.h and dim.h > 0) and (dim.w / dim.h) or (16 / 9))
end

-- The panel's font size, the `size` option clamped.
local function panel_size()
    return math.max(10, math.min(40, tonumber(opts.size) or 20))
end

-- The play's line, in the same canvas the panel rows are measured in. Returned as an ASS event so
-- both branches of draw() can append it. At the top, `right` is where the free width ends and
-- `edge` names what set it: the open panel's thumbnails or its text column (the block would run
-- under either), or the ☰'s strip (kept free so the block does not re-wrap on hover).
local function status_event(res_x, right, edge)
    local text, colour = status_line()
    last_status = text
    if text == "" then return nil end
    local idle = mp.get_property_native("idle-active")
    -- Plain idle, not `nothing_watched`: the line moves to the top only to keep off a file's subtitles. With the art
    -- drawn (an idle player's, or one over an episode that has ended) it sits in the band as on an idle player; an
    -- ended file with no art still shows its last frame and any subtitle on it, so the line keeps to the top there.
    local on_top = (not art_shown) and idle == false
    local size = panel_size()
    local width = on_top and math.min(res_x, right or res_x) or res_x
    local budget = math.max(16, math.floor((width - 32) / (size * 0.52)))
    -- `\N` is ASS's hard line break; with `an1` the block is anchored bottom-left, so extra lines
    -- grow upward into empty video rather than down into the OSC.
    -- At the top the width can stop at the open panel, and there is room to hang more lines, so
    -- the advice is not cut; the band has room for three.
    local max_lines = on_top and 6 or 3
    if not on_top and choice_entry() then max_lines = max_lines + CHOICE_LINES end
    local y, margin, font, sc = RES_Y - OSC_BAND, nil, nil, nil
    if on_top then y, margin, font, sc = status_top_y() end
    -- Once per change: where the block went, the numbers that put it there and what they
    -- were computed from.
    local placed = on_top
        and string.format("top y=%d (osd-margin-y=%s osd-font-size=%s osd-scale=%s) right=%d (%s)"
                          .. " budget=%d max_lines=%d", y, tostring(margin), tostring(font), tostring(sc),
                          math.floor(width), edge or "canvas", budget, max_lines)
        or string.format("band y=%d budget=%d max_lines=%d", y, budget, max_lines)
    placed = placed .. " (idle-active=" .. tostring(idle) .. " eof-reached="
             .. tostring(mp.get_property_native("eof-reached")) .. " art_shown=" .. tostring(art_shown) .. ")"
    if placed ~= status_placed then
        status_placed = placed
        msg.debug("status block: " .. placed)
    end
    local wrapped = wrap_block(sanitize(text), budget, max_lines)
    for i = 1, #wrapped do wrapped[i] = LRM .. esc(wrapped[i]) end
    -- The show line sits above the status line, inside the band the backdrop leaves.
    -- Inserted *after* the escaping loop and with its tags outside `esc`, the way the header at
    -- draw() does it: `escape-ass` escapes every brace and follows every backslash with a word
    -- joiner (measured on this player: "{\b1}x{\b0}" comes back with both braces backslashed and
    -- U+2060 after each backslash), so a tag passed through it draws as literal text.
    -- LRM fixes the line's base direction and FSI…PDI isolates the untrusted show title, exactly as
    -- the panel header does it and for the same reason (mpv sets Encoding=-1 on every OSD style, so
    -- a title whose first strong character is RTL flips the line it is on). The pair is added
    -- *after* sanitize and truncate: sanitize strips these very code points, and a truncation
    -- inside the pair would drop the PDI and unbalance the whole block.
    local head = art_shown and art_line() or ""
    if head ~= "" then
        table.insert(wrapped, 1,
                     "{\\b1}" .. LRM .. FSI .. esc(utf8_truncate(sanitize(head), budget)) .. PDI .. "{\\b0}")
    end
    -- `an7` hangs the block down from the top; `an1` grows it upward from the band.
    local anchor = on_top and ("\\an7\\pos(16," .. y .. ")") or ("\\an1\\pos(16," .. y .. ")")
    return "{" .. anchor .. "\\fs" .. size .. "\\bord2\\shad0\\1c" .. colour .. "}"
           .. font_tag() .. table.concat(wrapped, "\\N")
end

-- A cell's `thumb` is only ever a path (or absent/""); the file itself can still be missing, half
-- written by a concurrent `ensure()`, or the wrong size (a stale buffer from before THUMB_W/THUMB_H
-- changed). `overlay-add` has no way to report any of that back — it would just draw noise or
-- nothing — so this is checked here, and a row that fails it renders exactly as a thumbnail-less
-- row does (no failure in a polish path may fail a play, or even a redraw).
local function thumb_file_ok(path)
    -- `io.open` raises on a non-string argument, and this call sits between `overlay:update()`
    -- and `scope_mouse()` in `draw()` -- an uncaught error here would also skip mouse-area
    -- scoping, and a forced section defaults to a full-screen area (`unbind`'s own comment calls
    -- that this file's one real blast radius). A malformed sidecar must not reach this far, but
    -- checking costs one comparison.
    if type(path) ~= "string" or path == "" then return false end
    local f = io.open(path, "rb")
    if not f then return false end
    local size = f:seek("end")
    f:close()
    return size == THUMB_W * THUMB_H * 4
end

-- Every overlay id this script has ever allocated, gone. Called on every path that must leave
-- nothing behind: hide(), the closed-panel branch of draw(), and shutdown.
local function clear_thumbs()
    for slot in pairs(thumb_ids) do
        pcall(mp.commandv, "overlay-remove", THUMB_BASE_ID + slot - 1)
    end
    thumb_ids = {}
end

-- The picture layer. `eps` is the full episode array; `lines`/`rows` are draw()'s own text-layout
-- arrays (rows[li] is the episode index for a text row, nil for the header/scroll-indicator/hint
-- lines) — reusing them rather than recomputing which lines are episode rows is what keeps a
-- thumbnail's row and its text row from ever disagreeing about which line they're on. `x`/`ys`/
-- `row_h` are the text column's own geometry in RES_Y units (`ys[li]` each line's own top, since
-- an episode row and a text line are no longer the same height), so a thumbnail sits
-- flush against the row it belongs to; `scale()` (the same factor `pointer_ass`/`scope_mouse`
-- already trust for the opposite conversion) turns that into screen pixels (measured).
--
-- A slot is a row's *position in the visible window* (1st visible episode row, 2nd, ...), not the
-- episode number: ids stay small and stable across a redraw instead of growing with the episode
-- count, and "reuse ids across redraws, remove one only when its row leaves the
-- window" falls out of comparing this frame's `active` set against `thumb_ids` from the last one.
--
-- Every row shares one `row_h`, so every picture this call might draw is the same size — computed
-- once, not per row. Returns the ASS-space left edge of that picture when at least one was
-- actually drawn this frame, else `x` unchanged: this is `draw()`'s `geom.thumb_left`, the left
-- bound `scope_mouse`/`row_under` hit-test against, so a picture is never a wheel/click dead zone
-- and a panel with no pictures drawn is bit-for-bit the same
-- rectangle it always was.
-- The picture column's geometry for a row height and text column: display size in pixels and the
-- ASS-space left edge. Shared with draw(), which stops the play's block before it.
local function thumb_geometry(k, row_h, x)
    -- One row tall, native aspect ratio — the same `k` converts both axes (measured),
    -- so the aspect ratio survives the conversion unchanged.
    local disp_h = math.floor(row_h / k + 0.5)
    local disp_w = math.floor(disp_h * THUMB_W / THUMB_H + 0.5)
    return disp_w, disp_h, x - THUMB_GAP - disp_w * k
end

local function draw_thumbs(eps, lines, rows, ys, row_h, x)
    local k = scale()
    if not k or k <= 0 then clear_thumbs() return x end
    local disp_w, disp_h, ass_left = thumb_geometry(k, row_h, x)
    -- Not enough room to the left of the text column (a very wide panel, or a narrow window):
    -- degrade every row to thumbnail-less rather than draw any of them off-screen.
    local fits = disp_w > 0 and disp_h > 0 and ass_left >= 0
    local px_x = fits and math.floor(ass_left / k + 0.5) or 0
    local active, slot, any_active = {}, 0, false
    for li = 1, #lines do
        local i = rows[li]
        if i then
            slot = slot + 1
            if slot <= THUMB_MAX_SLOTS then
                local id = THUMB_BASE_ID + slot - 1
                local ep = eps[i]
                -- The user's rule -- every unwatched picture is blurred, on every surface.
                local path = ep and ((ep.state == "watched") and ep.thumb or ep.blurThumb)
                if fits and thumb_file_ok(path) then
                    local px_y = math.floor(ys[li] / k + 0.5)
                    -- `mp.commandv` doesn't throw on a command failure, it returns `nil, error`
                    -- (manual, `:19288`) -- `pcall`'s own boolean is only ever true here, so the
                    -- command's *own* return has to be checked too, or a rejected overlay-add
                    -- (e.g. a path mmap fails) would still be recorded as a live thumbnail.
                    local pok, res = pcall(mp.commandv, "overlay-add", id, px_x, px_y, path, "0",
                                           "bgra", THUMB_W, THUMB_H, THUMB_W * 4, disp_w, disp_h)
                    if pok and res then
                        thumb_ids[slot] = i
                        active[slot] = true
                        any_active = true
                    end
                end
            end
        end
    end
    for existing_slot in pairs(thumb_ids) do
        if not active[existing_slot] then
            pcall(mp.commandv, "overlay-remove", THUMB_BASE_ID + existing_slot - 1)
            thumb_ids[existing_slot] = nil
        end
    end
    return any_active and ass_left or x
end

-- ------------------------------------------------------------------ the background play's line
-- A play in the background keeps one small line in the top-left corner, from the menu click to its switch or its end,
-- so the viewer is never left with a silent player: three of three recorded busy plays were 17-47 s of nothing. It
-- says what Browse and the popout say: `status_text` (playstate.render's sentence, its counter included), a ready
-- play's own `message`, or a failure's sentences as the popout has them (`failure_sentences`). One line at 80 % of the
-- panel's size (the hint's size), where the play's block goes over a file (`status_top_y`: under mpv's own OSD line,
-- clear of the subtitles' band), on an ass-events overlay of its own: never `mp.osd_message`, the channel seeks and
-- volume use, and never the panel's overlay, whose every redraw re-adds the thumbnails. Its words move once a second;
-- the tick, at 0.5 s while the line shows (`tick_status`), redraws it then and only then.
--
-- How long a running play is believed: playstate.SILENT_AFTER_S, not ABANDON_AFTER_S. The search writes once before
-- its indexers and again after them (up to their 55 s deadline; 18.9 s recorded), so at 20 s the line would go blank
-- mid-search. Display only: the click's gate (`play_pending`) keeps its 20 s.
local BACKGROUND_TRUST_S = 60
local line_overlay = nil
local line_drawn = ""      -- the event as last sent, so a tick costs a string compare
local line_text = ""       -- its words, for `dump`
local line_sig = ""        -- what the line showed, last logged: one log line per state, not one per second
local line_placed = ""     -- where it went and whether it was cut, last logged: once per change
local line_quit = false    -- mpv's `shutdown`: the player quits, and the line with it
-- The end already on disk when the file on screen started (`trace|finishedAt`): an ended play's line (ready, held,
-- failed) belongs to the file it ended over, so it never comes back on the next episode, nor after a player restart
-- (whose file starts anew). An end and the next file can fall in the same second, so this is the end itself, not a
-- time. "" until a `start-file`: a script loaded over IPC into a playing file takes the file as it finds it.
local end_before_file = ""

local function end_key(e)
    return tostring(e and e.trace) .. "|" .. tostring(e and e.finishedAt)
end

-- A failure in the popout's words, `ServiceLogic.errorSentences` read the same way: the CLI's `say` headline, else the
-- error (else the message); its action, else the half of `suggest` before " · " (`errorAction`). The popout puts them
-- on two lines; here they share the one, the action only when it fits (`draw_line`). The hint stays the popout's.
local function failure_sentences(e)
    local said = type(e.say) == "table" and e.say or {}
    local headline = said_or(said.headline, said_or(e.error, said_or(e.message, "the play failed")))
    local suggest = said_or(e.suggest, "")
    local cut = suggest:find(" \194\183 ", 1, true)
    return headline, said_or(said.action, cut and suggest:sub(1, cut - 1) or suggest)
end

-- What the line says, why (one word per rule, for the log), what that rule measured, and a failure's action (said only
-- if it fits).
local function background_text()
    local e = play_entry
    if line_quit then return "", "quit", "", "" end
    -- The click's two seconds, until the CLI's first write: the entry on disk is still another play's.
    if launching() and not nothing_watched() and not (e and e.trace == launched.trace) then
        return starting_text(), "launch", "", ""
    end
    if not (e and e.background == true) then return "", "not-background", "", "" end
    -- A stop idles the player; an episode that ended is drawn as any play is: either way nothing is watched.
    if nothing_watched() then return "", "nothing-watched", "", "" end
    if e.state == "running" then
        local quiet = status_quiet(e)
        if quiet > BACKGROUND_TRUST_S then
            return "", "quiet", "quiet=" .. quiet .. "s over " .. BACKGROUND_TRUST_S .. "s", ""
        end
        return status_text(e), "running", "", ""
    end
    -- An end: ready, held or failed, shown for FAIL_HOLD_S after it, the hold a failure has everywhere in this file.
    if end_key(e) == end_before_file then return "", "ended-before-file", end_before_file, "" end
    local age = os.time() - (tonumber(e.finishedAt) or 0)
    if age >= FAIL_HOLD_S then return "", "hold-over", "age=" .. age .. "s of " .. FAIL_HOLD_S .. "s", "" end
    if e.state == "later" and type(e.detail) == "table" and e.detail.readyInBackground == true then
        return tostring(e.message or ""), "ready", "", ""
    end
    if e.state == "failed" then
        local headline, action = failure_sentences(e)
        return "Episode " .. ep_num(e) .. " failed — " .. headline, "failed", "", action
    end
    if e.state == "held" then return status_text(e), "held", "", "" end
    -- "Tell me when it's ready", pressed in Browse: Browse said so; the play has ended.
    return "", "told", "state=" .. tostring(e.state), ""
end

-- One line. Too long for the width, the sentence is cut with an ellipsis and its counter kept, so it still visibly
-- moves; no word is ever replaced by a shorter one of this file's own.
local function fit_line(text, budget)
    if utf8_len(text) <= budget then return text end
    local head, counter = text:match("^(.-)( \194\183 %d+s)$")
    if not head then head, counter = text, "" end
    return utf8_truncate(head, math.max(4, budget - utf8_len(counter) - 1)) .. counter
end

-- Each decision in the plugin's own log too, under the play's trace (`omarchy-anime trace <id>`): mpv runs with
-- --no-terminal and its output goes to /dev/null, so the `msg.info` beside it reaches no one. Async, and
-- detached: the player never waits on it, and mpv's exit (`quit` is a decision too) does not kill it. The click's two
-- seconds (`launch`) go under the trace the click gave the play: the entry on disk is still another play's. The rule
-- is the line's result too, so `omarchy-anime trace` says it without `--json`.
local function log_line(shown, why, measured, e, placed)
    local trace = why == "launch" and launched.trace or tostring(e.trace or "")
    if opts.bin == "" or trace == "" then return end
    local data = { why = why, state = e.state, background = e.background, nothing_watched = nothing_watched(),
                   launching = launching(), placed = placed }
    if measured ~= "" then data.measured = measured end
    if e.trace ~= nil and tostring(e.trace) ~= trace then data.entry_trace = tostring(e.trace) end
    local json, err = utils.format_json(data)
    if not json then
        msg.warn("background line not logged: " .. tostring(err))
        return
    end
    mp.command_native_async(cli_command({
        detach = true,
        head = { opts.bin, "--trace", trace, "log-event" },
        words = { "player.line", shown and "shown" or "none", "--result", why, "--data", json },
    }), function(success, result, error)
        if not success or (type(result) == "table" and (tonumber(result.status) or 0) < 0) then
            msg.warn("background line log-event failed trace=" .. trace .. " error=" .. tostring(error)
                     .. " status=" .. tostring(type(result) == "table" and result.status))
        end
    end)
end

local function draw_line()
    if not line_overlay then return end
    local text, why, measured, action = background_text()
    local event, place = "", nil
    if text ~= "" then
        local res_x = canvas_res_x()
        -- It stops where the open panel's pictures (or text) begin, else short of the ☰'s strip, as the play's block.
        local open = shown and geom.thumb_left > 0
        local right = open and (geom.thumb_left - 16) or (math.floor(res_x) - TAB_W - TAB_GAP)
        local size = math.max(8, math.floor(panel_size() * 0.8))
        local budget = math.max(12, math.floor((right - 32) / (size * 0.52)))
        local y = status_top_y()
        text = sanitize(text)
        local said_action = "none"
        if action ~= "" then
            local both = text .. " " .. sanitize(action)
            said_action = utf8_len(both) <= budget and "kept" or "dropped (does not fit)"
            if said_action == "kept" then text = both end
        end
        local fitted = fit_line(text, budget)
        event = "{\\an7\\pos(16," .. y .. ")\\fs" .. size .. "\\bord2\\shad0\\1c&HFFFFFF&}" .. font_tag()
                .. LRM .. esc(fitted)
        -- Once per change: where it went, the width that set its budget, and whether it was cut.
        local placed = string.format("y=%d right=%d (%s) size=%d budget=%d action=%s ", y, math.floor(right),
                                     open and "panel" or "tab strip", size, budget, said_action)
                       .. (fitted == text and "cut=no" or ("cut=yes (" .. utf8_len(text) .. " to " .. budget .. ")"))
        if placed ~= line_placed then
            line_placed = placed
            msg.debug("background line placed: " .. placed)
        end
        place = { y = y, right = math.floor(right), edge = open and "panel" or "tab strip", size = size, budget = budget,
                  action = said_action, cut = fitted ~= text }
    end
    local was = line_text ~= ""
    line_text = text
    local e = play_entry or {}
    local sig = why .. "|" .. tostring(e.trace) .. "|" .. tostring(e.state)
    if sig ~= line_sig and (text ~= "" or was) then
        line_sig = sig
        msg.info("background line: " .. (text ~= "" and "shown" or "none") .. " (" .. why
                 .. (measured ~= "" and (" " .. measured) or "") .. ", trace="
                 .. tostring(e.trace) .. " state=" .. tostring(e.state) .. " background=" .. tostring(e.background)
                 .. " nothing_watched=" .. tostring(nothing_watched()) .. " launching=" .. tostring(launching()) .. ")")
        log_line(text ~= "", why, measured, e, place)
    end
    if event == line_drawn then return end
    line_drawn = event
    line_overlay.res_y, line_overlay.res_x, line_overlay.z = RES_Y, 0, PANEL_Z
    line_overlay.data = event
    line_overlay:update()
end

local function draw()
    if not overlay then return end
    redraws = redraws + 1
    if not shown or not data then
        -- Closed (or no show loaded yet): nothing but the ☰ way-in, and only while the pointer is
        -- over the window — and definitely no picture left over from when it was open.
        clear_thumbs()
        local rx = canvas_res_x()
        geom.rows, geom.close, geom.tab, geom.thumb_left = {}, nil, nil, 0
        local closed = {}
        local band = band_event(rx)
        if band then closed[#closed + 1] = band end
        if tab_shown then
            local x0 = math.floor(rx) - TAB_W
            geom.tab = { x0, 8, math.floor(rx) - 4, 44 }
            closed[#closed + 1] = "{\\an7\\pos(" .. (x0 + 8) .. ",12)\\fs24\\bord2\\shad0\\1c&HFFFFFF&}"
                                  .. font_tag() .. "\226\152\176"
        end
        -- A play started while the panel is closed — from the popout, or from a click that then
        -- hid it — still has to be visible. This layer exists either way.
        -- The ☰'s strip is kept free whether or not it is showing, so the block does not re-wrap
        -- every time the pointer enters or leaves the window.
        local sl = status_event(rx, math.floor(rx) - TAB_W - TAB_GAP, "tab strip")
        if sl then closed[#closed + 1] = sl end
        overlay.data = table.concat(closed, "\n")
        overlay.res_y, overlay.res_x, overlay.z = RES_Y, 0, PANEL_Z
        overlay:update()
        draw_line()          -- its width follows the panel's
        scope_mouse()
        return
    end
    local eps = data.episodes
    local n = #eps
    local first, last = window(n)
    local lines = {}
    -- How many characters fit across the panel. `res_x = 0` makes libass derive PlayResX from
    -- res_y and the **window** aspect (that is what keeps an ASS pixel square), so the right-hand
    -- edge is `res_y * w/h` of the current window, not 1280 — measured: a 930x1003 window derives
    -- 668, and a panel positioned from a hardcoded 16:9 drew off-screen and showed nothing at all.
    local size = panel_size()
    local width = math.max(10, math.min(60, tonumber(opts.width) or 32))
    local res_x = canvas_res_x()
    -- 0.52 em is a rough average advance for the OSD's proportional face; an ellipsis is cheaper
    -- than a row that runs off the edge either way.
    local budget = math.max(8, math.floor((res_x * width / 100 - 16) / (size * 0.52)))
    -- three characters narrower than the rest: the ✕ sits in that corner.
    local header = esc(utf8_truncate(sanitize(data.title ~= "" and data.title or "Episodes"), math.max(4, budget - 3)))
    lines[#lines + 1] = { text = "{\\b1\\1c&HFFFFFF&}" .. LRM .. FSI .. header .. PDI .. "{\\b0}" }
    if first > 1 then
        lines[#lines + 1] = { text = "{\\1c&H808080&}" .. LRM .. "\226\150\178 " .. (first - 1) .. " above" }
    end
    for i = first, last do
        local ep = eps[i]
        local state = ep.state or "aired"
        local mark = MARK[state] or " "
        local prefix = string.format("%s %3d  ", mark, ep.episode)
        -- `tonumber` is a safe no-op across every shape `runtime` arrives in: absent, JSON `null`
        -- (also `nil` after `parse_json`), a plain number, or the rare numeric string -- never an
        -- error (the row is "thumbnail, episode number, title, runtime, state", and `runtime` was
        -- once the one of those never actually drawn).
        local runtime = tonumber(ep.runtime)
        -- The middot matches the hint line's own separator style below. Outside FSI…PDI: it is
        -- always-LTR literal text of our own, not untrusted, so it needs no isolate.
        local suffix = runtime and (" \194\183 " .. math.floor(runtime) .. "m") or ""
        local title = utf8_truncate(ep.title and sanitize(ep.title)
                                    or ("Episode " .. tostring(ep.episode)),
                                    math.max(4, budget - 7 - utf8_len(suffix)))
        -- LRM fixes the line's base direction, FSI…PDI isolates the untrusted title: without them a
        -- line whose first strong character is Arabic flips as a whole and the columns stop lining
        -- up (mpv sets Encoding=-1 on every OSD style, i.e. FRIBIDI_PAR_ON, and no mpv option
        -- reaches the OSD's ASS_Library — upstream mpv#17605).
        local body = LRM .. esc(prefix) .. FSI .. esc(title) .. PDI .. suffix
        if i == cursor then
            lines[#lines + 1] = { ep = i, text = "{\\b1\\1c" .. (COLOUR[state] or COLOUR.aired) .. "}" .. body .. "{\\b0}" }
        else
            lines[#lines + 1] = { ep = i, text = "{\\1c" .. (COLOUR[state] or COLOUR.aired) .. "}" .. body }
        end
    end
    if last < n then
        lines[#lines + 1] = { text = "{\\1c&H808080&}" .. LRM .. "\226\150\188 " .. (n - last) .. " below" }
    end
    local hint = "Enter or click plays \194\183 n next \194\183 e find \194\183 Esc"
    -- The hint draws at 80 % of body size, so it fits ~25 % more characters than `budget`.
    lines[#lines + 1] = { text = "{\\1c&H808080&\\fs" .. math.floor(size * 0.8) .. "}" .. LRM
                                 .. esc(utf8_truncate(hint, math.floor(budget * 1.25))) }

    -- Docked right; `an7` is top-left-anchored, so x is where the panel starts. Each line is its
    -- own `\pos`-ed event rather than one event with `\N`, which is what makes per-line geometry
    -- (`ys` below) known by construction instead of guessed at from libass's metrics. The manual:
    -- "it's better to put multiple lines into data, instead of adding multiple OSD overlays".
    local x = math.floor(res_x * (100 - width) / 100)
    -- The font's own line height, or THUMB_ROW_H when this show has a thumbnail to fit --
    -- never the font size multiplied up just to make more room for a picture (that is what
    -- once shrank every title). Checked across the whole episode list, not just the
    -- rows currently in `first..last`: a per-visible-window check would make the pitch jump as
    -- the user scrolls past episodes with and without a thumbnail, which is worse than a few
    -- pixels of unused space under a row whose particular episode has none this frame.
    local text_row_h = math.max(8, math.floor(size * 1.3))
    local any_thumb = false
    for _, e in ipairs(eps) do
        if (type(e.thumb) == "string" and e.thumb ~= "") or (type(e.blurThumb) == "string" and e.blurThumb ~= "") then any_thumb = true break end
    end
    local row_h = any_thumb and math.max(text_row_h, THUMB_ROW_H) or text_row_h
    -- An episode row (`lines[li].ep` set) is `row_h` tall, but the header, the scroll
    -- indicators and the hint are ordinary text and stay `text_row_h` -- charging every line at
    -- `row_h` pushed the hint (and, with both "N above"/"N below" showing, the last row too) past
    -- the OSC band and even off the 720-unit canvas. `ys[li]` is each line's own top, walked down
    -- from TOP_Y once; `y` after the loop is the panel's own lowest pixel (`geom.bottom`).
    local events, rows, ys = {}, {}, {}
    local y = TOP_Y
    for li = 1, #lines do
        ys[li] = y
        y = y + (lines[li].ep and row_h or text_row_h)
    end
    for li = 1, #lines do
        events[li] = "{\\an7\\pos(" .. x .. "," .. ys[li] .. ")\\fs" .. size
                     .. "\\bord1\\shad0}" .. font_tag() .. lines[li].text
        rows[li] = lines[li].ep
    end
    -- The visible way out, on the same hit test as the rows. Esc and Tab keep working.
    local right = math.floor(res_x)
    events[#events + 1] = "{\\an7\\pos(" .. (right - 24) .. "," .. TOP_Y .. ")\\fs" .. size
                          .. "\\bord1\\shad0\\1c&HB0B0B0&}" .. font_tag() .. "\226\156\149"
    -- The block stops 16 units short of whatever the panel draws furthest left: the picture column
    -- when this show has pictures (the same geometry draw_thumbs uses, and only when it fits on
    -- screen, as there), else the text column.
    local block_right, block_edge = x - 16, "panel text"
    local k = scale()
    if any_thumb and k and k > 0 then
        local disp_w, disp_h, ass_left = thumb_geometry(k, row_h, x)
        if disp_w > 0 and disp_h > 0 and ass_left >= 0 then
            block_right, block_edge = math.floor(ass_left) - 16, "thumbnails"
        end
    end
    local band = band_event(res_x)
    if band then table.insert(events, 1, band) end      -- first: everything else draws over it
    local sl = status_event(res_x, block_right, block_edge)
    if sl then events[#events + 1] = sl end
    geom.x, geom.right, geom.top = x, right, TOP_Y
    geom.row_h, geom.lines, geom.rows = row_h, #lines, rows
    geom.line_y, geom.bottom, geom.text_h = ys, y, text_row_h
    -- The header is a text line, not a `row_h`-tall one, so its own close box
    -- (the ✕) matches `text_row_h`, same as `ys[1] .. ys[1] + text_row_h` would.
    geom.close = { right - 30, TOP_Y, right, TOP_Y + text_row_h }
    geom.tab = nil
    overlay.res_y = RES_Y
    overlay.res_x = 0
    overlay.z = PANEL_Z
    overlay.data = table.concat(events, "\n")
    overlay:update()
    geom.thumb_left = draw_thumbs(eps, lines, rows, ys, row_h, x)
    draw_line()          -- it stops at the pictures just placed
    scope_mouse()
end

-- ------------------------------------------------------------------- saying what is happening
-- What is on screen is `status_event`'s line, redrawn from the poll. This decides whether a play is
-- active (so the poll can speed up) and writes a failure into mpv's log once, under the CLI's own
-- trace id, so `omarchy-anime trace <id>` and the player's log tell the same story.
--
-- The one thing the old code could not do at all: `subprocess … detach=true` returns before the
-- play has begun and reports nothing about it, so a failed play told the user nothing whatsoever.
local function announce()
    local active = play_pending() ~= nil or launching()
    local e = play_entry
    if active then
        announced_fail = ""
    elseif e and e.state == "failed" and (e.trace or "") ~= announced_fail then
        announced_fail = e.trace or ""
        msg.warn("play failed: " .. status_text(e)
                 .. ((e.suggest and e.suggest ~= "") and (" \194\183 " .. e.suggest) or "")
                 .. " trace=" .. tostring(e.trace))
    end
    return active
end

local function retune_timer(active)
    if not status_timer then return end
    local want = active and POLL_ACTIVE_S or POLL_IDLE_S
    if status_timer.timeout ~= want then
        status_timer.timeout = want
        status_timer:kill()
        status_timer:resume()
    end
end

-- `do_play` shows the special workspace at claim time, with the loading screen
-- on it, so the 5-28 s resolve is spent looking at the show instead of at nothing. Nothing put the
-- window away again when that resolve FAILED: the CLI has already exited by then, so the user was
-- left with a player window holding nothing at all. This side is the one still running, so the
-- retraction belongs here.
--
-- Every condition is required, and each one is a way this could take something from the user:
--   * the hold must have expired — the failure's reason and advice are on that window for their
--     full FAIL_HOLD_S, and hiding early would take the explanation away with the window;
--   * `idle-active` must be true — a play that failed while an EARLIER episode is still playing
--     must never close the window the user is watching (nothing here may cost a play). Plain idle, not
--     `nothing_watched`: an episode that has ended is still the viewer's, on the window they watched it in (a failed
--     auto-next leaves it there), and hiding that window would take the last episode away with it;
--   * one attempt per failure trace, latched before the spawn — so a user who brings the window
--     back by hand keeps it, and a spawn that fails does not retry every 2 s forever.
-- `detach=true` because the CLI outlives this call, and `playback_only=false` because the default
-- would kill the child the moment playback state changes.
local function retract_after_failure()
    local e = play_entry
    if not (e and e.state == "failed") then return end
    local trace = tostring(e.trace or "")
    if trace == "" or trace == hidden_fail_trace then return end
    if (os.time() - (tonumber(e.finishedAt) or 0)) < FAIL_HOLD_S then return end
    if opts.bin == "" then return end
    if mp.get_property_native("idle-active") ~= true then return end
    hidden_fail_trace = trace
    local ok, res = pcall(mp.command_native, cli_command({ detach = true, head = { opts.bin, "player", "hide" } }))
    msg.info("failed play trace=" .. trace .. ": hold expired and the player is idle, hiding the"
             .. " window it was shown for (spawn=" .. tostring(ok and res and res.status) .. ")")
end

-- The choice keys: each choice the entry gives a key and a command is bound, forced, while the choices are on screen
-- — nothing is being watched (`nothing_watched`: the player idles, or the episode on it has ended) and the episode
-- panel is closed (the panel force-binds `n` itself, and two forced bindings of one key in one section have no defined
-- winner) — and unbound the moment any of that stops (a seek back from the end: the `eof-reached` observer). Each runs
-- the CLI command the entry carries, the same one Browse's button runs, detached like a play from the panel. Rebound
-- only when the entry's choices change, so a 0.5 s tick costs a string compare.
local choice_bound = {}       -- key -> true while bound
local choice_sig = ""         -- what is bound: the entry's trace and state and each key's command

-- "We'll tell you when episode 3 is ready." is read on the OSD before the window goes (Browse says it as it closes).
local LATER_HIDE_S = 1.5

local function run_choice(a, e)
    if opts.bin == "" then
        mp.osd_message("omarchy-anime was not found; choose from the overlay")
        return
    end
    local trace = string.format("mpv-choice-%d-%s", os.time(), tostring(a.id or "choice"))
    -- The subcommand stays on the command line, as the shell's `Logic.choiceLaunch` keeps it: a play is told from
    -- other processes by the word `play` there (`playstate.pid_is_play`). The other words travel in the variable.
    local head = { opts.bin, "--trace", trace }
    local words = {}
    for i, v in ipairs(a.args) do
        local w = tostring(v)
        if i == 1 and CHOICE_COMMANDS[w] then head[#head + 1] = CHOICE_COMMANDS[w] else words[#words + 1] = w end
    end
    if type(a.ack) == "string" and a.ack ~= "" then
        -- "Tell me when it's ready": what the viewer is told is `later`'s own answer (its `message`), read when the
        -- command returns, so "We'll tell you" is said only when the request was recorded, and "That couldn't be
        -- saved" reaches the viewer too. Async: the player never waits on it. `--no-hide`: the window stays up while
        -- the answer is read, and this script hides it `LATER_HIDE_S` later, only when the request was recorded.
        if a.id == "later" then words[#words + 1] = "--no-hide" end
        mp.command_native_async(cli_command({ capture_stdout = true, head = head, words = words }),
            function(success, result, err)
                local answer = success and type(result) == "table" and utils.parse_json(result.stdout or "") or nil
                local said = type(answer) == "table" and type(answer.message) == "string" and answer.message or ""
                msg.info("choice " .. tostring(a.id) .. " answered trace=" .. trace .. " later="
                         .. tostring(type(answer) == "table" and answer.later) .. " error=" .. tostring(err))
                if said ~= "" then mp.osd_message(said, 4) end
                -- Hidden only when `later` ended the play (`ended`), and only while the play-state entry is still
                -- that play's: a play past its wait, or a new one started in these 1.5 s, keeps its window.
                if type(answer) == "table" and answer.later == true and answer.ended == true then
                    mp.add_timeout(LATER_HIDE_S, function()
                        local now = read_json(play_status_path())
                        if not (type(now) == "table" and now.trace == e.trace) then
                            msg.info("choice later hide skipped trace=" .. trace .. ": the play-state entry is "
                                     .. tostring(type(now) == "table" and now.trace or "gone") .. ", not "
                                     .. tostring(e.trace))
                            return
                        end
                        local hid = mp.command_native(cli_command({ detach = true,
                                                                    head = { opts.bin, "--trace", trace, "player",
                                                                             "hide" } }))
                        msg.info("choice later hide trace=" .. trace .. " spawn=" .. tostring(hid and hid.status))
                    end)
                elseif type(answer) == "table" and answer.later == true then
                    msg.info("choice later hide skipped trace=" .. trace .. ": the play did not end")
                end
            end)
        msg.info("choice " .. tostring(a.id) .. " for episode " .. ep_num(e) .. " (play trace=" .. tostring(e.trace)
                 .. ") trace=" .. trace .. " asked")
        return
    end
    if a.id == "confirm" then
        -- "Keep waiting" during the wait (`keep-waiting`): its answer is read, as `later`'s is, so a press that reached
        -- no waiting play (it left its wait meanwhile) says so ("Episode 3 is not waiting for TorBox.") instead of
        -- "Keep waiting". Async: the player never waits on it.
        mp.command_native_async(cli_command({ capture_stdout = true, head = head, words = words }),
            function(success, result, err)
                local answer = success and type(result) == "table" and utils.parse_json(result.stdout or "") or nil
                local confirmed = type(answer) == "table" and answer.confirmed == true
                local said = type(answer) == "table" and type(answer.message) == "string" and answer.message or ""
                msg.info("choice confirm answered trace=" .. trace .. " confirmed=" .. tostring(confirmed)
                         .. " error=" .. tostring(err))
                -- "Keep waiting" only when the play confirmed it: a CLI that failed, could not be spawned or answered
                -- nothing readable did not reach the play, and the label would claim it had.
                mp.osd_message(confirmed and tostring(a.label or "")
                               or (said ~= "" and said or "Keep waiting didn't go through"), 3)
            end)
        msg.info("choice confirm for episode " .. ep_num(e) .. " (play trace=" .. tostring(e.trace) .. ") trace="
                 .. trace .. " asked")
        return
    end
    local res = mp.command_native(cli_command({ detach = true, head = head, words = words }))
    msg.info("choice " .. tostring(a.id) .. " for episode " .. ep_num(e) .. " (play trace=" .. tostring(e.trace)
             .. ") trace=" .. trace .. " spawn=" .. tostring(res and res.status))
    mp.osd_message(tostring(a.label or ""), 3)
end

local function sync_choice_keys()
    local e = nil
    if not shown and nothing_watched() then e = choice_entry() end
    local want, sig = {}, ""
    if e then
        sig = tostring(e.trace) .. "|" .. tostring(e.state)
        for _, a in ipairs(e.choice.actions) do
            local key = (type(a.key) == "string" and a.key:match("^%l$")) and a.key or nil
            if key and type(a.args) == "table" and #a.args > 0 then
                want[key] = a
                sig = sig .. "|" .. key .. "=" .. table.concat(a.args, " ")
            end
        end
    end
    if sig == choice_sig then return end
    choice_sig = sig
    for key in pairs(choice_bound) do
        mp.remove_key_binding("omarchy_anime_choice_" .. key)
        choice_bound[key] = nil
    end
    local keys = {}
    for key, a in pairs(want) do
        mp.add_forced_key_binding(key, "omarchy_anime_choice_" .. key, function() run_choice(a, e) end)
        choice_bound[key] = true
        keys[#keys + 1] = key
    end
    table.sort(keys)
    msg.info("choice keys: " .. (#keys > 0 and (table.concat(keys, " ") .. " bound for trace=" .. tostring(e.trace)
             .. " state=" .. tostring(e.state)) or "none bound") .. " (idle-active="
             .. tostring(mp.get_property_native("idle-active")) .. " eof-reached="
             .. tostring(mp.get_property_native("eof-reached")) .. " panel=" .. tostring(shown) .. ")")
end

local function tick_status()
    poll_play_status()
    local active = announce()
    draw_art()
    retract_after_failure()
    sync_choice_keys()
    -- The background play's line is its own overlay, redrawn here when its words move (its counter: once a second).
    draw_line()
    -- Twice a second while it shows, too: a running play past the click gate's 20 s is still said (up to
    -- BACKGROUND_TRUST_S), its counter has to move, and an end's hold has to end on time.
    retune_timer(active or launching() or line_text ~= "")
    -- Only redraw when the line the panel shows would actually change (the same rule the hover
    -- observer uses): a 0.5 s unconditional redraw of a 15-row panel is waste. The band's cover follows the art.
    if (status_line()) ~= last_status or band_wanted() ~= band_drawn then draw() end
end

local function move(delta)
    if not data then return end
    free_top = false
    cursor = clamp(cursor + delta, #data.episodes)
    draw()
end

local function set_cursor(i)
    if not data then return end
    free_top = false
    cursor = clamp(i, #data.episodes)
    draw()
end

-- The wheel moves the window, not the cursor: scrolling to look must not change what Enter plays.
local function scroll(delta)
    if not data then return end
    local n = #data.episodes
    local rows = math.max(3, math.floor(tonumber(opts.rows) or 12))
    if n <= rows then return end
    free_top = true
    top = clamp(top + delta, n - rows + 1)
    draw()
end

-- `scalable` hands the binding mpv's scroll magnitude (1 per wheel click, more on a hi-res
-- touchpad), so scrolling is proportional. Measured: `keypress WHEEL_DOWN 3` arrives as scale 3.
local function wheel(direction)
    return function(e)
        if type(e) == "table" and e.event == "up" then return end
        local scale = (type(e) == "table" and tonumber(e.scale)) or 1
        scroll(direction * math.max(1, math.floor(scale + 0.5)))
    end
end

local function play(index)
    if not data then return end
    local ep = data.episodes[index]
    if not ep then return end
    if ep.state == "unaired" then
        mp.osd_message("Episode " .. ep.episode .. " has not aired yet")
        return
    end
    if opts.bin == "" then
        mp.osd_message("omarchy-anime was not found; start it from the overlay")
        return
    end
    -- The gate. Read the shared entry *now* rather than trusting the last poll: a click is
    -- exactly when being half a second out of date is expensive.
    poll_play_status()
    if launching() then
        -- The two seconds before the CLI has claimed anything. A second launch in here would race
        -- the first to the claim, and whichever lost would then supersede the winner — the mutual
        -- kill the gate is there to stop. Two seconds of "not yet" is the cheap way out.
        if launched.episode == ep.episode then
            mp.osd_message("Episode " .. ep.episode .. " is already starting", 3)
        else
            mp.osd_message("Still starting episode " .. launched.episode .. " — one moment", 3)
        end
        return
    end
    local inflight = play_pending()
    if inflight and math.floor(tonumber(inflight.episode) or 0) == ep.episode
       and math.floor(tonumber(inflight.mediaId) or 0) == math.floor(tonumber(data.mediaId) or 0) then
        -- Same episode: say so instead of starting a second one. Deliberately short — the stage it
        -- has reached is on the status line already. Mirrors `playstate.decide`'s "already" arm,
        -- and the CLI would refuse it anyway.
        mp.osd_message("Episode " .. ep.episode .. " is already starting", 3)
        return
    end
    if inflight then
        -- A different episode: the user meant it. Say so plainly; the CLI stops the other play
        -- (by pid, verified against /proc/<pid>/cmdline) before it claims.
        mp.osd_message("Switching to episode " .. ep.episode, 3)
    end
    -- Over an episode being watched the play runs in the background: no band line and no picture over it. Its corner
    -- line says "starting" for the two seconds before its first write (`background_text`), then the play's own words.
    launched.at, launched.episode = mp.get_time(), ep.episode
    -- playback_only=false is mandatory: the default kills the child when playback stops, which is
    -- exactly what the `loadfile replace` it is about to trigger does.
    -- A trace id of our own, so `omarchy-anime trace last` shows a play started from the panel as
    -- one timeline under a recognisable name.
    local trace = string.format("mpv-%d-%d", os.time(), ep.episode)
    launched.trace = trace
    local res = mp.command_native(cli_command({
        detach = true,
        head = { opts.bin, "--trace", trace, "play" },
        words = { tostring(data.mediaId), tostring(ep.episode) },
    }))
    -- `detach` returns immediately and reports nothing about the play itself — that is what made
    -- every failure invisible. The outcome now arrives through the status file, not from here.
    msg.info("play episode " .. ep.episode .. " trace=" .. trace ..
             " spawn=" .. tostring(res and res.status))
    retune_timer(true)
    -- Close the panel rather than redraw it. The loading screen is about to cover the
    -- window, and a list left open under an opaque bitmap is a panel the user can still scroll and
    -- press Enter in without seeing any of it. `hide()` also unbinds and clears the thumbnails;
    -- the closed branch of draw() still renders the band text, so the stage line is not lost.
    hide()
end

local function play_next()
    if not data then return end
    for i, ep in ipairs(data.episodes) do
        if ep.state == "next" then
            cursor = i
            play(i)
            return
        end
    end
    mp.osd_message("Nothing unwatched to play")
end

-- Left click plays the row, as Enter does and as a click does in the overlay's list. Right click
-- selects only. A double click is free: play()'s gate is the shared in-flight entry, so
-- the second click of one is answered with "already starting" rather than a second play.
local function click(select_only)
    local ax, ay = pointer_ass()
    if not ax then return end
    local c = geom.close
    if c and ax >= c[1] and ax <= c[3] and ay >= c[2] and ay <= c[4] then
        if not select_only then hide() end
        return
    end
    local i = row_under(ax, ay)
    if not i then return end
    set_cursor(i)
    if not select_only then play(i) end
end

-- `e` opens mpv's own fuzzy-filtering selector over the WHOLE list, for the long-running shows
-- where scrolling 1100 rows is the wrong tool. It is the secondary affordance, not the primary one:
-- console.lua runs every item through `escape-ass` itself, so a watched row cannot be dimmed and
-- only `default_item` is highlighted, and its bindings cannot be extended from outside (no j/k).
-- What the user asked for was a panel "on the side", which is what Tab gives.
local function open_selector()
    if not data then return end
    -- `input.select` is a **silent no-op** with --load-console=no: no error, no log line, the
    -- callback never fires. `load-console` is the guard that works — `client-list` answers
    -- "property not found" on 0.41, so it is not a usable probe either.
    if mp.get_property_native("load-console") == false then
        mp.osd_message("The built-in selector needs console.lua; use j/k and Enter instead")
        return
    end
    local ok, input = pcall(require, "mp.input")
    if not ok or type(input) ~= "table" or type(input.select) ~= "function" then
        mp.osd_message("This mpv has no mp.input.select (it arrived in 0.39)")
        return
    end
    local items = {}
    for _, ep in ipairs(data.episodes) do
        items[#items + 1] = string.format("%s %d  %s", MARK[ep.state] or " ", ep.episode,
                                          ep.title and sanitize(ep.title)
                                          or ("Episode " .. tostring(ep.episode)))
    end
    input.select({
        prompt = "Episode:",
        items = items,
        default_item = cursor,
        submit = function(id)
            if type(id) == "number" and data and data.episodes[id] then
                cursor = id
                play(id)
                draw()
            end
        end,
    })
end

local BINDINGS = {
    { "j", function() move(1) end, true },
    { "k", function() move(-1) end, true },
    { "DOWN", function() move(1) end, true },
    { "UP", function() move(-1) end, true },
    { "PGDWN", function() move(math.floor(tonumber(opts.rows) or 12)) end, true },
    { "PGUP", function() move(-math.floor(tonumber(opts.rows) or 12)) end, true },
    { "HOME", function() set_cursor(1) end, false },
    { "END", function() set_cursor(data and #data.episodes or 1) end, false },
    { "ENTER", function() play(cursor) end, false },
    { "n", play_next, false },
    { "e", open_selector, false },
    { "ESC", function() hide() end, false },
    -- Forced, and scoped to the panel's rectangle by scope_mouse(), so a click on the video is
    -- still mpv's. Both exist only while the panel is open.
    { "MBTN_LEFT", function() click(false) end, false },
    { "MBTN_RIGHT", function() click(true) end, false },
    { "WHEEL_UP", wheel(-1), false, { complex = true, scalable = true } },
    { "WHEEL_DOWN", wheel(1), false, { complex = true, scalable = true } },
}

-- Every navigation key is forced only while the panel is open, so j (cycle sub), ENTER
-- (playlist-next), UP/DOWN (seek) and ESC (leave fullscreen) keep their normal meaning the rest of
-- the time. `g` is deliberately not taken: it is bound to `ignore` in mpv's default section because
-- it is the prefix of select.lua's g-p / g-c / g-s sequences, and taking it kills every one.
local function bind()
    if bound then return end
    for _, b in ipairs(BINDINGS) do
        mp.add_forced_key_binding(b[1], "omarchy_anime_episodes_" .. b[1], b[2],
                                  b[4] or (b[3] and { repeatable = true } or nil))
    end
    bound = true
end

local function unbind()
    if not bound then return end
    -- Area first, bindings second: a leaked full-screen area on a live section would swallow
    -- every click in the video, which is this item's one real blast radius.
    mp.set_mouse_area(0, 0, 0, 0, SECTION)
    for _, b in ipairs(BINDINGS) do
        mp.remove_key_binding("omarchy_anime_episodes_" .. b[1])
    end
    bound = false
end

hide = function()
    shown = false
    unbind()          -- unconditional: a handler that errored must not leave j hijacked
    clear_thumbs()    -- explicit, not just a side effect of draw()'s closed branch below: a
                       -- thumbnail left on screen after the panel closes is worse than none at all
    sync_choice_keys()   -- the panel gave `n` back
    draw()
end

-- The ☰ is a click target only while on screen. It shares the panel's input section but is bound
-- on its own, and its rectangle is all `set_mouse_area` lets through while the panel is closed.
local set_tab

local function tab_click()
    local ax, ay = pointer_ass()
    local r = geom.tab
    if not ax or not r then return end
    if ax >= r[1] and ax <= r[3] and ay >= r[2] and ay <= r[4] then show() end
end

local function bind_tab(on)
    if on == tab_bound then return end
    if on then
        mp.add_forced_key_binding("MBTN_LEFT", "omarchy_anime_episodes_tab", tab_click)
    else
        mp.set_mouse_area(0, 0, 0, 0, SECTION)
        mp.remove_key_binding("omarchy_anime_episodes_tab")
    end
    tab_bound = on
end

set_tab = function(on)
    if tab_shown == on then return end
    tab_shown = on
    bind_tab(on)
    draw()
end

-- The OSC's contract: appear on pointer movement, go after `cursor-autohide` ms of stillness.
local function arm_tab_timer()
    local ms = tonumber(mp.get_property("cursor-autohide")) or 1000
    local secs = math.max(0.5, ms / 1000)
    if not tab_timer then
        tab_timer = mp.add_timeout(secs, function() set_tab(false) end)
    end
    tab_timer.timeout = secs
    tab_timer:kill()
    tab_timer:resume()
end

function show()
    set_tab(false)
    local ok, why = load_data()
    if not ok then
        -- The line can carry the show's title, which is not ours: no bidi control may reach the OSD.
        mp.osd_message(sanitize(why or "No episode list"))
        return
    end
    shown = true
    sync_choice_keys()   -- the choices give way while the panel has the keyboard
    bind()
    draw()
end

local function toggle()
    if shown then hide() else show() end
end

mp.register_script_message("reload", function()
    local was = shown
    load_data()
    if was then draw() end
end)

-- The CLI sends this right after writing `art`, so the backdrop is up before `show`
-- rather than on the next idle poll (up to 2 s of black window).
mp.register_script_message("art", function()
    poll_play_status()
    retune_timer(true)
    draw_art()
    draw()
end)

mp.register_script_message("toggle", toggle)

-- Introspection for the verification pass: the panel's state without a screenshot. Writing it into
-- `user-data` rather than a file keeps it readable over the same IPC socket everything else uses.
mp.register_script_message("dump", function()
    poll_play_status()
    local e = play_entry or {}
    mp.set_property_native("user-data/omarchy-anime/panel", {
        shown = shown, cursor = cursor, top = top, bound = bound,
        -- The shared entry as this side sees it, so the gate and the line on screen can be
        -- asserted over IPC instead of guessed at from a screenshot.
        playState = e.state or "", playEpisode = math.floor(tonumber(e.episode) or 0),
        playTrace = e.trace or "", playText = status_text(play_entry),
        -- Which failure's empty window has already been retracted, so "the window was left up
        -- after a failed play" is answerable over IPC rather than from a screenshot.
        hiddenFailTrace = hidden_fail_trace,
        playPending = play_pending() ~= nil, launching = launching(),
        statusLine = (status_line()), pollEvery = status_timer and status_timer.timeout or 0,
        -- The background play's corner line, as last drawn ("" when there is none).
        backgroundLine = line_text,
        -- The choice keys bound right now, so "bound only while the choice is on screen" is answerable
        -- over IPC.
        choiceKeys = (function()
            local out = {}
            for key in pairs(choice_bound) do out[#out + 1] = key end
            table.sort(out)
            return out
        end)(),
        -- Whether the loading screen's bitmaps are live, so "no backdrop left over the
        -- video" can be asserted over IPC rather than squinted at in a screenshot.
        artShown = art_shown,
        count = data and #data.episodes or 0,
        episode = (data and data.episodes[cursor] and data.episodes[cursor].episode) or 0,
        state = (data and data.episodes[cursor] and data.episodes[cursor].state) or "",
        mediaId = data and data.mediaId or 0,
        -- What named the menu's show when it last opened (`menu_show`): "player stamp", "now-playing",
        -- "latest play" or "none".
        menuFrom = menu_from,
        playing = data and data.playing or 0,
        x = geom.x, right = geom.right, gtop = geom.top, row_h = geom.row_h, lines = geom.lines,
        thumbLeft = geom.thumb_left, bottom = geom.bottom,
        redraws = redraws, hover = hover_row or 0, free_top = free_top,
        tab_shown = tab_shown, tab_bound = tab_bound,
        -- How many picture overlays are live right now, and which episode each holds --
        -- so "a thumbnail left on screen after close" and "the right row got the right picture"
        -- can be asserted over IPC instead of just eyeballed in a screenshot. A plain array of
        -- {slot, episode} pairs, not the sparse slot-keyed table itself: a Lua table with
        -- non-contiguous integer keys is ambiguous going through mp.set_property_native's
        -- array-vs-map heuristic, and this makes no assumption about it either way.
        thumbCount = (function() local n = 0 for _ in pairs(thumb_ids) do n = n + 1 end return n end)(),
        thumbEpisodes = (function()
            local out, n = {}, 0
            for slot, ep in pairs(thumb_ids) do
                n = n + 1
                out[n] = { slot = slot, episode = ep }
            end
            return out
        end)(),
    })
end)

-- Hover moves the shared cursor, as a hovered row does in the Browse overlay. The observer fires
-- on every pointer *pixel*, so the redraw is gated on the derived ROW changing.
mp.observe_property("mouse-pos", "native", function(_, pos)
    if not shown or not data then
        hover_row = nil
        if pos and pos.hover then
            set_tab(true)
            arm_tab_timer()
        else
            set_tab(false)
        end
        return
    end
    local ax, ay = pointer_ass()
    local row = ax and row_under(ax, ay) or nil
    if row == hover_row then return end
    hover_row = row
    if row then set_cursor(row) end
end)

-- Measured: the ass-events panel re-renders itself from its declarative `\pos`/`res_y` at
-- composite time, so a resize needs no help here — but `overlay-add` pixel positions do not track
-- a resize on their own and go stale (a small, wrongly-placed sliver after the window grows). This
-- is that reissue, hung off the same re-layout draw() already does for everything else rather than
-- a parallel path that would have to duplicate draw_thumbs's slot bookkeeping.
--
-- `osd-dimensions` also carries `par`/`mt`/`mb`/`ml`/`mr` (letterbox margins), which can change
-- without `w`/`h` moving at all -- and `scale()` (hence every pixel this file computes) depends on
-- `h` alone. Without this guard, a redraw fires on every one of those irrelevant changes too, and
-- on every intermediate frame of an interactive resize drag, each one re-`io.open`-ing up to
-- THUMB_MAX_SLOTS files for no visible change.
local last_osd_w, last_osd_h = nil, nil
mp.observe_property("osd-dimensions", "native", function(_, dim)
    local w, h = dim and dim.w, dim and dim.h
    if w == last_osd_w and h == last_osd_h then return end
    last_osd_w, last_osd_h = w, h
    -- The loading screen is drawn while the panel is usually *closed*, so its reissue
    -- has to happen above the `shown` gate — the same staleness, one layer out.
    if art_shown then draw_art() end     -- pixel overlays do not track a resize
    draw_line()                          -- its width in characters follows the window's
    if not shown then return end
    draw()
end)

mp.add_key_binding("TAB", "toggle", toggle)      -- also `script-binding omarchy_anime_episodes/toggle`
mp.register_event("shutdown", function()
    unbind() bind_tab(false) clear_thumbs() clear_art()
    line_quit = true
    draw_line()           -- the player quits: no line left behind
end)
-- A new file means a new show: drop the panel rather than showing the previous show's list.
mp.register_event("start-file", function()
    -- The play that was in flight has landed; the file it loaded is this one.
    launched.at, launched.episode = 0, 0
    -- An ended play's line belongs to the file before (`background_text`): the entry as it is now, one small read.
    poll_play_status()
    end_before_file = (play_entry and play_entry.state ~= "running") and end_key(play_entry) or ""
    if end_before_file ~= "" then
        msg.info("background line: this file starts after the end " .. end_before_file .. " (state="
                 .. tostring(play_entry.state) .. "); that end is never said over it")
    end
    draw_line()
    -- Which show this file is (`menu_show`): the CLI stamps the player before every `loadfile`, so the stamp now is
    -- this file's, unless that stamp write failed (the `loadfile` goes on under the previous stamp). A `loadfile` mpv
    -- refuses sends no `start-file`, and the file before keeps its own.
    file_stamp = mp.get_property_native(STAMP_KEY)
    if shown then hide() end
    data = nil
end)

-- mpv's own signal that the video is about to be presented. Not the status poll: the
-- entry flips to done on a 0.5 s poll, and the CLI writes it only after its own `file-loaded`
-- wait returns and the covers fetch and now-playing write are done — which would leave the
-- backdrop sitting over the first seconds of video.
mp.register_event("file-loaded", function()
    -- Remember *which* play landed, not just that one did. A new claim carries a new
    -- trace and draws normally; a failure never reaches this event, so FAIL_HOLD_S is untouched.
    landed_trace = (play_entry and play_entry.trace) or ""
    clear_art()
    -- The new episode is being watched: no choice keys over it. The `eof-reached` observer unbinds them too: the
    -- property is nil from `end-file` on, but when the observer runs depends on timing (man mpv: change events are
    -- coalesced; two probes saw its call before and after `start-file`), so this event, the one that says the file is
    -- on screen, does not wait for it.
    sync_choice_keys()
    draw()
end)
mp.observe_property("idle-active", "bool", function(_, idle)
    -- A failed play that then clears its hold, or a stopped player: nothing to back a screen for.
    if idle and not (play_pending() or launching()) then draw_art() end
    sync_choice_keys()
    -- The play's block sits at the top only while a file is on screen.
    if last_status ~= "" then draw() end
    draw_line()           -- idle: nothing is watched, so no corner line
end)
-- The loaded episode reached its end (`--keep-open=yes` keeps it on its last frame, `idle-active` false), or the viewer
-- sought back from it. Either way what `nothing_watched` answers has changed, so the held art, the choice lines and
-- the choice keys follow at once rather than on the next 2 s tick: at the end a held play's art and choices come up as
-- on an idle player, and a seek back takes them off the episode again. `draw()` every time: before the end a held
-- entry's line is "" (nothing is drawn for it over a playing episode), so `last_status` cannot gate it. nil (no
-- file, or one being swapped) reads as false.
mp.observe_property("eof-reached", "bool", function(_, ended)
    poll_play_status()       -- the entry as it is now, not as the last tick read it (one small file read)
    msg.debug("eof-reached=" .. tostring(ended) .. ": nothing_watched=" .. tostring(nothing_watched())
              .. " entry=" .. tostring(play_entry and play_entry.state)
              .. " trace=" .. tostring(play_entry and play_entry.trace))
    draw_art()
    sync_choice_keys()
    draw()
end)

local arabic_why
arabic_font, arabic_why = detect_arabic_font()
-- Two ass-events overlays, each its own id: the background play's line, then the panel's.
line_overlay = mp.create_osd_overlay("ass-events")
overlay = mp.create_osd_overlay("ass-events")
-- The only clock left in this file. It idles at one small read every two seconds so a play
-- started from the popout is announced here too, and steps up to twice a second while one is in
-- flight so the counter moves.
poll_play_status()
status_timer = mp.add_periodic_timer(POLL_IDLE_S, tick_status)
-- The guard `Mpv.ensure_script` reads: `scripts` only reflects --script= arguments, so it cannot
-- say whether this file was loaded over IPC.
mp.set_property_native("user-data/omarchy-anime/episodes", true)
msg.info("loaded as " .. mp.get_script_name() .. ", bin=" .. (opts.bin ~= "" and opts.bin or "<unset>")
         .. ", font=" .. (opts.font ~= "" and opts.font or "osd-font")
         .. ", arabic_font=" .. (arabic_font ~= "" and arabic_font or "none") .. " (" .. arabic_why .. ")")
