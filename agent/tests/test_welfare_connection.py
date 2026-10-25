"""Guard transport policy, read-only transactions and secret-safe diagnostics."""
import os
import traceback
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
from donghaeng_agent.welfare_connection import (
    WelfareDatabaseError, _connection_parameters, configured_schema,
    postgres_connection, remote_configured,
)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'WELFARE_DATABASE_URL': 'postgresql://reader:example-password@db.example.invalid/postgres',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_missing_password_is_rejected_before_network(self):
        os.environ['WELFARE_DATABASE_URL'] = 'postgresql://reader@db.example.invalid/postgres'
        with patch('psycopg.connect') as connect:
            with self.assertRaises(WelfareDatabaseError) as result:
                with postgres_connection():
                    pass
        self.assertEqual(result.exception.code, 'missing_password')
        connect.assert_not_called()

    def test_raw_password_overrides_encoded_uri_without_interpolation(self):
        raw = 'a@b:/?# ${VALUE} % more'
        os.environ['WELFARE_DATABASE_PASSWORD'] = raw
        params = _connection_parameters()
        self.assertEqual(params['password'], raw)
        self.assertEqual(params['sslmode'], 'require')
        self.assertEqual(params['connect_timeout'], 10)

    def test_encoded_password_and_stronger_tls_preserved(self):
        os.environ['WELFARE_DATABASE_URL'] = 'postgresql://reader:p%40ss%23word@db.example.invalid/postgres?sslmode=verify-full'
        params = _connection_parameters()
        self.assertEqual(params['password'], 'p@ss#word')
        self.assertEqual(params['sslmode'], 'verify-full')

    def test_no_credential_fallback_from_missing_or_placeholder_password(self):
        for password in ('', '[YOUR-PASSWORD]'):
            os.environ['WELFARE_DATABASE_URL'] = f'postgresql://reader:{password}@db.example.invalid/postgres'
            with self.assertRaises(WelfareDatabaseError) as result:
                _connection_parameters()
            self.assertEqual(result.exception.code, 'missing_password')

    def test_insecure_transport_and_invalid_schema_are_rejected(self):
        os.environ['WELFARE_DATABASE_URL'] += '?sslmode=disable'
        with self.assertRaises(WelfareDatabaseError) as result:
            _connection_parameters()
        self.assertEqual(result.exception.code, 'insecure_connection')
        for schema in ('', 'welfare; DROP TABLE programs', 'welfare,public'):
            os.environ['WELFARE_DATABASE_SCHEMA'] = schema
            with self.assertRaises(WelfareDatabaseError):
                configured_schema()

    def test_invalid_connection_string_does_not_leak_credential(self):
        os.environ['WELFARE_DATABASE_URL'] = 'postgresql://reader:private-secret@db.example.invalid/postgres?bad-field=x'
        try:
            _connection_parameters()
        except WelfareDatabaseError:
            error = traceback.format_exc()
        self.assertNotIn('private-secret', error)
        self.assertNotIn('postgresql://', error)

    def test_driver_connection_error_is_sanitized(self):
        with patch('psycopg.connect', side_effect=psycopg.OperationalError('private-secret in connection URL')):
            try:
                with postgres_connection():
                    pass
            except WelfareDatabaseError:
                error = traceback.format_exc()
        self.assertNotIn('private-secret', error)
        self.assertIn('WelfareDatabaseError', error)

    def test_transaction_is_read_only_before_any_query_and_closes(self):
        events = []

        class FakeConnection:
            read_only = False
            isolation_level = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append('closed')

            @contextmanager
            def transaction(self):
                events.append(('transaction', self.read_only, self.isolation_level))
                yield

            def execute(self, query):
                events.append(('execute', str(query), self.read_only))

        connection = FakeConnection()
        with patch('psycopg.connect', return_value=connection) as connect:
            try:
                with postgres_connection() as db:
                    self.assertIs(db, connection)
                    raise psycopg.ProgrammingError('private-secret from SQL failure')
            except WelfareDatabaseError:
                error = traceback.format_exc()
        self.assertEqual(events[0], ('transaction', True, psycopg.IsolationLevel.REPEATABLE_READ))
        self.assertTrue(all(event[2] for event in events if isinstance(event, tuple) and event[0] == 'execute'))
        self.assertEqual(events[-1], 'closed')
        self.assertNotIn('private-secret', error)
        self.assertIsNone(connect.call_args.kwargs['prepare_threshold'])
        self.assertFalse(connect.call_args.kwargs['autocommit'])

    def test_configured_flag_does_not_imply_valid_connection(self):
        os.environ['WELFARE_DATABASE_URL'] = 'https://example.invalid'
        self.assertTrue(remote_configured())
        with self.assertRaises(WelfareDatabaseError):
            _connection_parameters()
        os.environ['WELFARE_DATABASE_URL'] = ''
        self.assertFalse(remote_configured())


if __name__ == '__main__':
    unittest.main()
