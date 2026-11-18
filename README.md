


## RamzyBots Discord community

Set DISCORD_BOT_TOKEN to an application bot token, DISCORD_GUILD_ID to the RamzyBots server ID, and CHAT_RELAY=discord to disable Telegram forwarding. Owner authorization defaults to the server owner; DISCORD_OWNER_ID can pin it explicitly. Keep DISCORD_STATE_PATH on persistent storage.

The bot creates public community/voice channels and colored roles. Separate private IMVU room logs, owner controls, and VCOIN order tickets deny access to @everyone. No Administrator permission is requested. Customer email addresses are excluded from tickets and trial notifications.

Use /rooms, /control room, /say room message, /status and /help. Room logs have reply/control buttons. Controls cover quiet mode, language, welcomes/default welcome, music search/selection, skip and stop.

Room channels use name-first labels, followed by the full IMVU room ID, and update when the room name becomes available. Public IMVU messages show the cached public display name and exact account username. Profile lookup failure retains the message with a numeric ID fallback.

Enable Message Content Intent in the Discord Developer Portal for direct typing. The gateway detects the granted intent and reconnects within about a minute. Only the pinned DISCORD_OWNER_ID (ramzydc) can send from a room channel to its mapped IMVU room. Copied usernames, administrator roles, bots, webhooks, other guilds and unrelated channels cannot authorize a send. Existing viewers retain view access but lose Send Messages permission. Buttons still work while Message Content Intent is disabled.

The private trial-requests channel receives website customer trial requests, without customer email addresses. Existing requests are picked up on first deployment; each room is notified once across restarts. The website's automatic trial activation remains unchanged.

Website billing remains authoritative. The owner must verify receipt in IMVU Wallet and submit a transaction reference. Stale payment reports and already-confirmed orders cannot extend access twice. Website history, access and receipt-email processing use the existing confirmation transaction.

Queues and reply routes survive restarts, with five-second outage checkpoints. Discord rate-limit headers are honored; relayed mentions/link previews are disabled; concurrent interaction handling is bounded. Setup reuses existing categories, channels, roles and ticket mappings. Telegram state is retained for rollback.

