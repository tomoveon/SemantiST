"""Tool adapters for the full comparison runner."""

from .aflplusplus import AFLPlusPlusAdapter
from .icsfuzz import ICSFuzzAdapter
from .icsquartz import ICSQuartzAdapter
from .semantist import SemantiSTAdapter

ADAPTERS = {
    SemantiSTAdapter.tool_name: SemantiSTAdapter,
    AFLPlusPlusAdapter.tool_name: AFLPlusPlusAdapter,
    ICSFuzzAdapter.tool_name: ICSFuzzAdapter,
    ICSQuartzAdapter.tool_name: ICSQuartzAdapter,
}

DEFAULT_TOOL_ORDER = (
    SemantiSTAdapter.tool_name,
    AFLPlusPlusAdapter.tool_name,
    ICSQuartzAdapter.tool_name,
)
