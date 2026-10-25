"""Single-institution API boundary. Token issuance/SSO remains external to this service."""
from dataclasses import dataclass
from contextlib import closing
import re
import sqlite3

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

PERMISSIONS = frozenset({"read", "investigate", "review", "admin"})
LOCAL_PEERS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    permissions: frozenset[str]
    mode: str

    def audit_actor(self):
        return {"subject": self.subject, "tenant_id": self.tenant_id, "auth_mode": self.mode}


class AccessController:
    def __init__(self, settings):
        self.settings = settings
        self.public_key = None
        if settings.access_mode == "institution":
            # A mounted public key avoids outbound discovery and never accepts a
            # signing secret or the untrusted JWT header as the algorithm policy.
            try:
                key = load_pem_public_key(settings.auth_public_key_path.read_bytes())
                if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                    raise ValueError("RSA public key must be at least 2048 bits")
                self.public_key = key
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("institution authentication public key is unavailable or invalid") from exc

    def status(self):
        return {"mode": self.settings.access_mode,
                "authentication": "verified_rs256_jwt" if self.public_key else "loopback_development_only",
                "tenant_isolation": "single_institution_instance_and_database" if self.public_key else "development_only",
                "sso_integration": "not_verified", "permissions": sorted(PERMISSIONS)}

    def authenticate(self, request):
        if self.settings.access_mode == "local_development":
            if request.client is None or request.client.host not in LOCAL_PEERS:
                raise HTTPException(403, detail={"code": "local_development_only"})
            allowed_hosts = {"127.0.0.1", "::1", "localhost"}
            if request.client.host == "testclient":
                allowed_hosts.add("testserver")
            try:
                local_host = len(request.headers.getlist("host")) == 1 and request.url.hostname in allowed_hosts
            except ValueError:
                local_host = False
            forwarded = any(name.lower() == "forwarded" or name.lower().startswith("x-forwarded-")
                            or name.lower() in ("x-real-ip", "via") for name in request.headers)
            if not local_host or forwarded:
                raise HTTPException(403, detail={"code": "local_development_only"})
            return Principal("local-developer", "local-development", PERMISSIONS, "local_development")
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise HTTPException(401, detail={"code": "authentication_required"})
        scheme, _, token = values[0].partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 16384:
            raise HTTPException(401, detail={"code": "invalid_access_token"})
        try:
            claims = jwt.decode(token, self.public_key, algorithms=["RS256"],
                audience=self.settings.auth_audience, issuer=self.settings.auth_issuer,
                options={"require": ["iss", "aud", "exp", "iat", "sub", "tenant_id"], "strict_aud": True})
            subject, permissions = claims["sub"], claims.get("permissions", [])
            if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
                raise ValueError("invalid_subject")
            if claims["tenant_id"] != self.settings.auth_tenant_id:
                raise ValueError("wrong_tenant")
            if any(type(claims[key]) not in (int, float) for key in ("exp", "iat")):
                raise ValueError("invalid_token_time")
            if not 0 < claims["exp"] - claims["iat"] <= self.settings.auth_max_token_lifetime_seconds:
                raise ValueError("invalid_token_lifetime")
            if not isinstance(permissions, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions):
                raise ValueError("invalid_permissions")
            return Principal(subject, claims["tenant_id"], frozenset(permissions), "institution")
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(401, detail={"code": "invalid_access_token"}) from exc

    @staticmethod
    def required_permission(method, path):
        if path == "/api/security/configuration":
            return "admin"
        if method in ("GET", "HEAD"):
            return "read"
        if method == "POST" and path.startswith("/api/investigations"):
            return "review" if path.rstrip("/").endswith(("/reviews", "/followups")) else "investigate"
        if method == "POST" and path == "/api/runs":
            return "investigate"
        if method == "POST" and (path.rstrip("/") == "/api/model/analysis-reports" or
                                 re.fullmatch(r"/api/model/analysis-reports/[^/]+/investigate/?", path)):
            return "investigate"
        if ((method == "PUT" and re.fullmatch(r"/api/model/analysis-reports/[^/]+/response-workspace/?", path)) or
                (method == "POST" and re.fullmatch(r"/api/model/analysis-reports/[^/]+/response-reports/?", path))):
            return "investigate"
        return "admin"


def install_security(application, settings, access=None):
    access = access or AccessController(settings)
    application.state.access_controller = access

    @application.middleware("http")
    async def enforce_access(request, call_next):
        if request.url.path.startswith("/api/") and request.method != "OPTIONS":
            try:
                principal = access.authenticate(request)
                permission = access.required_permission(request.method, request.url.path)
                if permission not in principal.permissions and "admin" not in principal.permissions:
                    raise HTTPException(403, detail={"code": "permission_denied", "required_permission": permission})
                request.state.principal = principal
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {})
        response = await call_next(request)
        response.headers["X-Hermes-Access-Mode"] = settings.access_mode
        return response

    @application.get("/api/security/context")
    def context(request: Request):
        return {**access.status(), "actor": request.state.principal.audit_actor(),
                "granted_permissions": sorted(request.state.principal.permissions)}

    @application.get("/api/security/configuration")
    def configuration():
        return {**access.status(), "tenant_id": settings.auth_tenant_id}


def _bind_database(db, settings, data_tables):
    db.execute("CREATE TABLE IF NOT EXISTS service_security_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    bound = db.execute("SELECT value FROM service_security_metadata WHERE key='tenant_id'").fetchone()
    if bound:
        if settings.access_mode != "institution" or bound["value"] != settings.auth_tenant_id:
            raise ValueError("database_tenant_binding_mismatch")
    elif settings.access_mode == "institution":
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # The table names are server-owned constants, never request values.
        if any(db.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() for name in data_tables if name in existing):
            raise ValueError("existing_development_database_requires_reviewed_migration")
        db.execute("INSERT INTO service_security_metadata VALUES('tenant_id',?)", (settings.auth_tenant_id,))


def bind_investigation_tenant(repository, settings):
    """Business and checkpoint stores cannot change tenant or reopen as dev."""
    with repository.connection(write=True) as db:
        _bind_database(db, settings, ("cases",))
    settings.investigation_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.investigation_checkpoint_path)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute("BEGIN IMMEDIATE")
            _bind_database(db, settings, ("checkpoints", "writes"))
