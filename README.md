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
  "epg_url": "https://raw.githubusercontent.com/doms9/iptv/refs/heads/default/EPG/TV.xml",
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
  "subs_enabled_default": false
}
```

## Requirements

- Python 3.9+
- `mpv` or `vlc` for playback

## Disclaimer

The default channel and EPG sources are provided for inspiration and testing.
tvTUI is not affiliated with, endorsed by, or sponsored by those sources or
their authors.

## License

MIT. See `LICENSE`.
