import asyncio
import json
import time
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from bot import Bot, Radio, API
from permissions import OWNER_ID


class CommandRestoreTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        permissions=Mock()
        permissions.admins=set()
        permissions.blocked=set()
        permissions.room_admins=set()
        permissions.names={}
        permissions.allowed.return_value=True
        permissions.authorization_error.return_value=None
        permissions.manage.return_value='Owner: @Ramzy | Admins: @Friend'
        manager=SimpleNamespace(room_languages={'1-1':'ar-eg'},telegram=Mock())
        bot=Bot(Radio(),room='1-1',permissions=permissions,room_manager=manager)
        bot.connected=True
        bot.chat_ready=True
        bot.user_id='9'
        bot.queue='room/test'
        bot.packet=AsyncMock(return_value=7)
        bot.speech.render=AsyncMock(side_effect=AssertionError('Commands must never translate'))
        return bot

    async def test_move_help_and_admins_send_exact_command_output(self):
        bot=self.make_bot()
        bot.movement.move_near=AsyncMock(return_value='Moved near @Friend.')
        with patch('bot.asyncio.sleep',new=AsyncMock()):
            for command in ['!move','!help','!admins']:
                bot.cooldowns.clear()
                worker=asyncio.create_task(bot.command_worker())
                try:
                    await bot.commands.put((OWNER_ID,command,True))
                    await asyncio.wait_for(bot.commands.join(),2)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker,return_exceptions=True)
        bot.speech.render.assert_not_awaited()
        bot.movement.move_near.assert_awaited_once_with(OWNER_ID)
        delivered=[call.args[2] for call in bot.room_manager.telegram.record.call_args_list]
        self.assertIn('Moved near @Friend.',delivered)
        self.assertIn('Owner: @Ramzy | Admins: @Friend',delivered)
        self.assertTrue(any('!play' in line and '!move' in line for line in delivered))

    async def test_subscription_ready_is_not_blocked_by_translation(self):
        bot=self.make_bot()
        bot.chat_ready=False
        bot.announce_appearance=AsyncMock()
        with patch('bot.asyncio.sleep',new=AsyncMock()):
            await bot.confirm_chat_ready()
        self.assertTrue(bot.chat_ready)
        bot.announce_appearance.assert_awaited_once()
        bot.speech.render.assert_not_awaited()
        self.assertTrue(bot.packet.called)

    async def test_only_chat_uses_language_renderer(self):
        bot=self.make_bot()
        bot.say=AsyncMock(return_value=True)
        bot.speech.render=AsyncMock(return_value='رد بالمصري')
        await bot.say_chat('answer')
        bot.speech.render.assert_awaited_once_with('answer')
        bot.say.assert_awaited_once_with('رد بالمصري')

    async def test_welcome_names_person_and_exact_room(self):
        bot=self.make_bot()
        bot.room_name='The Actual Room Name'
        bot.members={'8','9'}
        bot.pending_welcomes=OrderedDict({'8':time.monotonic()})
        bot.announce_seat=AsyncMock()
        bot.api=AsyncMock(return_value={'denormalized':{API+'/user/user-8':{'data':{'username':'Guest_Visitor'}}}})
        bot.say=AsyncMock(return_value=True)
        await bot.send_welcomes()
        welcome=bot.say.call_args.args[0]
        self.assertIn('@Guest_Visitor',welcome)
        self.assertIn('The Actual Room Name',welcome)
        self.assertIn('نورت',welcome)
        bot.speech.render.assert_not_awaited()

    async def test_ten_search_results_remain_visible_and_selectable(self):
        bot=self.make_bot()
        bot.members={OWNER_ID}
        choices=[{'title':str(i)+' Actual original title '+'x'*60,'url':'https://www.youtube.com/watch?v=AAAAAAAAAA'+str(i-1)} for i in range(1,11)]
        bot.radio.search=AsyncMock(return_value=choices)
        bot.say=AsyncMock(return_value=True)
        await bot.play_request(OWNER_ID,'search words')
        messages=[call.args[0] for call in bot.say.call_args_list]
        self.assertTrue(all(len(message)<=420 for message in messages))
        combined='\n'.join(messages)
        for number in range(1,11):
            self.assertIn(str(number)+'. '+str(number)+' Actual original title',combined)
        self.assertIn('1-10',combined)
        bot.queue_song=AsyncMock(return_value=True)
        await bot.pick_song(OWNER_ID,'10')
        bot.queue_song.assert_awaited_once_with(OWNER_ID,choices[9]['url'])

    async def test_search_requests_up_to_ten_results(self):
        data={'entries':[{'id':'AAAAAAAAAA'+str(i),'title':'Title '+str(i),'duration':120} for i in range(10)]}
        with patch('bot.capture',new=AsyncMock(return_value=json.dumps(data))) as capture:
            results=await Radio().search('cairokee tee tee')
        self.assertEqual(len(results),10)
        self.assertIn('ytsearch10:cairokee tee tee',capture.call_args.args)

    async def test_room_title_is_loaded_from_room_resource(self):
        bot=self.make_bot()
        bot.api=AsyncMock(return_value={'denormalized':{API+'/room/room-1-1':{'data':{'name':'The Real Room Name'}}}})
        await bot.load_room_name({})
        self.assertEqual(bot.room_name,'The Real Room Name')
        bot.api.assert_awaited_once_with('GET','/room/room-1-1')
