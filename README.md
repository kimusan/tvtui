# tvTUI

![tvTUI logo](https://raw.githubusercontent.com/kimusan/tvtui/main/assets/logo.png)

tvTUI is a fast terminal UI for browsing IPTV channels with EPG (now/next)
information, favorites, categories, and quick search.

## Features

- Search-as-you-type channel list with category tags.
- EPG preview with now/next details and descriptions.
- Favorites and history.
- Mouse support (scroll, click, double-click).
- Configurable player and subtitle defaults.

## Install (pipx)

```bash
pipx install .
```

For a published package:

```bash
pipx install tvtui
```

## Run

```bash
tvtui
tvtui -f
tvtui --categories
```

## Keybindings

- `Enter` play selected channel
- `f` toggle favorites view
- `F` add/remove favorite
- `c` categories
- `m` content mode (tv/movie/series)
- `v` tv source (m3u/xtream/both)
- `s` sort (default/name/category)
- `r` refresh channels/EPG
- `/` search mode
- `Left`/`Right` cycle description
- `t` subtitles on/off (next playback)
- `h` toggle help panel
- `q` quit

Mouse:

- Wheel scroll
- Click select
- Double-click play

## Configuration

Config file defaults to `~/.config/tvtui/config.json`.

```json
{
  "epg_url_m3u": "https://raw.githubusercontent.com/doms9/iptv/refs/heads/default/EPG/TV.xml",
  "epg_url_xtream": "http://xtreamcode.ex/xmltv.php?username=Mike&password=1234",
  "source_url": "https://s.id/d9Base",
  "streamed_base": "https://raw.githubusercontent.com/doms9/iptv/default/M3U8",
  "show_help_panel": true,
  "use_emoji_tags": false,
  "player": "auto",
  "player_args": ["--ontop"],
  "custom_command": ["myplayer", "--flag"],
  "custom_subs_on_args": ["--subs=on"],
  "custom_subs_off_args": ["--subs=off"],
  "mpv_subs_on_args": ["--sub-visibility=yes", "--sid=auto"],
  "mpv_subs_off_args": ["--sub-visibility=no", "--sid=no"],
  "vlc_sub_track": 1,
  "subs_enabled_default": false,
  "xtream_base_url": "http://xtreamcode.ex:8080",
  "xtream_username": "Mike",
  "xtream_password": "1234",
  "xtream_use_for_tv": true,
  "tv_source_mode": "both",
  "epg_fuzzy_match": true,
  "epg_fuzzy_threshold": 0.85
}
```

### Xtream API

If `xtream_base_url`, `xtream_username`, and `xtream_password` are set, use `m`
to toggle content mode between live TV, movies, and series. Use `v` to toggle
the TV source between `m3u`, `xtream`, and `both`. When `tv_source_mode` is
`both`, channels are tagged with `[M3U]` or `[XT]` in the list.

## Requirements

- Python 3.9+
- `mpv` or `vlc` for playback

## Disclaimer

The default channel and EPG sources are provided for inspiration and testing.
tvTUI is not affiliated with, endorsed by, or sponsored by those sources or
their authors.

## License

MIT. See `LICENSE`.
