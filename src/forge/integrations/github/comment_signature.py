"""Sign and recognize the Forge bot's own GitHub comments.

These helpers let Forge tag its outbound comments with a configurable signature
and detect its own inbound comments, so shared bot/developer accounts do not
trigger webhook feedback loops. They live in the GitHub integration layer
because both the outbound client and the orchestrator webhook handlers depend
on them.
"""


def is_self_comment(
    sender_login: str,
    comment_body: str | None,
    bot_login: str,
    prefix: str | None = None,
) -> bool:
    """Determine if an incoming comment or review belongs to the bot itself.

    When a signature ``prefix`` is configured, a comment counts as the bot's own
    only if the sender matches the bot AND the body carries the signature; this
    lets a shared account post manual comments (without the signature) that Forge
    still treats as human feedback. Without a prefix it falls back to username
    matching. GitHub App accounts (``name[bot]``) are always treated as the bot.
    """
    comment_body = comment_body or ""
    sender_lower = sender_login.lower()
    bot_lower = bot_login.lower()

    # Check if the sender is our bot or matches our bot suffix
    is_same_bot = sender_lower == bot_lower or sender_lower == f"{bot_lower}[bot]"

    if sender_lower.endswith("[bot]") and is_same_bot:
        return True

    if prefix and prefix.strip():
        if is_same_bot:
            prefix_stripped = prefix.strip()
            prefixes_to_check: tuple[str, ...]
            if prefix_stripped.startswith("<!--") and prefix_stripped.endswith("-->"):
                wrapped_prefix = prefix_stripped
                prefixes_to_check = (prefix, prefix_stripped, wrapped_prefix)
            else:
                wrapped_prefix = f"<!-- {prefix_stripped} -->"
                wrapped_prefix_no_space = f"<!--{prefix_stripped}-->"
                prefixes_to_check = (
                    prefix,
                    prefix_stripped,
                    wrapped_prefix,
                    wrapped_prefix_no_space,
                )

            return comment_body.startswith(prefixes_to_check) or comment_body.lstrip().startswith(
                prefixes_to_check
            )
        return False

    return is_same_bot


def prepend_bot_prefix(comment_body: str | None, prefix: str | None = None) -> str:
    """Prepend a bot signature/comment prefix to the comment body."""
    comment_body = comment_body or ""
    if prefix is None:
        from forge.config import get_settings

        prefix = get_settings().forge_bot_comment_prefix

    if not prefix:
        return comment_body

    prefix_stripped = prefix.strip()
    if not prefix_stripped:
        return comment_body

    if prefix_stripped.startswith("<!--") and prefix_stripped.endswith("-->"):
        wrapped_prefix = prefix_stripped
    else:
        wrapped_prefix = f"<!-- {prefix_stripped} -->"

    if comment_body.startswith(wrapped_prefix) or comment_body.lstrip().startswith(wrapped_prefix):
        return comment_body

    if not comment_body:
        return wrapped_prefix

    return f"{wrapped_prefix}\n\n{comment_body}"
