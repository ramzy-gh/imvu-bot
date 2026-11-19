import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from personality import Social, normalize_answer
from trivia import QUESTIONS, CATEGORIES, TriviaDeck


class TriviaTests(unittest.IsolatedAsyncioTestCase):
    def social(self):
        return Social(AsyncMock(), language_policy=lambda: 'ar-eg')

    async def test_full_bank_unique_bilingual_and_no_repeats_until_exhausted(self):
        self.assertEqual(len(QUESTIONS), 240)
        self.assertEqual(len({q['id'] for q in QUESTIONS}), len(QUESTIONS))
        for q in QUESTIONS:
            self.assertTrue(q['ar'] and q['en'] and q['answers'])
            self.assertLess(len(q['ar']), 300)
            self.assertIn(q['category'], CATEGORIES)
        deck = TriviaDeck()
        ids = [(await deck.next())['id'] for _ in QUESTIONS]
        self.assertEqual(len(set(ids)), len(QUESTIONS))
        self.assertNotEqual((await deck.next())['id'], ids[-1])

    async def test_category_does_not_repeat_until_its_bank_is_exhausted(self):
        deck = TriviaDeck()
        questions = [await deck.next('riddles') for _ in range(20)]
        self.assertTrue(all(q['category'] == 'riddles' for q in questions))
        self.assertEqual(len({q['id'] for q in questions}), 20)
        self.assertNotEqual((await deck.next('riddles'))['id'], questions[-1]['id'])

    async def test_history_survives_restart_and_stays_room_local(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATABASE_URL': '', 'DATA_DIR': temp}):
            first = TriviaDeck('123-1')
            previous = {(await first.next('science'))['id'] for _ in range(10)}
            restarted = TriviaDeck('123-1')
            later = {(await restarted.next('science'))['id'] for _ in range(10)}
            self.assertFalse(previous & later)
            other = TriviaDeck('123-2')
            await other.next('science')
            self.assertEqual(len(other.seen), 1)

    async def test_arabic_category_question_accepts_english_answer_and_scores_once(self):
        social = self.social()
        await social.handle('1', '!game علوم')
        self.assertEqual(social.game['kind'], 'quiz')
        question = next(q for q in QUESTIONS if q['id'] == social.game['question_id'])
        self.assertEqual(question['category'], 'science')
        await social.handle('2', question['display_en'])
        self.assertIsNone(social.game)
        self.assertEqual(social.scores['2'], 1)
        await social.handle('2', question['display_en'])
        self.assertEqual(social.scores['2'], 1)

    async def test_hints_once_and_only_starter_can_skip(self):
        social = self.social()
        await social.handle('1', '!game riddles')
        await social.handle('2', '!تلميح')
        self.assertTrue(social.game['hinted'])
        social.cooldowns.clear()
        await social.handle('2', '!skipgame')
        self.assertIsNotNone(social.game)
        social.cooldowns.clear()
        await social.handle('1', '!skipgame')
        self.assertIsNone(social.game)
        self.assertFalse(social.scores)

    async def test_help_unknown_category_active_round_and_room_isolation(self):
        social = self.social()
        await social.handle('1', '!game help')
        self.assertIsNone(social.game)
        social.cooldowns.clear()
        await social.handle('1', '!game nonsense')
        self.assertIsNone(social.game)
        social.cooldowns.clear()
        await social.handle('1', '!game geography')
        question = social.game['question_id']
        await social.handle('2', '!game science')
        self.assertEqual(social.game['question_id'], question)
        other = self.social()
        self.assertIsNone(other.game)
        self.assertFalse(other.trivia.seen)

    async def test_language_switch_uses_same_deck_and_normalized_arabic_digits(self):
        social = self.social()
        await social.handle('1', '!game math')
        value = social.game['display'].translate(str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩'))
        await social.handle('2', value)
        self.assertEqual(social.scores['2'], 1)
        old = set(social.trivia.seen)
        social.language_policy = lambda: 'en'
        social.cooldowns.clear()
        await social.handle('1', '!game math')
        self.assertNotIn(social.game['question_id'], old)
        self.assertEqual(social.game['language'], 'en')

    async def test_store_failure_does_not_break_rounds(self):
        deck = TriviaDeck()
        deck.store = Mock()
        with patch.object(deck, 'store') as store:
            store.load.side_effect = OSError('offline')
            store.save.side_effect = OSError('offline')
            first = await deck.next('culture')
            second = await deck.next('culture')
        self.assertNotEqual(first['id'], second['id'])
