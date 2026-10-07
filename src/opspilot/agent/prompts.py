"""Load versioned prompt files and fill them safely.

Prompt files live in ``src/opspilot/prompts/*.md``. The first line is ``version: <id>``.
Placeholders are ``{{name}}`` (double braces, so JSON examples can use single braces).
Untrusted text (tool output, logs, retrieved documents, alert text) is always wrapped
with :func:`untrusted`, which neutralises any attempt to close the wrapper early.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
SECTIONS = (
    "## Role",
    "## Retrieved content",
    "## Instructions",
    "## Examples",
    "## Critical reminders",
)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_CLOSE_TAG = re.compile(r"</?\s*untrusted_data", re.IGNORECASE)


class PromptError(ValueError):
    """A prompt file is malformed or a placeholder was not provided."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    sha256: str
    template: str

    @property
    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.template))

    def render(self, **values: str) -> str:
        """Fill every placeholder; unknown or missing names are errors."""
        missing = self.placeholders - set(values)
        extra = set(values) - self.placeholders
        if missing or extra:
            raise PromptError(f"{self.name}: missing {sorted(missing)}, unexpected {sorted(extra)}")
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], self.template)


@cache
def load_prompt(name: str) -> Prompt:
    """Load ``prompts/<name>.md`` and check its version line and 5-part structure."""
    path = PROMPTS_DIR / f"{name}.md"
    text = path.read_text()
    first, _, body = text.partition("\n")
    if not first.startswith("version:"):
        raise PromptError(f"{path.name}: first line must be 'version: <id>'")
    positions = [body.find(section) for section in SECTIONS]
    if -1 in positions or positions != sorted(positions):
        raise PromptError(f"{path.name}: needs sections {SECTIONS} in that order")
    return Prompt(
        name=name,
        version=first.removeprefix("version:").strip(),
        sha256=hashlib.sha256(text.encode()).hexdigest()[:12],
        template=body.strip() + "\n",
    )


def untrusted(source: str, text: str) -> str:
    """Wrap data in an <untrusted_data> block that the data itself cannot close."""
    safe = _CLOSE_TAG.sub("[untrusted-tag-removed]", text)
    return f'<untrusted_data source="{source}">\n{safe}\n</untrusted_data>'
