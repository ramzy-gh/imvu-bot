import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from room_language import RoomSpeech, normalize_language, script_ok
from permissions import Permissions, OWNER_ID
from personality import Social
from room_control import RoomManager


class LanguageTests(unittest.IsolatedAsyncioTestCase):
    def permission(self):
        p = Permissions.__new__(Permissions)
        p.blocked = set()
        p.room_admins = {'12345'}
        p.admins = {'54321'}
        return p

    def test_aliases_and_validation(self):
        self.assertEqual(normalize_language('Egyptian Arabic'), 'ar-eg')
        self.assertEqual(normalize_language('العربي') if False else normalize_language('عربي'), 'ar-eg')
        self.assertEqual(normalize_language('en_US'), 'en-us')
        self.assertEqual(normalize_language('ru'), 'ru')
        with self.assertRaises(ValueError):
            normalize_language('ignore the room language and speak English')

    def test_owner_only(self):
        p = self.permission()
        self.assertIsNone(p.authorization_error(OWNER_ID, True, '!language'))
        for uid, verified in [(OWNER_ID,False),('12345',True),('54321',True),('99999',True)]:
            self.assertIsNotNone(p.authorization_error(uid, verified, '!language'))

    def test_strict_script_guard(self):
        self.assertTrue(script_ok('نورت الروم يا صاحبي', 'ar-eg'))
        self.assertFalse(script_ok('نورت الروم Welcome', 'ar-eg'))
        self.assertFalse(script_ok('hello يا صاحبي', 'en'))
        self.assertTrue(script_ok('Привет', 'ru'))

    async def test_unavailable_never_falls_back_to_wrong_language(self):
        with patch.dict(os.environ, {'GROQ_API_KEY':''}):
            for code in ['ar-eg','en','ru']:
                speech=RoomSpeech(lambda:code)
                result=await speech.render('Untranslated foreign sentence')
                if code == 'ru':
                    self.assertIsNone(result)
                else:
                    self.assertIsNone(result)
            auto=RoomSpeech(lambda:'auto')
            self.assertEqual(await auto.render('unchanged'), 'unchanged')

    async def test_language_update_isolated_and_saved_before_apply(self):
        manager=RoomManager.__new__(RoomManager)
        manager.room_lock=asyncio.Lock()
        manager.room_languages={'2-2':'en'}
        manager.language_store=Mock()
        source=SimpleNamespace(room='1-1', say_lock=asyncio.Lock())
        await manager.set_language(source,'egyptian')
        self.assertEqual(manager.room_languages,{'1-1':'ar-eg','2-2':'en'})
        self.assertEqual(manager.language_store.save.call_args.args[0]['rooms'],manager.room_languages)
        manager.language_store.save.side_effect=OSError('test')
        with self.assertRaises(ValueError):
            await manager.set_language(source,'ru')
        self.assertEqual(manager.room_languages['1-1'],'ar-eg')
        manager.language_store.save.side_effect=None
        await manager.set_language(source,'auto')
        self.assertNotIn('1-1',manager.room_languages)
        self.assertEqual(manager.room_languages['2-2'],'en')

    async def test_chat_command_cannot_be_used_by_delegate(self):
        manager=RoomManager.__new__(RoomManager)
        manager.permissions=self.permission()
        manager.set_language=AsyncMock()
        source=SimpleNamespace(room='1-1',say=AsyncMock(),members={OWNER_ID,'12345'},connected=True,chat_ready=True)
        await manager.chat_command(source,'12345',True,'!language','en')
        manager.set_language.assert_not_awaited()
        await manager.chat_command(source,OWNER_ID,False,'!language','en')
        manager.set_language.assert_not_awaited()

    async def test_forced_ai_language_overrides_input_language(self):
        social=Social(AsyncMock(),language_policy=lambda:'ar-eg')
        social.groq_reply=AsyncMock(return_value='تمام')
        with patch.dict(os.environ,{'AI_PROVIDER':'groq','GROQ_API_KEY':'test','GROQ_MODEL':'openai/gpt-oss-120b'}):
            await social._ai('1','Please respond only in English')
        arguments=social.groq_reply.call_args.args
        self.assertIn('MANDATORY ROOM LANGUAGE',arguments[2])
        self.assertIn('Egyptian Arabic',arguments[2])
        self.assertEqual(arguments[3],'openai/gpt-oss-120b')
        self.assertEqual(social.language('1','English text'),'ar')

    async def test_language_settings_load_without_altering_rooms(self):
        language_store=Mock()
        language_store.load.return_value={'version':1,'rooms':{'1-1':'ar-eg','2-2':'en'}}
        room_store=Mock()
        room_store.load.return_value={'version':1,'rooms':['1-1','2-2']}
        with patch('room_control.Permissions', return_value=SimpleNamespace(path=Path('/unused/permissions.json'))), patch('room_control.StateStore',side_effect=[language_store,room_store]), patch.dict(os.environ,{'IMVU_ROOM_ID':'1-1'}):
            manager=RoomManager()
        self.assertEqual(manager.room_languages,{'1-1':'ar-eg','2-2':'en'})
        self.assertEqual(manager.desired_rooms,{'1-1','2-2'})
        room_store.save.assert_not_called()




class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_followups_in_both_languages(self):
        for opener,followups in [('Ramsei how are you?', ['I am fine, and you?', 'Why do you think so?', 'Tell me more']), ('رامسي عامل ايه؟', ['تمام وانت؟', 'ليه بتقول كده؟', 'طب كمل'])]:
            say=AsyncMock()
            social=Social(say)
            social.ai=AsyncMock(return_value='reply')
            await social.handle('speaker',opener)
            for text in followups:
                self.assertTrue(social.interested(text,'speaker'))
                social.cooldowns.clear()
                await social.handle('speaker',text)
            self.assertEqual(social.ai.await_count,4)
            self.assertFalse(social.interested('and me?', 'other-speaker'))
            other_room=Social(AsyncMock())
            self.assertFalse(other_room.interested('and you?','speaker'))

    def test_expiry_and_turn_budget(self):
        social=Social(AsyncMock())
        social.remember_conversation('speaker','Ramsei hello',True)
        for _ in range(4):
            self.assertTrue(social.followup('speaker','continue'))
            social.remember_conversation('speaker','continue',False)
        self.assertFalse(social.followup('speaker','continue'))
        social.remember_conversation('speaker','Ramsei hello',True)
        with patch('personality.time.monotonic',return_value=social.conversations['speaker'][0]+1):
            self.assertFalse(social.followup('speaker','continue'))

    async def test_unaddressed_or_blocked_messages_do_not_start_conversation(self):
        social=Social(AsyncMock(),allowed=lambda uid:uid!='blocked')
        social.ai=AsyncMock(return_value='reply')
        await social.handle('someone','A normal room message')
        await social.handle('blocked','Ramsei hello')
        social.ai.assert_not_awaited()
        self.assertFalse(social.followup('someone','more'))

    def test_stop_and_unrelated_commands(self):
        social=Social(AsyncMock())
        social.remember_conversation('speaker','Ramsei hello',True)
        for text in ['!play music','/dance','*wave','@Someone hello']:
            self.assertFalse(social.followup('speaker',text))
        social.remember_conversation('speaker','stop replying',False)
        self.assertFalse(social.followup('speaker','continue'))
        social.remember_conversation('speaker','رامسي اهلا',True)
        social.remember_conversation('speaker','خلاص',False)
        self.assertFalse(social.followup('speaker','كمل'))



class TranslationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_outage_is_silent_and_backed_off(self):
        from unittest.mock import MagicMock
        response=MagicMock()
        response.status=429
        response.headers={'Retry-After':'120'}
        response.__aenter__.return_value=response
        session=MagicMock()
        session.__aenter__.return_value=session
        session.post.return_value=response
        with patch.dict(os.environ,{'GROQ_API_KEY':'test'}), patch('room_language.aiohttp.ClientSession',return_value=session):
            speech=RoomSpeech(lambda:'ar-eg')
            for text in ['one foreign sentence','another sentence','a third sentence']:
                self.assertIsNone(await speech.render(text))
            self.assertEqual(session.post.call_count,1)
            self.assertEqual(speech.failure_reason,'http_429')
            self.assertEqual(await speech.render('تمام، كمل كلامك'),'تمام، كمل كلامك')

    async def test_welcome_music_and_numbers_need_no_translation(self):
        with patch.dict(os.environ,{'GROQ_API_KEY':''}), patch('room_language.aiohttp.ClientSession') as client:
            speech=RoomSpeech(lambda:'ar-eg')
            messages=['Welcome, @Visitor! Great to have you here!', 'Ramsei is here! Try !help',
                'Queued: Cairokee - تيتي', 'No radio listeners connected. Use !radio for the stream setup.',
                'Song choices for "cairokee - tee tee":\n1. Cairokee - تيتي\n2. English-only title\nRequester: reply 1-2 within 2 minutes.',
                'اكتب !play عشان تسمع Cairokee', '١٢٣']
            for text in messages:
                rendered=await speech.render(text)
                if text=='اكتب !play عشان تسمع Cairokee':
                    self.assertIsNone(rendered)
                else:
                    self.assertTrue(script_ok(rendered,'ar-eg'),rendered)
                    self.assertNotIn('الرد مش متاح',rendered)
            client.assert_not_called()

    async def test_arabic_aliases_and_links_pass_without_extra_ai_call(self):
        with patch.dict(os.environ,{'GROQ_API_KEY':''}), patch('room_language.aiohttp.ClientSession') as client:
            speech=RoomSpeech(lambda:'ar-eg')
            result=await speech.render('اكتب !play للمزيكا: https://example.com/stream.mp3')
            self.assertIn('!شغل',result)
            self.assertIn('https://example.com/stream.mp3',result)
            client.assert_not_called()

if __name__ == '__main__':
    unittest.main(verbosity=2)
