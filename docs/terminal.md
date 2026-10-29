# From a terminal

[Back to the README](../README.md)

`omarchy-anime` in a command here is the plugin's own command, on your PATH once you have run the link line in
the README's [Install](../README.md#install).

The plugin is a Quickshell front end over a standard-library Python CLI: all the decisions
(searching, scoring, providers, mpv, AniList) live in `bin/omarchy-anime`, and the QML is layout,
keys and process plumbing. [docs/architecture.md](architecture.md) walks through
the modules and one play stage by stage. The CLI is usable on its own:

```sh
omarchy-anime watching | top | airing | search <query> | media <id>
omarchy-anime resolve <mediaId> <episode> --dry-run     # what it would play, and why
omarchy-anime play <mediaId> <episode>
omarchy-anime releases <mediaId> <episode>              # the release list: passed and set-aside releases
omarchy-anime accounts                                  # accounts and which indexer add-ons loaded
omarchy-anime covers warm --view watching               # pre-download cover images
omarchy-anime subs find <mediaId> <episode> [--offset]  # which releases could donate your language
omarchy-anime subs fetch <mediaId> <episode>            # download and cache that track
omarchy-anime subs status | subs prune                  # the borrowed-subtitle cache
omarchy-anime thumbs status | thumbs prune              # derived pictures: row buffers, blurred stills, fanart, logo; pruned daily after a play
omarchy-anime status | player show|hide|pause|stop
omarchy-anime player status                             # includes `stale`: is the player older than its script?
omarchy-anime player restart                            # replace the process, re-load what was playing
omarchy-anime player start                              # start one, or re-park and warn if the running one is stale
```

**Editing `mpv/omarchy-anime-episodes.lua`**: mpv reads `--script=` once, at process start, so a
changed file changes nothing about a player that is already running — it keeps executing the copy it
loaded, while the socket and `player status` still report a healthy player. `omarchy-anime player
restart` is the fix: it stores the position, stops the player by its own pid, starts a fresh one
through the launcher, and re-loads the same stream at the same second.

Every command prints JSON on stdout and logs to stderr; `--trace <id>` ties a run to a UI action. `watching`, `list`, `top` and `airing` print the list itself, except while AniList can't be read: then they print the saved list as `{"ok": true, "entries": [...], "stale": {"savedAt", "ageSeconds", "reason"}}`, with exit 0.
