# Settings

Every setting the plugin reads, what it does, its default and the range the plugin accepts. The
defaults here are the plugin's own (`DEFAULT_SETTINGS` in `lib/omarchy_anime/core.py`); a test
checks that every key in the manifest and in that table is documented here with that default.

`omarchy-anime` in a command here is the plugin's own command, on your PATH once you have run the link line in
the README's [Install](../README.md#install).

## Where a setting is changed

**The settings sheet.** Click **Settings** in the Anime bar popout, or run
`omarchy-shell omaanime browse settings` (inside the Browse overlay, `,` opens it too). Omarchy 4.0.3 has no settings screen for a bar widget, so
the plugin brings its own. Every change is saved as you make it. The keys marked *advanced* below sit
behind the sheet's **Advanced** disclosure (`omarchy-shell omaanime browse advanced` opens the sheet
with it expanded).

**From a terminal:**

```sh
omarchy-shell omaanime setSetting watchedThreshold 90     # the value is JSON
omarchy-shell omaanime setSetting subtitleLanguage '"eng"'
omarchy-shell omaanime setSetting autoNext reset          # remove it, so the layer below applies
omarchy-shell omaanime settings                           # every value, and which layer it came from

omarchy bar set io.github.asemhub.omaanime watchedThreshold 90 --json
```

`omarchy bar set` without `--json` stores the value as a string (`"90"`, `"true"`). The plugin reads
those as the number and the boolean they say (`"true"`/`"false"`, `"on"`/`"off"`, `"yes"`/`"no"`,
`"1"`/`"0"`), but `--json` keeps the file typed. A boolean it cannot read (`"maybe"`) is the default,
and `omarchy-anime trace last` names it.

## Where a setting lives

Three layers, each one beating the one above it:

1. the plugin's defaults (the tables below);
2. `~/.config/omaanime/config.json` (`$XDG_CONFIG_HOME/omaanime/config.json`), if you create it. Every
   key can live here; it is the file to copy between machines. The plugin never writes it. A file that
   is not valid JSON stops every command with a message naming the file;
3. the plugin's entry in `~/.config/omarchy/shell.json`: what the sheet, `setSetting` and `omarchy bar
   set` write.

The sheet says under each setting which layer it is reading, and **reset** removes the key from
`shell.json` so the layer below applies again.

A list is stored in `shell.json` comma-joined (`torbox,realdebrid`), not as a JSON array, because
Omarchy's own writer flattens a top-level array to exactly that. `config.json` takes either form.

A number outside its range, from a hand-edited file or `omarchy bar set`, is replaced by the default
and logged as a warning (`omarchy-anime trace`); it never fails a play. The ranges below are the ones
the plugin enforces; the sheet's own controls may offer a narrower choice.

## Playback and subtitles

### `subtitleLanguage`

Default: `"eng"`

Your subtitle language, as an ISO 639-2 code. A release that carries this language ranks higher; when the
release that plays lacks it, the plugin can borrow it (see `sideloadSubtitles`) or fetch it from Subdl and
OpenSubtitles. The sheet offers the 38 languages that release track lists carry and the subtitle databases
offer: Arabic `ara`, Basque `baq`, Bosnian `bos`, Catalan `cat`, Chinese `chi`, Croatian `hrv`, Czech `cze`,
Danish `dan`, Dutch `dut`, English `eng`, Filipino `fil`, Finnish `fin`, French `fre`, Galician `glg`, German
`ger`, Greek `gre`, Hebrew `heb`, Hindi `hin`, Hungarian `hun`, Indonesian `ind`, Italian `ita`, Japanese
`jpn`, Korean `kor`, Malay `may`, Norwegian `nor`, Persian `per`, Polish `pol`, Portuguese `por`, Romanian
`rum`, Russian `rus`, Sinhala `sin`, Slovak `slo`, Spanish `spa`, Swedish `swe`, Thai `tha`, Turkish `tur`,
Ukrainian `ukr`, Vietnamese `vie`. Another code of one of them is read as it (`zho` is `chi`, `fra` is `fre`);
a code that is none of them is refused with a warning in `omarchy-anime trace` and the default is used. A
language you set before English became the default (`ara`, …) stays as you set it.

Six regional choices are offered too: Spanish (Latin America) `es-419`, Spanish (Spain) `es-ES`, Portuguese
(Brazil) `pt-BR`, Portuguese (Portugal) `pt-PT`, Chinese (Simplified) `zh-Hans` and Chinese (Traditional)
`zh-Hant`. They rank releases as their language does (release lists carry no region). In a file with several
tracks of the language the player shows yours first (`zh-CN` counts as Simplified, `zh-HK` as Traditional, `es-MX`
as Latin America), then a plain one, then any other; a file with only another region plays it. Subdl and
OpenSubtitles are asked for your region first, then the language's other files, and your region's files are tried
first; Subdl has one Spanish code (`ES`), so both Spanish choices ask it for all Spanish files. Any region of a
language counts as that language
(`ar-SA` is Arabic) with or without a regional choice. The same choices apply to `subtitleFallbackLanguage`.

### `subtitleFallbackLanguage`

Default: `"eng"`

The second language, used only when no release of the episode has yours (no read track list has it and no
release name states it). Then a release whose track list has this language ranks 15 points higher; every other
release is scored by its name, so the pick does not depend on which releases the plugin happened to read.
`off` leaves the second language out: every release is scored by its name alone. When no track of the playing
release has your language, the second language also chooses the track mpv shows; `off` leaves mpv's own
choice. In the measurement of 50 episodes, `off` picks no other episode: its 8 such picks were all
one episode's `Part 2 - 10`, a filter defect since fixed, not a property of `off`. Any language of `subtitleLanguage`'s
list can be chosen; when it is your own language it does nothing.

### `subtitlePolicy`

Default: `"prefer"`

`prefer` plays the best release even without your language and says so in the popout. `require`
refuses to play rather than show another language. `require` means "your language will be on screen",
not "the release embeds it": a play whose track can be borrowed from another release, or from a
subtitle database, still goes ahead.

### `dubs`

Default: `"avoid"`

What to do with a dubbed release. A release is dubbed when its name says so (`[English Dub]`, `Eng Dub`, `Dubbed`,
`[Arabic Dub]`, `Dublado`, a bare `[Dub]`). A name is no dub when it states audio in more than one language (`Dual
Audio`, `Multi-Audio`, `Triple Audio`), names the show's own language anywhere (`[Japanese Audio]`, `[JP/EN]`), or names
a subtitle beside the dub (`Sub/Dub`, `[Eng Dub + Eng Sub]`): those offer both. The group tag (the leading bracket) and
the show's own titles are not read for it. A release is also dubbed when ffprobe read its audio and every track is in
another language than the show's original one (Japanese for a Japanese show, from AniList's country of origin). English
alone does not count, since a track with no language reads as English; a show whose country is not known is judged by
the name alone.
`avoid` never plays one while a release in the original language exists: when TorBox has only a dub cached, the best
original-audio release is added to TorBox and the play waits for its download, with TorBox's percentage, the speed and
the time left on screen once it reports progress. It says so at once and offers three choices, on the player's loading
screen (by key), in the overlay and in the bar's popout (by button or key): watch the dub now (it names your subtitle
language when a verified track for it can be side-loaded), keep waiting (`w`: the question goes, the wait goes on), or
be told when it's ready. Nothing but TorBox, you, or three hours ends the wait; at three hours the play stops waiting
without failing and keeps the same three choices on screen; the download carries on in TorBox. When it is ready, see
`torboxWaitSeconds` for whether it starts by itself.
"Be told when it's ready" says so on the player (then hides it) or in the overlay, and sends a desktop notification
once TorBox has the episode ("Episode 3 is ready in Japanese with Arabic subtitles. Click to play it."), checking every
30 seconds at first and every 5 minutes at most. Clicking the notification plays it. On Omarchy the toast stays up 30
seconds, then the notification is in the notification history, still clickable there while it is among the ten newest
notifications, and after a shell restart. A request older than a day is checked
once more; if TorBox still does not have the episode, or no longer has the download, the notification says so instead,
and clicking it plays the dub when there was one. A wait longer than `torboxWaitSeconds` (a minute by default) that
ends with the episode opens it paused ("Space plays it.") and sends the same notification, with a sound, so nothing
plays to an empty room; a shorter one plays it. When every
release found is a dub, the dub plays and the loading screen says only a dubbed version was found. `allow` ranks dubs as before (lower
than the rest) and plays a cached one rather than waiting. A release you choose yourself (`p`) and a pinned pack play
whatever they are.

### `preferredResolution`

Default: `1080`

Range: `240` to `4320`

The picture height the ranking rewards. Other heights still play. The sheet offers 720, 1080 and 2160.

### `subtitleFont`

Default: `""`

The font mpv uses for subtitles (`--sub-font`), which is what a styled subtitle falls back to when the
font it names is missing. Empty means automatic: Noto Naskh Arabic UI for Arabic and Persian, Noto Sans
Devanagari UI for Hindi, Noto Sans CJK SC for Chinese and Noto Sans CJK JP for Japanese, mpv's default for every
other language. A family you type is used only when `fc-match` confirms it is installed. It applies
from the next episode.

### `watchedThreshold`

Default: `85`

Range: `50` to `100`

Percent of the episode after which it is written to AniList as watched. Reaching the end of the file
also counts. A stream that stops well short of the end (a dropped connection) does not: the position is
kept, so the next play resumes there, and auto-next waits.

### `autoNext`

Default: `false`

When an episode ends, start the next episode that has aired.

### `prefetchNext`

Default: `true`

Once you have watched two minutes of an episode, the next one is downloaded at TorBox if TorBox doesn't
have it yet, so it starts at once when you get to it (by auto-next or by playing it). Two minutes means
two minutes into the file and two since it loaded, so a resume or a skip ahead waits two more minutes; a
pause counts, so an episode left open for two minutes counts as watched.
The release is the one its play would pick, in your languages; nothing plays and nothing is shown.

At most one download per episode, and only when the next episode has aired, is not past the show's last,
is not one you have already watched (unless you are rewatching the show), and TorBox comes first in
`providers` with a token. A pinned pack is left alone. When TorBox refuses (its hourly limit, no server
free), nothing is retried: the episode's play adds it as before. `omarchy-anime trace last` passes over
the prefetch's own runs; the playing episode's trace names the prefetch's trace (`prefetch spawn`).

### `allowBatches`

Default: `true`

Packs (one release holding several episodes) are candidates too, and the episode's own file is
streamed out of the pack. When the first search finds fewer than three releases with people
sharing them, the indexer add-ons are asked once more, for packs.

### `sideloadSubtitles`

Default: `true`

When the release being played has no track in your language, borrow one from another release of the
same episode (a donor, found through an indexer add-on that offers subtitle tracks) and, when none has
it or its timing could not be checked, look in Subdl and OpenSubtitles if you added their keys in
Accounts.

### `subtitleSyncMaxOffset`

Default: `60`

Range: `0` to `120`

How far, in seconds, a borrowed subtitle file may be shifted to match the release being played. `0`
loads it unshifted.

## The bar and the Browse overlay

### `showTitleInBar`

Default: `false`

Show "Title · Ep N" next to the bar icon while something plays.

### `maxTitleWidth`

Default: `180`

Range: `0` to `560`

Width in pixels, in the bar's font, before the bar title ("Title · Ep N") is cut with an ellipsis.
`0` is unlimited.

### `pollIntervalSec`

Default: `5`

Range: `2` to `30`

How often, in seconds, the bar asks mpv for the playback position.

### `showCompletedInTop`

Default: `false`

Show the shows you have completed in the Top 100 view (`c` there toggles it).

## Indexer add-ons (advanced)

### `indexers`

Default: `[]`

Add-on file names without `.py`, asked in this order. Files in the add-on folder that the list does
not name are asked after these, by name, so an empty list means "every add-on in the folder, by name".
A name with no file is shown in Settings as not loaded. See [Indexer add-ons](addons.md).

### `indexerTimeoutSeconds`

Default: `60`

Range: `5` to `300`

How long one add-on call may take (an episode search, an own-words search, and each subtitle-track,
file-list or show-id call). An add-on that takes longer is skipped for that call and the other add-ons
are used; one that runs past it on a per-release call (subtitle tracks, files, show ids) is not asked
for that capability again in that command. While a play checks subtitle tracks, each read gets what is
left of `subtitleCheckSeconds` instead, when that is shorter (never under 5 seconds); an add-on whose
read is cut there is not asked for subtitle tracks again in that command either.
Reading one subtitle track's text gets at most 10 seconds, or this, if lower.
The built-in Torznab add-on asks its feeds side by side within this time and answers a little before it
runs out, so a slow feed costs that feed only. Jackett's `all` feed waits up to 40 seconds for its own
indexers, so keep this at 50 or more if you use one.

### `preferredGroups`

Default: `[]`

Fansub groups to rank up, each as `name` (worth 8 points) or `name=points` (from -20 to 20). This only
counts for a group the plugin has not learned about yet: once it has seen which subtitle languages a
group ships, what it learned decides. Empty by default; the plugin favours no group.

### `englishOnlyGroups`

Default: `[]`

Fansub groups known to ship English subtitles only. When your subtitle language is not English, a
release from one of them is placed after any other release with the same score and the same number of
people sharing it; it gains or loses no points. A group also in `preferredGroups` is not treated as
English-only.

### `subtitleCheckCount`

Default: `16`

Range: `1` to `40`

How many of the best-ranked releases have their subtitle tracks read per play, when their add-on
offers subtitle tracks. A release whose add-on offers none is skipped and does not count. Reading
stops at the first release that has your language. Each read can be one request to the add-on's
site, so more means a better chance of your language and a slower start.

### `subtitleCheckSeconds`

Default: `15`

Range: `1` to `120`

How many seconds reading those subtitle tracks may take per play. No read starts once the time is
spent, and a read still running is given up at what is left of it (but never under 5 seconds), so a
slow or unreachable site costs about this long, not one wait per release. An add-on whose read is
given up on, or that fails two reads in a row, is not asked again for this play; the reading stops
when no other add-on can read what is left. One read took about 1 second when measured (most under
1.5), so 15 seconds is about `subtitleCheckCount`'s 16 reads; more means a better chance of your
language on a slow site and a slower start.

## Stream providers (advanced)

### `providers`

Default: `["torbox", "realdebrid"]`

The stream providers to try, in order (`torbox`, `realdebrid`). A provider without a token is skipped.
An empty or unknown list falls back to the default. The plugin never streams a torrent itself.

### `probeCount`

Default: `3`

Range: `1` to `10`

How many releases each provider may try per play before the best fallback is accepted.

### `providerBlockMinutes`

Default: `15`

Range: `1` to `120`

A provider that refuses the account for three releases in a row is skipped for the rest of the play
and for this many minutes afterwards, then asked once more; any success clears it. Your `providers`
list is never changed by this.

### `rdPollSeconds`

Default: `30`

Range: `5` to `120`

How long to wait for Real-Debrid to convert a release it does not have ready before moving on.

### `torboxWaitSeconds`

Default: `60`

Range: `0` to `600`

When a play waited for TorBox to download the one release it added (the original-audio release when TorBox had only
a dub cached, or the best release when TorBox had none of the episode): the longest wait, from starting the play until
TorBox has the episode, after which it still starts playing by itself. After a longer wait you will have looked away,
so the episode loads paused with "Episode 3 is ready. Space plays it." and a notification with a sound, so nothing
plays to an empty room and AniList is not updated behind your back. Showing or hiding the player makes no difference:
hide it during a long wait and the notification calls you back. With Omarchy's do-not-disturb on, the notification goes
to the history without a toast or a sound. `0`: a ready episode always loads paused. A value stored before this
meaning keeps its number: 300 now lets a wait of up to five minutes play by itself.

This setting does not end the wait. The play waits until TorBox is done, until you choose (`w` keep waiting, `n` be
told when it's ready, `d` the dub when TorBox has one: on the player, in the overlay and in the bar's popout, from the
start of the wait), or for three hours at most, and then keeps your choices on screen. The loading screen says what
TorBox is doing: finding people who share the release, nobody sharing it right now, and once TorBox reports progress
its percentage, the speed and the time left ("TorBox is downloading it: 43 %, 1.8 MB/s, about 3 minutes left"). The
speed and the time left come from TorBox's own byte counts over about the last minute, not from TorBox's estimate
(which swings by hours): the speed shows from TorBox's first count, the time left once two different counts have come
in. Both go, leaving the percentage, when the count has not moved for a minute and a half, and while TorBox has lost
its seed but still says it is downloading. When TorBox does not answer for a moment they stay until that same minute
and a half is up. TorBox is asked every 3 seconds (it allows
300 requests a minute); it refreshes its counts only about every 30 seconds, so the numbers move about that often,
each on screen within about 5 seconds of TorBox. Once TorBox has finished downloading, the line says it is finishing
or has it.

An episode you play while you are watching another one (an episode loaded and not at its end, in the player window on
screen, paused or not) never interrupts it. If TorBox has it, it replaces the one you watch once its link is ready.
While it is looked for, and while TorBox downloads it, the play runs in the background: the player keeps playing, with
one small line in its top-left corner, in the words the overlay and the bar's popout use: what the play is doing
(looking, TorBox's percentage and time left) and the seconds since you asked. When it is ready, or if it fails, the
line says so for half a minute, then goes; the overlay and the bar's popout show the wait's line and its choices, or
why it failed. The ready notification comes with a sound, and you switch when you choose: its click, or playing the
episode again. If the episode you watch ends during the wait, the wait shows in the player as any wait does; if the
player is hidden when TorBox is done, the episode loads into it as above (paused after a long wait).

### `torrentInfoSeconds`

Default: `5`

Range: `0` to `30`

When no loaded add-on lists a pack's files, TorBox can read them from the BitTorrent network without
adding the pack, so the right episode file is chosen and packs are compared by episode size. How long
it may search (up to four packs at once); `0` turns it off. Asked only when TorBox is in `providers`
and has a token.

## Network and cache (advanced)

### `httpTimeout`

Default: `15`

Range: `5` to `60`

Seconds before one HTTP request gives up, for AniList, ani.zip, the add-ons (`ctx.http`) and the
providers. Retries come on top of it.

### `subsCacheMaxBytes`

Default: `16777216`

Range: `1048576` to `536870912`

The size limit of the borrowed-subtitle cache, in bytes (the sheet shows and steps it in MiB).
`omarchy-anime subs prune` enforces it (`--max-bytes` sets another limit for one run), and the plugin
prunes to it once a day, after a play.

### `anilistClientId`

Default: `51175`

Range: `1` to `1000000000`

The AniList API client the sign-in page is opened for. Change it only if you registered your own
AniList app. Implicit-grant client ids are public; the token they produce is not.
