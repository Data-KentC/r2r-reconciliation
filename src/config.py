# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/config.py
#
# Loads config.yaml and provides typed access to all settings.
# Every other module imports from here — never read config.yaml directly.
#
# Usage:
#   from src.config import config
#   threshold = config.materiality.variance_threshold_usd
# =============================================================================

import os
import yaml
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# -----------------------------------------------------------------------------
# CONFIG FILE PATH
# -----------------------------------------------------------------------------

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__),
    "../config.yaml"
)


# -----------------------------------------------------------------------------
# DATACLASSES — Typed wrappers for each config section
# Using dataclasses means IDE autocomplete works and typos fail loudly
# -----------------------------------------------------------------------------

@dataclass
class EntityConfig:
    name:                str
    currency:            str
    functional_currency: str
    primary_controller:  str
    backup_controller:   str
    telegram_chat_id:    str
    active:              bool


@dataclass
class NetSuiteFields:
    internal_id:    str
    line_sequence:  str
    tran_date:      str
    tran_id:        str
    account:        str
    debit_amount:   str
    credit_amount:  str
    currency:       str
    fx_amount:      str
    line_subsidiary: str
    cseg_ic:        str
    memo:           str


@dataclass
class ICAccountRange:
    start: str
    end:   str


@dataclass
class NetSuiteConfig:
    fields:             NetSuiteFields
    ic_due_from_range:  ICAccountRange
    ic_due_to_range:    ICAccountRange


@dataclass
class Tier3Config:
    enabled:            bool
    max_variance_usd:   float
    max_variance_pct:   float
    timing_gap_periods: int


@dataclass
class Tier4Config:
    enabled:            bool
    max_combination_depth: int
    tolerance_amount:   float


@dataclass
class Tier5Config:
    enabled:            bool
    min_confidence:     float
    batch_orphans:      bool


@dataclass
class FuzzyConfig:
    levenshtein_threshold: int
    jaro_winkler_weight:   float
    levenshtein_weight:    float
    ngram_weight:          float


@dataclass
class MatchingConfig:
    tier_3:  Tier3Config
    tier_4:  Tier4Config
    tier_5:  Tier5Config
    fuzzy:   FuzzyConfig


@dataclass
class MaterialityConfig:
    variance_threshold_usd: float
    variance_threshold_pct: float
    reporting_currency:     str
    fx_rounding_threshold:  float


@dataclass
class LLMProviderConfig:
    provider:  str
    model:     str
    rpm_limit: int


@dataclass
class LLMConfig:
    primary:    LLMProviderConfig
    secondary:  LLMProviderConfig
    tertiary:   LLMProviderConfig
    quaternary: LLMProviderConfig
    temperature:          float
    batch_orphans:        bool
    confidence_threshold: float


@dataclass
class EscalationConfig:
    je_posting_sla_warning_hours:    int
    je_posting_sla_escalation_hours: int
    je_posting_sla_critical_hours:   int
    exception_tier_1_days:           int
    exception_tier_2_days:           int
    exception_tier_3_days:           int
    head_of_accounting_email:        str
    cfo_email:                       str


@dataclass
class GovernanceConfig:
    last_verified_date:      datetime
    system_owner_email:      str
    executive_sponsor_email: str
    reminder_days:           int
    warning_days:            int
    failsafe_days:           int
    stp_alert_drop_pct:      int


@dataclass
class DatabaseConfig:
    provider:        str
    connection_port: int
    ssl_mode:        str
    pool_pre_ping:   bool
    pool_recycle:    int
    pool_size:       int
    max_overflow:    int
    sqlite_fallback_enabled: bool
    sqlite_fallback_path:    str


@dataclass
class OutputConfig:
    filename_pattern: str
    tab_0_name:       str
    tab_1_name:       str
    tab_2_name:       str
    tab_3_name:       str
    audit_tab_name:   str
    freeze_panes:     str


@dataclass
class AppConfig:
    """
    Root configuration object.
    Import this in every module:
        from src.config import config
    """
    environment:  str
    owner:        str
    entities:     Dict[str, EntityConfig]
    netsuite:     NetSuiteConfig
    matching:     MatchingConfig
    materiality:  MaterialityConfig
    llm:          LLMConfig
    escalation:   EscalationConfig
    governance:   GovernanceConfig
    database:     DatabaseConfig
    output:       OutputConfig

    # Derived helpers — computed once on load
    valid_entity_codes: List[str] = field(default_factory=list)
    valid_currencies:   List[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# LOADER
# -----------------------------------------------------------------------------

def _load_raw() -> dict:
    """Reads config.yaml and returns the raw dictionary."""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"config.yaml not found at {CONFIG_PATH}. "
            f"Ensure the file exists before running the pipeline."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_config(raw: dict) -> AppConfig:
    """
    Converts the raw YAML dictionary into typed dataclasses.
    Fails loudly with a descriptive error if any required field is missing.
    """
    try:
        # --- Entities ---
        entities = {}
        for code, e in raw["entities"].items():
            entities[code] = EntityConfig(
                name=                e["name"],
                currency=            e["currency"],
                functional_currency= e["functional_currency"],
                primary_controller=  e["primary_controller"],
                backup_controller=   e["backup_controller"],
                telegram_chat_id=    e.get("telegram_chat_id", ""),
                active=              e.get("active", True),
            )

        # --- NetSuite ---
        ns = raw["netsuite"]
        netsuite = NetSuiteConfig(
            fields=NetSuiteFields(**ns["fields"]),
            ic_due_from_range=ICAccountRange(**ns["ic_due_from_range"]),
            ic_due_to_range=ICAccountRange(**ns["ic_due_to_range"]),
        )

        # --- Matching ---
        m = raw["matching"]
        matching = MatchingConfig(
            tier_3=Tier3Config(
                enabled=            m["tier_3"]["enabled"],
                max_variance_usd=   m["tier_3"]["max_variance_usd"],
                max_variance_pct=   m["tier_3"]["max_variance_pct"],
                timing_gap_periods= m["tier_3"]["timing_gap_periods"],
            ),
            tier_4=Tier4Config(
                enabled=               m["tier_4"]["enabled"],
                max_combination_depth= m["tier_4"]["max_combination_depth"],
                tolerance_amount=      m["tier_4"]["tolerance_amount"],
            ),
            tier_5=Tier5Config(
                enabled=         m["tier_5"]["enabled"],
                min_confidence=  m["tier_5"]["min_confidence"],
                batch_orphans=   m["tier_5"]["batch_orphans"],
            ),
            fuzzy=FuzzyConfig(
                levenshtein_threshold= m["fuzzy"]["levenshtein_threshold"],
                jaro_winkler_weight=   m["fuzzy"]["jaro_winkler_weight"],
                levenshtein_weight=    m["fuzzy"]["levenshtein_weight"],
                ngram_weight=          m["fuzzy"]["ngram_weight"],
            ),
        )

        # --- Materiality ---
        mat = raw["materiality"]
        materiality = MaterialityConfig(
            variance_threshold_usd= mat["variance_threshold_usd"],
            variance_threshold_pct= mat["variance_threshold_pct"],
            reporting_currency=     mat["reporting_currency"],
            fx_rounding_threshold=  mat["fx_rounding_threshold"],
        )

        # --- LLM ---
        llm_raw = raw["llm"]
        llm = LLMConfig(
            primary=    LLMProviderConfig(**llm_raw["primary"]),
            secondary=  LLMProviderConfig(**llm_raw["secondary"]),
            tertiary=   LLMProviderConfig(**llm_raw["tertiary"]),
            quaternary= LLMProviderConfig(**llm_raw["quaternary"]),
            temperature=          llm_raw["settings"]["temperature"],
            batch_orphans=        llm_raw["settings"]["batch_orphans"],
            confidence_threshold= llm_raw["settings"]["confidence_threshold"],
        )

        # --- Escalation ---
        esc = raw["escalation"]
        escalation = EscalationConfig(
            je_posting_sla_warning_hours=    esc["je_posting_sla"]["warning_hours"],
            je_posting_sla_escalation_hours= esc["je_posting_sla"]["escalation_hours"],
            je_posting_sla_critical_hours=   esc["je_posting_sla"]["critical_hours"],
            exception_tier_1_days=           esc["exception_sla"]["tier_1_working_days"],
            exception_tier_2_days=           esc["exception_sla"]["tier_2_working_days"],
            exception_tier_3_days=           esc["exception_sla"]["tier_3_working_days"],
            head_of_accounting_email=        esc["head_of_accounting"]["email"],
            cfo_email=                       esc["cfo"]["email"],
        )

        # --- Governance ---
        gov = raw["governance"]
        governance = GovernanceConfig(
            last_verified_date=      datetime.strptime(
                                         gov["last_verified_date"], "%Y-%m-%d"
                                     ),
            system_owner_email=      gov["system_owner_email"],
            executive_sponsor_email= gov["executive_sponsor_email"],
            reminder_days=           gov["staleness_policy"]["reminder_days"],
            warning_days=            gov["staleness_policy"]["warning_days"],
            failsafe_days=           gov["staleness_policy"]["failsafe_days"],
            stp_alert_drop_pct=      gov["stp_monitoring"]["alert_if_drop_pct"],
        )

        # --- Database ---
        db = raw["database"]
        database = DatabaseConfig(
            provider=        db["provider"],
            connection_port= db["connection_port"],
            ssl_mode=        db["ssl_mode"],
            pool_pre_ping=   db["pool_pre_ping"],
            pool_recycle=    db["pool_recycle"],
            pool_size=       db["pool_size"],
            max_overflow=    db["max_overflow"],
            sqlite_fallback_enabled= db["sqlite_fallback"]["enabled"],
            sqlite_fallback_path=    db["sqlite_fallback"]["path"],
        )

        # --- Output ---
        out = raw["output"]["excel"]
        output = OutputConfig(
            filename_pattern= out["filename_pattern"],
            tab_0_name=       out["tab_0_name"],
            tab_1_name=       out["tab_1_name"],
            tab_2_name=       out["tab_2_name"],
            tab_3_name=       out["tab_3_name"],
            audit_tab_name=   out["audit_tab_name"],
            freeze_panes=     out["freeze_panes"],
        )

        # --- Build root config ---
        cfg = AppConfig(
            environment=  raw["system"]["environment"],
            owner=        raw["system"]["owner"],
            entities=     entities,
            netsuite=     netsuite,
            matching=     matching,
            materiality=  materiality,
            llm=          llm,
            escalation=   escalation,
            governance=   governance,
            database=     database,
            output=       output,
        )

        # --- Derived helpers ---
        cfg.valid_entity_codes = [
            code for code, e in entities.items() if e.active
        ]
        cfg.valid_currencies = list(set(
            e.currency for e in entities.values() if e.active
        ))

        return cfg

    except KeyError as e:
        raise KeyError(
            f"Missing required field in config.yaml: {e}. "
            f"Check that all sections are present and correctly indented."
        ) from e


# -----------------------------------------------------------------------------
# GOVERNANCE CHECKS
# Run on every startup. Warns but never hard-stops the pipeline.
# -----------------------------------------------------------------------------

def _check_governance(cfg: AppConfig) -> None:
    """
    Evaluates config staleness and prints warnings.
    Never raises an exception — pipeline always continues.
    Fail-safe routing is handled by the notifier, not here.
    """
    age_days = (datetime.now() - cfg.governance.last_verified_date).days

    if age_days <= cfg.governance.reminder_days:
        return  # All good — no action needed

    if age_days <= cfg.governance.warning_days:
        print(
            f"[CONFIG] REMINDER: config.yaml has not been verified for "
            f"{age_days} days. Please review contacts and update "
            f"last_verified_date. Due in "
            f"{cfg.governance.reminder_days - age_days} days."
        )
        return

    if age_days <= cfg.governance.failsafe_days:
        print(
            f"[CONFIG] WARNING: config.yaml is {age_days} days old "
            f"(threshold: {cfg.governance.warning_days} days). "
            f"Escalation contacts may be stale. "
            f"Fail-safe routing activates in "
            f"{cfg.governance.failsafe_days - age_days} days."
        )
        return

    # Beyond failsafe threshold — log prominently but continue
    print(
        f"[CONFIG] FAIL-SAFE ACTIVE: config.yaml is {age_days} days old. "
        f"All alerts will be routed to CFO only until config is re-verified. "
        f"Update last_verified_date after reviewing all contacts."
    )


def _validate_environment() -> None:
    """
    Checks that required environment variables exist.
    Warns if missing — does not crash so developer can run locally
    with partial config during development.
    """
    required_secrets = [
        "SUPABASE_POOLER_URL",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
    ]
    optional_secrets = [
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "GMAIL_REFRESH_TOKEN",
        "GOOGLE_DRIVE_FOLDER_ID",
        "GOOGLE_SHEETS_ID",
        "TELEGRAM_BOT_TOKEN",
    ]

    missing_required = [s for s in required_secrets if not os.getenv(s)]
    missing_optional = [s for s in optional_secrets if not os.getenv(s)]

    if missing_required:
        print(
            f"[CONFIG] WARNING: Required environment variables not set: "
            f"{missing_required}. "
            f"Pipeline will use SQLite fallback for database operations."
        )

    if missing_optional:
        print(
            f"[CONFIG] INFO: Optional environment variables not set: "
            f"{missing_optional}. "
            f"Related features will be disabled."
        )


# -----------------------------------------------------------------------------
# MODULE-LEVEL SINGLETON
# Loaded once when first imported. All modules share the same instance.
# -----------------------------------------------------------------------------

def _initialise() -> AppConfig:
    """Loads, parses, and validates config on first import."""
    raw = _load_raw()
    cfg = _parse_config(raw)
    _check_governance(cfg)
    _validate_environment()
    return cfg


# This is what all other modules import:
#   from src.config import config
config: AppConfig = _initialise()


# -----------------------------------------------------------------------------
# CONVENIENCE HELPERS
# Frequently used lookups — call these instead of accessing config directly
# -----------------------------------------------------------------------------

def get_entity(entity_code: str) -> EntityConfig:
    """Returns EntityConfig for a given entity code. Raises if not found."""
    if entity_code not in config.entities:
        raise ValueError(
            f"Entity '{entity_code}' not found in config.yaml. "
            f"Valid entities: {config.valid_entity_codes}"
        )
    return config.entities[entity_code]


def get_functional_currency(entity_code: str) -> str:
    """Returns the functional currency for an entity."""
    return get_entity(entity_code).functional_currency


def is_valid_entity(entity_code: str) -> bool:
    """Returns True if the entity code is in the active APAC 5."""
    return entity_code in config.valid_entity_codes


def is_failsafe_mode() -> bool:
    """
    Returns True if config staleness has exceeded the failsafe threshold.
    Notifier checks this to decide whether to route alerts to CFO only.
    """
    age_days = (datetime.now() - config.governance.last_verified_date).days
    return age_days > config.governance.failsafe_days


def get_escalation_recipient(
    entity_code: str,
    hours_overdue: float
) -> str:
    """
    Returns the correct escalation email based on hours overdue.
    Implements the DoA matrix from config.yaml.
    If in failsafe mode, always returns CFO email.
    """
    if is_failsafe_mode():
        return config.escalation.cfo_email

    entity = get_entity(entity_code)
    esc = config.escalation

    if hours_overdue <= esc.je_posting_sla_warning_hours:
        return ""  # Not yet due
    elif hours_overdue <= esc.je_posting_sla_escalation_hours:
        return entity.primary_controller
    elif hours_overdue <= esc.je_posting_sla_critical_hours:
        return f"{entity.primary_controller},{entity.backup_controller}"
    else:
        return config.escalation.cfo_email


def get_netsuite_column(field_name: str) -> str:
    """
    Returns the exact NetSuite column header for a given field name.
    Use this instead of hardcoding column names in the ingestor.
    """
    return getattr(config.netsuite.fields, field_name)
