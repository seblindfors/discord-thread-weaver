from __future__ import annotations

import io
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord import Webhook

logger = logging.getLogger(__name__)

# Tracks thread IDs currently being merged to prevent overlapping merges.
_active_merges: set[int] = set()

# Per-channel webhook cache to avoid repeated API calls during a merge.
_webhook_cache: Dict[int, Webhook] = {}

# Discord link patterns:
#   3-segment: https://discord.com/channels/<guild>/<channel>/<thread>
#   2-segment: https://discord.com/channels/<guild>/<thread>  (forum posts)
_LINK_3 = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)
_LINK_2 = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:[?\s#]|$)"
)

WEBHOOK_NAME = "Thread Weaver"


def parse_thread_ref(arg: str) -> int:
    """Extract a thread ID from a Discord message link or a raw numeric ID."""
    match = _LINK_3.search(arg)
    if match:
        return int(match.group(3))
    match = _LINK_2.search(arg)
    if match:
        return int(match.group(2))
    arg = arg.strip()
    if arg.isdigit():
        return int(arg)
    raise ValueError(f"Could not parse a thread ID from: `{arg}`")


async def validate_threads(
    guild: discord.Guild, target_id: int, source_id: int
) -> Tuple[discord.Thread, discord.Thread]:
    """Fetch both threads and run sanity checks."""
    if target_id == source_id:
        raise ValueError("Cannot merge a post into itself.")

    target = guild.get_thread(target_id) or await _fetch_thread(guild, target_id)
    source = guild.get_thread(source_id) or await _fetch_thread(guild, source_id)

    if target is None:
        raise ValueError(f"Could not find target thread `{target_id}`.")
    if source is None:
        raise ValueError(f"Could not find source thread `{source_id}`.")

    # Both must be forum/media-channel threads (their parent is a ForumChannel).
    for label, thread in [("Target", target), ("Source", source)]:
        if not isinstance(thread.parent, (discord.ForumChannel, discord.channel.ForumChannel)):
            raise ValueError(f"{label} (`{thread.name}`) is not a forum post.")

    # Must be in the same guild.
    if target.guild.id != guild.id or source.guild.id != guild.id:
        raise ValueError("Both threads must be in the same server.")

    return target, source


async def _fetch_thread(guild: discord.Guild, thread_id: int) -> Optional[discord.Thread]:
    """Try to fetch a thread that isn't cached."""
    try:
        return await guild.fetch_channel(thread_id)  # type: ignore[return-value]
    except (discord.NotFound, discord.Forbidden):
        return None


async def get_or_create_webhook(channel: discord.ForumChannel, bot_user: discord.User) -> Webhook:
    """Get an existing bot-owned webhook on the forum channel, or create one."""
    if channel.id in _webhook_cache:
        return _webhook_cache[channel.id]

    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.user and wh.user.id == bot_user.id and wh.name == WEBHOOK_NAME:
            _webhook_cache[channel.id] = wh
            return wh

    webhook = await channel.create_webhook(name=WEBHOOK_NAME)
    _webhook_cache[channel.id] = webhook
    return webhook


async def fetch_all_messages(thread: discord.Thread) -> List[discord.Message]:
    """Retrieve the complete message history of a thread, oldest-first."""
    messages: List[discord.Message] = []
    async for msg in thread.history(limit=None, oldest_first=True):
        messages.append(msg)
    return messages


async def replay_message(
    webhook: Webhook, target: discord.Thread, original: discord.Message
) -> None:
    """Replay a single message into the target thread via webhook."""
    content = original.content or ""

    # Download attachments.
    files: List[discord.File] = []
    fallback_links: List[str] = []
    for attachment in original.attachments:
        try:
            data = await attachment.read()
            files.append(discord.File(io.BytesIO(data), filename=attachment.filename))
        except (discord.HTTPException, discord.NotFound):
            fallback_links.append(f"[{attachment.filename}]({attachment.url})")

    if fallback_links:
        content += ("\n" if content else "") + "\n".join(fallback_links)

    # Forward any embeds from the original message (e.g. merge headers from prior merges).
    embeds: List[discord.Embed] = list(original.embeds)

    # Add a timestamp footer for messages older than 1 day.
    age = datetime.now(timezone.utc) - original.created_at
    if age > timedelta(days=1):
        timestamp_str = discord.utils.format_dt(original.created_at, style="f")
        embed = discord.Embed(description=f"Originally posted {timestamp_str}")
        embed.colour = discord.Colour.light_grey()
        embeds.append(embed)

    # Determine author identity.
    display_name = original.author.display_name
    avatar_url = original.author.display_avatar.url if original.author.display_avatar else None

    # Suppress mentions in replayed content — only the merge header should ping.
    no_mentions = discord.AllowedMentions.none()

    # Split if the content exceeds Discord's 2000-char limit.
    chunks = _split_message(content) if content else [""]

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        try:
            await webhook.send(
                content=chunk,
                username=display_name,
                avatar_url=avatar_url,
                thread=target,
                files=files if is_last else [],
                embeds=embeds if is_last else [],
                allowed_mentions=no_mentions,
            )
        except discord.HTTPException as exc:
            if is_last and files and exc.status == 413:
                # Attachments too large — fall back to CDN links.
                file_links = " ".join(
                    f"[{a.filename}]({a.url})" for a in original.attachments
                )
                fallback = f"{chunk}\n{file_links}".strip() if chunk else file_links
                await webhook.send(
                    content=fallback,
                    username=display_name,
                    avatar_url=avatar_url,
                    thread=target,
                    embeds=embeds,
                    allowed_mentions=no_mentions,
                )
            else:
                raise
        await asyncio.sleep(1.2)


def _split_message(text: str, limit: int = 2000) -> List[str]:
    """Split text into chunks that fit within Discord's message limit."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split on a newline near the limit.
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def dry_run_report(
    target: discord.Thread, source: discord.Thread
) -> str:
    """Generate a preview summary of what a merge would do."""
    messages = await fetch_all_messages(source)
    total = len(messages)
    with_attachments = sum(1 for m in messages if m.attachments)

    attachment_info = f" ({with_attachments} with attachments)" if with_attachments else ""
    return (
        f"**Dry run — no changes made.**\n\n"
        f"Would move **{total}** message(s){attachment_info} "
        f"from **{source.name}** into **{target.name}**.\n"
        f"The source post would be deleted."
    )


async def merge_posts(
    target: discord.Thread,
    source: discord.Thread,
    webhook: Webhook,
    progress_callback,
) -> int:
    """Orchestrate a full merge from source into target.

    Returns the number of messages moved.
    Raises on unrecoverable errors.
    """
    if source.id in _active_merges:
        raise RuntimeError("A merge is already running for this source post.")

    _active_merges.add(source.id)
    try:
        # 1. Lock the source thread to prevent new messages.
        try:
            await source.edit(locked=True)
        except discord.Forbidden:
            raise RuntimeError(
                "I don't have permission to lock the source thread. "
                "I need the **Manage Threads** permission."
            )

        # 2. Fetch source messages.
        messages = await fetch_all_messages(source)
        if not messages:
            raise ValueError("Source post has no messages to merge.")

        await progress_callback(f"Merging {len(messages)} message(s)...")

        # 3. Send a header with the source post's title, mentioning only new authors.
        #    Collect real authors + user IDs mentioned in previous merge headers
        #    (webhook-replayed messages have bot authors, so we also parse <@id> from content).
        target_messages = await fetch_all_messages(target)
        existing_authors = set(m.author.id for m in target_messages)
        existing_authors.update(
            int(uid) for m in target_messages
            for uid in re.findall(r"<@!?(\d+)>", m.content or "")
        )
        source_authors: dict[int, None] = {}
        for m in messages:
            if not m.author.bot:
                source_authors.setdefault(m.author.id, None)
            for uid in re.findall(r"<@!?(\d+)>", m.content or ""):
                source_authors.setdefault(int(uid), None)
        new_authors = {uid: None for uid in source_authors if uid not in existing_authors}
        mentions = " ".join(f"<@{uid}>" for uid in new_authors)
        header_embed = discord.Embed(
            description=f"**Merged from: {source.name}**",
            colour=discord.Colour.blue(),
        )
        header_content = mentions if mentions else None
        await target.send(content=header_content, embed=header_embed)
        await asyncio.sleep(1.2)

        # 4. Replay each message into the target.
        for i, msg in enumerate(messages, 1):
            await replay_message(webhook, target, msg)
            if i % 10 == 0:
                await progress_callback(
                    f"Merging... {i}/{len(messages)} messages moved."
                )

        # 4. Delete the source thread.
        try:
            await source.delete()
        except discord.Forbidden:
            logger.warning("Could not delete source thread %s — missing permissions.", source.id)
            await progress_callback(
                f"Merged {len(messages)} message(s) into this post. "
                f"**Warning:** I couldn't delete the source post — please remove it manually."
            )
            return len(messages)

        # 5. Confirm.
        await progress_callback(
            f"Merge complete — {len(messages)} message(s) moved into this post."
        )
        return len(messages)

    finally:
        _active_merges.discard(source.id)
