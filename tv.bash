#!/bin/bash

# tvfzf - TV Channel streaming interface
VERSION="1.0.0"

# Configuration
CONFIG_DIR="$HOME/.config/tvfzf"
CACHE_DIR="$HOME/.cache/tvfzf"
FAVORITES_FILE="$CONFIG_DIR/favorites"
HISTORY_FILE="$CONFIG_DIR/history"
EPG_CACHE="$CONFIG_DIR/epg.xml"

# Create directories
mkdir -p "$CONFIG_DIR" "$CACHE_DIR"
touch "$FAVORITES_FILE" "$HISTORY_FILE"

update_epg_cache() {
  local epg_url="https://raw.githubusercontent.com/doms9/iptv/refs/heads/default/EPG/TV.xml"

  # Check if cache exists and is less than 12 hours old
  if [[ -f "$EPG_CACHE" ]] && [[ $(($(date +%s) - $(stat -c %Y "$EPG_CACHE" 2>/dev/null || echo 0))) -lt 43200 ]]; then
    return 0 # Cache is fresh
  fi

  echo "🔄 Downloading EPG data (17MB)..."
  if curl -s --progress-bar "$epg_url" >"$EPG_CACHE.tmp" 2>/dev/null && [[ -s "$EPG_CACHE.tmp" ]]; then
    mv "$EPG_CACHE.tmp" "$EPG_CACHE"
    echo "✅ EPG data updated"
  else
    rm -f "$EPG_CACHE.tmp"
    echo "❌ Failed to update EPG data"
    return 1
  fi
}

get_epg_info() {
  local channel_name="$1"

  # Ensure EPG cache exists
  if [[ ! -f "$EPG_CACHE" ]]; then
    update_epg_cache
  fi

  if [[ -f "$EPG_CACHE" ]]; then
    # Get current time in EPG format (YYYYMMDDHHMMSS)
    local current_time=$(date -u +"%Y%m%d%H%M%S")

    # Find channel ID from EPG
    local channel_id=$(grep -i "display-name.*$channel_name" "$EPG_CACHE" | head -1 | grep -o 'channel id="[^"]*"' | sed 's/.*id="//;s/"//')

    if [[ -n "$channel_id" ]]; then
      # Find current program
      local program_info=$(awk -v ch="$channel_id" -v now="$current_time" '
            /<programme.*channel="'"$channel_id"'"/ {
                match($0, /start="([^"]*)"/, start_arr)
                match($0, /stop="([^"]*)"/, stop_arr)
                start_time = start_arr[1]
                stop_time = stop_arr[1]
                
                # Remove timezone info for comparison
                gsub(/ [+-][0-9]{4}/, "", start_time)
                gsub(/ [+-][0-9]{4}/, "", stop_time)
                
                if (start_time <= now && stop_time > now) {
                    getline title_line
                    if (match(title_line, /<title[^>]*>([^<]*)<\/title>/, title_arr)) {
                        print "🎬 Now Playing: " title_arr[1]
                        print "🕒 " substr(start_time, 9, 2) ":" substr(start_time, 11, 2) " - " substr(stop_time, 9, 2) ":" substr(stop_time, 11, 2)
                        exit
                    }
                }
            }' "$EPG_CACHE")

      if [[ -n "$program_info" ]]; then
        echo "$program_info"
      else
        echo "📺 Live Programming"
        echo "🕒 $(date +"%H:%M") - Current Show"
      fi
    else
      echo "📺 Live Programming"
      echo "🕒 $(date +"%H:%M") - Current Show"
    fi
  else
    echo "📺 Live Programming"
    echo "🕒 EPG data unavailable"
  fi

  echo ""
  echo "📡 Live TV Stream from doms9/iptv"
}

show_help() {
  cat <<EOF
tvfzf - TV Channel streaming interface

USAGE:
    tvfzf [OPTIONS] [SEARCH_QUERY]

OPTIONS:
    -h, --help          Show this help
    -v, --version       Show version
    --clear-cache       Clear all cached data
    -c, --categories    Browse categories
    -f, --favorites     Show favorites only

INTERACTIVE KEYS:
    Enter               Play channel
    Alt+f               Add/remove from favorites
    Alt+c               Browse categories
    Alt+q               Quit

EXAMPLES:
    tvfzf               Show IPTV channels (146+ live streams)
    tvfzf "CNN"         Search for CNN
    tvfzf -f            Show favorites
    tvfzf -c            Browse categories
EOF
}

CHANNELS_CACHE="$CACHE_DIR/channels"

get_iptv_channels() {
  local current_time=$(date -u +"%Y%m%d%H%M%S")

  # Use cached channels if less than 1 hour old
  if [[ -f "$CHANNELS_CACHE" ]] && [[ $(($(date +%s) - $(stat -c %Y "$CHANNELS_CACHE" 2>/dev/null || echo 0))) -lt 3600 ]]; then
    cat "$CHANNELS_CACHE"
    return
  fi

  # Build EPG lookup from cache
  declare -A epg
  if [[ -f "$EPG_CACHE" ]]; then
    while IFS='=' read -r id program; do
      epg["$id"]="$program"
    done < <(awk -v now="$current_time" '
        /<programme / {
            match($0, /channel="([^"]+)"/, ch)
            match($0, /start="([0-9]+)/, st)
            match($0, /stop="([0-9]+)/, sp)
            if (ch[1] != "" && st[1] <= now && sp[1] > now) {
                getline
                match($0, /<title[^>]*>([^<]+)</, t)
                if (t[1] != "") {
                    gsub(/\&amp;/, "\\&", t[1])
                    gsub(/\&lt;/, "<", t[1])
                    gsub(/\&gt;/, ">", t[1])
                    gsub(/\&quot;/, "\"", t[1])
                    gsub(/\&apos;/, "'"'"'", t[1])
                    print ch[1] "=" t[1]
                }
            }
        }' "$EPG_CACHE" 2>/dev/null)
  fi

  # Process M3U8 and add EPG data
  curl -L -s "https://s.id/d9Base" 2>/dev/null | while IFS= read -r line; do
    if [[ "$line" =~ ^#EXTINF ]]; then
      channel_name=$(echo "$line" | sed -n 's/.*tvg-name="\([^"]*\)".*/\1/p')
      tvg_id=$(echo "$line" | sed -n 's/.*tvg-id="\([^"]*\)".*/\1/p')
      read -r url

      if [[ -n "$channel_name" && -n "$url" && ! "$url" =~ ^# ]]; then
        emoji="🎬"
        case "$channel_name" in
        *ESPN* | *NFL* | *NBA* | *MLB* | *Sports*) emoji="⚽" ;;
        *CNN* | *News* | *BBC*) emoji="📰" ;;
        *MTV* | *Music* | *BET*) emoji="🎵" ;;
        *Cartoon* | *Nick* | *Disney* | *Kids*) emoji="👶" ;;
        *Food* | *HGTV* | *Travel* | *Cooking*) emoji="🏠" ;;
        esac

        program="${epg[$tvg_id]:-Live Programming}"
        program="${program//&amp;/&}"
        program="${program//&lt;/<}"
        program="${program//&gt;/>}"
        printf "%s %s - %s\t%s\t%s\t%s\n" "$emoji" "$channel_name" "$program" "$url" "$tvg_id" "$channel_name"
      fi
    fi
  done | tee "$CHANNELS_CACHE"
}

get_fallback_channels() {
  cat <<'EOF'
📺 France 24	https://static.france24.com/live/F24_EN_LO_HLS/live_web.m3u8	France 24 English - International news
📺 CBS News	https://cbsn-us.cbsnstream.cbsnews.com/out/v1/55a8648e8f134e82a470f83d562deeca/master.m3u8	CBS News - US news
⚽ Red Bull TV	https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8	Red Bull TV - Extreme sports
🎬 Pluto TV Movies	https://service-stitcher.clusters.pluto.tv/stitch/hls/channel/5cb0cae7a461406ffe3f5213/master.m3u8	Pluto TV Movies - Free movies
EOF
}

show_categories() {
  cat <<'EOF'
📺 All	tv	TV.m3u8
🎭 Entertainment	base	base.m3u8
🏆 Live Events	events	events.m3u8
EOF
}

STREAMED_BASE="https://raw.githubusercontent.com/doms9/iptv/default/M3U8"

parse_m3u() {
  local url="$1" emoji="$2"
  local current_time=$(date -u +"%Y%m%d%H%M%S")
  local m3u_content=$(curl -s "$url" 2>/dev/null)

  # Build EPG lookup
  declare -A epg
  if [[ -f "$EPG_CACHE" ]]; then
    while IFS='=' read -r id program; do
      epg["$id"]="$program"
    done < <(awk -v now="$current_time" '
        /<programme / {
            match($0, /channel="([^"]+)"/, ch)
            match($0, /start="([0-9]+)/, st)
            match($0, /stop="([0-9]+)/, sp)
            if (ch[1] != "" && st[1] <= now && sp[1] > now) {
                getline
                match($0, /<title[^>]*>([^<]+)</, t)
                if (t[1] != "") print ch[1] "=" t[1]
            }
        }' "$EPG_CACHE" 2>/dev/null)
  fi

  local name="" tvg_id=""
  while IFS= read -r line; do
    if [[ "$line" =~ ^#EXTINF ]]; then
      name="${line##*,}"
      name="${name//&amp;/&}"
      name="${name//&lt;/<}"
      name="${name//&gt;/>}"
      [[ "$line" =~ tvg-id=\"([^\"]+)\" ]] && tvg_id="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^http ]] && [[ -n "$name" ]]; then
      local program="${epg[$tvg_id]:-}"
      program="${program//&amp;/&}"
      if [[ -n "$program" ]]; then
        printf "%s %s - %s\t%s\t%s\t%s\n" "$emoji" "$name" "$program" "$line" "$tvg_id" "$name"
      else
        printf "%s %s\t%s\t%s\t%s\n" "$emoji" "$name" "$line" "$tvg_id" "$name"
      fi
      name="" tvg_id=""
    fi
  done <<<"$m3u_content"
}

get_events() { parse_m3u "$STREAMED_BASE/events.m3u8" "🏆"; }
get_tv() { parse_m3u "$STREAMED_BASE/TV.m3u8" "📺"; }
get_base() { parse_m3u "$STREAMED_BASE/base.m3u8" "📺"; }

is_favorite() {
  local url="$1"
  grep -q $'\t'"$url"$'\t' "$FAVORITES_FILE" 2>/dev/null
}

add_to_favorites() {
  local channel_line="$1"
  # Strip any existing heart emoji and normalize
  local clean_line=$(echo "$channel_line" | sed 's/^❤️ //' | sed 's/^[^ ]* /🎬 /')
  local url=$(echo "$clean_line" | cut -f2)

  if grep -q $'\t'"$url"$'\t' "$FAVORITES_FILE" 2>/dev/null; then
    grep -v $'\t'"$url"$'\t' "$FAVORITES_FILE" >"$FAVORITES_FILE.tmp"
    mv "$FAVORITES_FILE.tmp" "$FAVORITES_FILE"
  else
    echo "$clean_line" >>"$FAVORITES_FILE"
  fi
}

# Global variable to track player process
PLAYER_PID=""

# Cleanup function - only kill player if starting new stream, not on exit
cleanup() {
  : # Do nothing on exit - let mpv keep playing
}

play_channel() {
  local channel_line="$1"
  local channel_name=$(echo "$channel_line" | cut -f1 | sed 's/^[^ ]* //')
  local channel_url=$(echo "$channel_line" | cut -f2)

  echo "$(date '+%Y-%m-%d %H:%M:%S') - $channel_name" >>"$HISTORY_FILE"

  # Kill existing player if running
  if [[ -n "$PLAYER_PID" ]] && kill -0 "$PLAYER_PID" 2>/dev/null; then
    kill "$PLAYER_PID" 2>/dev/null
    wait "$PLAYER_PID" 2>/dev/null
  fi

  # Start player silently in background
  if command -v mpv >/dev/null 2>&1; then
    mpv "$channel_url" >/dev/null 2>&1 &
    PLAYER_PID=$!
  elif command -v vlc >/dev/null 2>&1; then
    vlc "$channel_url" >/dev/null 2>&1 &
    PLAYER_PID=$!
  fi
}

main() {
  local search_query=""
  local show_favorites=false
  local show_categories_only=false

  while [[ $# -gt 0 ]]; do
    case $1 in
    -h | --help)
      show_help
      exit 0
      ;;
    -v | --version)
      echo "tvfzf $VERSION"
      exit 0
      ;;
    --clear-cache)
      rm -rf "$CACHE_DIR"/* "$EPG_CACHE"
      echo "Cache cleared"
      exit 0
      ;;
    -f | --favorites)
      show_favorites=true
      shift
      ;;
    -c | --categories)
      show_categories_only=true
      shift
      ;;
    *)
      search_query="$1"
      shift
      ;;
    esac
  done

  # Update EPG cache on startup
  update_epg_cache

  if ! command -v fzf >/dev/null 2>&1; then
    echo "Error: fzf is required but not installed"
    exit 1
  fi

  local channels=""
  local last_pos=1
  local last_url=""
  local show_help=false

  while true; do
    local prompt="📺 TV Channels"
    local header=""
    $show_help && header="Enter/dbl-click=play, Alt+F/right-click=fav, Alt+f=favs, Alt+c=categories, Alt+h=help"
    [[ -z "$last_pos" ]] && last_pos=1

    # Only reload channels if needed
    if [[ -z "$channels" ]]; then
      if $show_categories_only; then
        local selected_category=$(show_categories | fzf \
          --height=100% \
          --prompt="📂 Categories > " \
          --header="Select a category (Enter=browse, Alt+q=quit)" \
          --expect="alt-q" \
          --delimiter=$'\t' \
          --with-nth=1 \
          --preview='echo "Category: {2}"' \
          --preview-window=up:3:wrap)

        local key=$(echo "$selected_category" | head -1)
        local category_line=$(echo "$selected_category" | tail -1)

        case "$key" in
        "alt-q")
          exit 0
          ;;
        "")
          if [[ -n "$category_line" ]]; then
            local category_id=$(echo "$category_line" | cut -f2)
            case "$category_id" in
            events) channels=$(get_events) ;;
            tv) channels=$(get_tv) ;;
            base) channels=$(get_base) ;;
            esac
            prompt="📂 $(echo "$category_line" | cut -f1) Channels"
            show_categories_only=false
          else
            continue
          fi
          ;;
        esac
      elif $show_favorites; then
        if [[ ! -s "$FAVORITES_FILE" ]]; then
          echo "No favorites yet. Use Alt+F to add channels to favorites."
          sleep 2
          show_favorites=false
          continue
        fi
        # Add heart emoji to favorites
        channels=$(cat "$FAVORITES_FILE" | sed 's/^[^ ]* /❤️ /')
        prompt="❤️ Favorites"
      elif [[ -n "$search_query" ]]; then
        channels=$(get_iptv_channels | grep -i "$search_query")
        prompt="🔍 Search: $search_query"
      else
        channels=$(get_iptv_channels)
        if [[ -z "$channels" ]]; then
          echo "❌ Failed to load IPTV channels. Using fallback..."
          channels=$(get_fallback_channels)
        fi
        prompt="📡 IPTV Channels"
      fi
      # Mark favorites with heart and sort to top
      if [[ -f "$FAVORITES_FILE" && -s "$FAVORITES_FILE" ]]; then
        local fav_urls=$(cut -f2 "$FAVORITES_FILE" | sort -u)
        channels=$(echo "$channels" | while IFS= read -r line; do
          url=$(echo "$line" | cut -f2)
          if echo "$fav_urls" | grep -Fxq "$url"; then
            echo "$line" | sed 's/^[^ ]*/❤️/'
          else
            echo "$line"
          fi
        done | awk -F'\t' '/^❤️/{print "0\t" $0; next} {print "1\t" $0}' | sort -t$'\t' -k1,1 | cut -f2-)
      fi
    fi

    if [[ -z "$channels" ]]; then
      echo "No channels found"
      exit 1
    fi

    # Recalculate position from URL after channel list rebuild
    if [[ -n "$last_url" ]]; then
      last_pos=$(echo "$channels" | grep -n $'\t'"$last_url"$'\t' | head -1 | cut -d: -f1)
      last_pos=${last_pos:-1}
    fi

    local selected=$(echo "$channels" | fzf \
      --height=100% \
      --height=100% \
      --layout=reverse \
      --no-clear \
      --prompt="$prompt > " \
      --query="$search_query" \
      ${header:+--header="$header"} \
      --header-first \
      --expect="alt-F,alt-f,alt-c,alt-h,alt-q,enter,double-click,right-click" \
      --delimiter=$'\t' \
      --with-nth=1 \
      --print-query \
      --sync \
      --bind="start:pos($last_pos)" \
      --preview='channel=$(echo {1} | sed "s/ - .*//"); tvg_id={3}; EPG=~/.config/tvfzf/epg.xml; now=$(date -u +%Y%m%d%H%M%S); echo "📺 $channel" && if [[ -f "$EPG" && -n "$tvg_id" ]]; then grep -A 1 "channel=\"$tvg_id\"" "$EPG" 2>/dev/null | awk -v now="$now" "/<programme/ { match(\$0, /start=.([0-9]+)/, st); match(\$0, /stop=.([0-9]+)/, sp); getline t; match(t, /<title[^>]*>([^<]+)</, title); if (title[1] != \"\") { if (st[1] <= now && sp[1] > now) status=\"🎬 NOW: \"; else if (st[1] > now) status=\"⏭️ NEXT: \"; else next; print status title[1] \"|\" st[1] \"|\" sp[1] } }" | while IFS="|" read -r info start stop; do start_local=$(date -d "${start:0:8} ${start:8:2}:${start:10:2} UTC" +%H:%M 2>/dev/null || echo "${start:8:2}:${start:10:2}"); stop_local=$(date -d "${stop:0:8} ${stop:8:2}:${stop:10:2} UTC" +%H:%M 2>/dev/null || echo "${stop:8:2}:${stop:10:2}"); echo "$info ($start_local-$stop_local)"; done | sed "s/\&amp;/\&/g;s/\&lt;/</g;s/\&gt;/>/g;s/\&quot;/\"/g;s/\&apos;/'\''/g" | head -6; else echo "📺 Live Programming"; fi' \
      --preview-window=down:8:wrap)

    search_query=$(echo "$selected" | head -1)
    local key=$(echo "$selected" | sed -n '2p')
    local channel_line=$(echo "$selected" | tail -1)

    # Track position by URL (field 2) - survives emoji changes
    local last_url=""
    if [[ -n "$channel_line" ]]; then
      last_url=$(echo "$channel_line" | cut -f2)
    fi

    case "$key" in
    "alt-F" | "right-click")
      if [[ -n "$channel_line" ]]; then
        add_to_favorites "$channel_line"
        channels="" # Force rebuild to update hearts
      fi
      ;;
    "alt-f")
      $show_favorites && show_favorites=false || show_favorites=true
      channels=""
      ;;
    "alt-c")
      show_categories_only=true
      show_favorites=false
      search_query=""
      channels=""
      ;;
    "alt-h")
      $show_help && show_help=false || show_help=true
      ;;
    "alt-q")
      clear
      exit 0
      ;;
    "enter" | "double-click")
      if [[ -n "$channel_line" ]]; then
        play_channel "$channel_line"
      fi
      ;;
    "")
      # Empty key with no selection = Esc pressed, exit
      if [[ -z "$channel_line" ]]; then
        clear
        exit 0
      fi
      ;;
    esac
  done
}

main "$@"
