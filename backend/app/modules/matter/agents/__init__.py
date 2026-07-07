"""Matter module agents — importing this package registers every handler."""

from app.modules.matter.agents import trademark  # noqa: F401 — registers handler + hook
from app.modules.matter.agents import litigation  # noqa: F401 — litigation_summarizer v2
