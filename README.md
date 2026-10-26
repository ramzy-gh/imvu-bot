# RAMSEI room companion — experimental Bothost build

Configured account: **Ramsei**. Room: **330286535-177**.
Bot owner: **Ramzy**, numeric IMVU ID **214900673**.

**This is a prototype, not a live-verified IMVU bot.** Local tests verify parsing,
games and protective checks. IMVU login, 2FA, room subscription, visible avatar
presence, real YouTube extraction and shared room audio still need a live test.
Do not interpret a successful Docker deployment as proof that those work.

This implementation connects directly to IMVU's web API and IMQ transport;
it does not need an IMVU.js service token or subscription. The transport is
unofficial and can change. Nothing modifies the Classic client. The bot runs
on the server; people can use Classic to chat in the room.

## Personality and features

- Egyptian Arabic welcomes, with visitor names and emojis.
- Mention **Ramsei**, **رامسي**, **رامزي**, or **رامزى** for a reply.
- Real AI chat when `GEMINI_API_KEY` is configured; clearly limited canned replies otherwise.
- Only addressed messages and up to three recent exchanges with that participant
  are sent to Google when AI is enabled. Ordinary room chat is not sent.
- Riddles, trivia, number guessing, jokes, light challenges, playful fortunes.
- Games and scores are held in memory and reset on restart.
- Arabic and English music search; first YouTube search match, queue, MP3 radio.
- Cooldowns, queue limits, bounded listener buffers, bounded AI history and
  a default cap of 60 AI calls per hour. That cap is not a billing guarantee.
- No automatic ambient chatter: it responds to mentions, commands and game answers.

Example messages:

> نورت الروم يا كريم! 😎💛 أنا رامسي البوت، للمزيكا !play واسم الأغنية، وللعب !game 🎲

> رامسي عامل إيه يا معلم؟

> !play عمرو دياب تملي معاك

> !شغل محمد حماقي

## Deploy on Bothost

1. Extract the ZIP. Upload the contents of this folder as a Bothost project
   (the Dockerfile must be at the archive root).
2. Enable **Использовать собственный Dockerfile**. The entrypoint is already
   specified in the Dockerfile: `python bot.py`.
3. Enable **Использовать домен** and use the automatically assigned HTTPS domain.
4. Set `PORT=3000`. Bothost routes the domain to port **3000**. Leave it on
   3000 after enabling the domain; do not use the older port 8000.
5. Set the variables below directly in Bothost. Never upload a filled `.env` file.
6. Choose **Telegram** as the platform category. Bothost support confirmed that
   any category works with a custom Dockerfile. For the required Bot Token field,
   use the non-secret placeholder **imvu-custom**. Our code does not use it.
   Use the persistent `/app/data` folder provided by this Bothost deployment.
   Keep permission/login files under `/app/data/ramsei`, not `/tmp`. The startup
   entrypoint fixes ownership of that private folder after the host mounts it,
   then runs the bot without root privileges. On hosts without persistent storage,
   configure an external PostgreSQL `DATABASE_URL` instead.
   The entrypoint redirects the known legacy `/tmp/ramsei` and `/app/data/*.json`
   session/permission paths to `/app/data/ramsei`. Update the host variables too
   when possible. This does not recover files already erased by earlier rebuilds.
7. Deploy. Check `/health` on the assigned domain. It must say `connected` before
   trying room commands. This means transport authentication succeeded; still
   verify actual messages in IMVU.

| Variable | Value |
| --- | --- |
| `IMVU_USERNAME` | `Ramsei` |
| `IMVU_PASSWORD` | Enter the bot password privately |
| `IMVU_ROOM_ID` | `330286535-177` |
| `PORT` | `3000` |
| `DATABASE_URL` | Optional private external PostgreSQL connection string |
| `STATE_NAMESPACE` | `ramsei-330286535-177` (keep unchanged across deployments) |
| `SESSION_PATH` | `/app/data/ramsei/imvu-session.json` |
| `PERMISSIONS_PATH` | `/app/data/ramsei/permissions.json` |
| `ADMIN_IDS` | Leave empty; optional first-start admin seed only |
| `GEMINI_API_KEY` | Optional: your private Google AI Studio API key |
| `GEMINI_MODEL` | `gemini-flash-latest`, or an available text model for your key |
| `AI_MAX_CALLS_PER_HOUR` | `60` |

Create an optional AI key at https://aistudio.google.com/apikey . Account eligibility,
model availability, quotas and any charges are controlled by Google. The bot
does not create a key or enable billing for you.

## RAMSEI has 2FA — keep it enabled

1. First deploy with username/password and **without** `IMVU_2FA_CODE`.
2. If IMVU asks for verification, the process stops with a short verification
   message. Obtain the code through IMVU's normal verification channel.
3. Add the current code as the private Bothost variable `IMVU_2FA_CODE` and restart
   promptly. Do not paste the code into ChatGPT or into a shared repository.
4. On successful login, the bot saves the session to the configured database,
   or to `SESSION_PATH` if no database is configured. Remove `IMVU_2FA_CODE` once
   connected. Reuse is possible only while that saved session remains available
   and valid. Keep the file on persistent storage to reuse it across rebuilds.
5. If IMVU expires/revokes the session or requests another security challenge,
   repeat the normal verification flow. There is no 2FA bypass.

The local session file is a login secret stored as private JSON with mode `0600`
where the OS supports it; it is **not encrypted by this app**. Keep its volume
private and exclude it from archives/repositories. Losing the volume means
another normal login, potentially another 2FA check. The password is not written
to the session record. Never commit sessions or passwords to the repository.
IMVU can still reject this unofficial login method; then
the connector needs adjustment rather than disabling account protections.

## Optional durable state with external PostgreSQL

Enter a PostgreSQL connection URL from a database you control as the private
Bothost variable `DATABASE_URL`. This project does not create or pay for a database.
Leave it unset when using a verified persistent `/app/data/ramsei` volume.
Without either persistent files or a database, admins, blocks and saved login
will be lost on container replacement.

When configured, the app creates `ramsei_state` and stores the login session and
permission lists there. The database user needs permission to create that table
and read/write its rows. Connections require TLS. Keep the URL private, keep
`STATE_NAMESPACE` unchanged, and run one active bot per namespace. A distinct bot
should use a distinct namespace. Use an external database with durable storage,
not one running inside this same ephemeral Bothost container.

The app does not encrypt session payloads at rest; restrict database access.
It never logs connection strings or raw DB errors. If a configured database is
unavailable, it stops/denies state changes rather than silently losing blocks.
Existing local files are not automatically imported into a new database.
Configure the database before your first real admin/block setup where possible.
Games, points, music queues and AI memory remain temporary.
The database path has simulated local tests; a real database connection remains
a deployment check. No real database has been connected or provisioned.

## Connect the room radio

Set `PUBLIC_BASE_URL=https://YOUR-BOTHOST-DOMAIN` in the host variables. Type
`!radio` in the room for that room's stream URL and listener count. A queued song
does not automatically enable IMVU Radio. A room host/moderator must load the
stream and press Play; check the radio volume on each client. Classic requires MP3.
The bot keeps one MP3 encoder alive across songs, skips and idle silence. Individual
songs decode to PCM and feed that encoder; song changes do not reset the MP3
stream. `/health` reports `encoder_starts`, `track_starts` and encoder errors.
Listeners receive complete MP3 frames and a short startup buffer. If a listener
falls behind, the server skips stale frames while keeping its connection open;
`listener_resyncs` in `/health` counts these recoveries. HEAD probes return
immediately without creating a listener. Responses disable caching and byte ranges.
The bot cannot press Pause/Play inside other people's IMVU clients. Keeping the
stream continuous and preserving slow connections avoids server-induced resets;
live Classic playback still needs verification after each compatibility change.
`audio_bytes` increasing confirms encoder output, not audible client playback.
`listeners=0` means no client is connected to this server's stream.
These diagnostics do not prove that Classic's audio decoder is playing.

Each room has its own queue and stream:
`https://YOUR-BOTHOST-DOMAIN/rooms/330286535-177/stream.mp3`.
`/stream.mp3` remains an alias for the `IMVU_ROOM_ID` room.

After the server works, the room owner/moderator opens **Radio** in Classic,
loads `https://YOUR-BOTHOST-DOMAIN/stream.mp3`, then clicks Play.
Use the real domain from your Bothost panel; the example is a placeholder.

The stream emits silence when no track is queued so listeners can remain connected.
Request a song in room chat, e.g. `!play عمرو دياب`. If a test song fails,
check YouTube availability from the server and the current yt-dlp release.
No CAPTCHA bypass, proxy rotation or browser-cookie import is included.
Only play audio you are allowed to share.

Song/artist searches such as `!play billie eilish` show up to five numbered
YouTube results. The requester replies with a plain number (`1`–`5`) in the same
room within two minutes to queue that result. Each person and room has separate
choices; a new search replaces that person's previous list. Selection works
immediately without the usual command cooldown. If only one result is found,
it queues directly. An HTTPS YouTube link also queues directly. Long/live results
are filtered when search metadata is available; the selected video is checked
again before queuing. The bot does not interrupt a song already playing.

## Commands

`!move` asks RAMSEI to move its own avatar near the caller. It reads actual seat
coordinates from the room/furniture skeleton and chooses the closest unoccupied
seat on the caller's current room shell or furniture item. It never moves another
user or guesses seat numbers. If that item has no free seat, it reports that.
Animated furniture may differ from its base skeleton positions. Requests have a
10-second cooldown per room. IMVU must confirm the new seat before success is reported.

Welcomes are checked every five seconds and tracked separately in each room. New arrivals receive an English
welcome using their account username. Membership refreshes triggered by chat also
retain pending welcomes. Temporary lookup/send failures retry for up to two minutes
without disconnecting the room; rapid rejoins have a five-minute greeting cooldown.
People already present when the bot first joins are not greeted as new arrivals.

| Command | Arabic alias | What it does |
| --- | --- | --- |
| `!play <song / YouTube link>` | `!شغل` | First matching result, max 15 minutes |
| `!queue` | `!طابور` | Next five songs |
| `!np` | `!الاغنية` | Current song |
| `!skip` | `!عدي` | Everyone: skip |
| `!stop` | `!وقف` | Everyone: clear queue and stop |
| `!game` | `!لعبة` | Trivia/riddle, answer in normal chat |
| `!guess` | `!تخمين` | Guess a number from 1–30; Arabic digits work |
| `!answer <answer>` | `!جاوب` | Explicit game answer |
| `!joke` | `!نكتة` | Egyptian Arabic joke |
| `!dare` | `!تحدي` | Friendly room prompt |
| `!fortune` | `!حظ` | Clearly playful fortune |
| `!scores` | `!نتيجة` | Your points this session |
| `!ask <message>` | `!اسال` | Address the AI directly |
| `!help` | `!مساعدة` | Quick command list |
| `!radio` | — | Stream setup URL and connected listener count |

### Owner and admins

Everyone can use music controls (including skip/stop), games and name-triggered
chat unless an admin has blocked their account from the bot.

| Command | Who can use it | Effect |
| --- | --- | --- |
| `!addadmin Username` | Ramzy only | Promote that account to bot admin |
| `!removeadmin Username` | Owner only | Remove bot admin role |
| `!admins` | Owner/admins | Show owner and admin usernames |
| `!block Username` | Owner/admins | Block a regular account from bot commands, games and AI replies |
| `!unblock Username` | Owner/admins | Restore access to the bot |
| `!blocked` | Owner/admins | Show blocked usernames |

These blocks apply only to this bot; they do not kick someone from the room or
prevent normal chat. Welcomes still work. Admins cannot block the owner or other
admins. The owner must remove an admin role before that account can be blocked.
Promoting a blocked regular account also unblocks it. Account usernames/display
names are never used to grant privileges directly: the owner's fixed numeric ID is used.

Type the actual IMVU **account username**; letter case does not matter, and a
leading `@` is optional. Decorative display names are not accepted because they
can be duplicated. The bot resolves the exact username through IMVU, then stores
the stable account ID internally. You do not need to find or enter that ID.
Classic may hide the `Guest_` prefix: `!addadmin C4MU` and
`!addadmin Guest_C4MU` can both resolve the account `Guest_C4MU`.
Replies/lists show `@Guest_C4MU`, never that account's display name such as `Night`.
An exact account-name match takes priority over a hidden-Guest alias; ambiguous
results are rejected. The same rules apply to removeadmin, block and unblock.
Use `!addadmin C4MU`, `!removeadmin C4MU`, `!block C4MU` or `!unblock C4MU`.
Slash forms such as `/addadmin C4MU` also work if IMVU delivers them as room chat;
prefer `!` because clients can consume slash commands before the bot receives them.
Replies and lists use usernames. If a username is not found or the lookup is
ambiguous/unavailable, no permission change is made. If someone changes their
username, their existing admin/block status stays attached to the same account;
use their current username in future commands. The lookup route was verified
against the public Ramzy account, returning ID 214900673; live authenticated
room commands still need testing.

Management commands require a server-supplied sender identity matching the message
author. If IMQ does not supply the expected identity field, management stays locked
pending live diagnosis. Normal commands remain available.

Admin and block changes go to PostgreSQL when `DATABASE_URL` is configured;
otherwise they are saved atomically to `PERMISSIONS_PATH`. Use
`/app/data/ramsei/permissions.json` on this host's persistent data volume.
The bot flushes saved state to disk before replacing the file. Failed saves leave
permissions unchanged. Accounts lost during earlier `/tmp` deployments cannot be
recovered unless a backup exists; add those admins/blocks again once.
Do not delete the permissions file to update code. Remove the previous
`ADMIN_IDS=330286535` variable or set it empty if you do not want that account to
be an initial admin. Once a permissions file exists, it is authoritative; use
the owner's chat commands to change admins.

## Multiple rooms and host terminal controls

### Owner controls from room chat

Only the verified owner account **Ramzy (214900673)** can use these commands.
Admins cannot use them. Send them from any room the bot has already joined:

```text
!join 47377071-5395
!join https://www.imvu.com/next/chat/room-47377071-5395/
!rooms
!mainroom
!leave 47377071-5395
!leave here
```

`!join` adds another room while keeping existing rooms connected. `!rooms` shows
connection status, and marks the main room. `!leave` without an ID means the
current room. The configured `IMVU_ROOM_ID` (330286535-177) is the protected main
room: chat and terminal commands cannot make the bot leave it. It reconnects
normally if IMVU drops the connection. Server shutdown still closes all rooms.

Additional rooms are saved next to the permissions file in `rooms.json` (or in
PostgreSQL when configured) and restored after restart. A leave request removes
that room from the saved list. Failed saves do not apply changes. `IMVU_ROOM_IDS`
seeds the initial list only when no saved room list exists. Use commands for later
changes. A join request is not a guarantee of access: IMVU must admit the bot.
Slash versions work only if IMVU passes them to room chat; prefer `!`.

The bot refreshes its own account presence at room connection and about every
45 seconds, separately from chat membership keepalives. `/health` includes
`account_presence`. This keeps the online indicator updated while the bot can
reach IMVU; outages, shutdowns and IMVU account restrictions can still make it offline.

Set `IMVU_ROOM_IDS=330286535-177,OTHEROWNER-OTHERROOM` using real numeric room IDs
to request multiple rooms at startup. Omit it to use only `IMVU_ROOM_ID`.
There is a local limit of 10 rooms. Authentication is reused; each room gets
independent chat, games, music and AI history. Admins and blocks are shared across
all rooms. AI call limits currently apply per room, so consider the total usage.
Keep `STATE_NAMESPACE` unchanged. Chat and terminal room changes are saved to
persistent room storage and restored after restart, including the main room.

For control from a separate shell, set `TERMINAL_CONTROL=1`, restart the bot,
and run these inside the same bot container (normally `/app`):

```text
python botctl.py rooms
python botctl.py join 47377071-5395
python botctl.py leave 47377071-5395
```

This works without stdin attached to the bot process. The running bot listens
only on loopback with a random authentication token in a private temporary file;
no public HTTP control endpoint or additional exposed Docker port is added.
The CLI must run in the same container/network namespace as the bot. If the host's
terminal is a shell on the Docker node, an operator must use `docker exec` into
the RAMSEI container before running the CLI. Do not start a second `bot.py` process.

For direct interactive control, set `TERMINAL_CONTROL=1` and run `python bot.py` in a
foreground terminal with stdin attached. Enter:

```text
rooms
join 330286535-177
leave 330286535-177
help
```

`join` requests connection asynchronously; `rooms` and `/health` report readiness.
`leave` stops that room's tasks/audio and attempts to delete its IMVU presence.
If IMVU rejects the removal, presence may remain until IMVU expires it.
Leave then join again to retry a failed room task.

A hosting log viewer or a separate shell does not feed stdin to the running bot.
Use `python botctl.py ...` from a shell in the bot container instead.
Bothost must offer an interactive console attached to that process for these
commands to work there. With Docker, start interactively using `docker run -it`
plus your normal environment and port options. Closed stdin disables terminal
input without stopping the bot; the local CLI remains available. No public remote-control endpoint is added.

Simultaneous presence with one account uses the existing unofficial connector;
it requires a live IMVU test. IMVU may impose account/room/session limits, and
the bot cannot override them. Check that it remains visible and responsive in
two rooms, and that leaving one does not disconnect the other.

### Live checks

- Login succeeds while 2FA remains enabled; no secrets appear in logs.
- `/health` shows connected and Ramsei visibly appears in the target room.
- A second account joins and receives one welcome, within roughly 15 seconds.
- `!play` searches by Arabic/English name and produces audible music on two clients.
- Queue continues to the next song; a new listener can hear the current song.
- `رامسي عامل إيه؟` replies in Egyptian Arabic; `!game` and a correct answer score.
- With PostgreSQL configured, a rebuild reuses the saved session unless IMVU expired it.
- Regular accounts can use skip/stop, games and AI unless blocked.
- Only verified owner ID 214900673 can add/remove admins.
- Verified admins can block/unblock regulars; blocks persist through rebuilds with PostgreSQL.

If the avatar does not appear or chat does not arrive, that is an unresolved
connector issue. This build deliberately does not send fabricated identity or
avatar-spawn packets as a workaround.

## Local checks

`python -m unittest discover -s tests -v`

Requires the Python dependencies from `requirements.txt`. Python 3.11+,
Node 22 and FFmpeg are installed by the Dockerfile. The latest yt-dlp is resolved
at build time; rebuild to obtain extractor updates. Pin it to a known working
version after the live test if reproducible builds are needed.

## Sources used to establish the prototype

- https://support.imvu.com/support/solutions/articles/154000216641-radio-streaming
- https://bothost.ru/docs/custom-dockerfile
- https://bothost.ru/docs/web-apps-domains
- https://github.com/l33terallyno1/IMVU-Bot — protocol observations only; no source copied
- https://github.com/imckvu/tool — IMQ transport reference
- https://ai.google.dev/api/generate-content
- https://github.com/yt-dlp/yt-dlp

No external code is downloaded or executed on your behalf by this ZIP until you
deploy it. Dependencies are installed from their normal repositories during the build.
