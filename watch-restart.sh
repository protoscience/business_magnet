#!/usr/bin/env bash
# Watch Python files for changes and restart the relevant services.
# Requires inotifywait (inotify-tools package).

WATCH_DIR="/home/eswar/claude/trading-agent"

inotifywait -m -r -e modify,create,moved_to --include '\.py$|\.html$' "$WATCH_DIR" |
while read -r dir event file; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') Changed: ${dir}${file} (${event})"
    case "$file" in
        discord_bot.py)
            echo "  -> Restarting trading-discord"
            systemctl --user restart trading-discord.service
            ;;
        whatsapp_bridge.py)
            echo "  -> Restarting trading-wa-bridge"
            systemctl --user restart trading-wa-bridge.service
            ;;
        *)
            # Shared code (agent_core.py, tools/*, templates/*) — restart both
            echo "  -> Restarting trading-discord + trading-wa-bridge"
            systemctl --user restart trading-discord.service trading-wa-bridge.service
            ;;
    esac
done
