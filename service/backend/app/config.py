import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "shared" / "mock" / "dataset.json"
DEFAULT_RESEARCH = Path(__file__).resolve().parents[2] / "shared" / "research" / "isolation_snapshot.json"
DEFAULT_AGENT_ROOT = Path(__file__).resolve().parents[3] / "agent"
DEFAULT_SUPPORT = DEFAULT_AGENT_ROOT / "resources" / "support" / "manifest.json"
DEFAULT_CAPTURE_ROOT = DEFAULT_AGENT_ROOT / "evidence" / "support"
DEFAULT_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
DEFAULT_MODEL_ROOT = Path(__file__).resolve().parents[3] / "model"
DEFAULT_I5_BUNDLE = DEFAULT_MODEL_ROOT / "data" / "i5" / "national_2025_10group"


@dataclass(frozen=True)
class Settings:
    mode: Literal["final", "mock", "research", "live"] = "final"
    model_root: Path = DEFAULT_MODEL_ROOT
    final_model_bundle: Path | None = None
    survey_area_bundle: Path | None = None
    model_runtime_root: Path = DEFAULT_RUNTIME / "final_model"
    dataset_path: Path = DEFAULT_DATASET
    research_path: Path = DEFAULT_RESEARCH
    i5_bundle_path: Path | None = DEFAULT_I5_BUNDLE
    cors_origins: tuple[str, ...] = field(default_factory=lambda: ("http://localhost:5173", "http://127.0.0.1:5173"))
    max_runs: int = 200
    run_ttl_seconds: int = 3600
    investigation_db_path: Path = DEFAULT_RUNTIME / "investigations.sqlite3"
    investigation_checkpoint_path: Path = DEFAULT_RUNTIME / "checkpoints.sqlite3"
    support_manifest_path: Path = DEFAULT_SUPPORT
    agent_resource_root: Path = DEFAULT_AGENT_ROOT
    support_capture_root: Path = DEFAULT_CAPTURE_ROOT
    investigation_worker_enabled: bool = True
    access_mode: Literal["local_development", "institution"] = "local_development"
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_tenant_id: str | None = None
    auth_public_key_path: Path | None = None
    auth_max_token_lifetime_seconds: int = 3600

    def __post_init__(self):
        if self.mode not in ("final", "mock", "research", "live"):
            raise ValueError("APP_MODE must be final, mock, research or live")
        if not 1 <= self.max_runs <= 10000 or self.run_ttl_seconds < 1:
            raise ValueError("RUN_STORE_MAX must be 1..10000 and RUN_STORE_TTL_SECONDS positive")
        if self.access_mode not in ("local_development", "institution"):
            raise ValueError("ACCESS_MODE must be local_development or institution")
        if not 60 <= self.auth_max_token_lifetime_seconds <= 86400:
            raise ValueError("AUTH_MAX_TOKEN_LIFETIME_SECONDS must be 60..86400")
        if self.access_mode == "institution":
            if not all((self.auth_issuer, self.auth_audience, self.auth_tenant_id, self.auth_public_key_path)):
                raise ValueError("institution mode requires issuer, audience, tenant and an RSA public key file")
            if self.mode not in ("research", "final"):
                raise ValueError("institution mode supports research or final only")

    @classmethod
    def from_env(cls):
        agent_root = Path(os.getenv("DONGHAENG_AGENT_ROOT", str(DEFAULT_AGENT_ROOT)))
        return cls(
            mode=os.getenv("APP_MODE", "final"),
            model_root=Path(os.getenv("DONGHAENG_MODEL_ROOT", str(DEFAULT_MODEL_ROOT))).resolve(),
            final_model_bundle=Path(os.environ["FINAL_MODEL_BUNDLE"]).resolve() if os.getenv("FINAL_MODEL_BUNDLE") else None,
            survey_area_bundle=Path(os.getenv("I5_BUNDLE_PATH") or os.environ["SURVEY_AREA_BUNDLE"]).resolve()
                if os.getenv("I5_BUNDLE_PATH") or os.getenv("SURVEY_AREA_BUNDLE") else None,
            model_runtime_root=Path(os.getenv("MODEL_RUNTIME_ROOT", str(DEFAULT_RUNTIME / "final_model"))).resolve(),
            dataset_path=Path(os.getenv("DATASET_PATH", str(DEFAULT_DATASET))),
            research_path=Path(os.getenv("RESEARCH_PATH", str(DEFAULT_RESEARCH))),
            i5_bundle_path=Path(os.getenv("I5_BUNDLE_PATH") or str(DEFAULT_I5_BUNDLE)),
            cors_origins=tuple(x.strip() for x in os.getenv(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",") if x.strip()),
            max_runs=int(os.getenv("RUN_STORE_MAX", "200")),
            run_ttl_seconds=int(os.getenv("RUN_STORE_TTL_SECONDS", "3600")),
            investigation_db_path=Path(os.getenv("INVESTIGATION_DB_PATH", str(DEFAULT_RUNTIME / "investigations.sqlite3"))),
            investigation_checkpoint_path=Path(os.getenv("INVESTIGATION_CHECKPOINT_PATH", str(DEFAULT_RUNTIME / "checkpoints.sqlite3"))),
            support_manifest_path=Path(os.getenv("OFFICIAL_SUPPORT_MANIFEST", str(agent_root / "resources/support/manifest.json"))),
            agent_resource_root=agent_root,
            support_capture_root=Path(os.getenv("OFFICIAL_SUPPORT_CAPTURE_ROOT", str(agent_root / "evidence/support"))),
            investigation_worker_enabled=os.getenv("INVESTIGATION_WORKER_ENABLED", "true").lower() == "true",
            access_mode=os.getenv("ACCESS_MODE", "local_development"),
            auth_issuer=os.getenv("AUTH_JWT_ISSUER"),
            auth_audience=os.getenv("AUTH_JWT_AUDIENCE"),
            auth_tenant_id=os.getenv("AUTH_TENANT_ID"),
            auth_public_key_path=Path(os.environ["AUTH_JWT_PUBLIC_KEY_PATH"]) if os.getenv("AUTH_JWT_PUBLIC_KEY_PATH") else None,
            auth_max_token_lifetime_seconds=int(os.getenv("AUTH_MAX_TOKEN_LIFETIME_SECONDS", "3600")),
        )
