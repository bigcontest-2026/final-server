"""Server-only, read-only PostgreSQL access for the deployed welfare catalog.

Credentials are resolved here, never passed through graph state or API payloads.
Importing this module neither loads .env files nor opens a database connection.
"""
import os
import re
from contextlib import contextmanager


class WelfareDatabaseError(RuntimeError):
    """A safe diagnostic that never includes a connection string or driver error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def remote_configured():
    return bool(os.getenv('WELFARE_DATABASE_URL', '').strip())


def configured_schema():
    schema = os.getenv('WELFARE_DATABASE_SCHEMA', 'welfare').strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', schema):
        raise WelfareDatabaseError('invalid_schema', '복지 DB 스키마 설정을 확인해 주세요.')
    return schema


def _connection_parameters():
    # Do not include values in configuration errors: passwords may contain URI syntax.
    url = os.getenv('WELFARE_DATABASE_URL', '').strip()
    if not url:
        raise WelfareDatabaseError('missing_url', '복지 DB 접속 문자열이 비어 있습니다.')
    if not url.startswith(('postgresql://', 'postgres://')):
        raise WelfareDatabaseError('invalid_url', '복지 DB에는 PostgreSQL 접속 문자열이 필요합니다.')
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        raise WelfareDatabaseError('driver_unavailable', 'PostgreSQL 연결 패키지를 설치해 주세요.') from None
    try:
        params = conninfo_to_dict(url)
    except Exception:
        raise WelfareDatabaseError('invalid_url', '복지 DB 접속 문자열 형식을 확인해 주세요.') from None
    if not params.get('host') or not params.get('user') or not params.get('dbname'):
        raise WelfareDatabaseError('invalid_url', '복지 DB 접속 주소·계정·DB 이름을 확인해 주세요.')
    # A separate raw password avoids requiring users to percent-encode special chars.
    password = os.getenv('WELFARE_DATABASE_PASSWORD') or params.get('password')
    if not password or password.upper() in ('[YOUR-PASSWORD]', '[YOUR_PASSWORD]', '[PASSWORD]'):
        raise WelfareDatabaseError('missing_password', '복지 DB 비밀번호를 .env에 입력해 주세요.')
    params['password'] = password
    sslmode = params.get('sslmode', 'require')
    if sslmode not in ('require', 'verify-ca', 'verify-full'):
        raise WelfareDatabaseError('insecure_connection', '복지 DB 연결에는 sslmode=require 이상이 필요합니다.')
    params.update(sslmode=sslmode, connect_timeout=10, application_name='hermes-welfare-readonly')
    return params


@contextmanager
def postgres_connection():
    """Yield dict rows within one bounded, read-only, repeatable-read transaction.

    All SQL inside the context shares one snapshot. Driver exceptions are replaced
    with a safe message, including errors raised by a caller's query.
    """
    schema = configured_schema()
    params = _connection_parameters()
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(**params, autocommit=False, row_factory=dict_row,
                             prepare_threshold=None) as db:
            db.read_only = True
            db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with db.transaction():
                # Both transaction-local settings travel in one network round trip.
                db.execute(sql.SQL("SET LOCAL statement_timeout = '15s'; "
                    'SET LOCAL search_path TO pg_catalog, {}').format(sql.Identifier(schema)))
                yield db
    except psycopg.Error:
        raise WelfareDatabaseError('database_unavailable', 'Supabase 복지 DB 연결 또는 조회에 실패했습니다.') from None
