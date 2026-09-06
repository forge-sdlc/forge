"""Provider-neutral identification of Forge-authored comments."""


def is_self_comment(
    sender_login: str,
    comment_body: str | None,
    bot_login: str,
    prefix: str | None = None,
) -> bool:
    """Return whether a normalized comment was authored by this Forge identity."""
    body = comment_body or ""
    sender = sender_login.lower()
    bot = bot_login.lower()
    same_identity = sender == bot or sender == f"{bot}[bot]"
    if sender.endswith("[bot]") and same_identity:
        return True
    if prefix and prefix.strip():
        if not same_identity:
            return False
        stripped = prefix.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            candidates = (prefix, stripped)
        else:
            candidates = (prefix, stripped, f"<!-- {stripped} -->", f"<!--{stripped}-->")
        return body.startswith(candidates) or body.lstrip().startswith(candidates)
    return same_identity


__all__ = ["is_self_comment"]
