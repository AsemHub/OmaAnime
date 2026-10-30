# Using it

[Back to the README](../README.md)

`omarchy-anime` in a command here is the plugin's own command, on your PATH once you have run the link line in
the README's [Install](../README.md#install).

## Use

| Where | Action |
|---|---|
| Bar icon, left click | Popout: now playing, play/pause, stop, player window toggle, Continue Watching |
| Bar icon, middle click | Show or hide the player window |
| Bar icon, right click | Play / pause |
| Popout row, ▶ | Play the next unwatched episode (or rewatch the last one when the next has not aired). None on a row that says *Not out yet*: there is nothing to play |
| Popout, "Browse…" | Open the full-screen Browse overlay |
| Popout, subtitle line | Says which language is on screen and, when it was borrowed from another release, which release and what was done to its timing |
| Popout, ⏪ / ⏩ under the subtitle line | Move the subtitles 1 second earlier or later. The number between them is the current delay; ↺ puts it back to the timing the plugin chose. The delay is cleared on every new episode. |

### Browse overlay

Five views — Continue Watching, Top 100, Airing, Search, Lists — plus a per-anime detail page with
an episode grid. Settings opens in its own window (below). It is keyboard-first; every key below also has a pointer
route.

| Mouse | Does |
|---|---|
| Click a row | The same as `Enter` |
| Right-click a row, or its `⋯` | Open that show's episodes — the same as `e` |
| Hover a row or a cell | Moves the cursor, so `j` continues from where you are pointing. One highlight, never two |
| `↻` / `⚙` in the header | Reload the view (`r`) / open Settings (`,`) |
| "completed hidden (c)" on Top 100 | Show or hide completed entries (`c`) |
| "Copy trace" in the status line | Copy the failed play's trace id (`t`). It only appears after a failure |
| A click on the dimmed background | Close the overlay (in the release picker: back) |
| Wheel | Scrolls the list, the grid and Settings. A scrollbar appears when there is more |

| Key | Does |
|---|---|
| `Tab` / `Shift+Tab`, `h` `l`, `←` `→` | Cycle the five views |
| `j` `k`, `↑` `↓`, `PageUp` `PageDown`, `Home` `End` | Move the cursor |
| `Enter` | Continue Watching: play (or open the episodes when there is nothing to play, as for an entry that is not out yet). Lists: depends on the list (see below). Other views: open the episodes |
| `e` | Open the episodes of the selected show |
| `[` `]` | Lists: switch between Planning, Paused, Completed, Dropped and Rewatching |
| `,` | Open Settings; the overlay hides until it closes |
| `/` | Jump to Search and start typing (`Escape` there returns to the list) |
| `c` | Top 100: show or hide the shows you have completed |
| `r` | Reload the current view from AniList, ignoring the cache |
| `w` | Detail: add the show to your list as Watching, keeping its progress |
| `p` | Detail: choose the release for the selected episode. After a refused play: choose one for it |
| `t` | Copy the trace id of the last failed play to the clipboard |
| `Escape` | Detail: back. List: close the overlay |

**Lists** shows the five AniList lists that are not Continue Watching. What `Enter` does depends on
which one you are looking at:

| List | The row says | `Enter` |
|---|---|---|
| Planning | `Not started · N episodes` | moves the show to Watching and plays episode 1 |
| Paused | `Ep N of M · paused` | resumes at the next episode |
| Completed | `Watched all M` | opens the episode grid, so you pick what to rewatch |
| Dropped | `Stopped at Ep N` | opens the episode grid |
| Rewatching | `Ep N of M · rewatching` | resumes at the next episode; the entry stays Rewatching until its last episode completes it |

An entry AniList lists as not yet released, with no airing date, is not out: whatever its list, its row says
`Not out yet` (`Not out yet · expected Mar 2027` when AniList gives a date), `Enter` opens its episodes, and nothing
plays. Its episode cells are dimmed (a movie has one; a show whose count AniList does not know yet has none), and the
player's episode menu says the episode has not aired. A play of it started any other way (`omarchy-anime play`, a
script) is answered at once with "*title* is not out yet." and AniList's date when it has one: nothing is searched
and the player is not touched.

From a terminal the same lists are `omarchy-anime list --status PLANNING` (or `PAUSED`,
`COMPLETED`, `DROPPED`, `REPEATING`, `CURRENT`).

While an episode is being found the overlay stays up and the bottom line shows the progress. It
closes itself once the player takes over. If something fails, the line names the stage that failed,
what the providers answered, and `Enter` retries.

### Choosing a release

When auto-play will not choose — the releases belong to other shows of the franchise, every one was
ruled out, or none of them would play — the overlay opens the **release list** for that episode on
its own. `p` opens it any time from the episode grid. It lists every release the search found: the
ones that passed first, in the order auto-play ranks them (the top one is what it would play), then
the ruled-out ones, dimmed, grouped by why (another show, nobody sharing, the wrong size, outside
the pack's range…). Each row names the group, resolution, size, how many are sharing it, the
subtitles (your language named, the rest counted), and the other show its title belongs to, if any.

| Key | Does |
|---|---|
| `Enter` | Play exactly this release. It overrides every filter; if it fails, the list comes back with the row marked |
| `/` | Search with your own words, through the indexer add-ons that offer text search; not offered when none does (`Escape` returns to the list) |
| `r` | Search again automatically (and leave your own-words results) |
| `x` | Forget this show's pin |
| `Escape` | Back |

**Pins.** A release you choose is remembered for the show. If it is a pack that holds the next
episodes, they play straight from it with no search; if the pack stops working it is let go and the
next best plays. A pinned pack TorBox does not have yet is added there even when nobody knows how
many people share it (it is your choice), and the play then says "TorBox is downloading it (N %)"
instead of playing something else; it stays pinned and plays once TorBox has it. A later play
finds it on your TorBox account rather than adding it again, and if its download has not moved
for 6 hours that play looks for another copy instead. It is skipped only when fewer than 10 were
sharing it when you picked it (within the last day). Releases from the same group are preferred
afterwards, though never over one that has your subtitle language. The episode grid shows `Pinned: <group> · pack a–b` while a pin is set.

If auto-next refuses while the player is up, the player says so and `p` there opens the list above
the video. The popout offers **Choose a release** after a refused play.

Cover images are downloaded by the plugin's own process, never by the shell, so on a cold cache the
rows appear immediately with placeholders and the covers land a few seconds later. After that they
come off the disk.

### Keybinds

The plugin binds no key and does not touch your Hyprland config. If you want keys, add them to
`~/.config/hypr/bindings.lua`. These are examples; the combinations are not used by Omarchy 4.0.3's
default bindings, but check your own file first:

```lua
o.bind("SUPER + A", "Anime browse", "omarchy-shell shell toggle io.github.asemhub.omaanime")
o.bind("SUPER + CTRL + ALT + A", "Anime settings", "omarchy-shell omaanime browse settings")
o.bind("SUPER + ALT + A", "Anime player show/hide", "omarchy-shell omaanime toggleWindow")
o.bind("SUPER + ALT + P", "Anime play/pause", "omarchy-shell omaanime playPause")
```

Every command there works from a terminal as well.

`omarchy-shell shell summon|toggle io.github.asemhub.omaanime` opens the **Browse overlay**, not the bar
popout: that is how the shell routes a plugin that is both a bar widget and an overlay. The popout is
on the bar icon's left click, where it always was.

### Shell IPC

```sh
omarchy-shell omaanime status            # live JSON: playing, position, window, now playing
omarchy-shell omaanime play <mediaId> <episode>   # AniList media id, episode; prints the play's trace id,
                                                  #   or `refused: …` while another play is starting
omarchy-shell omaanime browse watching   # open the overlay on a view:
omarchy-shell omaanime browse top        #   watching | top | airing | search | lists | settings |
                                      #   advanced | accounts
                                      #   (the view argument is required by the shell's IPC)
omarchy-shell omaanime picker <mediaId> <episode> # open the release list for one episode
omarchy-shell omaanime toggleWindow | show | hide | playPause | stop
```

Subtitle timing from a terminal (the popout buttons do the same thing):

```sh
omarchy-anime player sub-delay +1.5     # 1.5 s later than it is now
omarchy-anime player sub-delay -0.5     # half a second earlier
omarchy-anime player sub-delay --set 38.74
omarchy-anime player sub-delay --reset
```


## Settings in short

**Open Settings**: click **Settings** in the Anime bar popout, or run
`omarchy-shell omaanime browse settings` (inside the Browse overlay, `,` opens it too). There is no per-widget settings screen in Omarchy 4.0.3
— no gear, no right-click menu, no `omarchy-menu` entry — so the plugin brings its own. Every change
is saved as you make it. **Accounts** and **Indexers** are sections of the same window.

Settings is a normal window, not part of the full-screen overlay: drag it by its title, Alt-Tab to it, and keep it
beside your browser while you sign in. `Escape` or **✕** closes it; opened from the overlay, the overlay comes back.
Asked for again while it is open (the popout's button, the command), it is shown again with what you typed kept.
`Tab` walks its controls, `j` `k` and `PageUp` `PageDown` scroll.

The same keys, from a terminal:

```sh
omarchy-shell omaanime setSetting watchedThreshold 90     # values are JSON
omarchy-shell omaanime setSetting autoNext true
omarchy-shell omaanime setSetting subtitleLanguage '"eng"'
omarchy-shell omaanime setSetting autoNext reset          # back to config.json or the default
omarchy-shell omaanime settings                           # what is set, and where it came from

omarchy bar set io.github.asemhub.omaanime watchedThreshold 90 --json
```

`omarchy bar set` **without `--json` stores the value as a string** (`"90"`, `"true"`) — the plugin
copes, but pass `--json` and you will not have to think about it. Settings always writes real
typed JSON.

Three layers, each beating the one above it: the plugin's defaults; `~/.config/omaanime/config.json`,
if you create it (every key can live there; it is the copy to carry between machines); and the
plugin's entry in `~/.config/omarchy/shell.json`, which Settings, `setSetting` and `omarchy bar set`
write. Settings says under each setting which layer it is reading, and its **reset** removes the key
from `shell.json`. A value outside a setting's range falls back to the default with a warning in
`omarchy-anime trace`, never a failed play.

Every setting, with its default, its range and what it changes, is in
[docs/settings.md](settings.md). In short:

| key | default | what it is |
|---|---|---|
| `subtitleLanguage` | `eng` | your subtitle language, ISO 639-2: one of the 38 in [docs/settings.md](settings.md#subtitlelanguage), or one of the six regional choices (`pt-BR`, `es-419`, `zh-Hant`, …) |
| `subtitleFallbackLanguage` | `eng` | the second language, used only when no release has yours: a release with it ranks higher; `off` scores every release by its name alone |
| `subtitlePolicy` | `prefer` | `prefer` plays the best release without your language and says so; `require` refuses unless your language will be on screen (embedded, borrowed or external) |
| `dubs` | `avoid` | `avoid` never plays a dubbed release while one in the show's original language exists: TorBox downloads that one and the play waits for it; `allow` plays a dub TorBox already has |
| `preferredResolution` | 1080 | the picture height the ranking rewards |
| `subtitleFont` | empty | mpv's subtitle font; empty follows the language (Noto Naskh Arabic UI for Arabic and Persian, Noto Sans Devanagari UI for Hindi, Noto Sans CJK SC / JP for Chinese / Japanese) |
| `watchedThreshold` | 85 | percent after which the episode is written to AniList (also at end of file, not when a stream stops early) |
| `autoNext` | false | start the next aired episode when one ends |
| `prefetchNext` | true | once you have watched two minutes of an episode, download the next aired one at TorBox if it doesn't have it yet, so it starts at once; one download per episode, nothing shown |
| `allowBatches` | true | packs are candidates too; the episode's file is streamed out of the pack |
| `sideloadSubtitles` | true | borrow your language from another release, then Subdl and OpenSubtitles, when the one playing lacks it |
| `subtitleSyncMaxOffset` | 60 | how far (seconds) a borrowed subtitle file may be shifted; 0 loads it unshifted |
| `showTitleInBar` | false | show "Title · Ep N" next to the icon |
| `maxTitleWidth` | 180 | pixels before the bar title is cut with an ellipsis; 0 is unlimited |
| `pollIntervalSec` | 5 | how often the bar asks mpv for the position |
| `showCompletedInTop` | false | show shows you have completed in the Top 100 view |

Behind **▸ Advanced** at the bottom of Settings (`omarchy-shell omaanime browse advanced` opens it
expanded), with the same per-key **reset** and a **reset all**:

| key | default | what it is |
|---|---|---|
| `indexers` | empty | add-on file names, in the order they are asked; unlisted add-ons follow by name |
| `indexerTimeoutSeconds` | 60 | how long one add-on call may take (a Jackett `all` feed can take up to 40 s on its side) |
| `preferredGroups` | empty | fansub groups to rank up, `name` or `name=points`; the plugin favours none |
| `englishOnlyGroups` | empty | groups that ship English only, placed after equal releases when your language is not English |
| `subtitleCheckCount` | 16 | releases whose subtitle tracks are read per play, when their add-on offers them |
| `subtitleCheckSeconds` | 15 | seconds those reads may take per play; a slow or unreachable site is given up on at about this time |
| `providers` | `torbox,realdebrid` | the stream providers to try, in order; one without a token is skipped |
| `probeCount` | 3 | releases each provider may try per play |
| `providerBlockMinutes` | 15 | how long a provider that refused three releases in a row is stepped over |
| `rdPollSeconds` | 30 | how long to wait for Real-Debrid to convert a release |
| `torboxWaitSeconds` | 60 | when a play waited for the one download it starts at TorBox: a wait up to this long, from starting the play until TorBox has it, plays by itself; a longer one loads paused and you are notified with a sound (not with Omarchy's do-not-disturb on); showing or hiding the player makes no difference; 0 always loads it paused. It does not end the wait: that runs until TorBox is done, you choose, or three hours |
| `torrentInfoSeconds` | 5 | how long TorBox may look up a pack's files when no add-on lists them; 0 turns it off |
| `httpTimeout` | 15 | seconds before an HTTP request gives up |
| `subsCacheMaxBytes` | 16777216 | size limit of the borrowed-subtitle cache (Settings shows MiB) |
| `anilistClientId` | 51175 | only if you use your own AniList app |

**Stream providers** is two fixed rows you turn on and off and move with ▲ ▼. **Indexers** and the
two fansub-group lists are editable text with ▲ ▼ × and an *Add* button. A list is stored in
`shell.json` comma-joined (`torbox,realdebrid`) because Omarchy's writer flattens a top-level array;
`config.json` takes either form.

## In the player

`Tab` shows the show's episode list down the right-hand side of the video, so the next episode is
three keys away instead of a trip back to the overlay.

| Key | Does |
|---|---|
| `Tab` | Show or hide the episode list |
| `j` `k`, `↑` `↓`, `PageUp` `PageDown`, `Home` `End` | Move the cursor |
| `Enter` | Play the selected episode |
| `n` | Jump to the first unwatched episode and play it |
| `e` | Search the whole list (mpv's own filter box — useful at 1100 episodes) |
| `Esc` | Close the list |

With a mouse:

| Mouse | Does |
|---|---|
| `☰` at the right edge of the video | Open the list. It appears when you move the pointer over the window and fades after a second of stillness, like mpv's own controls |
| Click a row | Play that episode — the same as `Enter` |
| Right-click a row | Select it without playing |
| Wheel | Scroll the list. The cursor stays where it is, so `Enter` still plays what you chose |
| `✕` at the panel's top right | Close the list |

The pointer only belongs to the panel inside the panel: a click on the video still does whatever
mpv does, the wheel over the video still changes the volume, and none of it survives the list being
closed.

A tick marks a watched episode, `▶` the next one, `·` one that has not aired yet. Titles come from
the same ani.zip data the overlay uses, off the disk — the panel makes no network call of its own.

**While an episode is starting**, a line at the bottom left of the video says what is happening and
how long it has been going — "Episode 9 — Checking whether TorBox already has it · 5s", then
"Opening it in the player".
It stays until the episode starts or the attempt fails; it is not a fixed-length message. If the
attempt fails, that line turns red and holds the reason and what to do about it for half a minute.
The same line appears in the bar popout, whether the play was started there or in the player.

**Clicking twice does not start two plays.** Clicking the episode that is already starting says so
and changes nothing. Clicking a *different* episode says "Switching to episode 8" and takes over —
the first attempt is stopped properly first, which matters because two attempts at once fight over
the same downloader and can leave you with neither.
These keys only exist while the list is open, so `j`, `Enter`, `↑`/`↓` and `Esc` keep their usual
mpv meanings the rest of the time.

To bind Tab to something else, put this in `~/.config/mpv/input.conf`:

```
F2 script-binding omarchy_anime_episodes/toggle
```

## The player window

The plugin parks mpv on the `special:omaanime` workspace, floats it and sizes it to the largest 16:9 box
inside 90 % of the monitor's usable logical area — 1040 x 585 on a 1920 x 1080 panel at scale 1.6,
1728 x 972 on the same panel unscaled. It re-fits the window whenever the window does not fit the
screen and leaves your own size alone when it does. If you move the player to a workspace yourself
(SUPER+SHIFT+2, say), it counts as on screen whenever a monitor shows that workspace. Hiding it
(middle click, the popout's toggle, `omarchy-anime player hide`) or stopping it parks it back on
`special:omaanime`, and showing it brings it up there. **No Hyprland window rule is needed**; if you
want one anyway, one line is enough:

```lua
o.window("^omaanime-player$", { workspace = "special:omaanime silent", float = true })
```
