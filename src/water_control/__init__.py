"""稳油控水协同调度领域包。"""

from .coordination import ALGORITHM_VERSION
from .service import WaterControlService

__all__ = ["ALGORITHM_VERSION", "WaterControlService"]
