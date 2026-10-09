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
