"""Music regressions using only stdlib, without live IMVU credentials."""
import ast
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import unittest
from collections import OrderedDict, deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

source = Path(__file__).resolve().parents[1] / 'music_bot_candidate.py'
if not source.exists():
    source = source.with_name('bot.py')
tree = ast.parse(source.read_text(encoding='utf-8'))
tree.body = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef))
             and n.name in {'MusicLookupError', 'capture', 'Radio', 'Bot'}]
ns = dict(globals(), LOG=logging.getLogger('music-test'))
exec(compile(tree, str(source), 'exec'), ns)
Radio, Bot, MusicLookupError = (ns[n] for n in ('Radio', 'Bot', 'MusicLookupError'))


class MusicFallback(unittest.IsolatedAsyncioTestCase):
    async def test_hls_seeking_decodes_before_discarding_samples(self):
        args = Radio().decoder_args({'url': 'https://source.invalid/audio', 'offset': 0.35})
        self.assertLess(args.index('-i'), args.index('-ss'))
        self.assertEqual(args[args.index('-ss') + 1], '0.35')

    async def test_extractor_error_is_classified_without_exposing_stderr(self):
        proc = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b'',
            b'Sign in to confirm you are not a bot https://secret.invalid/token')))
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
            with self.assertLogs('music-test', level='WARNING') as logs:
                with self.assertRaises(MusicLookupError) as error:
                    await ns['capture']('extractor')
        self.assertEqual(error.exception.reason, 'youtube_blocked')
        self.assertNotIn('secret.invalid', str(error.exception) + str(logs.output))

    async def test_block_uses_relay_and_keeps_youtube_search_results(self):
        radio = Radio()
        track = {'title': 'Original', 'source': 'https://www.youtube.com/watch?v=abcdefghijk'}
        radio.lookup_relay = AsyncMock(return_value=track)
        with patch.dict(ns, capture=AsyncMock(side_effect=MusicLookupError('youtube_blocked'))):
            self.assertEqual(await radio.lookup(track['source']), track)
        self.assertGreater(radio.youtube_blocked_until, time.monotonic())
        with patch.dict(ns, capture=AsyncMock()) as _:
            self.assertEqual(await radio.lookup(track['source']), track)
            ns['capture'].assert_not_awaited()
        with patch.dict(ns, capture=AsyncMock(return_value=json.dumps({'entries':[
                {'id':'abcdefghijk','title':'YouTube original','duration':123}]}))):
            self.assertEqual((await radio.search('Song'))[0]['title'], 'YouTube original')
        radio.lookup_relay.assert_awaited_with('abcdefghijk')

    async def test_relay_failure_preserves_soundcloud_fallback_trigger(self):
        radio = Radio()
        radio.youtube_blocked_until = time.monotonic() + 60
        radio.lookup_relay = AsyncMock(side_effect=MusicLookupError('source_unavailable'))
        with self.assertRaises(MusicLookupError) as error:
            await radio.lookup('https://youtu.be/abcdefghijk')
        self.assertEqual(error.exception.reason, 'youtube_blocked')

    async def test_relay_requires_matching_video_and_safe_relay_audio(self):
        video_id = 'abcdefghijk'
        data = {'videoId':video_id,'lengthSeconds':123,'title':'Original',
                'adaptiveFormats':[{'itag':140,'type':'audio/mp4',
                                    'url':'https://yt.chocolatemoo53.com/videoplayback?token=private'}]}
        track = Radio.relay_track(data, video_id)
        self.assertEqual(track['source'], 'https://www.youtube.com/watch?v=' + video_id)
        self.assertEqual(track['provider'], 'youtube_relay')
        for changes in ({'videoId':'wrong'}, {'lengthSeconds':901}, {'liveNow':True},
                        {'adaptiveFormats':[{'type':'audio/mp4','url':'https://127.0.0.1/private'}]},
                        {'adaptiveFormats':[{'type':'audio/mp4','url':'https://evil.invalid/audio'}]}):
            with self.assertRaises(MusicLookupError):
                Radio.relay_track(dict(data, **changes), video_id)

    async def test_supported_youtube_links_resolve_same_video(self):
        for link in ['https://youtu.be/abcdefghijk',
                     'https://www.youtube.com/watch?v=abcdefghijk',
                     'https://music.youtube.com/watch?v=abcdefghijk',
                     'https://www.youtube.com/shorts/abcdefghijk']:
            self.assertEqual(Radio.youtube_video_id(link), 'abcdefghijk')
        self.assertIsNone(Radio.youtube_video_id('https://evil.invalid/watch?v=abcdefghijk'))

    async def test_fallback_offers_choices_without_queuing_unselected_audio(self):
        bot = Bot.__new__(Bot)
        bot.members = {'1'}
        bot.permissions = SimpleNamespace(allowed=lambda uid: True)
        bot.song_choices = OrderedDict({'1': (time.monotonic() + 120,
            [{'title': 'Original song (Official video)', 'search_query': 'artist song', 'url': 'https://www.youtube.com/watch?v=abcdefghijk'}])})
        bot.say = AsyncMock()
        bot.queue_song = AsyncMock(side_effect=MusicLookupError('youtube_blocked'))
        alternatives = [{'title': '[SC] Alternative', 'url': 'https://soundcloud.com/artist/song'}]
        bot.radio = SimpleNamespace(search_soundcloud=AsyncMock(return_value=alternatives))
        await bot.pick_song('1', '1')
        self.assertEqual(bot.song_choices['1'][1], alternatives)
        bot.radio.search_soundcloud.assert_awaited_once_with('artist song')
        bot.queue_song.assert_awaited_once()
        self.assertTrue(any('SoundCloud' in call.args[0] for call in bot.say.await_args_list))

    async def test_soundcloud_results_reject_nonpublic_source_urls(self):
        entries = [{'title': 'Good', 'id': '123', 'webpage_url': 'https://soundcloud.com/artist/song', 'duration': 123},
                   {'url': 'https://127.0.0.1/private'}, {'url': 'http://soundcloud.com/artist/song'},
                   {'url': 'https://soundcloud.com/artist/long', 'duration': 1000}]
        with patch.dict(ns, capture=AsyncMock(return_value=json.dumps({'entries': entries}))):
            result = await Radio().search_soundcloud('test')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], '[SC] Good')
        self.assertEqual(result[0]['url'], 'soundcloud-track:123')
        self.assertEqual(Radio.lookup_target(result[0]['url']), 'https://api.soundcloud.com/tracks/123')
        for query in ['https://evil.invalid/song', 'https://soundcloud.com.evil.invalid/song']:
            with self.assertRaises(ValueError):
                Radio.lookup_target(query)


if __name__ == '__main__':
    unittest.main()
