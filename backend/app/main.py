from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
import os
import hashlib
import hmac
import secrets
import time
import json
import math
import re
from collections import defaultdict

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel, Field, model_validator
from backend.app.live_research import clean_query_term, plan_research_queries

try:
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_requests
except Exception:
    google_id_token = None
    google_requests = None

import httpx
from sqlalchemy import create_engine, String, Float, DateTime, ForeignKey, Text, Boolean, Integer, select, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker

APP_VERSION = "1.6.0"
BUILD_VERSION = "1.7.0-intelligence-v0.1"
INTELLIGENCE_MODEL_VERSION = "iei-0.1"
LIVE_RESEARCH_EXPERIMENT_VERSION = "lre-0.1"
LIVE_RESEARCH_MAX_HYPOTHESES = 3
LIVE_RESEARCH_MAX_QUERIES = 6
INTELLIGENCE_WINDOW_DAYS = 90
INTELLIGENCE_MIN_DAYS = 8
INTELLIGENCE_MAX_HYPOTHESES = 40
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nexgene.db")
DEV_MODE = os.getenv("DEV_MODE", "true").lower() == "true"
SECRET_KEY = os.getenv("SECRET_KEY")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_DAYS = 7
RATE_WINDOW = 60
MAX_LOGIN_ATTEMPTS = 5
MAX_REGISTER_ATTEMPTS = 10
MAX_RESET_ATTEMPTS = 5
MAX_VERIFY_ATTEMPTS = 10
MAX_CHECKIN_KEYS = 16
MAX_VALUE_LENGTH = 256
MAX_REQUEST_BYTES = 16 * 1024
MAX_BIOMARKER_BATCH = 24
MAX_EVIDENCE_BATCH = 100
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
AI_ANALYSIS_ENABLED = os.getenv("AI_ANALYSIS_ENABLED", "false").lower() == "true"
CLINICAL_DATABASE_URL = os.getenv("CLINICAL_DATABASE_URL", "sqlite:///./nexgene_clinical.db")
CLINICAL_PROVIDER_KEY = os.getenv("CLINICAL_PROVIDER_KEY", "").strip()
CLINICAL_PROVIDER_ID = os.getenv("CLINICAL_PROVIDER_ID", "development-provider").strip()
GENOMIC_DATABASE_URL = os.getenv("GENOMIC_DATABASE_URL", "sqlite:///./nexgene_genomic.db")
GENOMIC_PROVIDER_KEY = os.getenv("GENOMIC_PROVIDER_KEY", "").strip()
GENOMIC_PROVIDER_ID = os.getenv("GENOMIC_PROVIDER_ID", "development-genomics-provider").strip()
LIVE_CONTEXT_ENABLED = os.getenv("LIVE_CONTEXT_ENABLED", "true").lower() == "true"
EVIDENCE_LIVE_ENABLED = os.getenv("EVIDENCE_LIVE_ENABLED", "true").lower() == "true"
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "nexgene-local@example.invalid").strip()
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO", NCBI_EMAIL).strip()

# Production must never start with development secrets or insecure cookies.
if not DEV_MODE:
    if not SECRET_KEY or len(SECRET_KEY) < 32 or SECRET_KEY == "nexgene-dev-secret-change-me":
        raise RuntimeError("SECRET_KEY must be a strong random value of at least 32 characters when DEV_MODE=false")
    if not COOKIE_SECURE:
        raise RuntimeError("COOKIE_SECURE=true is required when DEV_MODE=false")
elif not SECRET_KEY:
    # Local-only fallback. It is deliberately not acceptable in production.
    SECRET_KEY = "nexgene-local-development-secret-only"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine)
pwd = CryptContext(
    schemes=["pbkdf2_sha256"],
    pbkdf2_sha256__default_rounds=600000,
    deprecated="auto",
)

# Used only to make login timing similar when an email does not exist.
DUMMY_PASSWORD_HASH = "$pbkdf2-sha256$600000$9PBQ3N3VVpJN.thwvCU_MQ$DVs5WExtR1AqiTUGZRi4Mo5gcARwsRsGlq.YmryBjUM"

app = FastAPI(
    title="NexGene API",
    version=APP_VERSION,
    docs_url="/docs" if DEV_MODE else None,
    redoc_url="/redoc" if DEV_MODE else None,
    openapi_url="/openapi.json" if DEV_MODE else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.middleware("http")
async def request_size_limit(request: Request, call_next):
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_REQUEST_BYTES:
                return Response("Request body too large", status_code=413)
        except ValueError:
            return Response("Invalid Content-Length", status_code=400)
    return await call_next(request)


class Base(DeclarativeBase):
    pass


class ClinicalBase(DeclarativeBase):
    pass


class GenomicBase(DeclarativeBase):
    pass


clinical_engine = create_engine(
    CLINICAL_DATABASE_URL,
    connect_args={"check_same_thread": False} if CLINICAL_DATABASE_URL.startswith("sqlite") else {},
)
ClinicalSessionLocal = sessionmaker(bind=clinical_engine)

genomic_engine = create_engine(
    GENOMIC_DATABASE_URL,
    connect_args={"check_same_thread": False} if GENOMIC_DATABASE_URL.startswith("sqlite") else {},
)
GenomicSessionLocal = sessionmaker(bind=genomic_engine)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    ai_analysis_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class SessionToken(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    age_range: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    occupation: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    student: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    study_field: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    schedule: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    location_city: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    live_context_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ClinicalAccessGrant(Base):
    __tablename__ = "clinical_access_grants"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(120), index=True)
    subject_ref: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    scopes: Mapped[str] = mapped_column(Text, default='["read"]')
    user_granted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_granted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ClinicalRecord(ClinicalBase):
    __tablename__ = "clinical_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_ref: Mapped[str] = mapped_column(String(160), index=True)
    provider_id: Mapped[str] = mapped_column(String(120), index=True)
    record_type: Mapped[str] = mapped_column(String(80), index=True)
    code: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    display: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(40), default="final")
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_system: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    clinical_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GenomicAccessGrant(Base):
    __tablename__ = "genomic_access_grants"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(120), index=True)
    subject_ref: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    scopes: Mapped[str] = mapped_column(Text, default='["read"]')
    user_granted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_granted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GenomicVariant(GenomicBase):
    __tablename__ = "genomic_variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_ref: Mapped[str] = mapped_column(String(160), index=True)
    provider_id: Mapped[str] = mapped_column(String(120), index=True)
    gene: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    variant_id: Mapped[str] = mapped_column(String(200), index=True)
    zygosity: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    classification: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    significance: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    source_system: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")


class PolygenicScore(GenomicBase):
    __tablename__ = "polygenic_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_ref: Mapped[str] = mapped_column(String(160), index=True)
    provider_id: Mapped[str] = mapped_column(String(120), index=True)
    trait_code: Mapped[str] = mapped_column(String(120), index=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    percentile: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reference_population: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")


class OneTimeToken(Base):
    __tablename__ = "one_time_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    id: Mapped[int] = mapped_column(primary_key=True)
    bucket_key: Mapped[str] = mapped_column(String(320), index=True)
    window_start: Mapped[int] = mapped_column(index=True)
    count: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("bucket_key", "window_start", name="uq_rate_bucket"),)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    value_numeric: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BiomarkerSignal(Base):
    __tablename__ = "biomarker_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    marker_code: Mapped[str] = mapped_column(String(100), index=True)
    marker_name: Mapped[str] = mapped_column(String(160))
    result_mode: Mapped[str] = mapped_column(String(24), default="deviation")
    direction: Mapped[str] = mapped_column(String(24), default="within")
    significance: Mapped[str] = mapped_column(String(24), default="none")
    value_numeric: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    reference_type: Mapped[str] = mapped_column(String(40), default="personal_baseline")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quality: Mapped[str] = mapped_column(String(24), default="good")
    source_type: Mapped[str] = mapped_column(String(40), default="molecular_sensor")
    device_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    calibration_version: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    citation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    published_year: Mapped[Optional[int]] = mapped_column(nullable=True)
    evidence_grade: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    topic_tags: Mapped[str] = mapped_column(Text, default="[]")
    abstract_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class InferenceRecord(Base):
    __tablename__ = "inference_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    inference_type: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(32), default="observed")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class LearningRun(Base):
    __tablename__ = "learning_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model_family: Mapped[str] = mapped_column(String(100), index=True)
    model_version: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="completed")
    input_window_days: Mapped[int] = mapped_column(Integer, default=90)
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PhenotypeDefinition(Base):
    __tablename__ = "phenotype_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(200))
    definition_json: Mapped[str] = mapped_column(Text, default="{}")
    model_version: Mapped[str] = mapped_column(String(120), default=INTELLIGENCE_MODEL_VERSION)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PhenotypeObservation(Base):
    __tablename__ = "phenotype_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    phenotype_code: Mapped[str] = mapped_column(String(120), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    model_version: Mapped[str] = mapped_column(String(120), default=INTELLIGENCE_MODEL_VERSION)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class IntelligenceRun(Base):
    __tablename__ = "intelligence_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model_family: Mapped[str] = mapped_column(String(100), index=True)
    model_version: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="completed")
    input_window_days: Mapped[int] = mapped_column(Integer, default=90)
    data_quality: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    id: Mapped[int] = mapped_column(primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subject_scope: Mapped[str] = mapped_column(String(120), default="individual")
    exposure: Mapped[str] = mapped_column(String(120), index=True)
    outcome: Mapped[str] = mapped_column(String(120), index=True)
    time_window: Mapped[str] = mapped_column(String(80), default="daily")
    lag: Mapped[int] = mapped_column(Integer, default=0)
    population_scope: Mapped[str] = mapped_column(String(160), default="this_person")
    mechanistic_rationale: Mapped[str] = mapped_column(Text, default="")
    alternative_explanations_json: Mapped[str] = mapped_column(Text, default="[]")
    predictions_json: Mapped[str] = mapped_column(Text, default="[]")
    falsifiers_json: Mapped[str] = mapped_column(Text, default="[]")
    required_data_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    statistical_test_json: Mapped[str] = mapped_column(Text, default="{}")
    effect_estimate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uncertainty_json: Mapped[str] = mapped_column(Text, default="{}")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="GENERATED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    model_version: Mapped[str] = mapped_column(String(120), default=INTELLIGENCE_MODEL_VERSION)


class HypothesisTest(Base):
    __tablename__ = "hypothesis_tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(String(80), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("intelligence_runs.id"), index=True)
    method: Mapped[str] = mapped_column(String(120))
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    effect_estimate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    q_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ci_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ci_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    data_quality: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    robustness_json: Mapped[str] = mapped_column(Text, default="{}")
    result_status: Mapped[str] = mapped_column(String(32), default="INCONCLUSIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(String(80), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    target_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    target_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exposure_condition: Mapped[str] = mapped_column(Text)
    predicted_outcome: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_direction: Mapped[str] = mapped_column(String(32))
    baseline_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    evaluation_json: Mapped[str] = mapped_column(Text, default="{}")
    model_version: Mapped[str] = mapped_column(String(120), default=INTELLIGENCE_MODEL_VERSION)


class SourceRecord(Base):
    """User-owned provenance node for fragmented health information."""
    __tablename__ = "source_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    label: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    reliability: Mapped[str] = mapped_column(String(32), default="unknown")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SourceDocument(Base):
    """Metadata-only document manifest. Raw documents are intentionally not stored here."""
    __tablename__ = "source_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    document_type: Mapped[str] = mapped_column(String(80), index=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ObservationSourceLink(Base):
    """Explicit provenance link between an observation and its originating source."""
    __tablename__ = "observation_source_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observations.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    role: Mapped[str] = mapped_column(String(32), default="origin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class EvidenceSnapshot(Base):
    """Persisted, user-scoped metadata snapshot from live evidence retrieval."""
    __tablename__ = "evidence_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(240), index=True)
    title: Mapped[str] = mapped_column(String(500))
    published: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    journal: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1200), nullable=True)
    query: Mapped[str] = mapped_column(String(300), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


Base.metadata.create_all(engine)
ClinicalBase.metadata.create_all(clinical_engine)
GenomicBase.metadata.create_all(genomic_engine)

# Lightweight development migration for upgrading an existing v0.7.1/v0.8.0 SQLite volume.
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import inspect, text as sql_text
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("users")} if "users" in tables else set()
    if "email_verified" not in cols and "users" in tables:
        with engine.begin() as conn:
            conn.execute(sql_text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 0"))
    cols = {c["name"] for c in insp.get_columns("users")} if "users" in tables else set()
    if "google_sub" not in cols and "users" in tables:
        with engine.begin() as conn:
            conn.execute(sql_text("ALTER TABLE users ADD COLUMN google_sub VARCHAR(255)"))
    if "evidence_snapshots" in tables:
        evidence_cols = {c["name"] for c in insp.get_columns("evidence_snapshots")}
        if "user_id" not in evidence_cols:
            with engine.begin() as conn:
                conn.execute(sql_text("ALTER TABLE evidence_snapshots ADD COLUMN user_id INTEGER"))
    cols = {c["name"] for c in insp.get_columns("users")} if "users" in tables else set()
    if "ai_analysis_enabled" not in cols and "users" in tables:
        with engine.begin() as conn:
            conn.execute(sql_text("ALTER TABLE users ADD COLUMN ai_analysis_enabled BOOLEAN DEFAULT 0"))
    if "users" in tables:
        with engine.begin() as conn:
            conn.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub_unique ON users(google_sub) WHERE google_sub IS NOT NULL"))
    if "profiles" in tables:
        pcols = {c["name"] for c in insp.get_columns("profiles")}
        with engine.begin() as conn:
            if "location_city" not in pcols:
                conn.execute(sql_text("ALTER TABLE profiles ADD COLUMN location_city VARCHAR(160)"))
            if "live_context_enabled" not in pcols:
                conn.execute(sql_text("ALTER TABLE profiles ADD COLUMN live_context_enabled BOOLEAN DEFAULT 1"))


class AuthIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)


class GoogleAuthIn(BaseModel):
    credential: str = Field(min_length=20, max_length=8192)


class AISettingsIn(BaseModel):
    enabled: bool


class ProfileIn(BaseModel):
    age_range: Optional[str] = Field(default=None, max_length=32)
    country: Optional[str] = Field(default=None, max_length=100)
    occupation: Optional[str] = Field(default=None, max_length=120)
    student: Optional[bool] = None
    study_field: Optional[str] = Field(default=None, max_length=120)
    schedule: Optional[str] = Field(default=None, max_length=80)
    timezone: Optional[str] = Field(default=None, max_length=64)
    location_city: Optional[str] = Field(default=None, max_length=160)
    live_context_enabled: bool = True

    @model_validator(mode="after")
    def normalize(self):
        for name in ("age_range", "country", "occupation", "study_field", "schedule", "timezone", "location_city"):
            value = getattr(self, name)
            if value is not None:
                value = value.strip()
                setattr(self, name, value or None)
        return self


class ResetIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=12, max_length=128)


ALLOWED_OBSERVATIONS = {
    "sleep_duration", "sleep_quality", "energy", "mood", "stress", "focus",
    "activity_level", "activity_duration", "morning_context", "diet_quality",
    "caffeine", "alcohol", "nicotine", "weight", "heart_rate", "hrv",
    "blood_pressure_systolic", "blood_pressure_diastolic", "glucose", "temperature",
}


class CheckinIn(BaseModel):
    values: dict[str, float | str]

    @model_validator(mode="after")
    def validate_values(self):
        if len(self.values) > MAX_CHECKIN_KEYS:
            raise ValueError(f"A check-in may contain at most {MAX_CHECKIN_KEYS} values")
        for kind, value in self.values.items():
            if kind not in ALLOWED_OBSERVATIONS:
                raise ValueError("Unsupported observation kind")
            if not isinstance(kind, str) or len(kind) > 80:
                raise ValueError("Invalid observation kind")
            if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
                raise ValueError("Observation value is too long")
        return self


class BiomarkerSignalIn(BaseModel):
    marker_code: str = Field(min_length=1, max_length=100)
    marker_name: str = Field(min_length=1, max_length=160)
    result_mode: Literal["deviation", "quantitative"] = "deviation"
    direction: Literal["low", "within", "elevated", "unknown"] = "within"
    significance: Literal["none", "mild", "significant", "unknown"] = "none"
    value_numeric: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=40)
    reference_type: Literal["personal_baseline", "population_reference", "laboratory_reference", "unknown"] = "personal_baseline"
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    quality: Literal["good", "limited", "poor", "unknown"] = "good"
    source_type: Literal["molecular_sensor", "wearable", "laboratory", "hospital", "manual"] = "molecular_sensor"
    device_id: Optional[str] = Field(default=None, max_length=160)
    calibration_version: Optional[str] = Field(default=None, max_length=80)
    recorded_at: Optional[datetime] = None


class BiomarkerBatchIn(BaseModel):
    signals: list[BiomarkerSignalIn] = Field(min_length=1, max_length=MAX_BIOMARKER_BATCH)


class LiveResearchRunIn(BaseModel):
    """Bounded live-evidence experiment; accepts only the user's own hypothesis IDs."""
    hypothesis_ids: list[str] = Field(default_factory=list, max_length=LIVE_RESEARCH_MAX_HYPOTHESES)
    max_queries: int = Field(default=3, ge=1, le=LIVE_RESEARCH_MAX_QUERIES)
    results_per_query: int = Field(default=5, ge=1, le=10)


class EvidenceSourceIn(BaseModel):
    source_type: Literal["guideline", "research", "medical_report", "clinical_reference", "review"]
    title: str = Field(min_length=1, max_length=300)
    publisher: Optional[str] = Field(default=None, max_length=200)
    citation: Optional[str] = Field(default=None, max_length=4000)
    url: Optional[str] = Field(default=None, max_length=1000)
    published_year: Optional[int] = Field(default=None, ge=1800, le=2200)
    evidence_grade: Optional[str] = Field(default=None, max_length=40)
    topic_tags: list[str] = Field(default_factory=list, max_length=20)
    abstract_summary: Optional[str] = Field(default=None, max_length=8000)


class EvidenceBatchIn(BaseModel):
    sources: list[EvidenceSourceIn] = Field(min_length=1, max_length=MAX_EVIDENCE_BATCH)


SOURCE_TYPES = {"self_reported", "document", "lab", "clinical", "wearable", "genomic", "molecular", "other"}


class SourceIn(BaseModel):
    source_type: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    reliability: str = Field(default="unknown", max_length=32)
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source(self):
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("Unsupported source type")
        return self


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    document_type: str = Field(min_length=1, max_length=80)
    issuer: Optional[str] = Field(default=None, max_length=200)
    issued_at: Optional[datetime] = None
    checksum_sha256: Optional[str] = Field(default=None, min_length=64, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)
    reliability: str = Field(default="reported", max_length=32)


class GenomicConsentIn(BaseModel):
    provider_id: str = Field(min_length=1, max_length=120)
    scopes: list[Literal["read"]] = Field(default_factory=lambda: ["read"], max_length=4)


class GenomicProviderGrantIn(BaseModel):
    subject_ref: str = Field(min_length=20, max_length=160)
    provider_id: str = Field(min_length=1, max_length=120)


class GenomicVariantIn(BaseModel):
    subject_ref: str = Field(min_length=20, max_length=160)
    provider_id: str = Field(min_length=1, max_length=120)
    gene: Optional[str] = Field(default=None, max_length=120)
    variant_id: str = Field(min_length=1, max_length=200)
    zygosity: Optional[str] = Field(default=None, max_length=40)
    classification: Optional[str] = Field(default=None, max_length=80)
    significance: Optional[str] = Field(default=None, max_length=80)
    source_system: Optional[str] = Field(default=None, max_length=200)
    observed_at: Optional[datetime] = None
    provenance: dict = Field(default_factory=dict)


class GenomicVariantBatchIn(BaseModel):
    variants: list[GenomicVariantIn] = Field(min_length=1, max_length=100)


class PolygenicScoreIn(BaseModel):
    subject_ref: str = Field(min_length=20, max_length=160)
    provider_id: str = Field(min_length=1, max_length=120)
    trait_code: str = Field(min_length=1, max_length=120)
    score: Optional[float] = None
    percentile: Optional[float] = Field(default=None, ge=0, le=100)
    reference_population: Optional[str] = Field(default=None, max_length=200)
    model_version: Optional[str] = Field(default=None, max_length=120)
    observed_at: Optional[datetime] = None
    provenance: dict = Field(default_factory=dict)


class PolygenicScoreBatchIn(BaseModel):
    scores: list[PolygenicScoreIn] = Field(min_length=1, max_length=100)


class ClinicalConsentIn(BaseModel):
    provider_id: str = Field(min_length=1, max_length=120)
    scopes: list[Literal["read"]] = Field(default_factory=lambda: ["read"], max_length=4)


class ClinicalProviderGrantIn(BaseModel):
    subject_ref: str = Field(min_length=20, max_length=160)
    provider_id: str = Field(min_length=1, max_length=120)


class ClinicalRecordIn(BaseModel):
    subject_ref: str = Field(min_length=20, max_length=160)
    provider_id: str = Field(min_length=1, max_length=120)
    record_type: Literal["diagnosis", "symptom", "medication", "lab", "imaging", "pathology", "procedure", "hospitalization", "clinical_note"]
    code: Optional[str] = Field(default=None, max_length=120)
    display: str = Field(min_length=1, max_length=300)
    status: str = Field(default="final", max_length=40)
    summary: Optional[str] = Field(default=None, max_length=8000)
    source_system: Optional[str] = Field(default=None, max_length=200)
    external_id: Optional[str] = Field(default=None, max_length=200)
    clinical_date: Optional[datetime] = None
    payload: dict = Field(default_factory=dict)


class ClinicalRecordBatchIn(BaseModel):
    records: list[ClinicalRecordIn] = Field(min_length=1, max_length=24)


def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def clinical_db():
    s = ClinicalSessionLocal()
    try:
        yield s
    finally:
        s.close()


def clinical_provider_auth(request: Request):
    if not CLINICAL_PROVIDER_KEY:
        raise HTTPException(503, "Clinical provider integration is not configured")
    supplied = request.headers.get("X-Clinical-Provider-Key", "")
    if not supplied or not hmac.compare_digest(supplied, CLINICAL_PROVIDER_KEY):
        raise HTTPException(401, "Clinical provider authentication required")
    return CLINICAL_PROVIDER_ID


def _clinical_grant(s: Session, user_id: int, provider_id: str):
    return s.scalar(select(ClinicalAccessGrant).where(
        ClinicalAccessGrant.user_id == user_id,
        ClinicalAccessGrant.provider_id == provider_id,
        ClinicalAccessGrant.revoked_at.is_(None),
    ))


def _clinical_dual_authorized(grant: Optional[ClinicalAccessGrant]):
    return bool(grant and grant.user_granted_at and grant.provider_granted_at and not grant.revoked_at)


def genomic_provider_auth(request: Request):
    if not GENOMIC_PROVIDER_KEY:
        raise HTTPException(503, "Genomic provider integration is not configured")
    supplied = request.headers.get("X-Genomic-Provider-Key", "")
    if not supplied or not hmac.compare_digest(supplied, GENOMIC_PROVIDER_KEY):
        raise HTTPException(401, "Genomic provider authentication required")
    return GENOMIC_PROVIDER_ID


def _genomic_dual_authorized(grant: Optional[GenomicAccessGrant]):
    return bool(grant and grant.user_granted_at and grant.provider_granted_at and not grant.revoked_at)


def genomic_db():
    s = GenomicSessionLocal()
    try:
        yield s
    finally:
        s.close()


def now():
    return datetime.now(timezone.utc)


def hash_token(v: str):
    return hashlib.sha256(v.encode()).hexdigest()


def valid_password(p: str):
    return (
        len(p) >= 12
        and any(c.islower() for c in p)
        and any(c.isupper() for c in p)
        and any(c.isdigit() for c in p)
        and any(not c.isalnum() for c in p)
    )


def issue_session(u: User, s: Session):
    raw = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    s.add(
        SessionToken(
            user_id=u.id,
            token_hash=hash_token(raw),
            csrf_hash=hash_token(csrf),
            expires_at=now() + timedelta(days=SESSION_DAYS),
        )
    )
    s.commit()
    return raw, csrf


def set_auth_cookies(response: Response, raw: str, csrf_token: str):
    response.set_cookie(
        "nexgene_session", raw, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", max_age=SESSION_DAYS * 86400, path="/"
    )
    response.set_cookie(
        "nexgene_csrf", csrf_token, httponly=False, secure=COOKIE_SECURE,
        samesite="lax", max_age=SESSION_DAYS * 86400, path="/"
    )


def get_session(request: Request, s: Session):
    raw = request.cookies.get("nexgene_session")
    if not raw:
        return None
    st = s.scalar(
        select(SessionToken).where(
            SessionToken.token_hash == hash_token(raw),
            SessionToken.revoked_at.is_(None),
        )
    )
    if not st or st.expires_at < now():
        return None
    return st


def require_csrf(request: Request, st: SessionToken):
    token = request.headers.get("X-CSRF-Token")
    cookie = request.cookies.get("nexgene_csrf")
    if not token or not cookie or hash_token(token) != st.csrf_hash or not secrets.compare_digest(token, cookie):
        raise HTTPException(403, "CSRF validation failed")


def current_user(request: Request, s: Session = Depends(db)):
    st = get_session(request, s)
    if not st:
        raise HTTPException(401, "Authentication required")
    return s.get(User, st.user_id)


def current_session(request: Request, s: Session = Depends(db)):
    st = get_session(request, s)
    if not st:
        raise HTTPException(401, "Authentication required")
    return st


def rate_limit(request: Request, key: str, limit: int, s: Session):
    ip = request.client.host if request.client else "unknown"
    bucket_key = f"{key}:{ip}"
    window_start = int(time.time()) // RATE_WINDOW
    row = s.scalar(
        select(RateLimitBucket).where(
            RateLimitBucket.bucket_key == bucket_key,
            RateLimitBucket.window_start == window_start,
        )
    )
    if row is None:
        row = RateLimitBucket(bucket_key=bucket_key, window_start=window_start, count=0)
        s.add(row)
        try:
            s.flush()
        except Exception:
            s.rollback()
            row = s.scalar(
                select(RateLimitBucket).where(
                    RateLimitBucket.bucket_key == bucket_key,
                    RateLimitBucket.window_start == window_start,
                )
            )
    if row.count >= limit:
        s.commit()
        raise HTTPException(429, "Too many attempts. Please try again shortly.")
    row.count += 1
    s.commit()
    # Opportunistic cleanup keeps this shared limiter small.
    if window_start % 20 == 0:
        s.query(RateLimitBucket).filter(RateLimitBucket.window_start < window_start - 5).delete(synchronize_session=False)
        s.commit()


def password_policy_or_400(password: str):
    if not valid_password(password):
        raise HTTPException(400, "Password must be 12+ characters and include uppercase, lowercase, number, and symbol.")



def create_session_for_user(u: User, response: Response, s: Session):
    token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    s.add(SessionToken(
        user_id=u.id,
        token_hash=hash_token(token),
        csrf_hash=hash_token(csrf_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
    ))
    s.commit()
    response.set_cookie(
        "nexgene_session", token, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", path="/", max_age=SESSION_DAYS * 86400
    )
    response.set_cookie(
        "nexgene_csrf", csrf_token, httponly=False, secure=COOKIE_SECURE,
        samesite="lax", path="/", max_age=SESSION_DAYS * 86400
    )


@app.get("/api/v1/auth/google/config")
def google_config():
    return {"enabled": bool(GOOGLE_CLIENT_ID), "client_id": GOOGLE_CLIENT_ID or None}


@app.post("/api/v1/auth/google")
def google_login(x: GoogleAuthIn, request: Request, response: Response, s: Session = Depends(db)):
    if not GOOGLE_CLIENT_ID or google_id_token is None or google_requests is None:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    rate_limit(request, "google_login", MAX_LOGIN_ATTEMPTS, s)
    try:
        info = google_id_token.verify_oauth2_token(
            x.credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Could not authenticate with Google")
    google_sub = info.get("sub")
    email = str(info.get("email", "")).strip().lower()
    email_verified = bool(info.get("email_verified"))
    if not google_sub or not email or not email_verified:
        raise HTTPException(status_code=401, detail="Could not authenticate with Google")
    u = s.scalar(select(User).where(User.google_sub == google_sub))
    if not u:
        u = s.scalar(select(User).where(User.email == email))
        if u:
            raise HTTPException(status_code=409, detail="An account already exists. Sign in with your password, then link Google.")
        u = User(email=email, password_hash=pwd.hash(secrets.token_urlsafe(32)), email_verified=True, google_sub=google_sub)
        s.add(u)
        s.commit()
        s.refresh(u)
    create_session_for_user(u, response, s)
    return {"status": "ok", "id": u.id, "email": u.email, "provider": "google"}


@app.post("/api/v1/auth/google/link")
def google_link(x: GoogleAuthIn, request: Request, response: Response, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    if not GOOGLE_CLIENT_ID or google_id_token is None or google_requests is None:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    require_csrf(request, st)
    try:
        info = google_id_token.verify_oauth2_token(x.credential, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not authenticate with Google")
    google_sub = info.get("sub")
    email = str(info.get("email", "")).strip().lower()
    if not google_sub or not email or not info.get("email_verified") or email != u.email:
        raise HTTPException(status_code=400, detail="Google account email must match your NexGene email")
    existing = s.scalar(select(User).where(User.google_sub == google_sub))
    if existing and existing.id != u.id:
        raise HTTPException(status_code=409, detail="That Google account is already linked")
    u.google_sub = google_sub
    u.email_verified = True
    s.commit()
    return {"status": "linked"}



@app.get("/api/v1/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/", include_in_schema=False)
def root():
    return FileResponse("mobile/index.html")


app.mount("/static", StaticFiles(directory="mobile"), name="mobile")


@app.get("/api/v1/auth/csrf")
def csrf(request: Request, response: Response, s: Session = Depends(db)):
    st = get_session(request, s)
    if st:
        new_token = secrets.token_urlsafe(32)
        st.csrf_hash = hash_token(new_token)
        s.commit()
        response.set_cookie(
            "nexgene_csrf", new_token, httponly=False, secure=COOKIE_SECURE,
            samesite="lax", path="/", max_age=SESSION_DAYS * 86400
        )
    return {"status": "ok"}


@app.post("/api/v1/auth/register")
def register(x: AuthIn, request: Request, response: Response, s: Session = Depends(db)):
    rate_limit(request, "register", MAX_REGISTER_ATTEMPTS, s)
    password_policy_or_400(x.password)
    email = x.email.strip().lower()
    if s.scalar(select(User).where(User.email == email)):
        # Uniform success-shaped response prevents simple registration enumeration.
        return {"status": "ok", "message": "If the account can be created, you can continue with NexGene."}
    u = User(email=email, password_hash=pwd.hash(x.password), email_verified=False)
    s.add(u)
    s.commit()
    s.refresh(u)
    raw, csrf_token = issue_session(u, s)
    set_auth_cookies(response, raw, csrf_token)
    verify_raw = secrets.token_urlsafe(32)
    s.add(OneTimeToken(user_id=u.id, token_hash=hash_token(verify_raw), purpose="verify", expires_at=now() + timedelta(hours=24)))
    s.commit()
    out = {"status": "created", "email_verified": False}
    if DEV_MODE:
        out["dev_verification_token"] = verify_raw
    return out


@app.post("/api/v1/auth/login")
def login(x: AuthIn, request: Request, response: Response, s: Session = Depends(db)):
    rate_limit(request, "login", MAX_LOGIN_ATTEMPTS, s)
    u = s.scalar(select(User).where(User.email == x.email.strip().lower()))
    if not u:
        pwd.verify(x.password, DUMMY_PASSWORD_HASH)
        raise HTTPException(401, "Invalid email or password")
    if not pwd.verify(x.password, u.password_hash):
        raise HTTPException(401, "Invalid email or password")
    raw, csrf_token = issue_session(u, s)
    set_auth_cookies(response, raw, csrf_token)
    return {"status": "authenticated", "email_verified": u.email_verified}


@app.post("/api/v1/auth/verify-email")
def verify_email(token: str, request: Request, response: Response, s: Session = Depends(db)):
    rate_limit(request, "verify", MAX_VERIFY_ATTEMPTS, s)
    row = s.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == hash_token(token),
            OneTimeToken.purpose == "verify",
            OneTimeToken.used_at.is_(None),
        )
    )
    if not row or row.expires_at < now():
        raise HTTPException(400, "Invalid or expired verification token")
    u = s.get(User, row.user_id)
    u.email_verified = True
    row.used_at = now()
    s.commit()
    return {"status": "verified"}


@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    st.revoked_at = now()
    s.commit()
    response.delete_cookie("nexgene_session", path="/")
    response.delete_cookie("nexgene_csrf", path="/")
    return {"status": "signed_out"}


@app.post("/api/v1/auth/password-reset/request")
def reset_request(x: ResetIn, request: Request, s: Session = Depends(db)):
    rate_limit(request, "reset", MAX_RESET_ATTEMPTS, s)
    u = s.scalar(select(User).where(User.email == x.email.strip().lower()))
    out = {"status": "ok"}
    if u:
        raw = secrets.token_urlsafe(32)
        s.add(OneTimeToken(user_id=u.id, token_hash=hash_token(raw), purpose="reset", expires_at=now() + timedelta(minutes=30)))
        s.commit()
        # Local development only. Production delivery must use email and never expose tokens in JSON.
        if DEV_MODE:
            out["dev_reset_token"] = raw
    return out


@app.post("/api/v1/auth/password-reset/confirm")
def reset_confirm(x: PasswordResetConfirm, request: Request, response: Response, s: Session = Depends(db)):
    rate_limit(request, "reset_confirm", MAX_RESET_ATTEMPTS, s)
    password_policy_or_400(x.password)
    row = s.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == hash_token(x.token),
            OneTimeToken.purpose == "reset",
            OneTimeToken.used_at.is_(None),
        )
    )
    if not row or row.expires_at < now():
        raise HTTPException(400, "Invalid or expired reset token")
    u = s.get(User, row.user_id)
    u.password_hash = pwd.hash(x.password)
    row.used_at = now()
    s.query(SessionToken).filter(SessionToken.user_id == u.id, SessionToken.revoked_at.is_(None)).update({"revoked_at": now()})
    s.commit()
    response.delete_cookie("nexgene_session", path="/")
    response.delete_cookie("nexgene_csrf", path="/")
    return {"status": "password_reset"}


@app.get("/api/v1/auth/me")
def me(u: User = Depends(current_user)):
    return {"id": u.id, "email": u.email, "email_verified": u.email_verified}


@app.get("/api/v1/profile")
def get_profile(u: User = Depends(current_user), s: Session = Depends(db)):
    p = s.scalar(select(Profile).where(Profile.user_id == u.id))
    if not p:
        return {"complete": False, "profile": {}}
    data = {
        "age_range": p.age_range, "country": p.country, "occupation": p.occupation,
        "student": p.student, "study_field": p.study_field, "schedule": p.schedule,
        "timezone": p.timezone,
    }
    complete = bool(p.country and (p.occupation or p.student is not None))
    return {"complete": complete, "profile": data}


@app.put("/api/v1/profile")
def update_profile(data: ProfileIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    p = s.scalar(select(Profile).where(Profile.user_id == u.id))
    if not p:
        p = Profile(user_id=u.id)
        s.add(p)
    for field in ("age_range", "country", "occupation", "student", "study_field", "schedule", "timezone"):
        setattr(p, field, getattr(data, field))
    p.updated_at = now()
    s.commit()
    return {"status": "saved", "complete": bool(p.country and (p.occupation or p.student is not None))}


def save_checkin(period, data, u, s):
    recorded = now()
    source = s.scalar(select(SourceRecord).where(
        SourceRecord.user_id == u.id,
        SourceRecord.source_type == "self_reported",
        SourceRecord.label == "NexGene check-ins",
        SourceRecord.status == "active",
    ))
    if source is None:
        source = SourceRecord(
            user_id=u.id, source_type="self_reported", label="NexGene check-ins",
            reliability="self_reported", metadata_json=json.dumps({"periods": ["morning", "evening"]}),
        )
        s.add(source)
        s.flush()
    observations = []
    for kind, value in data.values.items():
        observation = Observation(
            user_id=u.id,
            kind=kind,
            value_numeric=float(value) if isinstance(value, (int, float)) else None,
            value_text=None if isinstance(value, (int, float)) else str(value),
            recorded_at=recorded,
        )
        s.add(observation)
        observations.append(observation)
    s.flush()
    for observation in observations:
        s.add(ObservationSourceLink(user_id=u.id, observation_id=observation.id, source_id=source.id, role="origin"))
    s.commit()
    return {"saved": len(data.values), "period": period, "source_id": source.id}


@app.post("/api/v1/checkins/morning")
def morning(data: CheckinIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    return save_checkin("morning", data, u, s)


@app.post("/api/v1/checkins/evening")
def evening(data: CheckinIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    return save_checkin("evening", data, u, s)


@app.post("/api/v1/sources")
def create_source(data: SourceIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    source = SourceRecord(
        user_id=u.id, source_type=data.source_type, label=data.label.strip(),
        reliability=data.reliability.strip() or "unknown", metadata_json=json.dumps(data.metadata, ensure_ascii=False),
    )
    s.add(source); s.commit()
    return {"status": "created", "source_id": source.id, "source_type": source.source_type}


@app.post("/api/v1/sources/documents")
def create_document(data: DocumentIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    source = SourceRecord(
        user_id=u.id, source_type="document", label=data.title.strip(),
        reliability=data.reliability.strip() or "reported", metadata_json=json.dumps({"document_type": data.document_type}, ensure_ascii=False),
    )
    s.add(source); s.flush()
    doc = SourceDocument(
        user_id=u.id, source_id=source.id, title=data.title.strip(), document_type=data.document_type.strip(),
        issuer=data.issuer.strip() if data.issuer else None, issued_at=data.issued_at,
        checksum_sha256=data.checksum_sha256.lower() if data.checksum_sha256 else None, notes=data.notes.strip() if data.notes else None,
    )
    s.add(doc); s.commit()
    return {"status": "registered", "document_id": doc.id, "source_id": source.id, "storage": "metadata_only"}


@app.get("/api/v1/sources")
def list_sources(u: User = Depends(current_user), s: Session = Depends(db)):
    rows = s.scalars(select(SourceRecord).where(SourceRecord.user_id == u.id, SourceRecord.status == "active").order_by(SourceRecord.id.asc())).all()
    return [{"id": r.id, "source_type": r.source_type, "label": r.label, "reliability": r.reliability, "created_at": r.created_at.isoformat(), "metadata": json.loads(r.metadata_json or "{}")} for r in rows]


@app.get("/api/v1/baseline/living")
def living_baseline(u: User = Depends(current_user), s: Session = Depends(db)):
    since = now() - timedelta(days=90)
    observations = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.recorded_at >= since)).all()
    biomarkers = s.scalars(select(BiomarkerSignal).where(BiomarkerSignal.user_id == u.id, BiomarkerSignal.recorded_at >= since)).all()
    sources = s.scalars(select(SourceRecord).where(SourceRecord.user_id == u.id, SourceRecord.status == "active")).all()
    documents = s.scalars(select(SourceDocument).where(SourceDocument.user_id == u.id)).all()
    source_counts = defaultdict(int)
    for src in sources:
        source_counts[src.source_type] += 1
    active_days = len({r.recorded_at.date().isoformat() for r in observations})
    kinds = sorted({r.kind for r in observations})
    has_self_report = source_counts["self_reported"] > 0
    stage = "starting"
    if active_days >= 3 and len(kinds) >= 2:
        stage = "forming"
    if active_days >= 14 and len(kinds) >= 3:
        stage = "useful"
    if active_days >= 30 and len(kinds) >= 4:
        stage = "established"
    coverage = {
        "lifestyle": bool(has_self_report),
        "physiological": any(k in kinds for k in {"blood_pressure", "weight", "heart_rate", "hrv", "glucose"}) or source_counts["wearable"] > 0,
        "molecular": source_counts["molecular"] > 0 or len(biomarkers) > 0,
        "clinical": source_counts["clinical"] > 0,
        "genetic": source_counts["genomic"] > 0,
        "documents": len(documents) > 0,
    }
    next_layers = [k for k, present in coverage.items() if not present]
    return {
        "status": "active", "stage": stage,
        "principle": "Start with what the person has today and progressively build the longitudinal profile.",
        "window_days": 90, "active_days": active_days, "observation_count": len(observations),
        "observation_kinds": kinds, "biomarker_count": len(biomarkers), "document_count": len(documents),
        "source_counts": dict(source_counts), "coverage": coverage,
        "next_layers": next_layers,
        "limitations": ["A living baseline is descriptive and longitudinal; it is not a diagnosis.", "Source availability does not imply clinical completeness or clinical validation."],
    }


@app.get("/api/v1/today")
def today(u: User = Depends(current_user), s: Session = Depends(db)):
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.recorded_at >= since).order_by(Observation.recorded_at.desc())).all()
    out = {}
    for r in rows:
        out.setdefault(r.kind, r.value_numeric if r.value_numeric is not None else r.value_text)
    return out


@app.get("/api/v1/timeline")
def timeline(u: User = Depends(current_user), s: Session = Depends(db)):
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id).order_by(Observation.recorded_at.desc()).limit(300)).all()
    return [{"kind": r.kind, "value": r.value_numeric if r.value_numeric is not None else r.value_text, "recorded_at": r.recorded_at.isoformat()} for r in rows]


@app.get("/api/v1/patterns")
def patterns(days: int = 30, u: User = Depends(current_user), s: Session = Depends(db)):
    days = max(7, min(days, 90))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.recorded_at >= since).order_by(Observation.recorded_at.asc())).all()
    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.value_numeric is not None:
            buckets[r.recorded_at.date().isoformat()][r.kind].append(r.value_numeric)
    wanted = ["sleep_duration", "sleep_quality", "energy", "mood", "stress", "focus"]
    series = []
    for day, vals in buckets.items():
        point = {"date": day}
        for k in wanted:
            if vals.get(k):
                point[k] = round(sum(vals[k]) / len(vals[k]), 2)
        series.append(point)
    avgs = {}
    for k in wanted:
        allv = [r.value_numeric for r in rows if r.kind == k and r.value_numeric is not None]
        if allv:
            avgs[k] = round(sum(allv) / len(allv), 2)
    return {"days": days, "series": series, "averages": avgs, "observation_count": len(rows)}


@app.get("/api/v1/ai/settings")
def ai_settings(u: User = Depends(current_user), s: Session = Depends(db)):
    # AI is opt-in. We currently keep the preference in a lightweight per-user attribute
    # only when the schema is extended in a later migration; for this build, availability
    # is reported without changing user data until the user explicitly enables it.
    return {"available": bool(AI_ANALYSIS_ENABLED and OPENAI_API_KEY), "enabled": bool(u.ai_analysis_enabled)}


class AIReportIn(BaseModel):
    enabled: bool


@app.post("/api/v1/ai/settings")
def set_ai_settings(x: AIReportIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    if x.enabled and not (AI_ANALYSIS_ENABLED and OPENAI_API_KEY):
        raise HTTPException(status_code=503, detail="AI analysis is not configured")
    u.ai_analysis_enabled = bool(x.enabled)
    s.commit()
    return {"enabled": bool(u.ai_analysis_enabled), "available": bool(AI_ANALYSIS_ENABLED and OPENAI_API_KEY)}


@app.post("/api/v1/biomarkers/signals")
def add_biomarker_signals(data: BiomarkerBatchIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    recorded_default = now()
    rows = []
    for item in data.signals:
        if item.result_mode == "deviation" and item.value_numeric is not None:
            raise HTTPException(422, "Deviation signals should not masquerade as quantitative concentrations")
        rows.append(BiomarkerSignal(
            user_id=u.id, marker_code=item.marker_code.strip(), marker_name=item.marker_name.strip(),
            result_mode=item.result_mode, direction=item.direction, significance=item.significance,
            value_numeric=item.value_numeric, unit=item.unit, reference_type=item.reference_type,
            confidence=item.confidence, quality=item.quality, source_type=item.source_type,
            device_id=item.device_id, calibration_version=item.calibration_version,
            recorded_at=item.recorded_at or recorded_default,
        ))
    s.add_all(rows)
    s.commit()
    return {"status": "recorded", "saved": len(rows), "mode": "molecular_biomarker_signal"}


@app.get("/api/v1/biomarkers")
def biomarker_signals(days: int = 30, u: User = Depends(current_user), s: Session = Depends(db)):
    days = max(1, min(days, 365))
    since = now() - timedelta(days=days)
    rows = s.scalars(select(BiomarkerSignal).where(BiomarkerSignal.user_id == u.id, BiomarkerSignal.recorded_at >= since).order_by(BiomarkerSignal.recorded_at.desc()).limit(500)).all()
    return [{
        "marker_code": r.marker_code, "marker_name": r.marker_name, "result_mode": r.result_mode,
        "direction": r.direction, "significance": r.significance, "value_numeric": r.value_numeric,
        "unit": r.unit, "reference_type": r.reference_type, "confidence": r.confidence,
        "quality": r.quality, "source_type": r.source_type, "device_id": r.device_id,
        "calibration_version": r.calibration_version, "recorded_at": r.recorded_at.isoformat(),
    } for r in rows]


def _topic_tokens(text: str):
    return {x for x in text.lower().replace("/", " ").replace("-", " ").split() if len(x) > 3}


def _retrieve_evidence(s: Session, topics: set[str], limit: int = 8):
    rows = s.scalars(select(EvidenceSource).where(EvidenceSource.active.is_(True)).order_by(EvidenceSource.published_year.desc().nullslast(), EvidenceSource.id.desc()).limit(500)).all()
    scored = []
    for r in rows:
        tags = set()
        try: tags = set(json.loads(r.topic_tags))
        except Exception: pass
        corpus = _topic_tokens(r.title + " " + (r.abstract_summary or "")) | {str(x).lower() for x in tags}
        score = len(topics & corpus)
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: (x[0], x[1].published_year or 0, x[1].id), reverse=True)
    return [r for _, r in scored[:limit]]


@app.get("/api/v1/evidence/search")
def evidence_search(q: str = "", limit: int = 8, u: User = Depends(current_user), s: Session = Depends(db)):
    limit = max(1, min(limit, 20))
    topics = _topic_tokens(q)
    rows = _retrieve_evidence(s, topics, limit) if topics else s.scalars(select(EvidenceSource).where(EvidenceSource.active.is_(True)).order_by(EvidenceSource.published_year.desc().nullslast(), EvidenceSource.id.desc()).limit(limit)).all()
    return [{"id": r.id, "source_type": r.source_type, "title": r.title, "publisher": r.publisher, "citation": r.citation, "url": r.url, "published_year": r.published_year, "evidence_grade": r.evidence_grade, "topic_tags": json.loads(r.topic_tags or "[]"), "abstract_summary": r.abstract_summary} for r in rows]


@app.post("/api/v1/evidence/import")
def evidence_import(data: EvidenceBatchIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    if not DEV_MODE:
        raise HTTPException(404, "Not found")
    rows = []
    for item in data.sources:
        rows.append(EvidenceSource(
            source_type=item.source_type, title=item.title.strip(), publisher=item.publisher, citation=item.citation,
            url=item.url, published_year=item.published_year, evidence_grade=item.evidence_grade,
            topic_tags=json.dumps([x.strip().lower() for x in item.topic_tags if x.strip()][:20]),
            abstract_summary=item.abstract_summary,
        ))
    s.add_all(rows); s.commit()
    return {"status": "imported", "saved": len(rows), "dev_only": True}


def _daily_numeric(rows):
    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.value_numeric is not None:
            buckets[r.recorded_at.date().isoformat()][r.kind].append(float(r.value_numeric))
    daily = defaultdict(dict)
    for day, vals in buckets.items():
        for kind, values in vals.items():
            daily[day][kind] = sum(values) / len(values)
    return daily


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = (sum(x * x for x in dx) * sum(y * y for y in dy)) ** 0.5
    if den == 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / den


def _data_quality(rows, biomarkers, days=30):
    end = now()
    observed_days = {r.recorded_at.date().isoformat() for r in rows}
    kinds = sorted({r.kind for r in rows})
    quality_counts = defaultdict(int)
    for b in biomarkers:
        quality_counts[b.quality] += 1
    return {
        "window_days": days,
        "observed_days": len(observed_days),
        "coverage_ratio": round(len(observed_days) / max(days, 1), 3),
        "observation_count": len(rows),
        "observation_kinds": kinds,
        "biomarker_count": len(biomarkers),
        "biomarker_quality": dict(quality_counts),
        "limitations": [
            "Lifestyle observations are user-entered and may be incomplete.",
            "Associations are exploratory and do not establish causation.",
        ],
    }


def _mean(values):
    return sum(values) / len(values) if values else None


def _variance(values):
    if len(values) < 2:
        return None
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def _pearson_xy(xs, ys):
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return sum(a * b for a, b in zip(dx, dy)) / den if den else None


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for k in order[i:j]:
            ranks[k] = rank
        i = j
    return ranks


def _spearman(xs, ys):
    if len(xs) < 3:
        return None
    return _pearson_xy(_rank(xs), _rank(ys))


def _fisher_ci(r, n, z=1.96):
    if r is None or n <= 3 or abs(r) >= 1:
        return None, None
    clipped = max(-0.999999, min(0.999999, r))
    se = 1 / math.sqrt(n - 3)
    zr = 0.5 * math.log((1 + clipped) / (1 - clipped))
    lo, hi = zr - z * se, zr + z * se
    def inv(v):
        e = math.exp(2 * v)
        return (e - 1) / (e + 1)
    return inv(lo), inv(hi)


def _normal_p_from_r(r, n):
    if r is None or n < 4 or abs(r) >= 1:
        return 0.0 if r is not None and abs(r) >= 1 else 1.0
    # Two-sided normal approximation to the correlation t statistic.
    t = abs(r) * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
    return math.erfc(t / math.sqrt(2))


def _bh_adjust(pairs):
    ordered = sorted(enumerate(pairs), key=lambda x: x[1][1])
    out = [1.0] * len(pairs)
    prev = 1.0
    for rank_from_one, (idx, (_, p)) in enumerate(reversed(ordered), 1):
        rank = len(pairs) - rank_from_one + 1
        q = min(prev, p * len(pairs) / rank)
        out[idx] = q
        prev = q
    return out


def _daily_series(rows, window_start):
    buckets = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.value_numeric is None or row.recorded_at is None:
            continue
        day = row.recorded_at.date().isoformat()
        buckets[day][row.kind].append(float(row.value_numeric))
    out = {}
    for day, kinds in buckets.items():
        out[day] = {k: _mean(v) for k, v in kinds.items() if v}
    return out


def _lag_pairs(daily, exposure, outcome, lag):
    days = sorted(daily)
    index = {d: i for i, d in enumerate(days)}
    xs, ys, dates = [], [], []
    for d in days:
        i = index[d]
        target_i = i + lag
        if target_i >= len(days):
            continue
        td = days[target_i]
        if exposure in daily[d] and outcome in daily[td]:
            xs.append(daily[d][exposure])
            ys.append(daily[td][outcome])
            dates.append((d, td))
    return xs, ys, dates


def _data_quality_v01(rows, daily, tested_pairs=0):
    if not rows:
        return 0.0
    days = len(daily)
    span = max(1, (max(r.recorded_at for r in rows) - min(r.recorded_at for r in rows)).days + 1)
    coverage = min(1.0, days / min(INTELLIGENCE_WINDOW_DAYS, span))
    numeric = sum(1 for r in rows if r.value_numeric is not None)
    numeric_ratio = numeric / len(rows)
    density = min(1.0, numeric / max(1, days * 3))
    return round(100 * (0.40 * coverage + 0.30 * numeric_ratio + 0.30 * density), 1)


def _scientific_alternatives(exposure, outcome, lag):
    alternatives = [
        "A third variable may influence both exposure and outcome.",
        "The association may reflect weekday/weekend or schedule structure.",
        "Reverse temporal influence or reciprocal feedback may be present.",
        "Missing observations may make the observed days unrepresentative.",
        "A small number of unusual days may drive the relationship.",
    ]
    if lag == 0:
        alternatives.insert(0, "Same-day measurements may share context without one causing the other.")
    return alternatives


def _mechanistic_rationale(exposure, outcome, lag):
    if lag > 0:
        return f"The candidate relationship is temporally ordered: {exposure} on an earlier day is evaluated against {outcome} {lag} day(s) later. Temporal precedence is supportive context, not proof of causation."
    return f"{exposure} and {outcome} repeatedly co-occur in the personal longitudinal record. The statistical relationship is exploratory and requires competing-explanation testing."


def _hypothesis_status(q, robust, n):
    if n < INTELLIGENCE_MIN_DAYS:
        return "INCONCLUSIVE"
    if q <= 0.05 and robust:
        return "SUPPORTED"
    if q <= 0.20:
        return "TESTING"
    return "INCONCLUSIVE"


def _upsert_phenotypes(s, user_id, daily, rows):
    definitions = {
        "sleep_restriction": ("Sleep restriction", {"kind": "sleep_duration", "lt": 6.0}),
        "high_stress_state": ("High-stress state", {"kind": "stress", "gte": 7.0}),
        "low_focus_state": ("Low-focus state", {"kind": "focus", "lte": 4.0}),
        "high_caffeine_exposure": ("High caffeine exposure", {"kind": "caffeine", "gte": 3.0}),
    }
    for code, (label, definition) in definitions.items():
        if s.scalar(select(PhenotypeDefinition).where(PhenotypeDefinition.code == code)) is None:
            s.add(PhenotypeDefinition(code=code, label=label, definition_json=json.dumps(definition), model_version=INTELLIGENCE_MODEL_VERSION))
    s.flush()
    for day, vals in daily.items():
        matched = []
        for code, (_, d) in definitions.items():
            value = vals.get(d["kind"])
            if value is None:
                continue
            if "lt" in d and value < d["lt"] or "gte" in d and value >= d["gte"] or "lte" in d and value <= d["lte"]:
                matched.append(code)
        for code in matched:
            start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            exists = s.scalar(select(PhenotypeObservation).where(
                PhenotypeObservation.user_id == user_id,
                PhenotypeObservation.phenotype_code == code,
                PhenotypeObservation.start_at == start,
            ))
            if exists is None:
                s.add(PhenotypeObservation(user_id=user_id, phenotype_code=code, start_at=start, end_at=start + timedelta(days=1), confidence=0.8, provenance_json=json.dumps({"source": "derived_from_observations", "day": day}), model_version=INTELLIGENCE_MODEL_VERSION))


def _evaluate_pending_predictions(u, s):
    pending = s.scalars(select(Prediction).where(Prediction.user_id == u.id, Prediction.status == "PENDING", Prediction.target_end <= now()).limit(100)).all()
    evaluated = 0
    for p in pending:
        h = s.scalar(select(Hypothesis).where(Hypothesis.hypothesis_id == p.hypothesis_id, Hypothesis.user_id == u.id))
        if h is None:
            p.status = "INCONCLUSIVE"
            p.evaluation_json = json.dumps({"reason": "hypothesis_not_found"})
            continue
        outcome_rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.kind == h.outcome, Observation.recorded_at >= p.target_start, Observation.recorded_at <= p.target_end, Observation.value_numeric.is_not(None)).order_by(Observation.recorded_at.asc())).all()
        exposure_rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.kind == h.exposure, Observation.recorded_at >= p.target_start - timedelta(days=max(0, h.lag)), Observation.recorded_at <= p.target_end, Observation.value_numeric.is_not(None))).all()
        if not outcome_rows or not exposure_rows:
            p.status = "INCONCLUSIVE"
            p.evaluation_json = json.dumps({"reason": "insufficient_future_observations", "outcome_count": len(outcome_rows), "exposure_count": len(exposure_rows)})
            evaluated += 1
            continue
        observed = _mean([float(r.value_numeric) for r in outcome_rows])
        baseline = p.baseline_value
        if baseline is None:
            baseline = observed
        delta = observed - baseline
        expected_sign = 1 if p.expected_direction == "higher" else -1
        # A small tolerance avoids calling negligible noise a directional hit.
        tolerance = max(0.1, abs(baseline) * 0.02)
        if abs(delta) <= tolerance:
            status = "INCONCLUSIVE"
        else:
            status = "SUPPORTED" if delta * expected_sign > 0 else "WEAKENED"
        p.status = status
        p.evaluation_json = json.dumps({"observed_outcome_mean": observed, "baseline": baseline, "delta": delta, "tolerance": tolerance, "outcome_count": len(outcome_rows), "exposure_count": len(exposure_rows), "evaluated_at": now().isoformat()})
        if status == "SUPPORTED":
            h.confidence = min(0.99, (h.confidence or 0.5) + 0.08)
        elif status == "WEAKENED":
            h.confidence = max(0.01, (h.confidence or 0.5) - 0.12)
        h.status = "SUPPORTED" if status == "SUPPORTED" and (h.confidence or 0) >= 0.55 else ("WEAKENED" if status == "WEAKENED" and (h.confidence or 0) < 0.45 else h.status)
        evaluated += 1
    if evaluated:
        s.flush()
    return evaluated


def run_intelligence_engine(u, s, window_days=INTELLIGENCE_WINDOW_DAYS):
    end = now()
    evaluated_predictions = _evaluate_pending_predictions(u, s)
    since = end - timedelta(days=window_days)
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.recorded_at >= since).order_by(Observation.recorded_at.asc())).all()
    daily = _daily_series(rows, since)
    quality = _data_quality_v01(rows, daily)
    run = IntelligenceRun(user_id=u.id, model_family="nexgene-intelligence-engine", model_version=INTELLIGENCE_MODEL_VERSION, status="running", input_window_days=window_days, data_quality=quality)
    s.add(run)
    s.flush()
    _upsert_phenotypes(s, u.id, daily, rows)

    candidates = []
    kinds = sorted({r.kind for r in rows if r.value_numeric is not None})
    for exposure in kinds:
        for outcome in kinds:
            if exposure == outcome:
                continue
            for lag in (0, 1, 2):
                xs, ys, dates = _lag_pairs(daily, exposure, outcome, lag)
                if len(xs) < INTELLIGENCE_MIN_DAYS:
                    continue
                r = _pearson_xy(xs, ys)
                if r is None or abs(r) < 0.30:
                    continue
                rho = _spearman(xs, ys)
                p = _normal_p_from_r(r, len(xs))
                lo, hi = _fisher_ci(r, len(xs))
                robust = rho is not None and abs(rho) >= 0.25 and (r == 0 or (r * rho > 0))
                candidates.append({"exposure": exposure, "outcome": outcome, "lag": lag, "r": r, "rho": rho, "p": p, "lo": lo, "hi": hi, "n": len(xs), "robust": robust, "dates": dates})
    candidates.sort(key=lambda x: abs(x["r"]), reverse=True)
    candidates = candidates[:120]
    qs = _bh_adjust([(f"{c['exposure']}->{c['outcome']}:{c['lag']}", c["p"]) for c in candidates]) if candidates else []
    candidates_with_q = []
    for c, q in zip(candidates, qs):
        c["q"] = q
        candidates_with_q.append(c)

    # Replace only hypotheses from this model/user with current candidates, retaining history.
    created = 0
    tested = 0
    for c in candidates_with_q[:INTELLIGENCE_MAX_HYPOTHESES]:
        hkey = f"{u.id}:{c['exposure']}:{c['outcome']}:{c['lag']}"
        h = s.scalar(select(Hypothesis).where(Hypothesis.hypothesis_id == hkey))
        alternatives = _scientific_alternatives(c["exposure"], c["outcome"], c["lag"])
        prediction = f"When {c['exposure']} is {('higher' if c['r'] > 0 else 'lower')} than its recent personal level, {c['outcome']} is expected to move {('higher' if c['r'] > 0 else 'lower')} {c['lag']} day(s) later." if c["lag"] else f"On days when {c['exposure']} is higher, {c['outcome']} is expected to be {'higher' if c['r'] > 0 else 'lower'} than its recent personal level."
        falsifiers = [
            "The relationship disappears in later observations.",
            "The direction reverses repeatedly in comparable contexts.",
            "Removing unusual observations eliminates the effect.",
            "A plausible competing explanation accounts for the relationship better.",
        ]
        conf = max(0.0, min(0.99, (abs(c["r"]) * 0.55) + (min(1.0, c["n"] / 30) * 0.20) + (0.15 if c["robust"] else 0) + (0.10 if c["q"] <= 0.05 else 0)))
        status = _hypothesis_status(c["q"], c["robust"], c["n"])
        if h is None:
            h = Hypothesis(hypothesis_id=hkey, user_id=u.id, exposure=c["exposure"], outcome=c["outcome"], lag=c["lag"], time_window="daily", mechanistic_rationale=_mechanistic_rationale(c["exposure"], c["outcome"], c["lag"]), alternative_explanations_json=json.dumps(alternatives), predictions_json=json.dumps([prediction]), falsifiers_json=json.dumps(falsifiers), required_data_json=json.dumps([c["exposure"], c["outcome"]]), status="GENERATED", model_version=INTELLIGENCE_MODEL_VERSION)
            s.add(h)
            s.flush()
            created += 1
        h.effect_estimate = c["r"]
        h.confidence = conf
        h.status = status
        h.last_tested_at = end
        h.statistical_test_json = json.dumps({"method": "pearson_with_spearman_robustness", "lag_days": c["lag"], "n": c["n"]})
        h.uncertainty_json = json.dumps({"ci_95": [c["lo"], c["hi"]], "p_value": c["p"], "q_value": c["q"]})
        h.evidence_json = json.dumps([])
        test = HypothesisTest(hypothesis_id=h.hypothesis_id, user_id=u.id, run_id=run.id, method="pearson + Fisher CI + Spearman sensitivity + Benjamini-Hochberg", sample_size=c["n"], effect_estimate=c["r"], p_value=c["p"], q_value=c["q"], ci_low=c["lo"], ci_high=c["hi"], data_quality=quality, robustness_json=json.dumps({"spearman": c["rho"], "sign_agreement": c["robust"]}), result_status=status)
        s.add(test)
        tested += 1

        # Issue a forward prediction only for the stronger candidate signals.
        if c["q"] <= 0.20 and c["robust"]:
            existing_pending = s.scalar(select(Prediction).where(Prediction.hypothesis_id == h.hypothesis_id, Prediction.status == "PENDING"))
            if existing_pending is None:
                target_start = end + timedelta(days=max(1, c["lag"]))
                target_end = target_start + timedelta(days=1)
                s.add(Prediction(hypothesis_id=h.hypothesis_id, user_id=u.id, target_start=target_start, target_end=target_end, exposure_condition=prediction, predicted_outcome=f"Expected direction: {'positive' if c['r'] > 0 else 'negative'} relationship between {c['exposure']} and {c['outcome']}.", expected_effect=c["r"], expected_direction="higher" if c["r"] > 0 else "lower", baseline_value=_mean([daily[d].get(c["outcome"]) for d in daily if daily[d].get(c["outcome"]) is not None]), status="PENDING", model_version=INTELLIGENCE_MODEL_VERSION))

    summary = {
        "engine": "NexGene Intelligence Engine v0.1",
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "window_days": window_days,
        "observation_count": len(rows),
        "active_days": len(daily),
        "numeric_kinds": kinds,
        "candidate_relationships": len(candidates_with_q),
        "hypotheses_tested": tested,
        "hypotheses_created": created,
        "predictions_pending": s.query(Prediction).filter(Prediction.user_id == u.id, Prediction.status == "PENDING").count(),
        "predictions_evaluated_this_run": evaluated_predictions,
        "data_quality": quality,
        "methodology": ["daily aggregation", "lagged associations", "Pearson correlation", "Spearman sensitivity", "Fisher 95% CI", "Benjamini-Hochberg multiple-testing adjustment", "alternative explanations", "forward prediction", "prediction outcome evaluation"],
        "epistemic_boundary": "These are personal observational hypotheses. They do not establish causation, diagnosis, treatment, or population-level medical validity.",
    }
    run.summary_json = json.dumps(summary)
    run.status = "completed"
    s.commit()
    return summary


@app.post("/api/v1/intelligence/engine/run")
def intelligence_engine_run(u: User = Depends(current_user), s: Session = Depends(db)):
    return run_intelligence_engine(u, s)


@app.get("/api/v1/intelligence/engine/status")
def intelligence_engine_status(u: User = Depends(current_user), s: Session = Depends(db)):
    run = s.scalar(select(IntelligenceRun).where(IntelligenceRun.user_id == u.id).order_by(IntelligenceRun.id.desc()))
    hypotheses = s.scalars(select(Hypothesis).where(Hypothesis.user_id == u.id).order_by(Hypothesis.confidence.desc().nullslast(), Hypothesis.id.desc()).limit(50)).all()
    pending = s.scalars(select(Prediction).where(Prediction.user_id == u.id, Prediction.status == "PENDING").order_by(Prediction.target_start.asc()).limit(20)).all()
    return {
        "engine": "NexGene Intelligence Engine v0.1",
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "last_run": json.loads(run.summary_json) if run and run.summary_json else None,
        "hypothesis_counts": {status: s.query(Hypothesis).filter(Hypothesis.user_id == u.id, Hypothesis.status == status).count() for status in ["GENERATED", "SCREENING", "TESTABLE", "TESTING", "SUPPORTED", "WEAKENED", "FALSIFIED", "INCONCLUSIVE"]},
        "pending_predictions": [{"id": p.id, "hypothesis_id": p.hypothesis_id, "target_start": p.target_start.isoformat(), "target_end": p.target_end.isoformat(), "expected_direction": p.expected_direction, "expected_effect": p.expected_effect} for p in pending],
        "top_hypotheses": [{"hypothesis_id": h.hypothesis_id, "exposure": h.exposure, "outcome": h.outcome, "lag": h.lag, "status": h.status, "effect_estimate": h.effect_estimate, "confidence": h.confidence, "uncertainty": json.loads(h.uncertainty_json or "{}"), "alternatives": json.loads(h.alternative_explanations_json or "[]"), "predictions": json.loads(h.predictions_json or "[]"), "falsifiers": json.loads(h.falsifiers_json or "[]"), "model_version": h.model_version} for h in hypotheses],
    }


@app.get("/api/v1/intelligence/hypotheses")
def intelligence_hypotheses(status: Optional[str] = None, limit: int = 40, u: User = Depends(current_user), s: Session = Depends(db)):
    limit = max(1, min(limit, 100))
    stmt = select(Hypothesis).where(Hypothesis.user_id == u.id)
    if status:
        stmt = stmt.where(Hypothesis.status == status.upper())
    rows = s.scalars(stmt.order_by(Hypothesis.confidence.desc().nullslast(), Hypothesis.id.desc()).limit(limit)).all()
    return [{"hypothesis_id": h.hypothesis_id, "exposure": h.exposure, "outcome": h.outcome, "lag": h.lag, "time_window": h.time_window, "status": h.status, "effect_estimate": h.effect_estimate, "confidence": h.confidence, "uncertainty": json.loads(h.uncertainty_json or "{}"), "alternatives": json.loads(h.alternative_explanations_json or "[]"), "predictions": json.loads(h.predictions_json or "[]"), "falsifiers": json.loads(h.falsifiers_json or "[]"), "required_data": json.loads(h.required_data_json or "[]"), "evidence": json.loads(h.evidence_json or "[]"), "last_tested_at": h.last_tested_at.isoformat() if h.last_tested_at else None, "model_version": h.model_version} for h in rows]


@app.get("/api/v1/intelligence/predictions")
def intelligence_predictions(status: Optional[str] = None, limit: int = 40, u: User = Depends(current_user), s: Session = Depends(db)):
    limit = max(1, min(limit, 100))
    stmt = select(Prediction).where(Prediction.user_id == u.id)
    if status:
        stmt = stmt.where(Prediction.status == status.upper())
    rows = s.scalars(stmt.order_by(Prediction.target_start.asc(), Prediction.id.desc()).limit(limit)).all()
    return [{"id": p.id, "hypothesis_id": p.hypothesis_id, "issued_at": p.issued_at.isoformat(), "target_start": p.target_start.isoformat(), "target_end": p.target_end.isoformat(), "exposure_condition": p.exposure_condition, "predicted_outcome": p.predicted_outcome, "expected_effect": p.expected_effect, "expected_direction": p.expected_direction, "baseline_value": p.baseline_value, "status": p.status, "evaluation": json.loads(p.evaluation_json or "{}"), "model_version": p.model_version} for p in rows]


@app.get("/api/v1/intelligence/audit/{hypothesis_id}")
def intelligence_audit(hypothesis_id: str, u: User = Depends(current_user), s: Session = Depends(db)):
    h = s.scalar(select(Hypothesis).where(Hypothesis.hypothesis_id == hypothesis_id, Hypothesis.user_id == u.id))
    if h is None:
        raise HTTPException(404, "Hypothesis not found")
    tests = s.scalars(select(HypothesisTest).where(HypothesisTest.hypothesis_id == hypothesis_id, HypothesisTest.user_id == u.id).order_by(HypothesisTest.id.desc()).limit(50)).all()
    predictions = s.scalars(select(Prediction).where(Prediction.hypothesis_id == hypothesis_id, Prediction.user_id == u.id).order_by(Prediction.id.desc()).limit(20)).all()
    return {"hypothesis": {"hypothesis_id": h.hypothesis_id, "exposure": h.exposure, "outcome": h.outcome, "lag": h.lag, "status": h.status, "confidence": h.confidence, "model_version": h.model_version, "alternatives": json.loads(h.alternative_explanations_json or "[]"), "falsifiers": json.loads(h.falsifiers_json or "[]")}, "tests": [{"id": t.id, "run_id": t.run_id, "method": t.method, "sample_size": t.sample_size, "effect_estimate": t.effect_estimate, "p_value": t.p_value, "q_value": t.q_value, "ci_low": t.ci_low, "ci_high": t.ci_high, "data_quality": t.data_quality, "robustness": json.loads(t.robustness_json or "{}"), "result_status": t.result_status, "created_at": t.created_at.isoformat()} for t in tests], "predictions": [{"id": p.id, "target_start": p.target_start.isoformat(), "target_end": p.target_end.isoformat(), "status": p.status, "evaluation": json.loads(p.evaluation_json or "{}"), "expected_effect": p.expected_effect} for p in predictions], "provenance": {"data_source": "personal longitudinal observations", "analysis_version": INTELLIGENCE_MODEL_VERSION, "reproducible": True}}


def _build_intelligence(u, s):
    end = now()
    since = end - timedelta(days=30)
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.recorded_at >= since).order_by(Observation.recorded_at.asc())).all()
    biomarkers = s.scalars(select(BiomarkerSignal).where(BiomarkerSignal.user_id == u.id, BiomarkerSignal.recorded_at >= since).order_by(BiomarkerSignal.recorded_at.asc())).all()
    daily = _daily_numeric(rows)
    findings = []
    topics = set()

    # Recent-vs-personal-baseline comparisons. A 7-day window is compared with the preceding 21 days.
    recent_start = end - timedelta(days=7)
    prior_start = end - timedelta(days=28)
    for kind, label, unit in [
        ("sleep_duration", "sleep", "hours"),
        ("energy", "energy", "points"),
        ("stress", "stress", "points"),
        ("focus", "focus", "points"),
        ("activity_duration", "activity", "minutes"),
    ]:
        recent = [r.value_numeric for r in rows if r.kind == kind and r.value_numeric is not None and r.recorded_at >= recent_start]
        prior = [r.value_numeric for r in rows if r.kind == kind and r.value_numeric is not None and prior_start <= r.recorded_at < recent_start]
        if len(recent) >= 3 and len(prior) >= 4:
            ra = sum(recent) / len(recent)
            pa = sum(prior) / len(prior)
            delta = ra - pa
            threshold = 0.5 if unit in {"hours", "points"} else 30.0
            if abs(delta) >= threshold:
                direction = "higher" if delta > 0 else "lower"
                findings.append({
                    "status": "observed",
                    "type": "baseline_shift",
                    "title": f"Your recent {label} is {direction} than your recent baseline.",
                    "body": f"The latest 7-day average is {abs(delta):.1f} {unit} {direction} than the preceding 21-day window.",
                    "confidence": "moderate",
                    "reason_codes": ["personal_baseline", "sufficient_recent_data"],
                })
                topics.add(label)

    # Exploratory multi-factor associations on daily values. We never call these causal.
    pairs = [
        ("sleep_duration", "focus", "sleep", "focus"),
        ("sleep_duration", "energy", "sleep", "energy"),
        ("stress", "focus", "stress", "focus"),
        ("activity_duration", "sleep_duration", "activity", "sleep"),
        ("caffeine", "sleep_duration", "caffeine", "sleep"),
    ]
    seen_pairs = set()
    for left, right, left_label, right_label in pairs:
        paired = [(d[left], d[right]) for d in daily.values() if left in d and right in d]
        if len(paired) < 7:
            continue
        r = _pearson([x for x, _ in paired], [y for _, y in paired])
        if r is None or abs(r) < 0.45:
            continue
        key = tuple(sorted((left, right)))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        direction = "move together" if r > 0 else "move in opposite directions"
        findings.append({
            "status": "inferred",
            "type": "exploratory_association",
            "title": f"{left_label.title()} and {right_label} {direction} in your recent data.",
            "body": f"Across {len(paired)} paired days, the exploratory association was r={r:.2f}. This is a personal pattern, not proof of causation or a medical finding.",
            "confidence": "exploratory",
            "reason_codes": ["paired_daily_data", "personal_longitudinal_signal", "not_causal"],
        })
        topics.update({left_label, right_label})

    # Persistent molecular deviations are observed signals, not diagnoses.
    for marker in sorted({b.marker_code for b in biomarkers}):
        recent = [b for b in biomarkers if b.marker_code == marker and b.significance == "significant" and b.quality != "poor"]
        if len(recent) >= 3:
            last = recent[-1]
            findings.append({
                "status": "observed",
                "type": "molecular_persistence",
                "title": f"A repeated molecular deviation is being watched: {last.marker_name}.",
                "body": "The molecular layer has recorded repeated significant deviation signals. NexGene does not convert this signal into a diagnosis or a concentration that the sensor did not measure.",
                "confidence": "source_dependent",
                "reason_codes": ["repeated_molecular_signal", "quality_screened"],
            })
            topics.update(_topic_tokens(last.marker_name))

    evidence = _retrieve_evidence(s, topics, 8) if topics else []
    next_steps = [
        "Keep collecting the signals involved so NexGene can test whether the pattern persists.",
        "Review the evidence sources attached to an inference before treating it as meaningful.",
    ]
    if any(f["type"] == "molecular_persistence" for f in findings):
        next_steps.insert(0, "If a significant molecular deviation persists, consider discussing the result with a qualified healthcare professional rather than relying on the app alone.")

    clinical_context = {"status": "not_authorized", "record_count": 0, "record_types": []}
    try:
        grants = s.scalars(select(ClinicalAccessGrant).where(ClinicalAccessGrant.user_id == u.id, ClinicalAccessGrant.revoked_at.is_(None))).all()
        authorized_refs = [g.subject_ref for g in grants if _clinical_dual_authorized(g)]
        if authorized_refs:
            with ClinicalSessionLocal() as cs:
                crows = cs.scalars(select(ClinicalRecord).where(ClinicalRecord.subject_ref.in_(authorized_refs)).order_by(ClinicalRecord.id.desc()).limit(300)).all()
                clinical_context = {
                    "status": "authorized",
                    "record_count": len(crows),
                    "record_types": sorted({r.record_type for r in crows}),
                    "source_systems": sorted({r.source_system for r in crows if r.source_system}),
                }
                for r in crows[:100]:
                    topics.update(_topic_tokens((r.display or "") + " " + (r.summary or "")))
                evidence = _retrieve_evidence(s, topics, 8) if topics else evidence
    except Exception:
        clinical_context = {"status": "unavailable", "record_count": 0, "record_types": []}

    return {
        "status": "active" if findings else ("baseline_forming" if rows or biomarkers else "starting"),
        "scope": {"observations_days": 30, "biomarker_signals": len(biomarkers), "clinical_records": clinical_context["record_count"], "evidence_sources_retrieved": len(evidence)},
        "data_quality": _data_quality(rows, biomarkers),
        "clinical_context": clinical_context,
        "findings": findings[:10],
        "evidence": [{"id": r.id, "source_type": r.source_type, "title": r.title, "publisher": r.publisher, "citation": r.citation, "url": r.url, "published_year": r.published_year, "evidence_grade": r.evidence_grade} for r in evidence],
        "next_steps": next_steps[:3],
        "clinical_escalation": "not_assessed_in_v1_4",
    }


@app.get("/api/v1/clinical/status")
def clinical_status(u: User = Depends(current_user), s: Session = Depends(db), cs: Session = Depends(clinical_db)):
    grants = s.scalars(select(ClinicalAccessGrant).where(ClinicalAccessGrant.user_id == u.id, ClinicalAccessGrant.revoked_at.is_(None))).all()
    out = []
    for g in grants:
        authorized = _clinical_dual_authorized(g)
        count = cs.query(ClinicalRecord).filter(ClinicalRecord.subject_ref == g.subject_ref).count() if authorized else 0
        out.append({
            "provider_id": g.provider_id,
            "subject_ref": g.subject_ref if authorized else None,
            "user_authorized": bool(g.user_granted_at),
            "provider_authorized": bool(g.provider_granted_at),
            "dual_authorized": authorized,
            "scopes": json.loads(g.scopes or '["read"]'),
            "record_count": count,
        })
    return {"status": "active", "compartment": "clinical", "grants": out, "separate_store": True}


@app.post("/api/v1/clinical/consent")
def clinical_consent(data: ClinicalConsentIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    provider_id = data.provider_id.strip()
    grant = _clinical_grant(s, u.id, provider_id)
    if grant is None:
        grant = ClinicalAccessGrant(
            user_id=u.id,
            provider_id=provider_id,
            subject_ref="nxc_" + secrets.token_urlsafe(32),
            scopes=json.dumps(data.scopes or ["read"]),
            user_granted_at=now(),
        )
        s.add(grant)
    else:
        grant.scopes = json.dumps(data.scopes or ["read"])
        grant.user_granted_at = now()
        grant.revoked_at = None
    s.commit()
    return {
        "status": "user_authorized",
        "provider_id": grant.provider_id,
        "subject_ref": grant.subject_ref,
        "provider_authorized": bool(grant.provider_granted_at),
        "dual_authorized": _clinical_dual_authorized(grant),
    }


@app.post("/api/v1/clinical/revoke")
def clinical_revoke(provider_id: str, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    grant = _clinical_grant(s, u.id, provider_id.strip())
    if not grant:
        return {"status": "revoked", "changed": False}
    grant.revoked_at = now()
    s.commit()
    return {"status": "revoked", "changed": True}


@app.post("/api/v1/clinical/provider/grant")
def clinical_provider_grant(data: ClinicalProviderGrantIn, request: Request, s: Session = Depends(db)):
    provider_id = clinical_provider_auth(request)
    if data.provider_id.strip() != provider_id:
        raise HTTPException(403, "Clinical provider identity mismatch")
    grant = s.scalar(select(ClinicalAccessGrant).where(
        ClinicalAccessGrant.subject_ref == data.subject_ref,
        ClinicalAccessGrant.provider_id == provider_id,
        ClinicalAccessGrant.revoked_at.is_(None),
    ))
    if not grant:
        raise HTTPException(404, "Clinical consent link not found")
    grant.provider_granted_at = now()
    s.commit()
    return {"status": "provider_authorized", "dual_authorized": _clinical_dual_authorized(grant)}


@app.post("/api/v1/clinical/provider/records")
def clinical_provider_records(data: ClinicalRecordBatchIn, request: Request, s: Session = Depends(db), cs: Session = Depends(clinical_db)):
    provider_id = clinical_provider_auth(request)
    if any(r.provider_id.strip() != provider_id for r in data.records):
        raise HTTPException(403, "Clinical provider identity mismatch")
    subject_refs = {r.subject_ref for r in data.records}
    grants = s.scalars(select(ClinicalAccessGrant).where(
        ClinicalAccessGrant.subject_ref.in_(subject_refs),
        ClinicalAccessGrant.provider_id == provider_id,
        ClinicalAccessGrant.revoked_at.is_(None),
    )).all()
    grant_map = {g.subject_ref: g for g in grants}
    if any(not _clinical_dual_authorized(grant_map.get(ref)) for ref in subject_refs):
        raise HTTPException(403, "Both user and clinical provider authorization are required")
    rows = []
    for item in data.records:
        rows.append(ClinicalRecord(
            subject_ref=item.subject_ref,
            provider_id=provider_id,
            record_type=item.record_type,
            code=item.code,
            display=item.display.strip(),
            status=item.status.strip(),
            summary=item.summary,
            source_system=item.source_system,
            external_id=item.external_id,
            clinical_date=item.clinical_date,
            payload_json=json.dumps(item.payload, ensure_ascii=False),
        ))
    cs.add_all(rows)
    cs.commit()
    return {"status": "synced", "saved": len(rows), "compartment": "clinical"}


@app.get("/api/v1/clinical/records")
def clinical_records(u: User = Depends(current_user), s: Session = Depends(db), cs: Session = Depends(clinical_db)):
    grants = s.scalars(select(ClinicalAccessGrant).where(ClinicalAccessGrant.user_id == u.id, ClinicalAccessGrant.revoked_at.is_(None))).all()
    authorized_refs = [g.subject_ref for g in grants if _clinical_dual_authorized(g)]
    if not authorized_refs:
        return {"status": "not_authorized", "records": []}
    rows = cs.scalars(select(ClinicalRecord).where(ClinicalRecord.subject_ref.in_(authorized_refs)).order_by(ClinicalRecord.clinical_date.desc().nullslast(), ClinicalRecord.id.desc()).limit(300)).all()
    return {
        "status": "authorized",
        "records": [{
            "record_type": r.record_type,
            "code": r.code,
            "display": r.display,
            "status": r.status,
            "summary": r.summary,
            "source_system": r.source_system,
            "external_id": r.external_id,
            "clinical_date": r.clinical_date.isoformat() if r.clinical_date else None,
            "payload": json.loads(r.payload_json or "{}"),
        } for r in rows],
    }


@app.get("/api/v1/genomics/status")
def genomic_status(u: User = Depends(current_user), s: Session = Depends(db)):
    grants = s.scalars(select(GenomicAccessGrant).where(GenomicAccessGrant.user_id == u.id, GenomicAccessGrant.revoked_at.is_(None))).all()
    authorized = [g for g in grants if _genomic_dual_authorized(g)]
    with GenomicSessionLocal() as gs:
        refs = [g.subject_ref for g in authorized]
        variants = gs.scalar(select(func.count(GenomicVariant.id)).where(GenomicVariant.subject_ref.in_(refs))) if refs else 0
        prs = gs.scalar(select(func.count(PolygenicScore.id)).where(PolygenicScore.subject_ref.in_(refs))) if refs else 0
    return {"status": "authorized" if authorized else "not_authorized", "authorized_sources": len(authorized), "variant_count": int(variants or 0), "polygenic_score_count": int(prs or 0)}


@app.post("/api/v1/genomics/consent")
def genomic_consent(data: GenomicConsentIn, request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    grant = s.scalar(select(GenomicAccessGrant).where(GenomicAccessGrant.user_id == u.id, GenomicAccessGrant.provider_id == data.provider_id, GenomicAccessGrant.revoked_at.is_(None)))
    if not grant:
        grant = GenomicAccessGrant(user_id=u.id, provider_id=data.provider_id, subject_ref="ngs_" + secrets.token_urlsafe(28), scopes=json.dumps(data.scopes))
        s.add(grant)
    grant.user_granted_at = now(); grant.revoked_at = None
    s.commit()
    return {"status": "user_authorized", "provider_id": data.provider_id, "subject_ref": grant.subject_ref, "dual_authorized": _genomic_dual_authorized(grant)}


@app.post("/api/v1/genomics/revoke")
def genomic_revoke(request: Request, u: User = Depends(current_user), st: SessionToken = Depends(current_session), s: Session = Depends(db)):
    require_csrf(request, st)
    grants = s.scalars(select(GenomicAccessGrant).where(GenomicAccessGrant.user_id == u.id, GenomicAccessGrant.revoked_at.is_(None))).all()
    for grant in grants: grant.revoked_at = now()
    s.commit()
    return {"status": "revoked", "revoked": len(grants)}


@app.post("/api/v1/genomics/provider/grant")
def genomic_provider_grant(data: GenomicProviderGrantIn, request: Request, s: Session = Depends(db)):
    provider_id = genomic_provider_auth(request)
    if data.provider_id.strip() != provider_id:
        raise HTTPException(403, "Genomic provider identity mismatch")
    grant = s.scalar(select(GenomicAccessGrant).where(GenomicAccessGrant.subject_ref == data.subject_ref, GenomicAccessGrant.provider_id == provider_id, GenomicAccessGrant.revoked_at.is_(None)))
    if not grant:
        raise HTTPException(404, "Genomic consent link not found")
    grant.provider_granted_at = now(); s.commit()
    return {"status": "provider_authorized", "dual_authorized": _genomic_dual_authorized(grant)}


@app.post("/api/v1/genomics/provider/variants")
def genomic_provider_variants(data: GenomicVariantBatchIn, request: Request, s: Session = Depends(db), gs: Session = Depends(genomic_db)):
    provider_id = genomic_provider_auth(request)
    if any(v.provider_id.strip() != provider_id for v in data.variants):
        raise HTTPException(403, "Genomic provider identity mismatch")
    refs = {v.subject_ref for v in data.variants}
    grants = s.scalars(select(GenomicAccessGrant).where(GenomicAccessGrant.subject_ref.in_(refs), GenomicAccessGrant.provider_id == provider_id, GenomicAccessGrant.revoked_at.is_(None))).all()
    gm = {g.subject_ref:g for g in grants}
    if any(not _genomic_dual_authorized(gm.get(ref)) for ref in refs):
        raise HTTPException(403, "Both user and genomic provider authorization are required")
    rows=[GenomicVariant(subject_ref=v.subject_ref, provider_id=provider_id, gene=v.gene, variant_id=v.variant_id, zygosity=v.zygosity, classification=v.classification, significance=v.significance, source_system=v.source_system, observed_at=v.observed_at, provenance_json=json.dumps(v.provenance)) for v in data.variants]
    gs.add_all(rows); gs.commit()
    return {"status":"synced", "saved":len(rows), "compartment":"genomic"}


@app.post("/api/v1/genomics/provider/polygenic-scores")
def genomic_provider_prs(data: PolygenicScoreBatchIn, request: Request, s: Session = Depends(db), gs: Session = Depends(genomic_db)):
    provider_id = genomic_provider_auth(request)
    if any(v.provider_id.strip() != provider_id for v in data.scores):
        raise HTTPException(403, "Genomic provider identity mismatch")
    refs = {v.subject_ref for v in data.scores}
    grants = s.scalars(select(GenomicAccessGrant).where(GenomicAccessGrant.subject_ref.in_(refs), GenomicAccessGrant.provider_id == provider_id, GenomicAccessGrant.revoked_at.is_(None))).all()
    gm={g.subject_ref:g for g in grants}
    if any(not _genomic_dual_authorized(gm.get(ref)) for ref in refs):
        raise HTTPException(403, "Both user and genomic provider authorization are required")
    rows=[PolygenicScore(subject_ref=v.subject_ref, provider_id=provider_id, trait_code=v.trait_code, score=v.score, percentile=v.percentile, reference_population=v.reference_population, model_version=v.model_version, observed_at=v.observed_at, provenance_json=json.dumps(v.provenance)) for v in data.scores]
    gs.add_all(rows); gs.commit()
    return {"status":"synced", "saved":len(rows), "compartment":"genomic"}


@app.get("/api/v1/genomics/data")
def genomic_data(u: User = Depends(current_user), s: Session = Depends(db), gs: Session = Depends(genomic_db)):
    grants=s.scalars(select(GenomicAccessGrant).where(GenomicAccessGrant.user_id==u.id, GenomicAccessGrant.revoked_at.is_(None))).all()
    refs=[g.subject_ref for g in grants if _genomic_dual_authorized(g)]
    if not refs: return {"status":"not_authorized", "variants":[], "polygenic_scores":[]}
    variants=gs.scalars(select(GenomicVariant).where(GenomicVariant.subject_ref.in_(refs)).order_by(GenomicVariant.id.desc()).limit(500)).all()
    prs=gs.scalars(select(PolygenicScore).where(PolygenicScore.subject_ref.in_(refs)).order_by(PolygenicScore.id.desc()).limit(200)).all()
    return {"status":"authorized", "variants":[{"gene":v.gene,"variant_id":v.variant_id,"zygosity":v.zygosity,"classification":v.classification,"significance":v.significance,"source_system":v.source_system,"observed_at":v.observed_at.isoformat() if v.observed_at else None} for v in variants], "polygenic_scores":[{"trait_code":x.trait_code,"score":x.score,"percentile":x.percentile,"reference_population":x.reference_population,"model_version":x.model_version,"observed_at":x.observed_at.isoformat() if x.observed_at else None} for x in prs]}


async def _retrieve_live_evidence(q: str, limit: int, user_id: int, s: Session):
    """Fetch bounded metadata from PubMed and Crossref independently.

    One provider failing does not discard results from the other. No full text is fetched.
    """
    q = q.strip()[:300]
    limit = max(1, min(int(limit), 10))
    if not q:
        return {"status": "empty", "query": q, "results": [], "source_status": {}}

    timeout = httpx.Timeout(8.0, connect=4.0)
    results = []
    source_status = {"PubMed": "not_attempted", "Crossref": "not_attempted"}
    headers = {"User-Agent": "NexGene/1.7 (live-research-experiment)"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        try:
            es = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params={"db":"pubmed","term":q,"retmode":"json","retmax":limit,"tool":"NexGene","email":NCBI_EMAIL, **({"api_key":NCBI_API_KEY} if NCBI_API_KEY else {})})
            es.raise_for_status()
            ids = es.json().get("esearchresult", {}).get("idlist", [])
            if ids:
                sm = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi", params={"db":"pubmed","id":",".join(ids),"retmode":"json","tool":"NexGene","email":NCBI_EMAIL, **({"api_key":NCBI_API_KEY} if NCBI_API_KEY else {})})
                sm.raise_for_status()
                data = sm.json().get("result", {})
                for pid in ids:
                    item = data.get(pid, {})
                    results.append({"source":"PubMed","id":pid,"title":item.get("title"),"published":item.get("pubdate"),"journal":item.get("fulljournalname"),"url":f"https://pubmed.ncbi.nlm.nih.gov/{pid}/"})
            source_status["PubMed"] = "ok"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            source_status["PubMed"] = "unavailable"

        try:
            cr = await client.get("https://api.crossref.org/v1/works", params={"query.bibliographic":q,"rows":limit,"mailto":CROSSREF_MAILTO})
            cr.raise_for_status()
            for item in cr.json().get("message", {}).get("items", []):
                doi = item.get("DOI")
                if not doi: continue
                results.append({"source":"Crossref","id":doi,"title":(item.get("title") or [None])[0],"published":((item.get("published-print") or item.get("published-online") or {}).get("date-parts") or [[None]])[0][0],"journal":(item.get("container-title") or [None])[0],"url":item.get("URL") or f"https://doi.org/{doi}"})
            source_status["Crossref"] = "ok"
        except (httpx.HTTPError, ValueError, KeyError):
            source_status["Crossref"] = "unavailable"

    seen = set()
    deduped = []
    for item in results:
        key = (item.get("source"), item.get("id"))
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    deduped = deduped[:limit * 2]
    for item in deduped:
        canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        existing = s.scalar(select(EvidenceSnapshot).where(
            EvidenceSnapshot.user_id == user_id,
            EvidenceSnapshot.source == item["source"],
            EvidenceSnapshot.external_id == str(item["id"]),
        ))
        if existing is None:
            s.add(EvidenceSnapshot(user_id=user_id, source=item["source"], external_id=str(item["id"]), title=(item.get("title") or "Untitled")[:500], published=str(item.get("published")) if item.get("published") is not None else None, journal=item.get("journal"), url=item.get("url"), query=q[:300], content_hash=digest, metadata_json=canonical))
        else:
            existing.retrieved_at = now()
            existing.query = q[:300]
            existing.content_hash = digest
            existing.metadata_json = canonical
    s.commit()
    status = "live" if deduped else ("unavailable" if all(v == "unavailable" for v in source_status.values()) else "no_results")
    return {"status": status, "query": q, "results": deduped, "source_status": source_status, "persisted_snapshots": len(deduped), "retrieved_at": now().isoformat()}


@app.get("/api/v1/evidence/live-search")
async def evidence_live_search(q: str = "", limit: int = 10, u: User = Depends(current_user), s: Session = Depends(db)):
    if not EVIDENCE_LIVE_ENABLED:
        raise HTTPException(503, "Live evidence retrieval is disabled")
    q = q.strip()[:300]
    if not q:
        return {"status": "empty", "results": []}
    return await _retrieve_live_evidence(q, limit, u.id, s)


@app.post("/api/v1/intelligence/research/run")
async def intelligence_research_run(data: LiveResearchRunIn, request: Request, st: SessionToken = Depends(current_session), u: User = Depends(current_user), s: Session = Depends(db)):
    """Run a bounded, auditable live-evidence retrieval experiment on own hypotheses."""
    require_csrf(request, st)
    rate_limit(request, "live_research", 3, s)
    if not EVIDENCE_LIVE_ENABLED:
        raise HTTPException(503, "Live evidence retrieval is disabled")

    stmt = select(Hypothesis).where(Hypothesis.user_id == u.id)
    if data.hypothesis_ids:
        stmt = stmt.where(Hypothesis.hypothesis_id.in_(data.hypothesis_ids))
    else:
        stmt = stmt.where(Hypothesis.status.in_(["GENERATED", "SCREENING", "TESTABLE", "TESTING", "SUPPORTED", "INCONCLUSIVE"]))
    hypotheses = s.scalars(stmt.order_by(Hypothesis.confidence.desc().nullslast(), Hypothesis.id.desc()).limit(LIVE_RESEARCH_MAX_HYPOTHESES)).all()
    if data.hypothesis_ids and {h.hypothesis_id for h in hypotheses} != set(data.hypothesis_ids):
        raise HTTPException(404, "One or more hypotheses were not found for this user")
    plan = plan_research_queries(hypotheses, data.max_queries)
    if not plan:
        return {"status":"no_hypotheses", "experiment_version":LIVE_RESEARCH_EXPERIMENT_VERSION, "message":"Run the Intelligence Engine first to generate structured hypotheses.", "queries":[], "results":[]}

    executions = []
    for item in plan:
        try:
            retrieved = await _retrieve_live_evidence(item["query"], data.results_per_query, u.id, s)
            executions.append({**item, **retrieved})
        except Exception as exc:
            # Isolate provider/network failures to this query and preserve the rest of the run.
            executions.append({**item, "status":"error", "results":[], "error_type":type(exc).__name__})
    return {
        "status":"completed" if any(x.get("results") for x in executions) else "completed_no_evidence",
        "experiment_version":LIVE_RESEARCH_EXPERIMENT_VERSION,
        "planner":"deterministic_hypothesis_query_planner_v0.1",
        "model_interpretation":"not_performed",
        "hypotheses_considered":[{"hypothesis_id":h.hypothesis_id,"exposure":h.exposure,"outcome":h.outcome,"status":h.status} for h in hypotheses],
        "queries":executions,
        "safety_note":"Retrieved bibliographic metadata is not evidence of causality or clinical validity. Human review and independent validation remain required.",
    }


@app.get("/api/v1/evidence/snapshots")
def evidence_snapshots(q: str = "", limit: int = 20, u: User = Depends(current_user), s: Session = Depends(db)):
    limit = max(1, min(limit, 50))
    stmt = select(EvidenceSnapshot).where(EvidenceSnapshot.user_id == u.id).order_by(EvidenceSnapshot.retrieved_at.desc()).limit(limit)
    if q.strip():
        stmt = select(EvidenceSnapshot).where(EvidenceSnapshot.user_id == u.id, EvidenceSnapshot.query.ilike(f"%{q.strip()[:100]}%")).order_by(EvidenceSnapshot.retrieved_at.desc()).limit(limit)
    rows=s.scalars(stmt).all()
    return [{"id":r.id,"source":r.source,"external_id":r.external_id,"title":r.title,"published":r.published,"journal":r.journal,"url":r.url,"query":r.query,"retrieved_at":r.retrieved_at.isoformat(),"content_hash":r.content_hash} for r in rows]


@app.get("/api/v1/context/live")
async def live_context(u: User = Depends(current_user), s: Session = Depends(db)):
    if not LIVE_CONTEXT_ENABLED: raise HTTPException(503,"Live context is disabled")
    p=s.scalar(select(Profile).where(Profile.user_id==u.id))
    if not p or not p.live_context_enabled or not p.location_city or not p.country:
        return {"status":"not_configured","message":"Add a city and country and enable live context to use live environmental signals."}
    timeout=httpx.Timeout(8.0,connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent":"NexGene/1.5"}) as client:
        geo=await client.get("https://geocoding-api.open-meteo.com/v1/search",params={"name":p.location_city,"count":1,"language":"en","format":"json"}); geo.raise_for_status(); g=geo.json().get("results",[])
        if not g: return {"status":"unavailable","reason":"location_not_found"}
        lat,lon=g[0]["latitude"],g[0]["longitude"]
        wx=await client.get("https://api.open-meteo.com/v1/forecast",params={"latitude":lat,"longitude":lon,"current":"temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m","timezone":"auto"}); wx.raise_for_status()
        return {"status":"live","location":{"city":g[0].get("name"),"country":g[0].get("country"),"latitude":lat,"longitude":lon},"weather":wx.json().get("current",{}),"retrieved_at":now().isoformat(),"source":"Open-Meteo"}


@app.get("/api/v1/intelligence/learning/status")
def learning_status(u: User = Depends(current_user), s: Session = Depends(db)):
    runs=s.scalars(select(LearningRun).where(LearningRun.user_id==u.id).order_by(LearningRun.id.desc()).limit(5)).all()
    return {"model_family":"personal-longitudinal-baseline","runs":[{"id":r.id,"version":r.model_version,"status":r.status,"window_days":r.input_window_days,"created_at":r.created_at.isoformat(),"output":json.loads(r.output_json or "{}")} for r in runs]}


@app.get("/api/v1/intelligence/brief")
def intelligence_brief(u: User = Depends(current_user), s: Session = Depends(db)):
    return _build_intelligence(u, s)


@app.get("/api/v1/intelligence/data-quality")
def intelligence_data_quality(u: User = Depends(current_user), s: Session = Depends(db)):
    end = now()
    since = end - timedelta(days=30)
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id, Observation.recorded_at >= since)).all()
    biomarkers = s.scalars(select(BiomarkerSignal).where(BiomarkerSignal.user_id == u.id, BiomarkerSignal.recorded_at >= since)).all()
    return _data_quality(rows, biomarkers)


@app.get("/api/v1/intelligence/insights")
def intelligence_insights(u: User = Depends(current_user), s: Session = Depends(db)):
    brief = _build_intelligence(u, s)
    return {
        "status": brief["status"],
        "observed": [f for f in brief["findings"] if f["status"] == "observed"],
        "inferred": [f for f in brief["findings"] if f["status"] == "inferred"],
        "evidence": brief["evidence"],
        "data_quality": brief["data_quality"],
        "next_steps": brief["next_steps"],
        "clinical_escalation": brief["clinical_escalation"],
    }


@app.get("/api/v1/reports/weekly")
def weekly_report(u: User = Depends(current_user), s: Session = Depends(db)):
    end = now()
    start = end - timedelta(days=7)
    baseline_start = end - timedelta(days=28)
    rows = s.scalars(
        select(Observation).where(
            Observation.user_id == u.id,
            Observation.recorded_at >= baseline_start,
        ).order_by(Observation.recorded_at.asc())
    ).all()
    week = [r for r in rows if r.recorded_at >= start]
    active_days = len({r.recorded_at.date().isoformat() for r in week})
    nums_week = defaultdict(list)
    nums_base = defaultdict(list)
    for r in week:
        if r.value_numeric is not None:
            nums_week[r.kind].append(r.value_numeric)
    for r in rows:
        if r.value_numeric is not None:
            nums_base[r.kind].append(r.value_numeric)

    p = s.scalar(select(Profile).where(Profile.user_id == u.id))
    context = {}
    if p:
        context = {"country": p.country, "occupation": p.occupation, "student": p.student, "study_field": p.study_field, "schedule": p.schedule}

    def avg(vals):
        return round(sum(vals) / len(vals), 2) if vals else None

    sections = []
    if active_days == 0:
        return {"status": "not_ready", "message": "Give NexGene a little more signal this week and the first report will take shape.", "profile": context, "coverage": {"days": 0, "observations": 0}, "sections": []}

    coverage = {"days": active_days, "observations": len(week)}
    sleep = avg(nums_week.get("sleep_duration", []))
    sleep_base = avg(nums_base.get("sleep_duration", []))
    if sleep is not None:
        if sleep_base is not None and len(nums_base.get("sleep_duration", [])) >= 4:
            delta = sleep - sleep_base
            if abs(delta) >= 0.25:
                direction = "up" if delta > 0 else "down"
                sections.append({"kind": "sleep", "title": "Your sleep moved a little this week.", "body": f"You averaged {sleep:.1f} hours, about {abs(delta):.1f} hours {direction} from the wider window. That's worth watching alongside how you felt."})
            else:
                sections.append({"kind": "sleep", "title": "Your sleep stayed fairly steady.", "body": f"You averaged about {sleep:.1f} hours this week. Your rhythm looks relatively close to your recent baseline."})
        else:
            sections.append({"kind": "sleep", "title": "We're getting a first read on your sleep.", "body": f"You averaged about {sleep:.1f} hours this week. NexGene needs a little more history before calling a change a pattern."})

    focus = avg(nums_week.get("focus", []))
    stress = avg(nums_week.get("stress", []))
    energy = avg(nums_week.get("energy", []))
    if focus is not None and sleep is not None:
        sections.append({"kind": "relationship", "title": "Sleep and focus are starting to become interesting.", "body": "You recorded both sleep and focus this week. NexGene will keep comparing them over time rather than treating one week as proof of a rule."})
    elif energy is not None and stress is not None:
        sections.append({"kind": "lifestyle", "title": "You've given us a useful bit of context.", "body": f"Energy averaged {energy:.1f}/10 while stress averaged {stress:.1f}/10 this week. More weeks will tell us whether that combination repeats."})

    positive = []
    if active_days >= 6:
        positive.append(f"You gave NexGene {active_days} days of signal this week. That's a strong enough thread to start comparing with your baseline.")
    if nums_week.get("activity_level"):
        positive.append("You recorded movement this week, which gives the report another piece of context.")
    if nums_week.get("diet_quality"):
        positive.append("You recorded eating quality this week, so future reports can look at it alongside the rest of your routine.")
    if positive:
        sections.append({"kind": "positive", "title": "One good thing.", "body": positive[0]})

    experiment = "Keep recording the basics. NexGene will wait for repeated patterns before making a stronger suggestion."
    if sleep is not None and sleep_base is not None and sleep < sleep_base - 0.5:
        experiment = "For next week, protect your usual sleep window on the days that tend to run longest. We'll see whether your energy or focus follows."
    elif focus is not None and sleep is not None:
        experiment = "Keep the same simple check-ins for another week. We'll compare sleep and focus again before treating the relationship as meaningful."
    elif stress is not None:
        experiment = "Keep recording stress alongside the rest of your day. The useful question is whether the same pattern repeats, not whether one reading explains it."

    return {
        "status": "ready",
        "title": "Your week with NexGene",
        "subtitle": "A small, human read of the signal so far.",
        "coverage": coverage,
        "profile": context,
        "sections": sections[:4],
        "experiment": experiment,
        "disclaimer": "This report describes patterns in your recorded data. It is not a diagnosis or medical advice.",
    }


@app.get("/api/v1/signals")
def signals(u: User = Depends(current_user), s: Session = Depends(db)):
    """Return gentle, data-driven reasons to come back without guilt mechanics."""
    rows = s.scalars(
        select(Observation)
        .where(Observation.user_id == u.id)
        .order_by(Observation.recorded_at.desc())
    ).all()
    if not rows:
        return {
            "status": "starting",
            "signals": [{
                "type": "welcome",
                "title": "Let's start with one small signal.",
                "body": "A morning or evening check-in gives NexGene something to learn from.",
                "action": "check_in",
            }],
        }

    by_day = defaultdict(list)
    numeric = defaultdict(list)
    for r in rows:
        by_day[r.recorded_at.date().isoformat()].append(r)
        if r.value_numeric is not None:
            numeric[r.kind].append((r.recorded_at, r.value_numeric))

    active_days = len(by_day)
    signals_out = []

    if active_days < 3:
        signals_out.append({
            "type": "curiosity",
            "title": "Your pattern is just beginning.",
            "body": f"You've given NexGene {active_days} day{'s' if active_days != 1 else ''} of signal. A few more will make the picture more interesting.",
            "action": "check_in",
        })
    elif active_days < 7:
        remaining = 7 - active_days
        signals_out.append({
            "type": "milestone",
            "title": "The first pattern is coming into focus.",
            "body": f"You've recorded {active_days} days. {remaining} more day{'s' if remaining != 1 else ''} will give NexGene a fuller first-week window.",
            "action": "check_in",
        })
    else:
        signals_out.append({
            "type": "milestone",
            "title": "You've built a real thread.",
            "body": f"NexGene has {active_days} days of signal to work with. Now the interesting part is seeing what moves together.",
            "action": "patterns",
        })

    def delta_signal(kind, label, unit, threshold):
        vals = numeric.get(kind, [])
        if len(vals) < 6:
            return None
        recent = sum(v for _, v in vals[:3]) / 3
        previous = sum(v for _, v in vals[3:6]) / 3
        delta = recent - previous
        if abs(delta) < threshold:
            return None
        direction = "up" if delta > 0 else "down"
        return {
            "type": "change",
            "title": f"Something changed in your {label}.",
            "body": f"Your latest readings are about {abs(delta):.1f} {unit} {direction} from the previous few. It may be worth watching what happens next.",
            "action": "patterns",
        }

    for args in [
        ("sleep_duration", "sleep", "hours", 0.5),
        ("stress", "stress", "points", 1.0),
        ("focus", "focus", "points", 1.0),
        ("energy", "energy", "points", 1.0),
    ]:
        sig = delta_signal(*args)
        if sig:
            signals_out.append(sig)
            break

    sleep = {r.recorded_at.date().isoformat(): r.value_numeric for r in rows if r.kind == "sleep_duration" and r.value_numeric is not None}
    focus = {r.recorded_at.date().isoformat(): r.value_numeric for r in rows if r.kind == "focus" and r.value_numeric is not None}
    paired = [(sleep[d], focus[d]) for d in sleep.keys() & focus.keys()]
    if len(paired) >= 4:
        high_sleep = [f for sl, f in paired if sl >= 7]
        low_sleep = [f for sl, f in paired if sl < 7]
        if high_sleep and low_sleep:
            hi = sum(high_sleep) / len(high_sleep)
            lo = sum(low_sleep) / len(low_sleep)
            if abs(hi - lo) >= 1.0:
                signals_out.append({
                    "type": "relationship",
                    "title": "There's a thread between sleep and focus.",
                    "body": "On the days you've slept more, your focus readings have tended to move differently. That's a pattern to keep watching, not a conclusion.",
                    "action": "patterns",
                })

    return {"status": "active", "signals": signals_out[:3]}


@app.get("/api/v1/insights")
def insights(u: User = Depends(current_user), s: Session = Depends(db)):
    rows = s.scalars(select(Observation).where(Observation.user_id == u.id).order_by(Observation.recorded_at.desc())).all()
    nums = defaultdict(list)
    for r in rows:
        if r.value_numeric is not None:
            nums[r.kind].append(r.value_numeric)
    if len(rows) < 8:
        return {"status": "baseline_forming", "items": ["Keep checking in. NexGene is learning your personal baseline."]}
    items = []

    def add_delta(kind, label, unit):
        v = nums.get(kind, [])
        if len(v) >= 6:
            recent = sum(v[:3]) / 3
            earlier = sum(v[3:6]) / 3
            delta = recent - earlier
            if abs(delta) >= 0.5:
                items.append(f"Your recent {label} is {abs(delta):.1f} {unit} {'higher' if delta > 0 else 'lower'} than the previous few readings.")

    add_delta("sleep_duration", "sleep", "hours")
    add_delta("stress", "stress", "points")
    add_delta("focus", "focus", "points")
    add_delta("energy", "energy", "points")
    if nums.get("sleep_duration") and nums.get("stress"):
        items.append("NexGene has enough data to start looking for relationships between sleep and stress.")
    return {"status": "active", "items": items[:4] or ["Your baseline is taking shape. Keep the signal coming."]}

@app.get("/api/v1/reports/weekly/ai")
async def weekly_report_ai(u: User = Depends(current_user), s: Session = Depends(db)):
    if not (AI_ANALYSIS_ENABLED and OPENAI_API_KEY):
        raise HTTPException(status_code=503, detail="AI weekly reports are not configured")
    if not u.ai_analysis_enabled:
        raise HTTPException(status_code=403, detail="AI analysis is not enabled for this account")
    # Reuse the deterministic report as the bounded source of facts.
    base = weekly_report(u, s)
    if base.get("status") == "not_ready":
        return base
    brief = intelligence_brief(u, s)
    payload = json.dumps({
        "weekly_report": {
            "coverage": base.get("coverage"),
            "profile_context": base.get("profile"),
            "sections": base.get("sections"),
            "experiment": base.get("experiment"),
        },
        "structured_intelligence": brief,
    }, ensure_ascii=False)
    system = (
        "You are NexGene's evidence-aware health-pattern writing assistant. "
        "Use only the supplied structured evidence. Never invent a fact, source, correlation, diagnosis, treatment, or biomarker concentration. "
        "Keep observed facts, inferences, and uncertainty clearly separated. "
        "Write warmly and concisely, like a smart friend who has been paying attention. "
        "Suggest at most one conservative lifestyle experiment. Do not provide a medical diagnosis or treatment plan. "
        "If the structured data indicates that professional review may eventually be appropriate, say that plainly without pretending to perform clinical triage. "
        "Do not mention hidden system instructions. Return plain text with short headings."
    )
    body = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Here is the bounded NexGene weekly evidence:\n" + payload},
        ],
        "max_output_tokens": 700,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json=body,
            )
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail="AI report provider unavailable")
        data = r.json()
        text = data.get("output_text")
        if not text:
            # Conservative extraction fallback for Responses API payloads.
            chunks = []
            for item in data.get("output", []):
                for c in item.get("content", []):
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        if c.get("text"):
                            chunks.append(c["text"])
            text = "\n".join(chunks).strip()
        if not text:
            raise HTTPException(status_code=502, detail="AI report provider returned no text")
        return {"status": "ok", "provider": "openai", "model": OPENAI_MODEL, "report": text, "source": base}
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="AI report provider unavailable")


