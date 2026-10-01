"""One line of Baldur around an LLM SDK client.

```python
import baldur
from openai import OpenAI

client = baldur.llm.wrap(
    OpenAI(),
    fallbacks=[baldur.llm.Endpoint(OpenAI(base_url=...), model="...")],
)

@baldur.protected("summarize", dlq=True, replay=True)
def summarize(doc_id: str) -> str:
    response = client.chat.completions.create(model="gpt-4o-mini", messages=[...])
    return response.choices[0].message.content
```

Every call the wrapped client makes waits out a provider's rate limit or
overload together with every other worker, moves to the next endpoint when
waiting cannot save it, and is never retried or moved when the provider
rejected the request itself. When no endpoint answers, the call raises
``LLMUnavailableError``; a job decorated with ``replay=True`` is then parked
with its arguments and re-run when its breaker closes.

Covers the OpenAI Python SDK (and every OpenAI-compatible server it reaches),
the Anthropic SDK and the Google Gen AI SDK (``google-genai``).

Status: Public
"""

from __future__ import annotations

from baldur.llm._wrap import Endpoint, wrap

__all__ = ["Endpoint", "wrap"]
