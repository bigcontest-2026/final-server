"""Start the standalone HERMES evidence/recommendation API from any directory."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def configure(root=ROOT):
    """Load server settings at startup; importing this module never reads secrets."""
    from dotenv import load_dotenv

    root = Path(root)
    # Keep exported deployment settings authoritative and preserve literal passwords.
    load_dotenv(root/'service/backend/.env', override=False, interpolate=False)
    for path in ['service/backend', 'agent/src', 'model']:
        resolved = str(root/path)
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    defaults = {
        'APP_MODE': 'final',
        'DONGHAENG_MODEL_ROOT': str(root/'model'),
        'DONGHAENG_AGENT_ROOT': str(root/'agent'),
        'FINAL_MODEL_BUNDLE': str(root/'bundles/activity'),
        'I5_BUNDLE_PATH': str(root/'model/data/i5/national_2025_10group'),
        'MODEL_RUNTIME_ROOT': str(root/'runtime/model'),
        'ACCESS_MODE': 'local_development',
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    if os.getenv('WELFARE_DATABASE_URL', '').strip():
        # The remote adapter resolves its own corpus; never create or select demo data.
        return None

    os.environ.setdefault('WELFARE_DB_PATH', str(root/'runtime/welfare_erd_v2.sqlite'))
    os.environ.setdefault('WELFARE_PUBLICATION_ID', 'erd-demo-v2')
    from donghaeng_agent.welfare_erd import create_demo
    database = Path(os.environ['WELFARE_DB_PATH'])
    if not database.exists():
        database.parent.mkdir(parents=True, exist_ok=True)
        create_demo(database)
    return database


def main():
    configure()
    import uvicorn
    uvicorn.run('app.main:app',host='127.0.0.1',port=int(os.getenv('PORT','8011')))


if __name__=='__main__':
    main()
