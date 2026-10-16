import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings
from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Settings(BaseSettings):
    app_name: str = "MPT Telecom RPA Control Center"
    app_env: str = "development"
    api_key: str = "CHANGE_ME_BEFORE_STARTING"
    database_url: str = "sqlite:///./apexive_rpa.db"
    frontend_origins: str = "http://127.0.0.1:8000,http://localhost:8000"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {},
    pool_pre_ping=True,
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RPAJob(Base):
    __tablename__ = "rpa_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), index=True)
    vendor: Mapped[str] = mapped_column(String(30), index=True)
    workflow_key: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str] = mapped_column(String(100), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class TrafficMonitorConfig(Base):
    __tablename__ = "traffic_monitor_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitoring_enabled: Mapped[bool] = mapped_column(default=False)
    auto_run_enabled: Mapped[bool] = mapped_column(default=False)
    threshold_percent: Mapped[float] = mapped_column(Float, default=20)
    vendor: Mapped[str] = mapped_column(String(30), default="simulator")
    workflow_key: Mapped[str] = mapped_column(
        String(180), default="mpt_traffic_fluctuation"
    )
    services_json: Mapped[str] = mapped_column(
        Text, default='["PS", "CS", "VoLTE"]'
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class NetworkTrafficSample(Base):
    __tablename__ = "network_traffic_samples"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_traffic_source_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_event_id: Mapped[str] = mapped_column(String(120), index=True)
    source: Mapped[str] = mapped_column(String(120))
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ps: Mapped[float] = mapped_column(Float)
    cs: Mapped[float] = mapped_column(Float)
    volte: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


Base.metadata.create_all(engine)

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "MPT telecom traffic monitoring and RPA job orchestration. "
        "Traffic data must be supplied by an authorized telemetry source."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.frontend_origins.split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT"],
    allow_headers=["Content-Type", "X-API-Key"],
)

FRONTEND_FILE = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def get_db():
    with Session(engine) as db:
        yield db


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if (
        not settings.api_key
        or settings.api_key == "CHANGE_ME_BEFORE_STARTING"
        or not secrets.compare_digest(x_api_key, settings.api_key)
    ):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured API key")


def audit(
    db: Session,
    action: str,
    resource_type: str,
    resource_id: str = "",
    details: str = "",
) -> None:
    db.add(
        AuditEvent(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details[:4000],
        )
    )
    db.commit()


class JobCreate(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    vendor: Literal["aisware", "cyclone", "simulator"]
    workflow_key: str = Field(min_length=2, max_length=180)
    payload: dict = Field(default_factory=dict)


class JobStatusUpdate(BaseModel):
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    error_message: str = Field(default="", max_length=4000)


class TrafficConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monitoring_enabled: bool
    auto_run_enabled: bool
    threshold_percent: float = Field(ge=1, le=1000)
    vendor: Literal["aisware", "cyclone", "simulator"]
    workflow_key: str = Field(min_length=2, max_length=180)
    services: list[Literal["PS", "CS", "VoLTE"]] = Field(
        min_length=1, max_length=3
    )

    @model_validator(mode="after")
    def validate_services(self):
        if len(set(self.services)) != len(self.services):
            raise ValueError("Services must not contain duplicates")
        if self.auto_run_enabled and not self.monitoring_enabled:
            raise ValueError("Auto-run requires monitoring to be enabled")
        return self


class TrafficSampleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_event_id: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=2, max_length=120)
    sampled_at: datetime
    ps: float = Field(ge=0, le=1_000_000_000)
    cs: float = Field(ge=0, le=1_000_000_000)
    volte: float = Field(ge=0, le=1_000_000_000)

    @model_validator(mode="after")
    def validate_timestamp(self):
        if self.sampled_at.utcoffset() is None:
            raise ValueError("sampled_at must include a timezone")
        if self.sampled_at > utcnow() + timedelta(minutes=5):
            raise ValueError("sampled_at cannot be in the future")
        return self


def get_or_create_traffic_config(db: Session) -> TrafficMonitorConfig:
    config = db.get(TrafficMonitorConfig, 1)
    if config is None:
        config = TrafficMonitorConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def serialize_traffic_config(config: TrafficMonitorConfig) -> dict:
    return {
        "monitoring_enabled": config.monitoring_enabled,
        "auto_run_enabled": config.auto_run_enabled,
        "sampling_interval_minutes": 60,
        "threshold_percent": config.threshold_percent,
        "vendor": config.vendor,
        "workflow_key": config.workflow_key,
        "services": json.loads(config.services_json),
        "updated_at": config.updated_at.isoformat(),
    }


def serialize_traffic_sample(sample: NetworkTrafficSample) -> dict:
    return {
        "id": sample.id,
        "source_event_id": sample.source_event_id,
        "source": sample.source,
        "sampled_at": sample.sampled_at.isoformat(),
        "ps": sample.ps,
        "cs": sample.cs,
        "volte": sample.volte,
    }


def traffic_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return (current - previous) / previous * 100


def serialize_job(job: RPAJob) -> dict:
    return {
        "id": job.id,
        "name": job.name,
        "vendor": job.vendor,
        "workflow_key": job.workflow_key,
        "status": job.status,
        "payload": json.loads(job.payload_json),
        "result": json.loads(job.result_json),
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@app.get("/")
def frontend():
    if not FRONTEND_FILE.is_file():
        raise HTTPException(status_code=404, detail="Frontend file not found")
    return FileResponse(FRONTEND_FILE)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
        "time": utcnow().isoformat(),
        "vendor_execution": "adapter_not_configured",
    }


@app.get("/api/dashboard", dependencies=[Depends(require_api_key)])
def dashboard(db: Session = Depends(get_db)):
    counts = dict(
        db.query(RPAJob.status, func.count(RPAJob.id))
        .group_by(RPAJob.status)
        .all()
    )
    return {
        "total_jobs": sum(counts.values()),
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "vendors": ["aisware", "cyclone", "simulator"],
        "pagination": {"min_page": 1, "max_page": 100},
    }


@app.post(
    "/api/jobs", status_code=201, dependencies=[Depends(require_api_key)]
)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    job = RPAJob(
        name=body.name,
        vendor=body.vendor,
        workflow_key=body.workflow_key,
        payload_json=json.dumps(body.payload),
        status="queued",
    )
    db.add(job)
    db.flush()
    audit(
        db,
        "job.created",
        "rpa_job",
        str(job.id),
        f"vendor={job.vendor}; workflow={job.workflow_key}",
    )
    db.refresh(job)
    return serialize_job(job)


@app.get("/api/jobs", dependencies=[Depends(require_api_key)])
def list_jobs(
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(10, ge=1, le=100),
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
):
    query = select(RPAJob)
    if status:
        if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise HTTPException(status_code=400, detail="Unsupported status filter")
        query = query.where(RPAJob.status == status)
    if vendor:
        if vendor not in {"aisware", "cyclone", "simulator"}:
            raise HTTPException(status_code=400, detail="Unsupported vendor filter")
        query = query.where(RPAJob.vendor == vendor)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    jobs = db.scalars(
        query.order_by(RPAJob.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [serialize_job(job) for job in jobs],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": min(100, max(1, (total + page_size - 1) // page_size)),
    }


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(RPAJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return serialize_job(job)


@app.patch(
    "/api/jobs/{job_id}/status", dependencies=[Depends(require_api_key)]
)
def update_job_status(
    job_id: int, body: JobStatusUpdate, db: Session = Depends(get_db)
):
    job = db.get(RPAJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = body.status
    job.error_message = body.error_message
    job.updated_at = utcnow()
    db.commit()
    db.refresh(job)
    audit(
        db,
        "job.status_updated",
        "rpa_job",
        str(job.id),
        f"status={job.status}",
    )
    return serialize_job(job)


@app.get("/api/traffic/config", dependencies=[Depends(require_api_key)])
def get_traffic_config(db: Session = Depends(get_db)):
    return serialize_traffic_config(get_or_create_traffic_config(db))


@app.put("/api/traffic/config", dependencies=[Depends(require_api_key)])
def update_traffic_config(
    body: TrafficConfigUpdate, db: Session = Depends(get_db)
):
    config = get_or_create_traffic_config(db)
    config.monitoring_enabled = body.monitoring_enabled
    config.auto_run_enabled = body.auto_run_enabled
    config.threshold_percent = body.threshold_percent
    config.vendor = body.vendor
    config.workflow_key = body.workflow_key
    config.services_json = json.dumps(body.services)
    config.updated_at = utcnow()
    db.commit()
    db.refresh(config)
    audit(
        db,
        "traffic.config_updated",
        "traffic_monitor",
        "1",
        (
            f"monitoring={config.monitoring_enabled}; "
            f"auto_run={config.auto_run_enabled}; "
            f"threshold={config.threshold_percent}%"
        ),
    )
    return serialize_traffic_config(config)


@app.get("/api/traffic", dependencies=[Depends(require_api_key)])
def get_traffic(db: Session = Depends(get_db)):
    config = get_or_create_traffic_config(db)
    samples = db.scalars(
        select(NetworkTrafficSample)
        .order_by(NetworkTrafficSample.sampled_at.desc(), NetworkTrafficSample.id.desc())
        .limit(25)
    ).all()
    latest = samples[0] if samples else None
    previous = None
    if latest:
        previous = db.scalar(
            select(NetworkTrafficSample)
            .where(
                NetworkTrafficSample.source == latest.source,
                NetworkTrafficSample.sampled_at < latest.sampled_at,
            )
            .order_by(
                NetworkTrafficSample.sampled_at.desc(),
                NetworkTrafficSample.id.desc(),
            )
            .limit(1)
        )
    changes = {}
    if latest and previous:
        changes = {
            service: round(
                traffic_change(
                    getattr(latest, service.lower()),
                    getattr(previous, service.lower()),
                ),
                2,
            )
            for service in ("PS", "CS", "VoLTE")
        }
    return {
        "config": serialize_traffic_config(config),
        "latest": serialize_traffic_sample(latest) if latest else None,
        "previous": serialize_traffic_sample(previous) if previous else None,
        "changes_percent": changes,
        "samples": [serialize_traffic_sample(sample) for sample in reversed(samples)],
        "data_source": "authorized_telemetry" if latest else "awaiting_telemetry",
        "robot_execution": "adapter_not_configured",
    }


@app.post(
    "/api/traffic/samples",
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
def create_traffic_sample(body: TrafficSampleCreate, db: Session = Depends(get_db)):
    existing = db.scalar(
        select(NetworkTrafficSample).where(
            NetworkTrafficSample.source_event_id == body.source_event_id
        )
    )
    if existing:
        return {
            "sample": serialize_traffic_sample(existing),
            "triggered_jobs": [],
            "duplicate": True,
        }

    latest = db.scalar(
        select(NetworkTrafficSample)
        .where(NetworkTrafficSample.source == body.source)
        .order_by(NetworkTrafficSample.sampled_at.desc(), NetworkTrafficSample.id.desc())
        .limit(1)
    )
    sampled_at = body.sampled_at.astimezone(timezone.utc)
    latest_sampled_at = latest.sampled_at if latest else None
    if latest_sampled_at and latest_sampled_at.utcoffset() is None:
        latest_sampled_at = latest_sampled_at.replace(tzinfo=timezone.utc)
    if latest_sampled_at and sampled_at <= latest_sampled_at:
        raise HTTPException(
            status_code=409,
            detail="Traffic samples must arrive in chronological order",
        )

    sample = NetworkTrafficSample(
        source_event_id=body.source_event_id,
        source=body.source,
        sampled_at=sampled_at,
        ps=body.ps,
        cs=body.cs,
        volte=body.volte,
    )
    db.add(sample)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        duplicate = db.scalar(
            select(NetworkTrafficSample).where(
                NetworkTrafficSample.source_event_id == body.source_event_id
            )
        )
        if duplicate is None:
            raise
        return {
            "sample": serialize_traffic_sample(duplicate),
            "triggered_jobs": [],
            "duplicate": True,
        }
    config = get_or_create_traffic_config(db)
    enabled_services = set(json.loads(config.services_json))
    changes = {}
    triggered_jobs = []

    if latest:
        for service in ("PS", "CS", "VoLTE"):
            current_value = getattr(sample, service.lower())
            previous_value = getattr(latest, service.lower())
            change = traffic_change(current_value, previous_value)
            changes[service] = round(change, 2)
            if (
                config.monitoring_enabled
                and config.auto_run_enabled
                and service in enabled_services
                and abs(change) >= config.threshold_percent
            ):
                job = RPAJob(
                    name=f"MPT {service} traffic fluctuation",
                    vendor=config.vendor,
                    workflow_key=config.workflow_key,
                    status="queued",
                    payload_json=json.dumps(
                        {
                            "service": service,
                            "source": sample.source,
                            "source_event_id": sample.source_event_id,
                            "sample_id": sample.id,
                            "current_value": current_value,
                            "previous_value": previous_value,
                            "change_percent": round(change, 2),
                            "threshold_percent": config.threshold_percent,
                            "sampled_at": sample.sampled_at.isoformat(),
                        }
                    ),
                )
                db.add(job)
                db.flush()
                triggered_jobs.append(serialize_job(job))
                audit(
                    db,
                    "traffic.auto_run_queued",
                    "rpa_job",
                    str(job.id),
                    f"service={service}; change={change:.2f}%",
                )

    audit(
        db,
        "traffic.sample_received",
        "traffic_sample",
        str(sample.id),
        f"source={sample.source}; event={sample.source_event_id}",
    )
    db.refresh(sample)
    return {
        "sample": serialize_traffic_sample(sample),
        "changes_percent": changes,
        "triggered_jobs": triggered_jobs,
        "duplicate": False,
    }
