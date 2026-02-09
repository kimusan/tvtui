#!/usr/bin/env python3
import argparse
import curses
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import difflib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

VERSION = "2.0.0"

DEFAULT_EPG_URL = "https://raw.githubusercontent.com/doms9/iptv/refs/heads/default/EPG/TV.xml"
DEFAULT_BASE_M3U_URL = "https://s.id/d9Base"
DEFAULT_STREAMED_BASE = "https://raw.githubusercontent.com/doms9/iptv/default/M3U8"

CACHE_MAX_AGE_SECS = 3600
EPG_MAX_AGE_SECS = 43200


@dataclass(frozen=True)
class Channel:
    name: str
    url: str
    tvg_id: str
    kind: str = "live"


@dataclass(frozen=True)
class Program:
    title: str
    start: dt.datetime
    stop: dt.datetime
    desc: str = ""


@dataclass(frozen=True)
class PlayerConfig:
    name: str
    args: List[str]
    custom_command: List[str]
    subs_on_args: List[str]
    subs_off_args: List[str]
    vlc_sub_track: int


def ensure_dirs(config_dir: str, cache_dir: str) -> None:
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)


def config_paths() -> Tuple[str, str, str, str, str]:
    config_dir = os.path.expanduser("~/.config/tvtui")
    cache_dir = os.path.expanduser("~/.cache/tvtui")
    favorites_file = os.path.join(config_dir, "favorites.tsv")
    history_file = os.path.join(config_dir, "history.log")
    epg_cache = os.path.join(config_dir, "epg.xml")
    channels_cache = os.path.join(cache_dir, "channels.json")
    return config_dir, cache_dir, favorites_file, history_file, epg_cache, channels_cache


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tvTUI/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url: str, timeout: int = 30) -> Tuple[object, str]:
    try:
        data = http_get(url, timeout=timeout)
        return json.loads(data.decode("utf-8", errors="replace")), ""
    except Exception:
        return None, "Unable to fetch Xtream data."


def xtream_api_url(base_url: str, username: str, password: str, action: str, **params) -> str:
    base = base_url.rstrip("/")
    query = {"username": username, "password": password, "action": action}
    query.update({k: v for k, v in params.items() if v is not None})
    return f"{base}/player_api.php?{urllib.parse.urlencode(query)}"


def update_epg_cache(epg_cache: str, epg_url: str) -> Tuple[bool, str]:
    if os.path.exists(epg_cache):
        age = time.time() - os.path.getmtime(epg_cache)
        if age < EPG_MAX_AGE_SECS:
            return True, ""
    try:
        data = http_get(epg_url, timeout=60)
    except Exception:
        return False, "Unable to download EPG data."

    tmp = epg_cache + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, epg_cache)
    return True, ""


def parse_epg_time(raw: str) -> Optional[dt.datetime]:
    if not raw:
        return None
    raw = raw.strip()
    base = raw[:14]
    try:
        base_dt = dt.datetime.strptime(base, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    tz = dt.timezone.utc
    if len(raw) > 14:
        offset = raw[14:].strip()
        m = re.match(r"([+-])(\d{2})(\d{2})", offset)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            hours = int(m.group(2))
            minutes = int(m.group(3))
            delta = dt.timedelta(hours=hours, minutes=minutes)
            tz = dt.timezone(sign * delta)
    return base_dt.replace(tzinfo=tz).astimezone(dt.timezone.utc)


def build_program_map(epg_cache: str, now: Optional[dt.datetime] = None) -> Dict[str, Program]:
    if not os.path.exists(epg_cache):
        return {}
    now = now or dt.datetime.now(dt.timezone.utc)
    programs: Dict[str, Program] = {}
    try:
        for _, elem in ET.iterparse(epg_cache, events=("end",)):
            if elem.tag != "programme":
                continue
            channel_id = elem.attrib.get("channel", "")
            if not channel_id or channel_id in programs:
                elem.clear()
                continue
            start = parse_epg_time(elem.attrib.get("start", ""))
            stop = parse_epg_time(elem.attrib.get("stop", ""))
            if not start or not stop:
                elem.clear()
                continue
            if start <= now < stop:
                title = elem.findtext("title") or "Live Programming"
                desc = elem.findtext("desc") or ""
                programs[channel_id] = Program(
                    title=title, start=start, stop=stop, desc=desc
                )
            elem.clear()
    except ET.ParseError:
        return {}
    return programs


def build_epg_channels(epg_cache: str) -> List[Tuple[str, str]]:
    if not os.path.exists(epg_cache):
        return []
    channels = []
    try:
        for _, elem in ET.iterparse(epg_cache, events=("end",)):
            if elem.tag != "channel":
                continue
            channel_id = (elem.attrib.get("id") or "").strip()
            display = (elem.findtext("display-name") or "").strip()
            if channel_id:
                channels.append((channel_id, display))
            elem.clear()
    except ET.ParseError:
        return []
    return channels


def normalize_name(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def repair_favorites_with_epg(
    favorites: List[Channel],
    epg_cache: str,
    fuzzy: bool,
    threshold: float,
) -> Tuple[List[Channel], bool]:
    if not favorites:
        return favorites, False
    epg_channels = build_epg_channels(epg_cache)
    if not epg_channels:
        return favorites, False
    id_map = {normalize_name(cid): cid for cid, _ in epg_channels}
    name_map = {normalize_name(name): cid for cid, name in epg_channels if name}
    updated = []
    changed = False
    for fav in favorites:
        if fav.tvg_id:
            updated.append(fav)
            continue
        key = normalize_name(fav.name)
        matched = None
        if key in id_map:
            matched = id_map[key]
        elif key in name_map:
            matched = name_map[key]
        elif fuzzy:
            candidates = list(name_map.keys()) + list(id_map.keys())
            best = difflib.get_close_matches(key, candidates, n=1, cutoff=threshold)
            if best:
                matched = name_map.get(best[0]) or id_map.get(best[0])
        if matched:
            updated.append(Channel(name=fav.name, url=fav.url, tvg_id=matched, kind=fav.kind))
            changed = True
        else:
            updated.append(fav)
    return updated, changed

def build_epg_index(epg_cache: str, now: Optional[dt.datetime] = None) -> Dict[str, List[Program]]:
    if not os.path.exists(epg_cache):
        return {}
    now = now or dt.datetime.now(dt.timezone.utc)
    index: Dict[str, List[Program]] = {}
    try:
        for _, elem in ET.iterparse(epg_cache, events=("end",)):
            if elem.tag != "programme":
                continue
            channel_id = elem.attrib.get("channel", "")
            if not channel_id:
                elem.clear()
                continue
            start = parse_epg_time(elem.attrib.get("start", ""))
            stop = parse_epg_time(elem.attrib.get("stop", ""))
            if not start or not stop:
                elem.clear()
                continue
            if stop < now:
                elem.clear()
                continue
            title = elem.findtext("title") or "Live Programming"
            desc = elem.findtext("desc") or ""
            index.setdefault(channel_id, []).append(
                Program(title=title, start=start, stop=stop, desc=desc)
            )
            elem.clear()
    except ET.ParseError:
        return {}
    for channel_id, items in index.items():
        items.sort(key=lambda p: p.start)
        index[channel_id] = items[:6]
    return index


def parse_m3u(content: str) -> Iterable[Channel]:
    name = ""
    tvg_id = ""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            tvg_id = ""
            name = ""
            m_name = re.search(r'tvg-name="([^"]+)"', line, re.IGNORECASE)
            if m_name:
                name = m_name.group(1)
            else:
                if "," in line:
                    name = line.split(",", 1)[1].strip()
            m_id = re.search(r'tvg-id="([^"]+)"', line, re.IGNORECASE)
            if m_id:
                tvg_id = m_id.group(1)
        elif line.startswith("http") and name:
            yield Channel(name=name, url=line, tvg_id=tvg_id)
            name = ""
            tvg_id = ""


def fetch_m3u(url: str) -> Tuple[List[Channel], str]:
    try:
        data = http_get(url)
    except Exception:
        return [], "Unable to download channel list."
    content = data.decode("utf-8", errors="replace")
    return list(parse_m3u(content)), ""


def load_channels_cache(channels_cache: str) -> Optional[List[Channel]]:
    if not os.path.exists(channels_cache):
        return None
    if time.time() - os.path.getmtime(channels_cache) > CACHE_MAX_AGE_SECS:
        return None
    try:
        with open(channels_cache, "r", encoding="utf-8") as f:
            payload = json.load(f)
        channels = [Channel(**item) for item in payload.get("channels", [])]
        return channels
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def save_channels_cache(channels_cache: str, channels: List[Channel]) -> None:
    payload = {"channels": [c.__dict__ for c in channels]}
    with open(channels_cache, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def get_iptv_channels(channels_cache: str, base_m3u_url: str) -> Tuple[List[Channel], str]:
    cached = load_channels_cache(channels_cache)
    if cached is not None:
        return cached, ""
    channels, error = fetch_m3u(base_m3u_url)
    if channels:
        save_channels_cache(channels_cache, channels)
    return channels, error


def get_fallback_channels() -> List[Channel]:
    return [
        Channel(
            name="France 24",
            url="https://static.france24.com/live/F24_EN_LO_HLS/live_web.m3u8",
            tvg_id="",
        ),
        Channel(
            name="CBS News",
            url="https://cbsn-us.cbsnstream.cbsnews.com/out/v1/55a8648e8f134e82a470f83d562deeca/master.m3u8",
            tvg_id="",
        ),
        Channel(
            name="Red Bull TV",
            url="https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8",
            tvg_id="",
        ),
        Channel(
            name="Pluto TV Movies",
            url="https://service-stitcher.clusters.pluto.tv/stitch/hls/channel/5cb0cae7a461406ffe3f5213/master.m3u8",
            tvg_id="",
        ),
    ]


def get_category_channels(category_id: str, streamed_base: str) -> Tuple[List[Channel], str]:
    if category_id == "events":
        return fetch_m3u(f"{streamed_base}/events.m3u8")
    if category_id == "tv":
        return fetch_m3u(f"{streamed_base}/TV.m3u8")
    if category_id == "base":
        return fetch_m3u(f"{streamed_base}/base.m3u8")
    return [], ""


def xtream_get_categories(
    base_url: str, username: str, password: str, kind: str
) -> Tuple[List[Tuple[str, str]], str]:
    action_map = {
        "live": "get_live_categories",
        "movie": "get_vod_categories",
        "series": "get_series_categories",
    }
    action = action_map.get(kind)
    if not action:
        return [], "Unknown Xtream category type."
    url = xtream_api_url(base_url, username, password, action)
    data, err = http_get_json(url)
    if err or not isinstance(data, list):
        return [], err or "Invalid Xtream category response."
    items = []
    for row in data:
        cat_id = str(row.get("category_id", "")).strip()
        name = str(row.get("category_name", "")).strip()
        if cat_id and name:
            items.append((name, cat_id))
    return items, ""


def xtream_stream_url(
    base_url: str, username: str, password: str, kind: str, stream_id: str, ext: str
) -> str:
    base = base_url.rstrip("/")
    if kind == "movie":
        suffix = f"{stream_id}.{ext or 'mp4'}"
        return f"{base}/movie/{username}/{password}/{suffix}"
    if kind == "series":
        suffix = f"{stream_id}.{ext or 'mp4'}"
        return f"{base}/series/{username}/{password}/{suffix}"
    return f"{base}/live/{username}/{password}/{stream_id}.m3u8"


def xtream_get_streams(
    base_url: str,
    username: str,
    password: str,
    kind: str,
    category_id: Optional[str] = None,
) -> Tuple[List[Channel], str]:
    action_map = {
        "live": "get_live_streams",
        "movie": "get_vod_streams",
        "series": "get_series",
    }
    action = action_map.get(kind)
    if not action:
        return [], "Unknown Xtream stream type."
    url = xtream_api_url(
        base_url,
        username,
        password,
        action,
        category_id=category_id,
    )
    data, err = http_get_json(url)
    if err or not isinstance(data, list):
        return [], err or "Invalid Xtream stream response."
    channels = []
    for row in data:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        if kind == "series":
            series_id = str(row.get("series_id", "")).strip()
            if not series_id:
                continue
            channels.append(Channel(name=name, url="", tvg_id=series_id, kind="series"))
        else:
            stream_id = str(row.get("stream_id", "")).strip()
            if not stream_id:
                continue
            ext = str(row.get("container_extension", "")).strip()
            url = xtream_stream_url(base_url, username, password, kind, stream_id, ext)
            tvg_id = str(row.get("epg_channel_id", "")).strip() if kind == "live" else ""
            channels.append(Channel(name=name, url=url, tvg_id=tvg_id, kind=kind))
    return channels, ""


def xtream_get_series_episodes(
    base_url: str, username: str, password: str, series_id: str
) -> Tuple[List[Tuple[str, str]], str]:
    url = xtream_api_url(
        base_url, username, password, "get_series_info", series_id=series_id
    )
    data, err = http_get_json(url)
    if err or not isinstance(data, dict):
        return [], err or "Invalid Xtream series response."
    episodes = []
    for season, items in (data.get("episodes") or {}).items():
        for ep in items or []:
            ep_id = str(ep.get("id", "")).strip()
            title = str(ep.get("title", "")).strip() or "Episode"
            num = str(ep.get("episode_num", "")).strip()
            ext = str(ep.get("container_extension", "")).strip()
            if not ep_id:
                continue
            label = f"S{season}E{num} {title}".strip()
            url = xtream_stream_url(base_url, username, password, "series", ep_id, ext)
            episodes.append((label, url))
    return episodes, ""


def load_favorites(favorites_file: str) -> List[Channel]:
    favorites = []
    if not os.path.exists(favorites_file):
        return favorites
    with open(favorites_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0]
                url = parts[1]
                tvg_id = parts[2] if len(parts) > 2 else ""
                favorites.append(Channel(name=name, url=url, tvg_id=tvg_id))
    return favorites


def merge_with_favorites(channels: List[Channel], favorites: List[Channel]) -> List[Channel]:
    seen = {c.url for c in channels}
    merged = list(channels)
    for fav in favorites:
        if fav.url not in seen:
            merged.append(fav)
    return merged


def save_favorites(favorites_file: str, favorites: List[Channel]) -> None:
    tmp = favorites_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for fav in favorites:
            f.write(f"{fav.name}\t{fav.url}\t{fav.tvg_id}\n")
    os.replace(tmp, favorites_file)


def toggle_favorite(favorites_file: str, channel: Channel) -> None:
    if not channel.url:
        return
    favorites = load_favorites(favorites_file)
    existing = [f for f in favorites if f.url == channel.url]
    if existing:
        favorites = [f for f in favorites if f.url != channel.url]
    else:
        favorites.append(channel)
    save_favorites(favorites_file, favorites)


def append_history(history_file: str, channel: Channel) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(f"{stamp} - {channel.name}\n")


class Player:
    def __init__(self, config: PlayerConfig) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.config = config

    def play(self, url: str, subs_enabled: bool) -> None:
        self.stop()
        cmd: Optional[List[str]] = None
        name = self.config.name
        if name == "auto":
            if shutil.which("mpv"):
                name = "mpv"
            elif shutil.which("vlc"):
                name = "vlc"
        if name == "mpv" and shutil.which("mpv"):
            cmd = ["mpv"] + self.config.args
            cmd += self.config.subs_on_args if subs_enabled else self.config.subs_off_args
            cmd.append(url)
        elif name == "vlc" and shutil.which("vlc"):
            cmd = ["vlc"] + self.config.args
            if subs_enabled:
                cmd.append(f"--sub-track={self.config.vlc_sub_track}")
            else:
                cmd.append("--sub-track=0")
            cmd.append(url)
        elif name == "custom" and self.config.custom_command:
            cmd = list(self.config.custom_command)
            cmd += self.config.subs_on_args if subs_enabled else self.config.subs_off_args
            if not any("{url}" in part for part in cmd):
                cmd.append(url)
            cmd = [part.replace("{url}", url) for part in cmd]
        if cmd is None:
            return
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def format_program(programs: Dict[str, Program], channel: Channel) -> str:
    if channel.tvg_id and channel.tvg_id in programs:
        return programs[channel.tvg_id].title
    return "Live Programming"


def format_time_window(program: Program) -> str:
    start = program.start.astimezone().strftime("%H:%M")
    stop = program.stop.astimezone().strftime("%H:%M")
    return f"{start}-{stop}"


def category_label(name: str, use_emoji: bool) -> Tuple[str, str]:
    upper = name.upper()
    if any(token in upper for token in ("ESPN", "NFL", "NBA", "MLB", "SPORT")):
        return ("⚽", "sports") if use_emoji else ("[SP]", "sports")
    if any(token in upper for token in ("CNN", "NEWS", "BBC")):
        return ("📰", "news") if use_emoji else ("[NW]", "news")
    if any(token in upper for token in ("MTV", "MUSIC", "BET")):
        return ("🎵", "music") if use_emoji else ("[MU]", "music")
    if any(token in upper for token in ("CARTOON", "NICK", "DISNEY", "KIDS")):
        return ("👶", "kids") if use_emoji else ("[KD]", "kids")
    if any(token in upper for token in ("FOOD", "HGTV", "TRAVEL", "COOK")):
        return ("🏠", "lifestyle") if use_emoji else ("[LIFE]", "lifestyle")
    if any(token in upper for token in ("MOVIE", "CINEMA", "FILM")):
        return ("🎬", "movies") if use_emoji else ("[MV]", "movies")
    return ("📺", "tv") if use_emoji else ("[TV]", "tv")


def tag_color(category_key: str) -> int:
    return {
        "sports": 1,
        "news": 2,
        "music": 3,
        "kids": 4,
        "lifestyle": 5,
        "movies": 6,
        "tv": 7,
    }.get(category_key, 7)


def program_listing(
    epg_index: Dict[str, List[Program]], channel: Channel
) -> List[Tuple[str, str, str, str]]:
    if channel.kind != "live":
        label = "VOD" if channel.kind == "movie" else "SER"
        return [(label, "--:--", channel.name, "")]
    if not channel.tvg_id or channel.tvg_id not in epg_index:
        return [("NOW", "--:--", "Live Programming", "")]
    now = dt.datetime.now(dt.timezone.utc)
    items = epg_index[channel.tvg_id]
    lines: List[Tuple[str, str, str, str]] = []
    for item in items:
        label = "NEXT"
        if item.start <= now < item.stop:
            label = "NOW"
        lines.append((label, format_time_window(item), item.title, item.desc))
    return lines[:5]


def clip_text(text: str, max_len: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "…"


def text_wrap(text: str, width: int) -> List[str]:
    if width <= 0:
        return []
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def apply_sort(
    channels: List[Channel], sort_mode: str, use_emoji_tags: bool
) -> List[Channel]:
    if sort_mode == "name":
        return sorted(channels, key=lambda c: c.name.lower())
    if sort_mode == "category":
        return sorted(
            channels,
            key=lambda c: (category_label(c.name, False)[1], c.name.lower()),
        )
    return channels


def render_screen(
    stdscr: "curses._CursesWindow",
    channels: List[Channel],
    programs: Dict[str, Program],
    epg_index: Dict[str, List[Program]],
    favorites_set: set,
    selected_index: int,
    top_index: int,
    query: str,
    mode: str,
    help_visible: bool,
    search_mode: bool,
    use_emoji_tags: bool,
    desc_index: int,
    status_message: str,
    sort_mode: str,
    content_mode: str,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    help_width = 28
    show_help_panel = help_visible and width >= help_width + 20
    list_width = width - help_width - 1 if show_help_panel else width
    search_label = "search" if search_mode else "nav"
    header = (
        f"tvTUI {VERSION} | view: {mode} | content: {content_mode} | "
        f"sort: {sort_mode} | channels: {len(channels)} | {search_label}: {query}"
    )
    if curses.has_colors():
        stdscr.attron(curses.color_pair(8))
    status = f" | {status_message}" if status_message else ""
    stdscr.addnstr(0, 0, header + status, list_width - 1, curses.A_BOLD)
    if curses.has_colors():
        stdscr.attroff(curses.color_pair(8))
    preview_height = 7
    list_height = max(1, height - 2 - preview_height)
    list_top = 1
    list_bottom = list_top + list_height

    hline = getattr(curses, "ACS_HLINE", ord("-"))
    vline = getattr(curses, "ACS_VLINE", ord("|"))

    for idx in range(list_height):
        chan_index = top_index + idx
        if chan_index >= len(channels):
            break
        chan = channels[chan_index]
        fav_mark = "*" if chan.url in favorites_set else " "
        label, category_key = category_label(chan.name, use_emoji_tags)
        line = f"[{fav_mark}] {label} {chan.name}"
        if chan_index == selected_index:
            stdscr.attron(curses.A_REVERSE)
            stdscr.addnstr(list_top + idx, 0, line, list_width - 1)
            stdscr.attroff(curses.A_REVERSE)
        else:
            stdscr.addnstr(list_top + idx, 0, line, list_width - 1)
        if curses.has_colors():
            tag_pos = line.find(label)
            if tag_pos >= 0:
                stdscr.attron(curses.color_pair(tag_color(category_key)))
                stdscr.addnstr(list_top + idx, tag_pos, label, len(label))
                stdscr.attroff(curses.color_pair(tag_color(category_key)))

    if channels:
        selected = channels[selected_index]
        if list_bottom < height:
            stdscr.hline(list_bottom, 0, hline, list_width - 1)
        if list_bottom + 1 < height:
            stdscr.addnstr(
                list_bottom + 1,
                0,
                "Name:",
                list_width - 1,
                curses.A_BOLD,
            )
            stdscr.addnstr(
                list_bottom + 1,
                6,
                f"{selected.name}",
                list_width - 7,
            )
        if list_bottom + 2 < height:
            stdscr.addnstr(
                list_bottom + 2,
                0,
                "URL:",
                list_width - 1,
                curses.A_BOLD,
            )
            stdscr.addnstr(
                list_bottom + 2,
                6,
                f"{selected.url}",
                list_width - 7,
            )
        if list_bottom + 3 < height:
            stdscr.addnstr(list_bottom + 3, 0, "EPG:", list_width - 1, curses.A_BOLD)
        epg_lines = program_listing(epg_index, selected)
        desc_width = min(60, max(24, list_width // 2))
        list_box_width = list_width - desc_width - 1
        if list_box_width < 22:
            desc_width = max(0, list_width - 23)
            list_box_width = list_width - desc_width - 1
        if desc_width <= 0 or list_box_width < 20:
            list_box_width = list_width
            desc_width = 0
        for offset, item in enumerate(epg_lines[:5]):
            row = list_bottom + 4 + offset
            if row >= height:
                break
            label, time_window, title, _desc = item
            stdscr.addnstr(row, 0, "  ", list_box_width - 1)
            stdscr.addnstr(row, 2, f"{label:<4}", list_box_width - 3, curses.A_BOLD)
            stdscr.addnstr(row, 7, f"{time_window:<11}", list_box_width - 8)
            stdscr.addnstr(row, 19, title, list_box_width - 20)
        if desc_width:
            panel_top = list_bottom + 1
            panel_bottom = min(height - 1, list_bottom + preview_height)
            box_x = list_box_width + 1
            for row in range(panel_top, panel_bottom + 1):
                stdscr.addch(row, list_box_width, vline)
            selected_desc = ""
            desc_label = ""
            if epg_lines:
                desc_index = max(0, min(desc_index, len(epg_lines) - 1))
                desc_label, _t, _title, selected_desc = epg_lines[desc_index]
            if panel_top < height:
                stdscr.addnstr(
                    panel_top,
                    box_x,
                    f"Description ({desc_label})",
                    desc_width - 1,
                    curses.A_BOLD,
                )
            if selected_desc:
                desc_text = clip_text(selected_desc, max(10, desc_width * 5))
            else:
                desc_text = "No description."
            desc_lines = text_wrap(desc_text, desc_width - 2)
            max_lines = max(0, panel_bottom - panel_top - 1)
            for i, line in enumerate(desc_lines[:max_lines]):
                row = panel_top + 1 + i
                if row >= height:
                    break
                stdscr.addnstr(row, box_x, line, desc_width - 1)
        key_row = list_bottom + 4 + min(5, len(epg_lines))
        if key_row < height:
            stdscr.addnstr(
                key_row,
                0,
                "Keys: Enter=play  F=favorite  f=favs  c=cat  m=mode  s=sort  r=refresh  t=subs  /=search  \u2190/\u2192 details  Esc=back  q=quit",
                list_width - 1,
            )
    else:
        if list_bottom + 1 < height:
            stdscr.addnstr(list_bottom + 1, 0, "No channels to display.", list_width - 1)

    if show_help_panel:
        panel_x = list_width + 1
        for row in range(height):
            stdscr.addch(row, list_width, vline)
        help_lines = [
            "Help",
            "",
            "/   search mode",
            "Esc exit search",
            "Enter play",
            "F   favorite",
            "f   favorites",
            "c   categories",
            "m   content mode",
            "s   sort",
            "r   refresh",
            "t   subtitles",
            "Mouse wheel scroll",
            "Click select",
            "Double-click play",
            "\u2190/\u2192 details",
            "q   quit",
            "",
            "Legend",
            "* favorite",
        ]
        for i, line in enumerate(help_lines):
            row = i
            if row >= height:
                break
            if i == 0:
                stdscr.addnstr(row, panel_x + 1, line, help_width - 2, curses.A_BOLD)
            else:
                stdscr.addnstr(row, panel_x + 1, line, help_width - 2)
    stdscr.refresh()


def show_popup(stdscr: "curses._CursesWindow", title: str, message: str) -> None:
    height, width = stdscr.getmaxyx()
    lines = text_wrap(message, max(20, width - 8))
    box_height = min(height - 4, max(5, len(lines) + 4))
    box_width = min(width - 4, max(30, max(len(title) + 4, max((len(l) for l in lines), default=0) + 4)))
    start_y = (height - box_height) // 2
    start_x = (width - box_width) // 2
    hline = getattr(curses, "ACS_HLINE", ord("-"))
    vline = getattr(curses, "ACS_VLINE", ord("|"))
    stdscr.attron(curses.A_BOLD)
    stdscr.addch(start_y, start_x, getattr(curses, "ACS_ULCORNER", ord("+")))
    stdscr.addch(start_y, start_x + box_width - 1, getattr(curses, "ACS_URCORNER", ord("+")))
    stdscr.addch(start_y + box_height - 1, start_x, getattr(curses, "ACS_LLCORNER", ord("+")))
    stdscr.addch(start_y + box_height - 1, start_x + box_width - 1, getattr(curses, "ACS_LRCORNER", ord("+")))
    stdscr.attroff(curses.A_BOLD)
    stdscr.hline(start_y, start_x + 1, hline, box_width - 2)
    stdscr.hline(start_y + box_height - 1, start_x + 1, hline, box_width - 2)
    for row in range(start_y + 1, start_y + box_height - 1):
        stdscr.addch(row, start_x, vline)
        stdscr.addch(row, start_x + box_width - 1, vline)
        stdscr.addnstr(row, start_x + 1, " " * (box_width - 2), box_width - 2)
    stdscr.addnstr(start_y + 1, start_x + 2, title, box_width - 4, curses.A_BOLD)
    for i, line in enumerate(lines[: box_height - 4]):
        stdscr.addnstr(start_y + 2 + i, start_x + 2, line, box_width - 4)
    stdscr.addnstr(start_y + box_height - 2, start_x + 2, "Press any key...", box_width - 4)
    stdscr.refresh()
    stdscr.get_wch()


def render_splash(stdscr: "curses._CursesWindow", message: str, spinner_char: str) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    logo = [
        "$$$$$$$$\\      $$$$$$$$\\ $$\\   $$\\ $$$$$$\\ ",
        "\\__$$  __|     \\__$$  __|$$ |  $$ |\\_$$  _|",
        "   $$ |$$\\    $$\\ $$ |   $$ |  $$ |  $$ |  ",
        "   $$ |\\$$\\  $$  |$$ |   $$ |  $$ |  $$ |  ",
        "   $$ | \\$$\\$$  / $$ |   $$ |  $$ |  $$ |  ",
        "   $$ |  \\$$$  /  $$ |   $$ |  $$ |  $$ |  ",
        "   $$ |   \\$  /   $$ |   \\$$$$$$  |$$$$$$\\ ",
        "   \\__|    \\_/    \\__|    \\______/ \\______|",
    ]
    start_y = max(1, (height // 2) - (len(logo) // 2) - 2)
    for i, line in enumerate(logo):
        x = max(0, (width - len(line)) // 2)
        stdscr.addnstr(start_y + i, x, line, width - 1, curses.A_BOLD)
    version_line = f"tvTUI {VERSION}"
    stdscr.addnstr(start_y + len(logo) + 1, max(0, (width - len(version_line)) // 2), version_line, width - 1)
    msg = f"{message} {spinner_char}"
    stdscr.addnstr(start_y + len(logo) + 3, max(0, (width - len(msg)) // 2), msg, width - 1)
    stdscr.refresh()


def filter_channels(channels: List[Channel], query: str, programs: Dict[str, Program]) -> List[Channel]:
    if not query:
        return channels
    query = query.lower()
    filtered = []
    for chan in channels:
        program = format_program(programs, chan)
        if query in chan.name.lower() or query in program.lower():
            filtered.append(chan)
    return filtered


def select_category(
    stdscr: "curses._CursesWindow",
) -> Optional[Tuple[str, str]]:
    categories = [
        ("All", "tv"),
        ("Entertainment", "base"),
        ("Live Events", "events"),
    ]
    index = 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, "Select a category (Enter=choose, q=quit)", width - 1)
        for i, (label, cat_id) in enumerate(categories):
            line = f"{label} ({cat_id})"
            if i == index:
                stdscr.attron(curses.A_REVERSE)
                stdscr.addnstr(2 + i, 2, line, width - 3)
                stdscr.attroff(curses.A_REVERSE)
            else:
                stdscr.addnstr(2 + i, 2, line, width - 3)
        stdscr.refresh()
        ch = stdscr.get_wch()
        if ch in ("q", "Q"):
            return None
        if ch in ("\n", "\r"):
            return categories[index]
        if ch in (curses.KEY_UP, "k"):
            index = max(0, index - 1)
        if ch in (curses.KEY_DOWN, "j"):
            index = min(len(categories) - 1, index + 1)


def select_from_list(
    stdscr: "curses._CursesWindow",
    title: str,
    items: List[Tuple[str, str]],
) -> Optional[Tuple[str, str]]:
    if not items:
        return None
    index = 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, title, width - 1)
        for i, (label, ident) in enumerate(items):
            line = f"{label} ({ident})"
            if i == index:
                stdscr.attron(curses.A_REVERSE)
                stdscr.addnstr(2 + i, 2, line, width - 3)
                stdscr.attroff(curses.A_REVERSE)
            else:
                stdscr.addnstr(2 + i, 2, line, width - 3)
        stdscr.refresh()
        ch = stdscr.get_wch()
        if ch in ("q", "Q", "\x1b"):
            return None
        if ch in ("\n", "\r"):
            return items[index]
        if ch in (curses.KEY_UP, "k"):
            index = max(0, index - 1)
        if ch in (curses.KEY_DOWN, "j"):
            index = min(len(items) - 1, index + 1)


def run_tui(
    initial_query: str,
    favorites_only: bool,
    categories_only: bool,
    favorites_file: str,
    history_file: str,
    epg_cache: str,
    channels_cache: str,
    epg_url: str,
    base_m3u_url: str,
    streamed_base: str,
    use_emoji_tags: bool,
    show_help_panel: bool,
    player_config: PlayerConfig,
    subs_default: bool,
    xtream_base_url: str,
    xtream_username: str,
    xtream_password: str,
    xtream_use_for_tv: bool,
    epg_fuzzy_match: bool,
    epg_fuzzy_threshold: float,
) -> bool:
    player = Player(player_config)
    result = {"help_visible": show_help_panel}

    def tui(stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_MAGENTA, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            curses.init_pair(5, curses.COLOR_BLUE, -1)
            curses.init_pair(6, curses.COLOR_RED, -1)
            curses.init_pair(7, curses.COLOR_WHITE, -1)
            curses.init_pair(8, curses.COLOR_YELLOW, -1)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        stdscr.keypad(True)
        query = initial_query
        help_visible = show_help_panel
        search_mode = False
        status_message = ""
        subs_enabled = subs_default
        sort_modes = ["default", "name", "category"]
        sort_mode = "default"
        content_modes = ["tv", "movie", "series"]
        xtream_enabled = bool(xtream_base_url and xtream_username and xtream_password)
        if not xtream_enabled:
            content_modes = ["tv"]
        content_mode = "tv"
        spinner = ["|", "/", "-", "\\"]
        filtered: List[Channel] = []
        favorites_set: set = set()
        selected_index = 0
        top_index = 0
        mode = "channels"

        def splash_with_spinner(message: str, func):
            result = {"value": None, "error": None}

            def target():
                try:
                    result["value"] = func()
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            i = 0
            while thread.is_alive():
                render_splash(stdscr, message, spinner[i % len(spinner)])
                time.sleep(0.1)
                i += 1
            thread.join()
            if result["error"]:
                raise result["error"]
            return result["value"]

        def fetch_with_spinner(message: str, func):
            nonlocal status_message
            result = {"value": None, "error": None}

            def target():
                try:
                    result["value"] = func()
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            i = 0
            while thread.is_alive():
                status_message = f"{message} {spinner[i % len(spinner)]}"
                render_screen(
                    stdscr,
                    filtered,
                    programs,
                    epg_index,
                    favorites_set,
                    selected_index,
                    top_index,
                    query,
                    mode,
                    help_visible,
                    search_mode,
                    use_emoji_tags,
                    desc_index,
                    status_message,
                    sort_mode,
                    content_mode,
                )
                time.sleep(0.1)
                i += 1
            thread.join()
            status_message = ""
            if result["error"]:
                raise result["error"]
            return result["value"]
        desc_index = 0
        last_selected_index = -1
        mode = "channels"
        if favorites_only:
            mode = "favorites"
        elif categories_only:
            mode = "categories"

        def initial_load():
            ok, err = update_epg_cache(epg_cache, epg_url)
            programs = build_program_map(epg_cache)
            epg_index = build_epg_index(epg_cache)
            if content_mode == "tv" and xtream_enabled and xtream_use_for_tv:
                channels, chan_err = xtream_get_streams(
                    xtream_base_url, xtream_username, xtream_password, "live"
                )
            else:
                channels, chan_err = get_iptv_channels(channels_cache, base_m3u_url)
            return ok, err, programs, epg_index, channels, chan_err

        ok, err, programs, epg_index, channels, chan_err = splash_with_spinner(
            "Loading", initial_load
        )
        if not channels:
            channels = get_fallback_channels()
            if chan_err:
                show_popup(stdscr, "Network Error", chan_err)
        if not ok and err:
            show_popup(stdscr, "Network Error", err)

        favorites = load_favorites(favorites_file)
        favorites, fav_changed = repair_favorites_with_epg(
            favorites, epg_cache, epg_fuzzy_match, epg_fuzzy_threshold
        )
        if fav_changed:
            save_favorites(favorites_file, favorites)
        favorites_set = {f.url for f in favorites}
        if content_mode == "tv":
            if content_mode == "tv":
                channels = merge_with_favorites(channels, favorites)
        if categories_only:
            selection = select_category(stdscr)
            if selection:
                _, cat_id = selection
                channels, cat_err = get_category_channels(cat_id, streamed_base)
                if not channels and cat_err:
                    show_popup(stdscr, "Network Error", cat_err)
            mode = "channels"
        filtered = apply_sort(
            filter_channels(channels, query, programs), sort_mode, use_emoji_tags
        )
        if favorites_only:
            filtered = apply_sort(filter_channels(favorites, query, programs), sort_mode, use_emoji_tags)
            mode = "favorites"

        selected_index = 0
        top_index = 0
        desc_index = 0
        last_selected_index = 0
        while True:
            if selected_index >= len(filtered):
                selected_index = max(0, len(filtered) - 1)
            height, width = stdscr.getmaxyx()
            list_height = max(1, height - 2 - 7)
            if selected_index < top_index:
                top_index = selected_index
            if selected_index >= top_index + list_height:
                top_index = selected_index - list_height + 1

            render_screen(
                stdscr,
                filtered,
                programs,
                epg_index,
                favorites_set,
                selected_index,
                top_index,
                query,
                mode,
                help_visible,
                search_mode,
                use_emoji_tags,
                desc_index,
                status_message,
                sort_mode,
                content_mode,
            )
            ch = stdscr.get_wch()
            if ch == curses.KEY_RESIZE:
                continue
            if ch == curses.KEY_MOUSE:
                try:
                    _mid, mx, my, _mz, bstate = curses.getmouse()
                except curses.error:
                    continue
                if bstate & curses.BUTTON4_PRESSED:
                    selected_index = max(0, selected_index - 1)
                elif bstate & curses.BUTTON5_PRESSED:
                    selected_index = min(len(filtered) - 1, selected_index + 1)
                else:
                    list_top = 1
                    list_bottom = list_top + list_height
                    if list_top <= my < list_bottom:
                        new_index = top_index + (my - list_top)
                        if 0 <= new_index < len(filtered):
                            selected_index = new_index
                            if bstate & getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0):
                                channel = filtered[selected_index]
                                if content_mode == "series":
                                    episodes, ep_err = xtream_get_series_episodes(
                                        xtream_base_url,
                                        xtream_username,
                                        xtream_password,
                                        channel.tvg_id,
                                    )
                                    if not episodes and ep_err:
                                        show_popup(stdscr, "Network Error", ep_err)
                                        continue
                                    selection = select_from_list(
                                        stdscr,
                                        "Select Episode (Enter=play, q=cancel)",
                                        episodes,
                                    )
                                    if selection:
                                        ep_label, ep_url = selection
                                        append_history(
                                            history_file, Channel(ep_label, ep_url, "")
                                        )
                                        player.play(ep_url, subs_enabled)
                                else:
                                    append_history(history_file, channel)
                                    player.play(channel.url, subs_enabled)
                if selected_index != last_selected_index:
                    desc_index = 0
                    last_selected_index = selected_index
                continue
            if search_mode:
                if ch in ("\n", "\r", "\x1b"):
                    search_mode = False
                    continue
                if ch in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    query = query[:-1]
                    filtered = apply_sort(
                        filter_channels(channels, query, programs), sort_mode, use_emoji_tags
                    )
                    if mode == "favorites":
                        filtered = apply_sort(
                            filter_channels(favorites, query, programs),
                            sort_mode,
                            use_emoji_tags,
                        )
                    selected_index = 0
                    top_index = 0
                    desc_index = 0
                    last_selected_index = 0
                    continue
                if isinstance(ch, str) and ch.isprintable():
                    query += ch
                    filtered = apply_sort(
                        filter_channels(channels, query, programs), sort_mode, use_emoji_tags
                    )
                    if mode == "favorites":
                        filtered = apply_sort(
                            filter_channels(favorites, query, programs),
                            sort_mode,
                            use_emoji_tags,
                        )
                    selected_index = 0
                    top_index = 0
                    desc_index = 0
                    last_selected_index = 0
                continue
            if ch in ("q", "Q"):
                result["help_visible"] = help_visible
                return
            if ch in ("\n", "\r"):
                if filtered:
                    channel = filtered[selected_index]
                    if content_mode == "series":
                        episodes, ep_err = fetch_with_spinner(
                            "Loading episodes",
                            lambda: xtream_get_series_episodes(
                                xtream_base_url,
                                xtream_username,
                                xtream_password,
                                channel.tvg_id,
                            ),
                        )
                        if not episodes and ep_err:
                            show_popup(stdscr, "Network Error", ep_err)
                            continue
                        selection = select_from_list(
                            stdscr, "Select Episode (Enter=play, q=cancel)", episodes
                        )
                        if selection:
                            ep_label, ep_url = selection
                            append_history(history_file, Channel(ep_label, ep_url, ""))
                            player.play(ep_url, subs_enabled)
                    else:
                        append_history(history_file, channel)
                        player.play(channel.url, subs_enabled)
                continue
            if ch in ("h", "H"):
                help_visible = not help_visible
                continue
            if ch in ("t", "T"):
                subs_enabled = not subs_enabled
                status_message = f"Subtitles: {'On' if subs_enabled else 'Off'}"
                continue
            if ch in ("f",):
                if mode == "favorites":
                    mode = "channels"
                else:
                    mode = "favorites"
                filtered = apply_sort(
                    filter_channels(channels, query, programs), sort_mode, use_emoji_tags
                )
                if mode == "favorites":
                    filtered = apply_sort(
                        filter_channels(favorites, query, programs),
                        sort_mode,
                        use_emoji_tags,
                    )
                selected_index = 0
                top_index = 0
                desc_index = 0
                last_selected_index = 0
                continue
            if ch in ("F",):
                if filtered:
                    channel = filtered[selected_index]
                    if content_mode != "series":
                        toggle_favorite(favorites_file, channel)
                    favorites = load_favorites(favorites_file)
                    favorites_set = {f.url for f in favorites}
                    if content_mode == "tv":
                        channels = merge_with_favorites(channels, favorites)
                    if mode == "favorites":
                        filtered = apply_sort(
                            filter_channels(favorites, query, programs),
                            sort_mode,
                            use_emoji_tags,
                        )
                        selected_index = 0
                        top_index = 0
                        desc_index = 0
                        last_selected_index = 0
                continue
            if ch in ("c", "C"):
                if content_mode == "tv" and (not xtream_enabled or not xtream_use_for_tv):
                    selection = select_category(stdscr)
                    if selection:
                        _, cat_id = selection
                        channels, cat_err = get_category_channels(cat_id, streamed_base)
                        programs = build_program_map(epg_cache)
                        epg_index = build_epg_index(epg_cache)
                        query = ""
                        mode = "channels"
                        if content_mode == "tv":
                            channels = merge_with_favorites(channels, favorites)
                        filtered = apply_sort(
                            filter_channels(channels, query, programs),
                            sort_mode,
                            use_emoji_tags,
                        )
                        selected_index = 0
                        top_index = 0
                        desc_index = 0
                        last_selected_index = 0
                        if not channels and cat_err:
                            show_popup(stdscr, "Network Error", cat_err)
                else:
                    if not xtream_enabled:
                        show_popup(stdscr, "Xtream Not Configured", "Add Xtream credentials in config.")
                        continue
                    cat_items, cat_err = fetch_with_spinner(
                        "Loading categories",
                        lambda: xtream_get_categories(
                            xtream_base_url, xtream_username, xtream_password, content_mode
                        ),
                    )
                    if cat_err:
                        show_popup(stdscr, "Network Error", cat_err)
                        continue
                    selection = select_from_list(
                        stdscr, "Select Category (Enter=browse, q=cancel)", cat_items
                    )
                    if selection:
                        _, cat_id = selection
                        channels, cat_err = fetch_with_spinner(
                            "Loading streams",
                            lambda: xtream_get_streams(
                                xtream_base_url,
                                xtream_username,
                                xtream_password,
                                content_mode,
                                category_id=cat_id,
                            ),
                        )
                        if cat_err:
                            show_popup(stdscr, "Network Error", cat_err)
                        query = ""
                        mode = "channels"
                        filtered = apply_sort(
                            filter_channels(channels, query, programs),
                            sort_mode,
                            use_emoji_tags,
                        )
                        selected_index = 0
                        top_index = 0
                        desc_index = 0
                        last_selected_index = 0
                continue
            if ch in ("m", "M"):
                if not xtream_enabled:
                    show_popup(stdscr, "Xtream Not Configured", "Add Xtream credentials in config.")
                    continue
                current = content_modes.index(content_mode)
                content_mode = content_modes[(current + 1) % len(content_modes)]
                status_message = f"Content: {content_mode}"
                if content_mode == "tv" and xtream_use_for_tv:
                    channels, chan_err = fetch_with_spinner(
                        "Loading Xtream TV",
                        lambda: xtream_get_streams(
                            xtream_base_url, xtream_username, xtream_password, "live"
                        ),
                    )
                elif content_mode == "tv":
                    channels, chan_err = get_iptv_channels(channels_cache, base_m3u_url)
                else:
                    channels, chan_err = fetch_with_spinner(
                        "Loading streams",
                        lambda: xtream_get_streams(
                            xtream_base_url, xtream_username, xtream_password, content_mode
                        ),
                    )
                if chan_err:
                    show_popup(stdscr, "Network Error", chan_err)
                query = ""
                mode = "channels"
                filtered = apply_sort(
                    filter_channels(channels, query, programs), sort_mode, use_emoji_tags
                )
                selected_index = 0
                top_index = 0
                desc_index = 0
                last_selected_index = 0
                continue
            if ch in ("s", "S"):
                current = sort_modes.index(sort_mode)
                sort_mode = sort_modes[(current + 1) % len(sort_modes)]
                status_message = f"Sort: {sort_mode}"
                filtered = apply_sort(filtered, sort_mode, use_emoji_tags)
                selected_index = 0
                top_index = 0
                desc_index = 0
                last_selected_index = 0
                continue
            if ch in ("r", "R"):
                status_message = "Refreshing..."
                render_screen(
                    stdscr,
                    filtered,
                    programs,
                    epg_index,
                    favorites_set,
                    selected_index,
                    top_index,
                    query,
                    mode,
                    help_visible,
                    search_mode,
                    use_emoji_tags,
                    desc_index,
                    status_message,
                    sort_mode,
                    content_mode,
                )
                ok, err = update_epg_cache(epg_cache, epg_url)
                programs = build_program_map(epg_cache)
                epg_index = build_epg_index(epg_cache)
                if content_mode == "tv" and xtream_enabled and xtream_use_for_tv:
                    channels, chan_err = fetch_with_spinner(
                        "Loading Xtream TV",
                        lambda: xtream_get_streams(
                            xtream_base_url, xtream_username, xtream_password, "live"
                        ),
                    )
                elif content_mode == "tv":
                    channels, chan_err = get_iptv_channels(channels_cache, base_m3u_url)
                else:
                    channels, chan_err = fetch_with_spinner(
                        "Loading streams",
                        lambda: xtream_get_streams(
                            xtream_base_url, xtream_username, xtream_password, content_mode
                        ),
                    )
                if not channels:
                    channels = get_fallback_channels()
                if content_mode == "tv":
                    channels = merge_with_favorites(channels, favorites)
                if mode == "favorites":
                    filtered = apply_sort(
                        filter_channels(favorites, query, programs),
                        sort_mode,
                        use_emoji_tags,
                    )
                else:
                    filtered = apply_sort(
                        filter_channels(channels, query, programs),
                        sort_mode,
                        use_emoji_tags,
                    )
                selected_index = 0
                top_index = 0
                desc_index = 0
                last_selected_index = 0
                status_message = ""
                if not ok and err:
                    show_popup(stdscr, "Network Error", err)
                elif chan_err:
                    show_popup(stdscr, "Network Error", chan_err)
                continue
            if ch in (curses.KEY_UP, "k"):
                selected_index = max(0, selected_index - 1)
                if selected_index != last_selected_index:
                    desc_index = 0
                    last_selected_index = selected_index
                continue
            if ch in (curses.KEY_DOWN, "j"):
                selected_index = min(len(filtered) - 1, selected_index + 1)
                if selected_index != last_selected_index:
                    desc_index = 0
                    last_selected_index = selected_index
                continue
            if ch in (curses.KEY_PPAGE,):
                selected_index = max(0, selected_index - 10)
                if selected_index != last_selected_index:
                    desc_index = 0
                    last_selected_index = selected_index
                continue
            if ch in (curses.KEY_NPAGE,):
                selected_index = min(len(filtered) - 1, selected_index + 10)
                if selected_index != last_selected_index:
                    desc_index = 0
                    last_selected_index = selected_index
                continue
            if ch in (curses.KEY_RIGHT,):
                if filtered:
                    items = program_listing(epg_index, filtered[selected_index])
                    if items:
                        desc_index = min(len(items) - 1, desc_index + 1)
                continue
            if ch in (curses.KEY_LEFT,):
                if filtered:
                    items = program_listing(epg_index, filtered[selected_index])
                    if items:
                        desc_index = max(0, desc_index - 1)
                continue
            if ch in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                continue
            if ch == "/":
                if search_mode:
                    query = ""
                search_mode = True
                continue

    curses.wrapper(tui)
    return result["help_visible"]


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tvtui",
        description="tvTUI - IPTV terminal user interface",
    )
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("-v", "--version", action="store_true", help="show version")
    parser.add_argument("--clear-cache", action="store_true", help="clear cached data")
    parser.add_argument("-f", "--favorites", action="store_true", help="show favorites only")
    parser.add_argument("-c", "--categories", action="store_true", help="browse categories")
    parser.add_argument(
        "--config",
        default="~/.config/tvtui/config.json",
        help="path to config file",
    )
    parser.add_argument("--epg-url", help="override EPG XML URL")
    parser.add_argument("--source-url", help="override IPTV M3U URL")
    parser.add_argument("--streamed-base", help="override categories base URL")
    parser.add_argument("--emoji-tags", action="store_true", help="use emoji category tags")
    parser.add_argument("--no-emoji-tags", action="store_true", help="disable emoji category tags")
    return parser.parse_args(argv)


def clear_cache(cache_dir: str, epg_cache: str) -> None:
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
    if os.path.exists(epg_cache):
        os.remove(epg_cache)
    print("Cache cleared.")


def load_config(path: str) -> Dict[str, str]:
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return {}
    try:
        with open(expanded, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def save_config(path: str, updates: Dict[str, object]) -> None:
    expanded = os.path.expanduser(path)
    base = os.path.dirname(expanded)
    if base:
        os.makedirs(base, exist_ok=True)
    data = load_config(path)
    for key, value in updates.items():
        data[key] = value
    tmp = expanded + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, expanded)


def parse_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return None


def normalize_args(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return shlex.split(value)
    return [str(value)]


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.version:
        print(f"tvTUI {VERSION}")
        return 0

    config_dir, cache_dir, favorites_file, history_file, epg_cache, channels_cache = config_paths()
    ensure_dirs(config_dir, cache_dir)

    if args.clear_cache:
        clear_cache(cache_dir, epg_cache)
        return 0

    config = load_config(args.config)
    epg_url = config.get("epg_url", DEFAULT_EPG_URL)
    base_m3u_url = config.get("source_url", DEFAULT_BASE_M3U_URL)
    streamed_base = config.get("streamed_base", DEFAULT_STREAMED_BASE)
    show_help_panel = True
    config_help = parse_bool(config.get("show_help_panel"))
    if config_help is not None:
        show_help_panel = config_help
    use_emoji_tags = False
    config_emoji = parse_bool(config.get("use_emoji_tags"))
    if config_emoji is not None:
        use_emoji_tags = config_emoji
    subs_default = False
    config_subs = parse_bool(config.get("subs_enabled_default"))
    if config_subs is not None:
        subs_default = config_subs
    player_name = str(config.get("player", "auto")).lower()
    player_args = normalize_args(config.get("player_args"))
    custom_command = normalize_args(config.get("custom_command"))
    custom_subs_on_args = normalize_args(config.get("custom_subs_on_args"))
    custom_subs_off_args = normalize_args(config.get("custom_subs_off_args"))
    mpv_subs_on_args = normalize_args(config.get("mpv_subs_on_args"))
    mpv_subs_off_args = normalize_args(config.get("mpv_subs_off_args"))
    vlc_sub_track = 1
    try:
        vlc_sub_track = int(config.get("vlc_sub_track", 1))
    except (TypeError, ValueError):
        vlc_sub_track = 1
    if not mpv_subs_on_args:
        mpv_subs_on_args = ["--sub-visibility=yes", "--sid=auto"]
    if not mpv_subs_off_args:
        mpv_subs_off_args = ["--sub-visibility=no", "--sid=no"]
    player_config = PlayerConfig(
        name=player_name,
        args=player_args,
        custom_command=custom_command,
        subs_on_args=custom_subs_on_args if player_name == "custom" else mpv_subs_on_args,
        subs_off_args=custom_subs_off_args if player_name == "custom" else mpv_subs_off_args,
        vlc_sub_track=vlc_sub_track,
    )
    xtream_base_url = str(config.get("xtream_base_url", "")).strip()
    xtream_username = str(config.get("xtream_username", "")).strip()
    xtream_password = str(config.get("xtream_password", "")).strip()
    xtream_use_for_tv = True
    xtream_tv = parse_bool(config.get("xtream_use_for_tv"))
    if xtream_tv is not None:
        xtream_use_for_tv = xtream_tv
    epg_fuzzy_match = False
    epg_fuzzy = parse_bool(config.get("epg_fuzzy_match"))
    if epg_fuzzy is not None:
        epg_fuzzy_match = epg_fuzzy
    epg_fuzzy_threshold = 0.85
    try:
        epg_fuzzy_threshold = float(config.get("epg_fuzzy_threshold", 0.85))
    except (TypeError, ValueError):
        epg_fuzzy_threshold = 0.85
    if args.epg_url:
        epg_url = args.epg_url
    if args.source_url:
        base_m3u_url = args.source_url
    if args.streamed_base:
        streamed_base = args.streamed_base
    if args.emoji_tags:
        use_emoji_tags = True
    if args.no_emoji_tags:
        use_emoji_tags = False

    show_help_panel = run_tui(
        initial_query=args.query,
        favorites_only=args.favorites,
        categories_only=args.categories,
        favorites_file=favorites_file,
        history_file=history_file,
        epg_cache=epg_cache,
        channels_cache=channels_cache,
        epg_url=epg_url,
        base_m3u_url=base_m3u_url,
        streamed_base=streamed_base,
        use_emoji_tags=use_emoji_tags,
        show_help_panel=show_help_panel,
        player_config=player_config,
        subs_default=subs_default,
        xtream_base_url=xtream_base_url,
        xtream_username=xtream_username,
        xtream_password=xtream_password,
        xtream_use_for_tv=xtream_use_for_tv,
        epg_fuzzy_match=epg_fuzzy_match,
        epg_fuzzy_threshold=epg_fuzzy_threshold,
    )
    save_config(args.config, {"show_help_panel": show_help_panel})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
