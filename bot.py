from __future__ import annotations

import logging
import os
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from merge import (
    dry_run_report,
    get_or_create_webhook,
    merge_posts,
    parse_thread_ref,
    redirect_post,
    validate_threads,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("thread-weaver")


class ThreadWeaverBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced commands to guild %s", guild_id)
        else:
            await self.tree.sync()
            logger.info("Synced commands globally")


client = ThreadWeaverBot()


@client.event
async def on_ready():
    logger.info("Logged in as %s (id: %s)", client.user, client.user.id)


@client.tree.command(name="merge", description="Merge one forum post into another")
@app_commands.describe(
    target="Target post — link or ID (omit to use current thread)",
    source="Source post — link or ID (messages will be moved from here)",
    dry_run="Preview the merge without making changes",
)
async def merge_command(
    interaction: discord.Interaction,
    source: str,
    target: Optional[str] = None,
    dry_run: bool = False,
):
    # Must be in a guild.
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    # Permission gate: caller must have manage_threads.
    if not interaction.permissions.manage_threads:
        await interaction.response.send_message(
            "You need the **Manage Threads** permission to use this command.",
            ephemeral=True,
        )
        return

    # Check bot permissions.
    bot_perms = interaction.app_permissions
    missing = []
    if not bot_perms.manage_threads:
        missing.append("Manage Threads")
    if not bot_perms.send_messages:
        missing.append("Send Messages")
    if not bot_perms.read_message_history:
        missing.append("Read Message History")
    if not bot_perms.attach_files:
        missing.append("Attach Files")
    if not bot_perms.manage_webhooks:
        missing.append("Manage Webhooks")
    if missing:
        await interaction.response.send_message(
            f"I'm missing permissions: **{', '.join(missing)}**. "
            "Please fix my role and try again.",
            ephemeral=True,
        )
        return

    # Resolve target: explicit argument or auto-detect current thread.
    try:
        if target is not None:
            target_id = parse_thread_ref(target)
        else:
            if isinstance(
                getattr(interaction.channel, "parent", None), discord.ForumChannel
            ):
                target_id = interaction.channel.id
            else:
                await interaction.response.send_message(
                    "When omitting `target`, run this command inside a forum post.\n"
                    "Or provide both: `/merge target:<link> source:<link>`",
                    ephemeral=True,
                )
                return

        source_id = parse_thread_ref(source)
    except ValueError as exc:
        await interaction.response.send_message(
            f"**Error:** {exc}", ephemeral=True
        )
        return

    # Defer — merges take a while.
    await interaction.response.defer(ephemeral=True)

    try:
        target_thread, source_thread = await validate_threads(
            interaction.guild, target_id, source_id
        )

        # Dry-run mode: just report and exit.
        if dry_run:
            report = await dry_run_report(target_thread, source_thread)
            await interaction.followup.send(report)
            return

        # Get or create a webhook on the forum channel.
        forum_channel = target_thread.parent
        webhook = await get_or_create_webhook(forum_channel, client.user)

        # Progress callback using interaction followup edits.
        status_content = [""]

        async def progress_callback(text: str):
            status_content[0] = text
            try:
                await interaction.edit_original_response(content=text)
            except discord.HTTPException:
                pass

        count = await merge_posts(
            target_thread, source_thread, webhook, progress_callback
        )
        logger.info(
            "Merge complete: %d messages moved from #%s (%s) into #%s (%s)",
            count,
            source_thread.name,
            source_thread.id,
            target_thread.name,
            target_thread.id,
        )

    except (ValueError, RuntimeError) as exc:
        await interaction.followup.send(f"**Error:** {exc}")
    except discord.HTTPException as exc:
        logger.exception("Discord API error during merge")
        await interaction.followup.send(
            f"**Discord API error:** {exc.text} (code {exc.code})"
        )


@client.tree.command(name="redirect", description="Close a duplicate post and tag its users into another")
@app_commands.describe(
    target="Target post — link or ID (omit to use current thread)",
    source="Duplicate post — link or ID (will be deleted)",
)
async def redirect_command(
    interaction: discord.Interaction,
    source: str,
    target: Optional[str] = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    if not interaction.permissions.manage_threads:
        await interaction.response.send_message(
            "You need the **Manage Threads** permission to use this command.",
            ephemeral=True,
        )
        return

    bot_perms = interaction.app_permissions
    missing = []
    if not bot_perms.manage_threads:
        missing.append("Manage Threads")
    if not bot_perms.send_messages:
        missing.append("Send Messages")
    if not bot_perms.read_message_history:
        missing.append("Read Message History")
    if missing:
        await interaction.response.send_message(
            f"I'm missing permissions: **{', '.join(missing)}**. "
            "Please fix my role and try again.",
            ephemeral=True,
        )
        return

    try:
        if target is not None:
            target_id = parse_thread_ref(target)
        else:
            if isinstance(
                getattr(interaction.channel, "parent", None), discord.ForumChannel
            ):
                target_id = interaction.channel.id
            else:
                await interaction.response.send_message(
                    "When omitting `target`, run this command inside a forum post.\n"
                    "Or provide both: `/redirect target:<link> source:<link>`",
                    ephemeral=True,
                )
                return

        source_id = parse_thread_ref(source)
    except ValueError as exc:
        await interaction.response.send_message(
            f"**Error:** {exc}", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_thread, source_thread = await validate_threads(
            interaction.guild, target_id, source_id
        )

        async def progress_callback(text: str):
            try:
                await interaction.edit_original_response(content=text)
            except discord.HTTPException:
                pass

        await redirect_post(target_thread, source_thread, progress_callback)
        logger.info(
            "Redirect: users from #%s (%s) tagged into #%s (%s)",
            source_thread.name,
            source_thread.id,
            target_thread.name,
            target_thread.id,
        )

    except (ValueError, RuntimeError) as exc:
        await interaction.followup.send(f"**Error:** {exc}")
    except discord.HTTPException as exc:
        logger.exception("Discord API error during redirect")
        await interaction.followup.send(
            f"**Discord API error:** {exc.text} (code {exc.code})"
        )


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN not set. Copy .env.example to .env and fill it in."
        )
    client.run(token)


if __name__ == "__main__":
    main()
