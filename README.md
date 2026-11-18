


## RamzyBots Discord community

Set DISCORD_BOT_TOKEN to an application bot token, DISCORD_GUILD_ID to the RamzyBots server ID, and CHAT_RELAY=discord to disable Telegram forwarding. Owner authorization defaults to the server owner; DISCORD_OWNER_ID can pin it explicitly. Keep DISCORD_STATE_PATH on persistent storage.

The bot creates public community/voice channels and colored roles. Separate private IMVU room logs, owner controls, and VCOIN order tickets deny access to @everyone. No Administrator permission or privileged gateway intents are requested. Customer email addresses are excluded from tickets.

Use /rooms, /control room, /say room message, /status and /help. Room logs have reply/control buttons. Controls cover quiet mode, language, welcomes/default welcome, music search/selection, skip and stop.

Website billing remains authoritative. The owner must verify receipt in IMVU Wallet and submit a transaction reference. Stale payment reports and already-confirmed orders cannot extend access twice. Website history, access and receipt-email processing use the existing confirmation transaction.

Queues and reply routes survive restarts, with five-second outage checkpoints. Discord rate-limit headers are honored; relayed mentions/link previews are disabled; concurrent interaction handling is bounded. Setup reuses existing categories, channels, roles and ticket mappings. Telegram state is retained for rollback.

