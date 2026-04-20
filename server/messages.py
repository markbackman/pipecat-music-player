"""Custom bus messages for the music player example."""

from dataclasses import dataclass
from typing import Any

from pipecat_subagents.bus import BusDataMessage


@dataclass
class BusUIContextMessage(BusDataMessage):
    """Carries a UI interaction event from the client to the UI agent.

    Parameters:
        data: The raw client event payload forwarded from RTVI.
    """

    data: Any | None = None
