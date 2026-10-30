# Privacy

[Back to the README](../README.md)

`omarchy-anime` in a command here is the plugin's own command, on your PATH once you have run the link line in
the README's [Install](../README.md#install).

### What is stored, and where

| What | Where |
|---|---|
| Your tokens and keys (AniList, TorBox, Real-Debrid, Subdl, OpenSubtitles) | `~/.config/omaanime/<name>-token`, mode 600, in a folder at mode 700. Nowhere else: not in `shell.json`, not in a log line, not on a command line. The plugin refuses to read a token file others can read |
| Your settings | `~/.config/omarchy/shell.json` (the plugin's entry) and, if you create it, `~/.config/omaanime/config.json` |
| Your indexer add-ons | `~/.config/omaanime/indexers/`. An add-on runs as you, so one is loaded only while the folder, the folder above it and the file are yours and not writable by others (by your group only when it is yours alone, as with a umask of 002), and a link only when it leads to a file inside the folder. It is read from the file itself each time: no `__pycache__` is used or written. Settings and `omarchy-anime addons check` name a refused one and why |
| Your Torznab servers and their API keys | `~/.config/omaanime/torznab-servers.json`, mode 600, beside the tokens and kept the same way: never in `shell.json`, a log line or a command line (Settings sends the key to the CLI over stdin), shown by its last four characters (none under 16), and not read while others can read it. Written only by **Add** and **Remove** in Accounts (or `omarchy-anime torznab`), one at a time under a lock, which keep any field they do not know, read it back, and refuse to rewrite a file they could not read whole |
| Caches: AniList lists, the shows around a later season, or a first season whose releases state a part (`anilist/franchise/`, 8 days; a day while a related show's count is not known or none is related; up to 30 days while AniList cannot be read), ani.zip mappings, covers and episode pictures, the last search per episode, borrowed and external subtitles, pins, resume positions, progress writes not yet sent, what was learned about groups, each add-on's own cache (`indexers/<name>/`; for Torznab, `indexers/torznab/`: each feed's capabilities, a refusal such as a wrong key for 10 minutes, a pause a feed asked for, and in `servers/` what each server is and a Prowlarr's indexer ids, names and priorities, with a fingerprint of the key, never a key) | `~/.cache/omaanime/`, each file mode 600 in folders at 700 from the moment it is written (a folder an earlier build made wider is tightened on the next write). Covers and pictures prune themselves (30 days, and a size limit), the borrowed subtitles keep 30 days and `subsCacheMaxBytes`, and the external subtitles 30 days; all of it can be deleted |
| The log | `~/.cache/omaanime/log/<date>.jsonl`, mode 600 in a folder at 700, kept 14 days |

**Every log line is scrubbed as it is written.** A URL keeps its host and path and loses any
credential parameter (`?token=`, `api_key=`, `passkey=` and similar) and any user name or password
(`user:password@`); any text equal to a token you stored is
replaced wherever it appears, a key you paste included, from before it is first checked, and a token another
command saved meanwhile; and `omarchy-anime trace` scrubs again when it renders. A token is shown
in the plugin's own output by its last four characters only. `omarchy-anime trace`, which a bug report asks for,
prints a service's control characters as `\xNN` (`\u00NN` in its `--json` and `--tail` forms; never as bytes your
terminal would act on) and your home folder as `~`.

**Signing out** (`omarchy-anime signout <name>`, or **Sign out** in Accounts) deletes the token file, a copy a
killed save left behind, and `accounts.json`, and:

- AniList: the cached lists (Watching and the others, Top 100, Airing, and their saved copies), and any queued
  progress write that names no account (an earlier version queued it). A queued write made under the account stays
  queued and is sent only to that account if it signs in again, never to another one.
- TorBox: the downloads you asked to be told about (`pending-downloads.json`), the record of the next episode
  added ahead (`prefetch.json`), and TorBox's entry in `provider-blocks.json`.
- Real-Debrid: its entry in `provider-blocks.json`.
- Subdl, OpenSubtitles: that service's entry in `extsubs/quota.json`.

Signing in again, or with another account, removes `accounts.json`, AniList's cached lists (for an AniList sign-in)
and the queued writes that name no account; the other records above stay.

### What is sent, and to whom

- **AniList** (`graphql.anilist.co`): your lists, your progress and your searches, with your token.
- **ani.zip** (`api.ani.zip`): the AniList id of each show you open or play, to map its episodes.
- **Image hosts** named in the AniList and ani.zip answers: cover images, episode stills and show
  art, fetched once and cached. They see the picture's address and your IP address; no account is
  involved.
- **Your indexer add-ons**: whatever each one sends to the index it searches. The plugin hands it the
  show's titles and ids, the episode, your own words when you search with them, and (when it offers
  those capabilities) a release id to read subtitle tracks, files or show ids for. The plugin ships no
  add-on for any site, so it contacts no index by itself.
- **Your own Torznab servers** (your Prowlarr or Jackett), only the ones you added in Accounts (or listed in
  `torznab-servers.json`), and nothing until you add one. **Add and check** and **Check** ask it at once what
  it is and read its indexer list and capabilities again; a server that fails the check on adding is not
  saved and asked nothing more. Otherwise: once, to tell what a server is, Prowlarr's status
  endpoint (`/api/v1/system/status`) and the capabilities call; for a Prowlarr, its indexer list
  (`/api/v1/indexer`) once a day; each feed's capabilities once a day; then per play the show's titles
  with the episode's number (or your own words) and the categories (anime, and movies for a film). Your
  API key goes to a Prowlarr in the `X-Api-Key` header, to any other feed as the `apikey` parameter. Of
  Prowlarr's answers only its name, version and URL base, and each indexer's id, name and priority, are
  kept; an indexer's own settings are never logged, and no error body of any call to a Torznab server
  (Prowlarr's own API, its indexer feeds, Jackett or any other feed) is: the log line gives its length in
  bytes. Prowlarr answers a failure with .NET stack text, and messages that can name its local paths;
  Jackett answers a failed search with the whole exception text. Of an error answer the plugin uses only
  the HTTP status, a `Retry-After`, a 401's `WWW-Authenticate`, and a Torznab `<error>`'s code and
  description. The log keeps the code when it is a number of up to five digits (else `?`), and the
  description's first line, cut before any nested exception or stack frame, at most 200 characters, with
  the key masked and local paths shown as `<path>`: a quoted path whole (as .NET quotes them), and up
  to its first space a UNC path (`\\host\share\…`), a `file://` address, or any other path of two or more
  parts that starts with `/`, `~/` or a drive letter. The links in their answers
  are never opened, and a redirect to another host (other than the same host's https address) is not
  followed while the key is attached. A server on another machine reached over plain `http://` is used, but
  Accounts says after each check that its key travels unencrypted, and the log warns; use `https://` unless
  that network is only yours.
- **Your AI agent**, only when you press one of the two buttons under **Indexers** in Settings: the prompt, which
  names this plugin's command and your add-on folder and nothing else. What the agent sends from then on, and to
  whom, is up to it and you.
- **TorBox**, when you sign in with a code: a request for a code, naming this app ("OmaAnime"), then
  the device code TorBox gave back, every few seconds, until you approve it. No key is sent: TorBox
  returns one.
- **TorBox**, with your key: the infohashes of the releases being considered (whether it already has
  them), a magnet naming one release to add, with seeding switched off, and the request for the file; while a
  download you asked to be told about is pending, its download list (every 30 s, growing to 5 minutes); with
  `prefetchNext` on, the next episode's add. When Accounts is opened (at most every 10 minutes) or refreshed (a
  sign-in, a sign-out, a Torznab change), `/user/me`, to check the key and read the plan.
  When no add-on lists a pack's files, the infohash of up to four packs is sent to TorBox's torrent
  information lookup, which needs no key and is sent none; only when TorBox is one of your providers
  and has a token (`torrentInfoSeconds`, 0 turns it off).
- **Real-Debrid**, with your token: a magnet naming one release, and the request for the file; and, when
  Accounts is opened or refreshed, `/user`, to check the token.
- **Subdl** and **OpenSubtitles**, only when you added their keys. For a play, only when no release has your
  language or the track borrowed from another release has timing that could not be checked:
  your language, the show's TMDB or IMDB id (or its title), the season and episode, and the download
  of the files chosen. And when Accounts is opened or refreshed, a check that the key works: Subdl's
  `/api/v2/me`, and for OpenSubtitles a download request for file 1: OpenSubtitles answers a working key that this
  file id is invalid, and nothing is downloaded.

**The player** is your own mpv with your `mpv.conf`, started so that it never writes what it plays to disk: the
plugin passes `--save-position-on-quit=no --write-filename-in-watch-later-config=no --save-watch-history=no
--log-file=`, because a TorBox stream's address carries your key (the plugin keeps its own resume point). For the
same reason it passes `--tls-verify=yes`: the player checks the stream server's certificate, and when the certificate
does not check out (a network that intercepts the connection) the episode does not open, the request and your key
are never sent, and the player's own line, the popout and the player's corner line all say one sentence: "The
player couldn't confirm TorBox's server was genuine, so it didn't open episode 7.", then "Try again on another
network". Your scripts still load, mpv-mpris (`/etc/mpv/scripts/mpris.so`, the media keys) included, and mpv-mpris
publishes the playing address, key included, as `xesam:url` on your desktop session bus: any program running as
you can read it there, as it can read your token files; no other user can.

The plugin never joins a torrent swarm: nothing is uploaded to other peers, and nothing is fetched
from a package registry while it runs.
