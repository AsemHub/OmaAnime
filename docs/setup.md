# Setting up

[Back to the README](../README.md)

`omarchy-anime` in a command here is the plugin's own command, on your PATH once you have run the link line in
the README's [Install](../README.md#install).

## Requirements

- **Omarchy 4.0.3 or later** — Quickshell 0.3.1 shell, Hyprland with the Lua config
- **mpv** (tested on 0.41), **socat**, **ffprobe** (ffmpeg) — the player, its liveness probe, and
  the subtitle-track check
- **Python 3** — standard library only, no pip packages (tested on 3.14, the version Omarchy ships)
- **An AniList account** — the list is the source of truth for what you watch
- **A TorBox account** (Essential is enough) — the stream provider. Real-Debrid works as a secondary
  provider but rejects most anime releases; see [Providers](#providers). Without one of the two
  nothing plays: the plugin never streams a torrent itself.
- **Something to search** — your own Prowlarr or Jackett, added in Accounts, or an indexer add-on;
  the plugin ships none (see [Indexer add-ons](addons.md#indexer-add-ons))

Optional: a Subdl key and an OpenSubtitles API key, for subtitles no release carries.

## Install

```sh
omarchy plugin add https://github.com/AsemHub/OmaAnime --enable
omarchy restart shell
mkdir -p ~/.local/bin && ln -sf ~/.config/omarchy/plugins/io.github.asemhub.omaanime/bin/omarchy-anime ~/.local/bin/
```

`omarchy plugin add` may print "omarchy-shell is not responding" while the shell loads the new plugin (it gives
each ask for the shell's plugin list 2 seconds, and asks again): when a line starting with `Enabled` follows, the
add and the enable both succeeded and the restart on the next line loads the plugin; if none follows, run
`omarchy plugin enable io.github.asemhub.omaanime` after the restart.

The widget lands in the bar's right section. The last line puts the plugin's command, `omarchy-anime`, on
your PATH: Omarchy adds `~/.local/bin` to it for the whole Hyprland session, whatever your shell. Every
terminal command in these guides and in the plugin's own messages is
written that way; without the link, type the full path,
`~/.config/omarchy/plugins/io.github.asemhub.omaanime/bin/omarchy-anime`, in its place. The bar, the overlay
and the player need none of this. Then:

1. **Connect your accounts** (below): AniList, and TorBox or Real-Debrid.
2. **Add something to search**: your Prowlarr or Jackett under **Torznab servers** in Accounts
   (below), or an indexer add-on, a Python file in `~/.config/omaanime/indexers/`
   ([Indexer add-ons](addons.md#indexer-add-ons) has the interface and an example). The plugin ships none. Settings can
   also hand your own AI agent a ready prompt for either ([Ask your AI agent](addons.md#ask-your-ai-agent)).
   Until one is set up, your lists work but a play stops at once with "No indexer is set up. Add your
   Prowlarr or Jackett in Accounts, or add an indexer add-on." and says where add-on files go.
   Settings lists what loaded.

**From the Settings window** (**Settings** in the Anime bar popout, or
`omarchy-shell omaanime browse accounts`) — **Accounts** is the first section. Settings is a normal window: drag it
by its title, put it beside your browser and Alt-Tab between the two, so nothing has to be closed while you copy a
code, a token or a key from one to the other. `Escape` or **✕** closes it.

**TorBox: sign in with a code (the default).** *Sign in* on the TorBox row shows a 6-digit code, a
short address and a QR code. Open the address (or scan the code with your phone), sign in to TorBox
if you are not already, enter the code and approve. The code is already on your clipboard (the row says "Copied to
the clipboard"), so paste it. Settings picks the key up by itself within
about 15 seconds of the approval and shows the account. The code is good for 10 minutes; if it runs out,
or the approval is declined, the row says so and **Get a new code** starts again. TorBox's approval
page may name the app "Third Party App" rather than this plugin. **Paste a token instead** is on the
same row if you would rather copy the key from <https://torbox.app/settings>.

**Every other provider** has *Sign in*, which opens its token page in your browser and gives you a
field to paste into. The field shows dots, the token is checked against the provider before anything
is written, and there is a **Clear clipboard** button next to it.

**Torznab servers: your Prowlarr or Jackett.** Under the providers, **Add a server** opens two fields:
the address and the API key (it shows dots). For Prowlarr, paste its own address as your browser shows
it (`http://host:9696`, with its URL base if it has one) and its API key, which Prowlarr shows under
*Settings > General*; every indexer in it that offers anime or movies is then searched. For Jackett,
paste the Torznab feed address of `all` (or of a filter such as `tag:anime`) and Jackett's API key,
shown at the top right of its dashboard. Any other Torznab feed works the same way.

**Found on this computer.** Each time Accounts opens, the plugin asks this computer's Prowlarr (port 9696) and
Jackett (port 9117) ports, without a key, and offers each one that answers and is not added yet: **Add Prowlarr**
or **Add Jackett** opens the add row with the address filled in, and you paste the key. Adding one leaves the other
offered; a server counts as added by its host and port (`localhost` and `127.0.0.1` are the same).

**Add and check** asks the server what it is before saving (up to 20 seconds; Settings stays usable
meanwhile) and says what it found: "Saved: Prowlarr with 12 indexers." or "Saved: a Torznab feed.". When
something is wrong it says what, and the server is not saved: a wrong API key, the server asks for a login
(a proxy in front of it), not a Torznab or Prowlarr address, no anime category, or a Prowlarr with no
indexer for anime or movies. When a new server only did not answer (no answer, no answer in time, a pause
it asked for, an error on its side), it is saved anyway and Settings says to check it again in a moment,
or, for a Prowlarr whose indexers it paused or could not search, where Prowlarr says why.
Adding an address that is already there replaces its key, but only when the check passes; a key that
fails leaves the old one, and what the plugin knew about the server, as they were.

Each server's row shows what it is and the key's last four characters (none of it when the key is under
16 characters). **Check** asks it again and names each Prowlarr indexer that cannot be searched now. One that
Prowlarr itself could not search (its site did not answer Prowlarr, say) reads "Prowlarr could not search it",
and Settings says that Prowlarr's own log says why (*System > Events* in Prowlarr).
**Remove** (click twice) deletes it and everything the plugin remembered about it. A row whose address the
plugin cannot use (a user name, a key or a query in it, or the same feed twice) says why, and can still be
checked and removed. Settings names a row by an id, so no address is ever on a command line.

**Or from a terminal**, if you prefer:

```sh
omarchy-anime auth torbox --device                 # prints a code and an address; approve it there
omarchy-anime auth anilist                         # prompts, and does not echo what you type
printf '%s\n' "$TORBOX_KEY" | omarchy-anime auth torbox --token -   # or paste a TorBox key instead
omarchy-anime accounts                             # what is connected, and until when
omarchy-anime signout realdebrid                   # removes the token and what it cached
# the names: anilist, torbox, realdebrid, subdl, opensubtitles

omarchy-anime torznab add http://127.0.0.1:9696    # asks for the API key without echoing it, checks, saves
printf '%s\n%s\n' "$URL" "$KEY" | omarchy-anime torznab add --stdin   # or both on stdin, one line each
omarchy-anime torznab list                         # your servers, each with its id and its key masked
omarchy-anime torznab check                        # every server (or one: check <address>, or --id <id>)
omarchy-anime torznab remove http://127.0.0.1:9696 # removes it (or --id <id>) and what the plugin remembered
```

`auth torbox --device` waits up to 10 minutes for the approval, asking TorBox every 5 to 15 seconds
(less often if TorBox asks for that), then checks and saves the key exactly as a pasted one. A dropped
connection along the way only means it asks again. It ends with a clear message when the code
expires or the sign-in is declined, and Ctrl-C stops it without saving anything; run it again for a
new code.

`--token` accepts nothing but `-`. A token given on a command line lands in your shell history and
in `/proc/<pid>/cmdline`, so the plugin refuses that form outright.

| Provider | Where the token comes from |
|---|---|
| AniList | the URL `auth anilist` prints (or *Sign in* in Settings) — approve, and copy what the page gives you |
| TorBox | nowhere: *Sign in* (or `auth torbox --device`) gets it for you; to paste one, <https://torbox.app/settings> |
| Real-Debrid | <https://real-debrid.com/apitoken> |
| Subdl (optional, external subtitles) | <https://subdl.com/panel/api> |
| OpenSubtitles (optional, external subtitles) | <https://www.opensubtitles.com/en/consumers> |

Tokens live at `~/.config/omaanime/<provider>-token`, mode 600, in a directory at mode 700, and
**nowhere else** — never in Omarchy's `shell.json`, never in a log line, never on a command line, and
never in this plugin's own output beyond the last four characters. The plugin refuses to read a token
file that is group- or world-readable. A token the provider does not recognise is not written at all,
so a mistyped one cannot replace a working one, and one shorter than 16 characters (none measured is: the shortest,
an OpenSubtitles key, has 32) is refused before it is sent anywhere.

## Providers

A play needs somewhere to stream the release from. The plugin tries the providers in order and skips
any whose token is missing. It never streams a torrent itself, so with no TorBox or Real-Debrid
token a play stops at once with "Playback needs TorBox or Real-Debrid."

- **TorBox** (default, recommended). Checks all candidate releases against its cache in one request,
  then serves the episode over a CDN link that supports seeking. Paid, from about $3/month.
  Everything the plugin adds is added with seeding off, whatever your TorBox setting says. A release
  TorBox said it has is added only if it still has it; if that cache expired in the meantime, the
  next release is tried rather than starting a download. A release TorBox does not have yet is
  added (it then downloads there) only after every release it has was tried, and not at all when a
  copy it has without your subtitle language is going to play anyway. TorBox allows 60 of those adds
  per hour: past that, the log names the limit and the rest of that play adds only releases TorBox
  already has. When TorBox answers that it has no download server free, the play says "TorBox can't
  start downloads right now." rather than blaming the release, adds nothing more it would have to
  download, and still plays a release TorBox already has; a pinned pack stays pinned. When TorBox
  answers with an error on its own side (a database or download-server error, or an error checking
  your account that is not a bad token), the release is not marked failed, a pinned pack stays
  pinned, and the other releases are still tried; if none of them got an answer of its own, the
  play says "TorBox ran into an error on its side."
- **Real-Debrid** (secondary). Supported and kept, but it often refuses anime: its filter on
  release names answers HTTP 451 for most anime WEB releases, so the play moves on to the next
  release or provider. TorBox is the provider that works for anime. Leave Real-Debrid in the list if
  you have an account; it never blocks the rest.

A provider that refuses three releases in a row is stepped over for the rest of that play and for
`providerBlockMinutes` (default 15) afterwards, then asked once more. Your `providers` list is not
changed and nothing here can switch a provider off for good: renew a plan and the next play picks it
up on its own. `omarchy-anime trace last` shows the decision as a `provider.block` line naming the
provider, how many refusals it took and what they cost.

Releases come from the indexer add-ons you installed; the plugin ships none. Episode numbering comes
from `api.ani.zip`, and whether a show's TMDB id is a movie's or a TV series' from Fribb's anime-lists on GitHub. The
plugin reads them, it does not upload anything.

## Uninstall

Quit the player first (`q` in its window): `omarchy-shell omaanime stop` stops playback but leaves
mpv running.

```sh
omarchy plugin disable io.github.asemhub.omaanime
omarchy plugin remove io.github.asemhub.omaanime
rm -f ~/.local/bin/omarchy-anime                # the link from Install, which would point at nothing now
rm -rf ~/.config/omaanime ~/.cache/omaanime     # tokens, settings, your indexer add-ons, caches, logs
```

The last line is optional and removes everything the plugin kept, including your add-ons in
`~/.config/omaanime/indexers/`; keep that folder if you plan to reinstall.
