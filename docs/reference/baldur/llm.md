# baldur.llm — LLM client wrap

One line around an LLM SDK client: every call it makes shares the provider's
waits across workers, retries on Baldur's coordinated ladder instead of the
SDK's own, moves to the next endpoint when waiting cannot save it, and raises
`LLMUnavailableError` when no endpoint answers. Covers the OpenAI Python SDK
(and every OpenAI-compatible server it reaches), the Anthropic SDK and the
Google Gen AI SDK (`google-genai`).

`baldur.llm` is loaded on first access, so `import baldur` does not import it.
How the provider's answers are classified, and what happens to a job no
endpoint could answer, is on the [LLM API rate limits](../../llm-rate-limits.md)
page.

::: baldur.llm.wrap

::: baldur.llm.Endpoint
