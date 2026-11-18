


## RamzyBots Discord community

Set DISCORD_BOT_TOKEN to an application bot token, DISCORD_GUILD_ID to the RamzyBots server ID, and CHAT_RELAY=discord to disable Telegram forwarding. Owner authorization defaults to the server owner; DISCORD_OWNER_ID can pin it explicitly. Keep DISCORD_STATE_PATH on persistent storage.

The bot creates public community/voice channels and colored roles. Separate private IMVU room logs, owner controls, and VCOIN order tickets deny access to @everyone. No Administrator permission is requested. Customer email addresses are excluded from tickets and trial notifications.

Use /rooms, /control room, /say room message, /status and /help. Room logs have reply/control buttons. Controls cover quiet mode, language, welcomes/default welcome, music search/selection, skip and stop.

Room channels use name-first labels, followed by the full IMVU room ID, and update when the room name becomes available. Public IMVU messages show the cached public display name and exact account username. Profile lookup failure retains the message with a numeric ID fallback.

Enable Message Content Intent in the Discord Developer Portal for direct typing. The gateway detects the granted intent and reconnects within about a minute. Only the pinned DISCORD_OWNER_ID (ramzydc) can send from a room channel to its mapped IMVU room. Copied usernames, administrator roles, bots, webhooks, other guilds and unrelated channels cannot authorize a send. Existing viewers retain view access but lose Send Messages permission. Buttons still work while Message Content Intent is disabled.

The private trial-requests channel receives website customer trial requests, without customer email addresses. Existing requests are picked up on first deployment; each room is notified once across restarts. The website's automatic trial activation remains unchanged.

Website billing remains authoritative. The owner must verify receipt in IMVU Wallet and submit a transaction reference. Stale payment reports and already-confirmed orders cannot extend access twice. Website history, access and receipt-email processing use the existing confirmation transaction.

Queues and reply routes survive restarts, with five-second outage checkpoints. Discord rate-limit headers are honored; relayed mentions/link previews are disabled; concurrent interaction handling is bounded. Setup reuses existing categories, channels, roles and ticket mappings. Telegram state is retained for rollback.

Private operations channels: room-alerts reports sustained disconnections, HTTP 403 access failures and recovery. Startup has a 60-second grace period and offline transitions require 30 seconds; repeated identical failures are suppressed across restarts. system-status maintains a message board with each room's name, ID, connection, occupants and song; updates edit existing messages every ~20 seconds. billing-reminders reports trials ending within 24 hours and trials expired within the last day. Reminders are durable, deduplicated by expiry, and cancelled if the access is renewed or paid before delivery.

VCOIN tickets provide Verify payment, Reject report, Request proof, Close/Reopen and Action history. Reject/proof require an owner reason and use the website's existing return-report action, without granting access or confirming payment. The reason is visible to the customer on the website. A new payment report invalidates old buttons and review notes. Closing preserves the website order and history, blocks confirmation until reopened, and does not delete a channel. Review actions remain owner-only.

In IMVU rooms, !boot ExactUsername lets a present, verified local bot admin, global Mod, native room moderator or room owner remove an ordinary participant. Account usernames are resolved exactly; decorative nicknames are not matched. The bot must itself hold native IMVU room-owner/moderator permission. The bot owner, bot account, native room owner/moderators, global Mods and bot admins in any configured room are protected. Moderator lists are read before and after lookup; failed or incomplete role checks prevent a boot. Removal uses IMVU's normal boot announcements followed by DELETE of that room's participant record. Successful boots are audited. No live participant is removed during automated tests.

