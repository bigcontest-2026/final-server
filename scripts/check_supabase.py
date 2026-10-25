"""Check the configured welfare database without displaying secrets or records."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT/'service/backend/.env', override=False, interpolate=False)
    sys.path.insert(0, str(ROOT/'agent/src'))
    from donghaeng_agent.welfare_connection import (
        WelfareDatabaseError, configured_schema, postgres_connection,
    )
    try:
        schema = configured_schema()
        with postgres_connection() as db:
            ready = db.execute("SELECT 1 AS ready, current_setting('transaction_read_only') AS read_only").fetchone()
            columns = db.execute('''SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns WHERE table_schema=%s
                ORDER BY table_name, ordinal_position''', (schema,)).fetchall()
            tables = {}
            for column in columns:
                tables.setdefault(column['table_name'], []).append({
                    key: column[key] for key in ('column_name', 'data_type', 'is_nullable')
                })
        print(json.dumps({'connected': ready['ready'] == 1, 'read_only': ready['read_only'] == 'on',
                          'schema': schema, 'table_count': len(tables), 'tables': tables},
                         ensure_ascii=False, indent=2))
        return 0
    except WelfareDatabaseError as exc:
        print(json.dumps({'connected': False, 'code': exc.code, 'message': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
