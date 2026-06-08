"""Dashboard-first prospect discovery workflow package."""

from prospect_system.prospect_config import ProspectSettings, load_prospect_settings
from prospect_system.dashboard_state import ProspectAnalysisState
from prospect_system.prospect_card import ProspectCard

__all__ = [
    "ProspectAnalysisState",
    "ProspectCard",
    "ProspectSettings",
    "load_prospect_settings",
]
