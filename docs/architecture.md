# Architecture

How the plugin is put together, for someone reading or changing the code. What it does for a viewer
is in the [README](../README.md); every setting is in [settings.md](settings.md).
`omarchy-anime` in a command here is the plugin's own command, on your PATH once you have run the link line in
the README's [Install](../README.md#install).

## The shape

```
Omarchy shell (Quickshell)                      Python CLI (standard library only)
  Service.qml   state, IPC, timers   ──runs──▶    bin/omarchy-anime
  Panel.qml     bar icon + popout                 └─ lib/omarchy_anime/
  Browse.qml    full-screen overlay                    AniList, ani.zip, indexer add-ons,
  *Model.js / ServiceLogic.js  pure logic              ranking, providers, subtitles, mpv
                                                              │
                                                              ▼
                                        mpv (one instance, JSON IPC socket)
                                          + mpv/omarchy-anime-episodes.lua
```

- **The QML is layout, keys and process plumbing.** Every decision (what to play, which provider,
  which subtitles, what to write to AniList) is made by the CLI. The QML starts it as a child process
  and reads one JSON answer from its standard output. The pure JavaScript beside the QML
  (`BrowseModel.js`, `ServiceLogic.js`) turns those answers into rows and sentences, and is tested
  with `node --test`.
- **The CLI** (`bin/omarchy-anime`) is usable on its own. Every command prints JSON on
  standard output and log lines on standard error; `--trace <id>` ties a run to the UI action that
  started it.
- **The player** is one mpv process, started by `bin/omarchy-anime-player`, parked on the
  Hyprland special workspace `special:omaanime` and driven over its IPC socket
  (`$XDG_RUNTIME_DIR/omaanime-mpv.sock`). The Lua script draws the in-player episode list and the
  "starting" line.

## Modules

| Module | Does |
|---|---|
| `core.py` | settings and their layers, token files, the JSONL log and its redaction, the HTTP client, atomic cache writes |
| `cli.py` | the commands and the play pipeline |
| `anilist.py` | AniList GraphQL: lists, search, media, progress writes, the rate-limit pacing |
| `anizip.py` | ani.zip: an AniList id to AniDB/TVDB/TMDB ids and per-episode numbering |
| `fribb.py` | Fribb's anime-lists: whether a show's TMDB id is a movie's or a TV series', fetched weekly by the subtitle worker into a small index |
| `indexers.py` | loading indexer add-ons, the guarded call, the query objects and capability handles |
| `addon_help.py` | the add-on interface (`addons spec`) and the two AI agent prompts (`addons prompt`), from one place the tables, example and prompts of [addons.md](addons.md) are checked against |
| `addon_check.py` | `addons check`: one add-on file loaded as the registry would and asked once, offline (every request answered with `ctx.HttpError`, a connection of its own through the usual socket calls refused: a check, not a sandbox) or `--live` |
| `torznab.py` | the built-in Torznab add-on: a server's feeds, caps, the query plan, safe XML, servers and feeds side by side (round-robin) within the add-on's time, and the Accounts `check` |
| `torznab_prowlarr.py` | Prowlarr detection and expansion: what a server is (status endpoint, then caps), remembered per server; a Prowlarr's indexer list filtered to its enabled torrent anime/movie indexers, each searched with the key in `X-Api-Key` |
| `torznab_accounts.py` | the Torznab servers under Settings' Accounts: `torznab` list, add, check and remove; an add checked before it is saved, every write read back at mode 600, a removal takes what the add-on remembered about the server |
| `torznab_servers.py` | the Torznab servers file (API keys, mode 600): its one reader and writer, and the keys the log scrubber masks |
| `score.py` | title parsing, filters and the ranking |
| `providers.py` | TorBox and Real-Debrid, and the guard that steps over a provider refusing the account |
| `torbox_device.py` | TorBox sign-in with a device code: the code to show, the wait for approval, the QR rows |
| `subs.py` | donor subtitles: finding your language in another release of the same episode, retiming it |
| `extsubs.py` | external subtitles from Subdl and OpenSubtitles |
| `player.py`, `mpv.py` | the mpv window on Hyprland, the IPC client, the resume store and the watched rule |
| `playstate.py` | the one record of an in-flight play, read by the popout, the player and the CLI |
| `pins.py`, `searches.py` | a viewer's remembered pick per show, and each episode's last search for the release list |
| `covers.py`, `thumbs.py` | cover images and episode stills, downloaded and converted by the CLI, never by the shell |
| `say.py` | every sentence a viewer reads, kept apart from the log's words |
| `notify.py` | desktop notifications: one `Notify` call on the session bus, the click as a token naming a private file, the ready sound |
| `sessionbus.py` | the plugin's own D-Bus client (standard library): the address, `AUTH EXTERNAL`, `Hello`, one call and its reply |
| `trace.py` | renders one trace id from the log as a timeline |
| `private_args.py` | the words a launch keeps off its command line: `OMAANIME_ARGS`, read and dropped by the command's entry, built for the CLI's own launches |

## One play

`omarchy-anime play <anilist id> <episode>` (what a click runs), stage by stage. Each stage writes log
lines under the play's trace id, and `omarchy-anime trace last` renders them in order.

1. **Map the episode.** ani.zip turns the AniList entry into AniDB ids and the episode's other
   numbers (a continuing or absolute number some releases use). A sequel with no entry of its own can
   inherit its prequel's mapping.
2. **Pin.** If you pinned a pack that holds this episode, it is played from the pack with no search.
3. **Search.** Every loaded indexer add-on is asked for the episode (`episode_search`), in the
   `indexers` order, each under its own time budget. The answers are merged by infohash. When fewer
   than three releases have people sharing them and packs are allowed, a second pass asks for packs. With no add-on loaded the play stops here and says where add-ons go.
4. **Filter and rank.** The add-ons set aside releases for another show or season (`query.reject`,
   with the plugin's own title check). The plugin then sets aside releases nobody is sharing, packs
   whose range cannot hold the episode and sizes that make no sense. Each keeps its reason, and the
   release list shows them. The rest are ranked: your subtitle language, resolution, people sharing,
   size per episode, group preferences and what the plugin has learned about each group.
5. **Read subtitle tracks.** For the best-ranked releases, an add-on that offers subtitle tracks
   says which languages each release really carries. Up to `subtitleCheckCount` releases are read,
   best first, until one has your language; a release no add-on can read is passed over without
   counting. The reads stop sooner when `subtitleCheckSeconds` is spent, so a slow or unreachable
   site costs about that long; an add-on that fails two reads in a row, or whose read is given up on,
   is passed over and the others are still read. A release several add-ons returned is read
   through the first of them that offers it.
   A verified track in your language ranks up, a release read and found without it ranks down.
6. **Learn pack files.** A pack's file list (from the provider, the add-on's `file_list`, or TorBox's
   torrent info) picks the episode's file and compares packs by episode size.
7. **Refusals.** When the releases name several shows of one franchise, when every release was set
   aside, or when `subtitlePolicy` is `require` and neither a release, a donor nor a subtitle database
   has your language, auto-play does not guess. It says why, and the release list (`p`) lets you
   choose.
8. **Providers.** The providers are tried in order, up to `probeCount` releases each. TorBox is
   asked about every candidate at once; Real-Debrid per release. When nothing said which subtitle
   languages a release carries, its stream's header is read with `ffprobe`. A provider that refuses
   the account three times in a row is stepped over for a while.
9. **Play.** mpv loads the stream. If the release lacks your language, a donor track (or up to three
   external subtitles) is added as a separate track a few seconds in.
10. **Progress.** The service reads the position from mpv; past `watchedThreshold` the episode is
    written to AniList and read back. A write that fails is queued and retried.

## Indexer add-ons

The plugin ships none. A viewer drops Python files into `~/.config/omaanime/indexers/`; every
command that needs releases loads them afresh (`indexers.load`), in the `indexers` order and then by
name. Each call runs on a separate thread under a time budget; an add-on that raises, hangs or answers
the wrong type is logged and skipped, never the play itself. The interface is documented in
[Indexer add-ons](addons.md).

## Sources of truth

| Data | Truth | Copies |
|---|---|---|
| What you watch, and your progress | AniList | the `anilist/` caches, the queue of progress writes not yet sent |
| The shows around a later season (a first season: only when a release name states a part its titles do not) | AniList | `anilist/franchise/` (8 days; a day while a related show's count is not known or none is related; up to 30 days while AniList cannot be read) |
| Episode numbering and cross-site ids | ani.zip | `mappings/` |
| Whether a TMDB id is a movie's or a TV series' | Fribb's anime-lists | `tmdb-kinds.json` (a week; an older one is used while a refresh fails) |
| Releases for an episode | your indexer add-ons | `searches/` (the last search, for the release list) |
| Whether a provider has a release | the provider | nothing lasting; a refusal is remembered for `providerBlockMinutes` |
| Settings | `shell.json` entry, then `config.json`, then the defaults | none |
| Tokens | `~/.config/omaanime/<service>-token` | none |
| An in-flight play | `play-status.json` | what the popout and the player show |

Everything under `~/.cache/omaanime/` can be deleted; the plugin rebuilds it. Deleting it also
forgets pins, resume positions, the queue of unsent progress and what the plugin learned about groups.

## Logging

One JSON line per stage, in `~/.cache/omaanime/log/<UTC date>.jsonl`: time, level, trace id,
component, stage, result (`PASS`, `FAIL`, `SKIP`, …), duration, a `suggest` naming what to do, and the
data behind the decision. Every external call logs what was asked, the status and how long it took.
Lines are scrubbed of tokens at the point they are written, the file is mode 600, and files older than
14 days are removed.
