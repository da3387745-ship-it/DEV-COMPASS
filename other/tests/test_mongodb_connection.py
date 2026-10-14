import os
import unittest
from unittest.mock import MagicMock, Mock, patch

from fastapi import HTTPException, Response
from pymongo.errors import ServerSelectionTimeoutError

from backend import bridge, check_mongodb_connection


class MongoDBConnectionTest(unittest.TestCase):
    def test_passwords_are_salted_and_verified(self):
        first = bridge.hash_password('correct horse battery staple')
        second = bridge.hash_password('correct horse battery staple')

        self.assertNotEqual(first, second)
        self.assertTrue(bridge.verify_password('correct horse battery staple', first))
        self.assertFalse(bridge.verify_password('wrong password', first))

    def test_signup_stores_password_hash_and_sets_http_only_session_cookie(self):
        database = MagicMock()
        users = MagicMock()
        sessions = MagicMock()
        database.__getitem__.side_effect = lambda name: {'users': users, 'sessions': sessions}[name]
        response = Response()
        payload = bridge.SignupRequest(
            email='  USER@example.com ',
            password='correct horse battery staple',
            display_name='User Name',
        )
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb+srv://example.invalid'}), \
                patch.object(bridge, '_mongo_client', None), \
                patch.object(bridge, 'mongo_database', return_value=database):
            with patch.object(bridge, 'ensure_auth_indexes'), \
                    patch.object(bridge, 'get_mongo_client', return_value=Mock()):
                result = bridge.signup(payload, response)

        stored_user = users.insert_one.call_args.args[0]
        self.assertEqual(stored_user['email'], 'user@example.com')
        self.assertNotEqual(stored_user['password_hash'], payload.password)
        self.assertTrue(bridge.verify_password(payload.password, stored_user['password_hash']))
        self.assertNotIn('password_hash', result['user'])
        self.assertIn('httponly', response.headers['set-cookie'].lower())
        sessions.insert_one.assert_called_once()

    def test_profile_update_persists_profile_and_preferences(self):
        user = {
            '_id': 'user-1',
            'email': 'user@example.com',
            'display_name': 'Before',
            'settings': {'theme': 'light', 'answer_style': 'concise'},
        }
        database = MagicMock()
        payload = bridge.ProfileUpdateRequest(
            display_name='After',
            theme='dark',
            answer_style='detailed',
        )
        with patch.object(bridge, 'require_user', return_value=user), \
                patch.object(bridge, 'mongo_database', return_value=database):
            result = bridge.update_profile(payload, Mock())

        self.assertEqual(result['username'], 'After')
        self.assertEqual(result['settings'], {'theme': 'dark', 'answer_style': 'detailed'})
        database['users'].update_one.assert_called_once_with(
            {'_id': 'user-1'},
            {'$set': {
                'display_name': 'After',
                'settings': {'theme': 'dark', 'answer_style': 'detailed'},
            }},
        )

    def test_dashboard_returns_saved_account_counts_and_activity(self):
        user = {'_id': 'user-1'}
        database = MagicMock()
        database['user_documents'].aggregate.return_value = [
            {'_id': None, 'count': 2, 'chunks': 9},
        ]
        database['chat_history'].count_documents.return_value = 4
        database['activity'].find.return_value.sort.return_value.limit.return_value = [
            {'type': 'question', 'title': 'How does this work?'},
        ]
        with patch.object(bridge, 'require_user', return_value=user), \
                patch.object(bridge, 'mongo_database', return_value=database):
            result = bridge.dashboard(Mock())

        self.assertEqual(result['question_count'], 4)
        self.assertEqual(result['document_count'], 2)
        self.assertEqual(result['activity'][0]['type'], 'question')

    def test_chat_history_returns_sessions_only_for_authenticated_user(self):
        user = {'_id': 'user-1'}
        database = MagicMock()
        chat_history = MagicMock()
        chat_sessions = MagicMock()
        database.__getitem__.side_effect = lambda name: {
            'chat_history': chat_history,
            'chat_sessions': chat_sessions,
        }[name]
        sessions = [{'_id': 'chat-1', 'title': 'A saved chat'}]
        chat_sessions.find.return_value.sort.return_value.limit.return_value = sessions
        with patch.object(bridge, 'require_user', return_value=user), \
                patch.object(bridge, 'mongo_database', return_value=database):
            result = bridge.chat_history(Mock(), limit=500)

        self.assertEqual(result, {'sessions': sessions})
        chat_history.find.assert_called_once_with(
            {'user_id': 'user-1', 'chat_id': {'$exists': False}},
            {'question': 1, 'created_at': 1},
        )
        chat_sessions.find.return_value.sort.return_value.limit.assert_called_once_with(50)

    def test_connection_check_reports_missing_uri(self):
        with (
            patch.dict(os.environ, {'MONGODB_URI': ''}),
            patch.object(
                check_mongodb_connection,
                'MongoClient',
            ) as client_factory,
        ):
            self.assertEqual(check_mongodb_connection.main(), 1)

        client_factory.assert_not_called()

    def test_connection_check_pings_mongodb(self):
        client = Mock()
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb+srv://example.invalid'}):
            with patch.object(
                check_mongodb_connection,
                'MongoClient',
                return_value=client,
            ) as client_factory:
                self.assertEqual(check_mongodb_connection.main(), 0)

        client_factory.assert_called_once_with(
            'mongodb+srv://example.invalid',
            serverSelectionTimeoutMS=5000,
            appname='DevCompassConnectionCheck',
        )
        client.admin.command.assert_called_once_with('ping')
        client.close.assert_called_once_with()

    def test_connection_check_does_not_leak_uri_on_failure(self):
        client = Mock()
        client.admin.command.side_effect = ServerSelectionTimeoutError(
            'secret URI'
        )
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb+srv://secret.example.invalid'}):
            with patch.object(check_mongodb_connection, 'MongoClient', return_value=client):
                with patch('builtins.print') as print_mock:
                    self.assertEqual(check_mongodb_connection.main(), 1)

        self.assertNotIn('secret.example.invalid', str(print_mock.call_args))
        client.close.assert_called_once_with()

    def test_missing_uri_reports_unconfigured_without_connecting(self):
        with (
            patch.dict(os.environ, {'MONGODB_URI': ''}),
            patch.object(bridge, 'MongoClient') as client_factory,
            patch.object(bridge, 'require_user'),
        ):
            result = bridge.database_status(Mock())

        self.assertFalse(result['ok'])
        self.assertFalse(result['configured'])
        client_factory.assert_not_called()

    def test_successful_ping_hides_database_name_and_closes_client(self):
        client = Mock()
        environment = {
            'MONGODB_URI': 'mongodb+srv://example.invalid',
            'MONGODB_DATABASE': 'devcompass_test',
        }
        with patch.dict(os.environ, environment):
            with patch.object(
                bridge,
                'MongoClient',
                return_value=client,
            ) as client_factory:
                with patch.object(bridge, 'require_user'):
                    result = bridge.database_status(Mock())

        self.assertEqual(
            result,
            {'ok': True, 'configured': True},
        )
        client_factory.assert_called_once_with(
            'mongodb+srv://example.invalid',
            serverSelectionTimeoutMS=5000,
            appname='DevCompass',
        )
        client.admin.command.assert_called_once_with('ping')
        client.close.assert_called_once_with()

    def test_failed_ping_returns_safe_error_without_uri(self):
        client = Mock()
        client.admin.command.side_effect = ServerSelectionTimeoutError('connection timed out')
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb+srv://secret.example.invalid'}):
            with patch.object(bridge, 'MongoClient', return_value=client):
                with patch.object(bridge, 'require_user'):
                    with self.assertRaises(HTTPException) as raised:
                        bridge.database_status(Mock())

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn('secret.example.invalid', raised.exception.detail)
        client.close.assert_called_once_with()

    def test_ask_saves_question_and_answer_in_chat_session(self):
        response = {
            'answer': 'Use a backend API.',
            'sources': ['README.md'],
            'provider': 'test',
            'mode': 'chat',
        }
        database = MagicMock()
        chat_history = MagicMock()
        activity = MagicMock()
        chat_sessions = MagicMock()
        chat_sessions.find_one.return_value = {'_id': 'chat-1'}
        chat_history.count_documents.return_value = 0
        chat_history.insert_one.return_value.inserted_id = 'turn-1'
        database.__getitem__.side_effect = lambda name: {
            'chat_history': chat_history,
            'activity': activity,
            'chat_sessions': chat_sessions,
        }[name]
        user = {'_id': 'user-1', 'settings': {'answer_style': 'concise'}}
        with patch.object(bridge, 'request_user', return_value=user), \
                patch.object(bridge, 'mongo_database', return_value=database), \
                patch.object(bridge.rag_service, 'ask', return_value=response):
            result = bridge.ask(
                bridge.AskRequest(question='  How does this work?  ', chat_id='chat-1'),
                Mock(cookies={}),
            )

        chat_history.insert_one.assert_called_once()
        saved_turn = chat_history.insert_one.call_args.args[0]
        self.assertEqual(saved_turn['user_id'], 'user-1')
        self.assertEqual(saved_turn['chat_id'], 'chat-1')
        self.assertEqual(saved_turn['question'], 'How does this work?')
        self.assertEqual(saved_turn['answer'], 'Use a backend API.')
        self.assertEqual(saved_turn['sources'], ['README.md'])
        self.assertEqual(result['turn_id'], 'turn-1')
        self.assertNotIn('provider', result)
        chat_sessions.update_one.assert_called_once()

    def test_guest_question_uses_backend_guest_scope_without_mongodb_write(self):
        request = Mock(cookies={'devcompass_guest': 'guest-token'})
        with patch.object(bridge, 'request_user', return_value=None), \
                patch.object(bridge, 'mongo_database') as database, \
                patch.object(bridge.rag_service, 'ask', return_value={'answer': 'answer'}) as ask:
            result = bridge.ask(bridge.AskRequest(question='A question'), request)

        self.assertEqual(result['answer'], 'answer')
        self.assertEqual(ask.call_args.kwargs['user_id'], bridge.guest_workspace_id(request))
        database.assert_not_called()

    def test_guest_question_reports_missing_ai_provider_without_login_prompt(self):
        request = Mock(cookies={'devcompass_guest': 'guest-token'})
        with patch.object(bridge, 'request_user', return_value=None), \
                patch.object(
                    bridge.rag_service,
                    'ask',
                    side_effect=bridge.AIProviderNotConfiguredError('No provider key'),
                ):
            with self.assertRaises(HTTPException) as raised:
                bridge.ask(bridge.AskRequest(question='Explain this document'), request)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn('AI answers need a provider key', raised.exception.detail)
        self.assertNotIn('Log in', raised.exception.detail)

    def test_ask_reports_database_write_failure_without_leaking_uri(self):
        database = MagicMock()
        database['chat_sessions'].find_one.return_value = {'_id': 'chat-1'}
        user = {'_id': 'user-1', 'settings': {'answer_style': 'concise'}}
        with patch.object(bridge, 'request_user', return_value=user), \
                patch.object(bridge, 'mongo_database', return_value=database), \
                patch.object(bridge.rag_service, 'ask', return_value={'answer': 'answer'}), \
                patch.object(
                    bridge,
                    'save_chat_turn',
                    side_effect=HTTPException(status_code=503, detail='Could not save chat.'),
                ):
            with self.assertRaises(HTTPException) as raised:
                bridge.ask(
                    bridge.AskRequest(question='A question', chat_id='chat-1'),
                    Mock(cookies={}),
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn('secret.example.invalid', raised.exception.detail)


if __name__ == '__main__':
    unittest.main()