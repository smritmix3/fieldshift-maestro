"""Check your keys and the fallback chain: `python -m maestro.check_keys`. Prints masked keys and a
tiny test call per endpoint. Never prints a full key."""
from __future__ import annotations

from . import config
from .llm import LLMError, build_chain


def main() -> None:
    print("Keys found:")
    print("  GEMINI_API_KEY     ", config.mask(config.get("GEMINI_API_KEY")))
    print("  OPENROUTER_API_KEY ", config.mask(config.get("OPENROUTER_API_KEY")))
    if not config.ENV_PATH.exists():
        print(f"\nNo .env file at {config.ENV_PATH}. Copy .env.example to .env and add your key.")
    chain = build_chain()
    if not chain.endpoints:
        print("\nNo provider configured. The app will run in template-only mode.")
        return
    print("\nEndpoints:")
    for ep in chain.endpoints:
        print(f"  {ep.name:18} {ep.model:45} {ep.state()}")
    print("\nTest call per endpoint (tiny JSON request):")
    for ep in chain.endpoints:
        if not ep.ready():
            print(f"  {ep.name:18} skipped ({ep.state()})")
            continue
        try:
            ep.complete("Reply with JSON only.", 'Return {"ok": true}', True, 30, 15.0)
            print(f"  {ep.name:18} OK")
        except LLMError as e:
            print(f"  {ep.name:18} FAILED: {e.kind} ({e})")


if __name__ == "__main__":
    main()
