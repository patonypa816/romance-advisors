"""Finale, award and optional-avatar checks use only synthetic local inputs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from romance.game import Game
from romance.server import Workbench
from romance.viewer_avatar import safe_avatar_url
from test_game import FakeModel
from test_program import FakeAvatar


AVATAR = 'https://p3.douyinpic.com/aweme/avatar.jpeg'


class SuggestionModel(FakeModel):
    async def generate(self, task, payload):
        result = await super().generate(task, payload)
        if task == 'select':
            valid = [p for p in payload['proposals'] if p['text'].startswith('建议')]
            ids = {p['id'] for p in valid}
            result['options'] = [o for o in result['options'] if o['proposal_id'] in ids]
            result['suggestion_proposal_ids'] = [p['id'] for p in valid]
        return result


class LastChoice:
    def __init__(self):
        self.award_draws = []

    def choice(self, values):
        if values and isinstance(values[0], str):
            self.award_draws.append(list(values))
            return values[-1]
        return values[0]

    def random(self):
        return .9


class FinaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'game.sqlite'
        self.model = SuggestionModel()
        self.game = Game(self.path, self.model)
        self.draw = LastChoice()
        self.game.rng = self.draw
        await self.game.new_episode()

    async def asyncTearDown(self):
        self.game.close()
        self.temp.cleanup()

    async def settle(self, author, delta, nickname=None, avatar_url=None):
        if self.game.current_round['phase'] == 'DONE':
            self.game.start_round()
        if author:
            self.game.submit_proposal(author, nickname or author, '建议认真解释', avatar_url=avatar_url)
        self.model.delta = delta
        state = await self.game.close_collection()
        if state['round']['phase'] == 'VOTING':
            self.game.vote('voter', '只投票', state['round']['options'][0]['id'])
            state = await self.game.close_voting()
        return state

    async def test_awards_count_actual_negative_points_not_counts_rate_or_net(self):
        await self.settle('a', -4)
        await self.settle('a', -4)
        await self.settle('b', -10)
        await self.settle('b', 20)
        state = await self.settle('c', 100)
        awards = state['finale']['awards']
        self.assertEqual(awards['worst']['author_id'], 'b')
        self.assertEqual(awards['worst']['negative_delta'], -10)
        self.assertEqual(awards['worst']['score_delta'], 10)
        self.assertEqual(awards['worst']['selection_reason'], 'most_negative_points')
        self.assertEqual(awards['best']['author_id'], 'c')
        self.assertEqual(awards['best']['positive_delta'], 48)
        self.assertEqual(state['leaderboards']['worst'][0]['author_id'], 'a')
        self.assertEqual(self.draw.award_draws, [])

    async def test_best_keeps_positive_rate_instead_of_positive_points(self):
        await self.settle('a', 1)
        await self.settle('b', 0)
        state = await self.settle('b', 100)
        self.assertEqual(state['finale']['awards']['best']['author_id'], 'a')
        self.assertEqual(state['finale']['awards']['best']['rate'], 1)

    async def test_no_negative_random_includes_unselected_suggestions_not_chat_or_votes(self):
        for author in 'abcde':
            self.game.submit_proposal(author, author, '建议' + author)
        self.game.submit_proposal('z-chat', '普通闲聊', '你好主播')
        self.model.delta = 50
        state = await self.game.close_collection()
        self.assertEqual(len(state['round']['options']), 4)
        self.game.vote('z-voter', '只投票', state['round']['options'][0]['id'])
        state = await self.game.close_voting()
        self.assertEqual(self.draw.award_draws, [list('abcde')])
        worst = state['finale']['awards']['worst']
        self.assertEqual(worst['author_id'], 'e')
        self.assertEqual(worst['adopted'], 0)
        self.assertEqual(worst['negative_delta'], 0)
        self.assertEqual(worst['selection_reason'], 'random_suggestion')
        self.assertNotIn('e', [r['author_id'] for r in state['leaderboards']['worst']])

    async def test_same_text_suggestions_keep_each_author_but_duplicate_author_one_draw(self):
        for author in ['a', 'a', 'b']:
            self.game.submit_proposal(author, '同名', '建议认真解释')
        self.model.delta = 50
        await self.game.close_collection()
        self.game.vote('voter', '投票', '1')
        state = await self.game.close_voting()
        self.assertEqual(self.draw.award_draws, [['a', 'b']])
        self.assertEqual(state['finale']['awards']['worst']['author_id'], 'b')

    async def test_single_suggester_may_win_both_and_never_gains_negative_record(self):
        state = await self.settle('only', 50)
        awards = state['finale']['awards']
        self.assertEqual(awards['best']['author_id'], awards['worst']['author_id'])
        self.assertEqual(state['leaderboards']['worst'][0]['negative'], 0)
        self.assertEqual(state['leaderboards']['worst'][0]['rate'], 0)

    async def test_self_only_has_no_fictional_awards(self):
        state = await self.settle(None, 50)
        self.assertEqual(state['finale']['awards'], {'best': None, 'worst': None})
        self.assertEqual(self.draw.award_draws, [])

    async def test_failure_also_awards_but_technical_ending_does_not(self):
        state = await self.settle('a', -100)
        self.assertEqual(state['finale']['status'], 'FAILED')
        self.assertEqual(state['finale']['awards']['worst']['negative_delta'], -50)
        await self.game.new_episode()
        self.game.abort_episode()
        self.assertIsNone(self.game.public_state()['finale'])

    async def test_new_episode_excludes_prior_scores_suggestions_and_old_random_winner(self):
        old = await self.settle('old', -50)
        self.assertEqual(old['finale']['awards']['worst']['author_id'], 'old')
        await self.game.new_episode()
        self.assertIsNone(self.game.public_state()['finale'])
        state = await self.settle('new', 50)
        self.assertEqual(state['finale']['awards']['best']['author_id'], 'new')
        self.assertEqual(state['finale']['awards']['worst']['author_id'], 'new')
        self.assertEqual({r['author_id'] for r in state['leaderboards']['best']}, {'new', 'old'})
        await self.game.new_session()
        self.assertIsNone(self.game.public_state()['finale'])
        self.assertEqual(self.game.public_state()['leaderboards']['best'], [])

    async def test_persisted_finale_survives_repeated_poll_retry_and_restart_without_redraw(self):
        with patch('romance.game.time.time', return_value=1000):
            state = await self.settle('a', 50)
        frozen = copy.deepcopy(self.game.episode['finale'])
        self.assertEqual(frozen['started_at'], 1000)
        self.assertEqual(frozen['duration_ms'], 6800)
        await self.game.retry()
        statements = []
        self.game.storage.db.set_trace_callback(statements.append)
        with patch('romance.game.time.time', return_value=1002):
            for _ in range(3):
                state = self.game.public_state()
                self.assertEqual(state['finale']['elapsed_ms'], 2000)
        self.assertFalse(any(q.startswith(('INSERT', 'UPDATE', 'DELETE')) for q in statements))
        self.assertEqual(self.game.episode['finale'], frozen)
        self.assertEqual(len(self.draw.award_draws), 1)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.episode['finale'], frozen)
        self.assertEqual(self.game.public_state()['finale']['awards'], frozen['awards'])

    async def test_old_ended_record_does_not_invent_clock_or_redraw_on_read(self):
        await self.settle('a', 50)
        self.game.episode.pop('finale')
        self.game._save()
        self.assertIsNone(self.game.public_state()['finale'])
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertIsNone(self.game.public_state()['finale'])

    async def test_final_reply_clock_and_awards_roll_back_as_one_transaction(self):
        self.game.storage.db.execute("CREATE TRIGGER fail_finale BEFORE UPDATE ON episodes WHEN json_extract(NEW.body, '$.status')='SUCCEEDED' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        state = await self.settle('a', 50)
        self.assertEqual(state['episode']['status'], 'ACTIVE')
        self.assertEqual(state['episode']['score'], 50)
        self.assertEqual(state['round']['phase'], 'WAITING_REPLY')
        self.assertEqual(len(state['messages']), 2)
        self.assertIsNone(state['finale'])
        self.assertNotIn('finale', self.game.storage.load()['episodes'][-1])
        self.game.storage.db.execute('DROP TRIGGER fail_finale')
        state = await self.game.retry()
        self.assertEqual(state['episode']['score'], 100)
        self.assertEqual(len(state['messages']), 3)
        self.assertIsNotNone(state['finale'])

    async def test_avatar_travels_with_original_author_not_model_payload(self):
        event = {'event_id': 'e', 'author_id': 'a', 'nickname': '<img onerror=bad>',
                 'text': '建议认真解释', 'avatar_url': AVATAR,
                 'expected_round_id': self.game.data['round_id'], 'expected_phase': 'COLLECTING'}
        receipt = self.game.accept_comment(event)
        self.assertEqual(self.game.accept_comment(event), receipt)
        with self.assertRaisesRegex(ValueError, 'invalid_comment_conflict'):
            self.game.accept_comment(dict(event, avatar_url=AVATAR + '?changed=1'))
        self.model.delta = 50
        await self.game.close_collection()
        self.game.vote('v', '投票者', '1')
        state = await self.game.close_voting()
        self.assertEqual(state['finale']['awards']['best']['avatar_url'], AVATAR)
        self.assertEqual(state['finale']['awards']['best']['author_id'], 'a')
        self.assertNotIn(AVATAR, json.dumps(self.model.calls))
        self.assertNotIn('WOMAN_PRIVATE', json.dumps(state))

    async def test_later_missing_picture_keeps_the_same_author_previous_valid_avatar(self):
        await self.settle('a', 1, avatar_url=AVATAR)
        state = await self.settle('a', 100)
        self.assertEqual(state['finale']['awards']['best']['avatar_url'], AVATAR)
        self.assertEqual(state['finale']['awards']['worst']['avatar_url'], AVATAR)

    async def test_invalid_avatar_is_optional_not_a_comment_failure(self):
        event = {'event_id': 'e', 'author_id': 'a', 'nickname': 'a', 'text': '建议认真解释',
                 'avatar_url': 'http://127.0.0.1/private',
                 'expected_round_id': self.game.data['round_id'], 'expected_phase': 'COLLECTING'}
        receipt = self.game.accept_comment(event)
        self.assertNotIn('avatar_url', receipt)
        self.assertEqual(self.game.accept_comment(event), receipt)
        self.assertNotIn('avatar_url', self.game.data['proposals'][-1])

    async def test_missing_or_untrusted_suggestion_ids_preserve_slots_and_safe_fallback(self):
        original = self.model.generate
        for field in [None, {'a': True}, ['forged'], [None, 3, ['bad']], ['too-many'] * 10]:
            with self.subTest(field=field):
                await self.game.new_episode()
                self.game.submit_proposal('a', '建议作者', '建议认真解释')
                self.game.submit_proposal('z-chat', '闲聊者', '你好主播')
                async def generate(task, payload):
                    result = await original(task, payload)
                    if task == 'select':
                        result['suggestion_proposal_ids'] = field
                    return result
                self.model.generate = generate
                self.model.delta = 50
                state = await self.game.close_collection()
                self.assertIsNone(state['error'])
                self.assertEqual(len(state['round']['options']), 1)
                self.game.vote('v', '投票者', '1')
                state = await self.game.close_voting()
                self.assertEqual(state['finale']['awards']['worst']['author_id'], 'a')
                self.assertNotIn('suggestion_proposal_ids', state['round'])
                self.model.generate = original

    async def test_clock_deadline_uses_commit_and_keeps_eight_seconds_on_reload(self):
        with patch('romance.game.time.time', return_value=1000):
            await self.settle('a', 50)
        wb = Workbench(self.path, model=FakeModel(), avatar_client=FakeAvatar())
        try:
            wb.settings['auto_advance'] = True
            with patch('romance.server.time.time', return_value=1003):
                state = wb.state()
            self.assertEqual(state['server_time'], 1003)
            self.assertEqual(wb.deadline, 1008)
            self.assertEqual(state['finale']['elapsed_ms'], 3000)
            wb.clock_key = None
            with patch('romance.server.time.time', return_value=1005):
                wb.state()
            self.assertEqual(wb.deadline, 1008)
        finally:
            await wb.close()


class AvatarURLTests(unittest.TestCase):
    def test_only_picture_cdn_https_without_credentials_or_local_targets(self):
        self.assertEqual(safe_avatar_url(AVATAR), AVATAR)
        for value in [None, 7, '', 'https://p3.douyinpic.com.evil.invalid/a.jpg',
                      'https://user:password@p3.douyinpic.com/a.jpg', 'http://p3.douyinpic.com/a.jpg',
                      'https://127.0.0.1/a.jpg', 'https://localhost/a.jpg', '//p3.douyinpic.com/a.jpg',
                      'data:image/svg+xml,secret', 'javascript:alert(1)',
                      'https://p3.douyinpic.com:9999/a.jpg', 'https://p3.douyinpic.com/a.svg',
                      'https://p3.douyinpic.com/a.jpg#token', 'https://p3.douyinpic.com/a\\b',
                      'https://p3.douyinpic.com/\na.jpg', 'https://[broken/a.jpg', 'x' * 2049]:
            with self.subTest(value=value):
                self.assertIsNone(safe_avatar_url(value))
