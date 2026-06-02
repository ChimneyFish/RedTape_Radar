from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship, declarative_base, sessionmaker
from sqlalchemy import create_engine
from datetime import datetime

DATABASE_URL = "sqlite:///./redtape_radar.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency helper to yield database sessions to FastAPI routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class User(Base):
    """Internal user directory managing local and SSO roles."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=True)
    role = Column(String(50), default="read_only")  # 'admin' or 'read_only'
    is_active = Column(Boolean, default=True)
    is_local = Column(Boolean, default=False)       # True for break-glass login
    hashed_password = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

class MonitoredTarget(Base):
    """Websites tracked for regulatory adjustments."""
    __tablename__ = "monitored_targets"

    id = Column(Integer, primary_key=True, index=True)
    resource = Column(String(50), nullable=False)  # e.g., "OSHA"
    url = Column(String(2048), unique=True, nullable=False)
    extraction_mode = Column(String(20), default="auto_clean")
    keyword_anchor = Column(String(100), nullable=True)
    last_scanned = Column(DateTime, default=datetime.utcnow)
    last_hash = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True)

    drafts = relationship("AlertDraft", back_populates="target", cascade="all, delete-orphan")

class AlertDraft(Base):
    """AI-extracted facts waiting for human validation."""
    __tablename__ = "alert_drafts"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(Integer, ForeignKey("monitored_targets.id"), nullable=False)
    topic = Column(String(255), nullable=False)
    summary_raw = Column(Text, nullable=False)
    detected_dates = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_reviewed = Column(Boolean, default=False)

    target = relationship("MonitoredTarget", back_populates="drafts")

class PublishedAlert(Base):
    """Finalized compliance logs approved by the department."""
    __tablename__ = "published_alerts"

    id = Column(Integer, primary_key=True, index=True)
    resource = Column(String(50), nullable=False)
    url = Column(String(2048), nullable=False)
    topic = Column(String(255), nullable=False)
    summary = Column(Text, nullable=False)
    actionable_steps = Column(Text, nullable=False)
    key_deadlines = Column(String(100), nullable=True)
    published_at = Column(DateTime, default=datetime.utcnow)
    confluence_page_id = Column(String(100), nullable=True)

class AppConfig(Base):
    """System settings (Entra ID details & Confluence API Tokens)."""
    __tablename__ = "app_config"

    key = Column(String(50), primary_key=True, index=True)
    value = Column(String(500), nullable=True)
    is_secret = Column(Boolean, default=False)