"""Documented user.avatar is opt-in; no live platform or image requests."""
import unittest

from dyhub_client import DyhubClient, _safe_business_event


class AvatarPolicyTests(unittest.TestCase):
    def event(self, kind='chat'):
        return {'platform': 'douyin', 'type': kind, 'id': 'synthetic', 'roomId': '123',
                'user': {'id': 'synthetic-viewer', 'nickname': '测试',
                         'avatar': 'https://p3.douyinpic.com/avatar.jpg', 'secUid': 'PRIVATE'},
                'data': {'content': '建议认真解释', 'giftId': 'g', 'giftName': '测试'}}

    def test_existing_consumer_still_drops_avatar(self):
        self.assertNotIn('avatar', _safe_business_event(self.event(), '123')['user'])

    def test_only_documented_chat_avatar_can_be_opted_in(self):
        validator = lambda url: url
        safe = _safe_business_event(self.event(), '123', avatar_validator=validator)
        self.assertEqual(safe['user']['avatar'], self.event()['user']['avatar'])
        self.assertNotIn('secUid', safe['user'])
        for kind in ('gift', 'member'):
            self.assertNotIn('avatar', _safe_business_event(self.event(kind), '123', avatar_validator=validator)['user'])

    def test_missing_invalid_and_guessed_aliases_do_not_enter(self):
        event = self.event()
        event['user'].pop('avatar')
        event['user']['avatar_url'] = 'https://p3.douyinpic.com/guessed.jpg'
        self.assertNotIn('avatar', _safe_business_event(event, '123', avatar_validator=lambda v: v)['user'])
        event['user']['avatar'] = 'x' * 2049
        self.assertNotIn('avatar', _safe_business_event(event, '123', avatar_validator=lambda v: v)['user'])
        self.assertNotIn('avatar', _safe_business_event(self.event(), '123', avatar_validator=lambda v: None)['user'])

    def test_source_type_option_is_bounded(self):
        for invalid in ((), ('unknown',), ('chat', 'chat'), 'chat', ['chat']):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                DyhubClient('http://127.0.0.1:8757', '123', None, None, event_types=invalid)
