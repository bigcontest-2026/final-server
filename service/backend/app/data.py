from pathlib import Path

from donghaeng_agent.mock_contracts import Dataset


def load_dataset(path: Path) -> Dataset:
    """Load lazily so imports and health work before the shared file arrives."""
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))
