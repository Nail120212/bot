import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
import aiohttp
import base64
import hashlib
import urllib.parse
import random
import datetime
import re
import io
from collections import defaultdict
from groq import AsyncGroq

# ─── ENV VARS ───────────────────────────────────────────────────────────────
TOKEN    = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
GROQ_KEY = os.environ["GROQ_API_KEY"]

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# ─── DATA HELPERS ────────────────────────────────────────────────────────────
def load(name):
    p = f"{DATA_DIR}/{name}.json"
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

def save(name, data):
    with open(f"{DATA_DIR}/{name}.json", "w") as f:
        json.dump(data, f, indent=2)

# ─── BOT SETUP ───────────────────────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None, owner_id=OWNER_ID)
tree = bot.tree

# ─── GROQ CLIENT ─────────────────────────────────────────────────────────────
groq_client = AsyncGroq(api_key=GROQ_KEY)

# ─── CONTAINER EMBED HELPER ──────────────────────────────────────────────────
def box(title=None, description=None, fields=None, footer=None, thumbnail=None, image=None):
    """Embed with no left colour stripe (discord.Colour.default())."""
    e = discord.Embed(
        title=title,
        description=description,
        colour=discord.Colour.default()
    )
    if fields:
        for name, value, inline in fields:
            e.add_field(name=name, value=value, inline=inline)
    if footer:
        e.set_footer(text=footer)
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    if image:
        e.set_image(url=image)
    return e

# ─── SNIPE STORAGE ───────────────────────────────────────────────────────────
snipe_cache  = {}   # channel_id -> (author, content, timestamp)
esnipe_cache = {}   # channel_id -> (author, before, after, timestamp)

# ─── DATA ACCESSORS ──────────────────────────────────────────────────────────
def get_warns(guild_id, user_id):
    w = load("warns")
    return w.get(str(guild_id), {}).get(str(user_id), [])

def add_warn(guild_id, user_id, reason, mod_id):
    w = load("warns")
    g = str(guild_id); u = str(user_id)
    w.setdefault(g, {}).setdefault(u, [])
    w[g][u].append({"reason": reason, "mod": str(mod_id), "time": str(datetime.datetime.utcnow())})
    save("warns", w)

def remove_last_warn(guild_id, user_id):
    w = load("warns")
    g = str(guild_id); u = str(user_id)
    if w.get(g, {}).get(u):
        w[g][u].pop()
        save("warns", w)

def clear_all_warns(guild_id, user_id):
    w = load("warns")
    g = str(guild_id); u = str(user_id)
    if g in w:
        w[g].pop(u, None)
    save("warns", w)

def is_whitelisted(guild_id, user_id):
    d = load("security")
    return str(user_id) in d.get(str(guild_id), {}).get("whitelist", [])

def is_extra_owner(guild_id, user_id):
    d = load("security")
    return str(user_id) in d.get(str(guild_id), {}).get("extraowners", [])

def is_role_protected(guild_id, role_id):
    d = load("security")
    return str(role_id) in d.get(str(guild_id), {}).get("roleprotect", [])

def get_security(guild_id):
    d = load("security")
    return d.get(str(guild_id), {})

def set_security(guild_id, data):
    d = load("security")
    d[str(guild_id)] = data
    save("security", d)

def get_ai_config(guild_id):
    d = load("ai_config")
    return d.get(str(guild_id), {})

def set_ai_config(guild_id, data):
    d = load("ai_config")
    d[str(guild_id)] = data
    save("ai_config", d)

def get_log_channels(guild_id):
    d = load("logs")
    return d.get(str(guild_id), {})

def set_log_channels(guild_id, data):
    d = load("logs")
    d[str(guild_id)] = data
    save("logs", d)

def get_config(guild_id):
    d = load("config")
    return d.get(str(guild_id), {})

def set_config(guild_id, data):
    d = load("config")
    d[str(guild_id)] = data
    save("config", d)

# ─── GROQ AI ─────────────────────────────────────────────────────────────────
async def ask_groq(messages_payload, system_prompt=None):
    sys = system_prompt or "You are a helpful Discord bot assistant. Be concise and friendly."
    response = await groq_client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[{"role": "system", "content": sys}] + messages_payload,
        max_tokens=1024
    )
    return response.choices[0].message.content

# ─── PERMISSION CHECKS ───────────────────────────────────────────────────────
def admin_check():
    async def pred(ctx):
        if ctx.author.id == OWNER_ID: return True
        if ctx.guild and ctx.author.guild_permissions.administrator: return True
        raise commands.MissingPermissions(["administrator"])
    return commands.check(pred)

def owner_check():
    async def pred(ctx):
        if ctx.author.id == OWNER_ID: return True
        if ctx.guild and ctx.guild.owner_id == ctx.author.id: return True
        raise commands.NotOwner()
    return commands.check(pred)

# ─── LOG HELPER ──────────────────────────────────────────────────────────────
async def send_log(guild, log_type, embed):
    if not guild: return
    lc = get_log_channels(guild.id)
    ch_id = lc.get(log_type)
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            try: await ch.send(embed=embed)
            except: pass

# ─────────────────────────────────────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name=".help | Railway"))

@bot.event
async def on_message_delete(message):
    if message.author.bot: return
    snipe_cache[message.channel.id] = (message.author, message.content, datetime.datetime.utcnow())
    await send_log(message.guild, "messagelogs", box(
        "📭 Message Deleted",
        f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:** {message.content or '*[no text]*'}"
    ))

@bot.event
async def on_message_edit(before, after):
    if before.author.bot: return
    esnipe_cache[before.channel.id] = (before.author, before.content, after.content, datetime.datetime.utcnow())
    await send_log(before.guild, "messagelogs", box(
        "✏️ Message Edited",
        f"**Author:** {before.author.mention}\n**Before:** {before.content}\n**After:** {after.content}"
    ))

@bot.event
async def on_member_join(member):
    cfg = get_config(member.guild.id)
    ar = cfg.get("autorole")
    if ar:
        role = member.guild.get_role(int(ar))
        if role:
            try: await member.add_roles(role)
            except: pass
    an = cfg.get("autonick")
    if an:
        try: await member.edit(nick=an)
        except: pass
    wch = cfg.get("welcome_channel")
    if wch:
        ch = member.guild.get_channel(int(wch))
        if ch:
            msg = cfg.get("welcome_msg", f"Welcome {member.mention} to **{member.guild.name}**!")
            await ch.send(embed=box("👋 Welcome!", msg.replace("{user}", member.mention).replace("{server}", member.guild.name)))
    await send_log(member.guild, "joinlogs", box("📥 Member Joined", f"{member.mention} (`{member.id}`)"))

@bot.event
async def on_member_remove(member):
    cfg = get_config(member.guild.id)
    lch = cfg.get("leave_channel")
    if lch:
        ch = member.guild.get_channel(int(lch))
        if ch:
            msg = cfg.get("goodbye_msg", f"**{member.name}** has left the server.")
            await ch.send(embed=box("👋 Goodbye!", msg))
    await send_log(member.guild, "leavelogs", box("📤 Member Left", f"{member.mention} (`{member.id}`)"))

@bot.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await send_log(after.guild, "nicknamelogs", box(
            "📝 Nickname Changed",
            f"**User:** {after.mention}\n**Before:** {before.nick or before.name}\n**After:** {after.nick or after.name}"
        ))
    if before.roles != after.roles:
        added   = [r for r in after.roles  if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        desc = ""
        if added:   desc += f"**Added:** {', '.join(r.mention for r in added)}\n"
        if removed: desc += f"**Removed:** {', '.join(r.mention for r in removed)}"
        await send_log(after.guild, "rolelogs", box("🎭 Roles Updated", f"**User:** {after.mention}\n{desc}"))

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel != after.channel:
        desc = f"{member.mention} joined **{after.channel.name}**" if after.channel else f"{member.mention} left **{before.channel.name}**"
        await send_log(member.guild, "voicelogs", box("🔊 Voice Update", desc))

@bot.event
async def on_member_ban(guild, user):
    await send_log(guild, "modlogs", box("🔨 Member Banned", f"{user.mention} (`{user.id}`)"))

@bot.event
async def on_member_unban(guild, user):
    await send_log(guild, "modlogs", box("✅ Member Unbanned", f"{user.mention} (`{user.id}`)"))

@bot.event
async def on_guild_channel_create(channel):
    await send_log(channel.guild, "channellogs", box("📢 Channel Created", f"**{channel.name}** (`{channel.id}`)"))

@bot.event
async def on_guild_channel_delete(channel):
    await send_log(channel.guild, "channellogs", box("🗑️ Channel Deleted", f"**{channel.name}** (`{channel.id}`)"))

@bot.event
async def on_guild_emojis_update(guild, before, after):
    added   = [e for e in after  if e not in before]
    removed = [e for e in before if e not in after]
    for e in added:   await send_log(guild, "emojilogs", box("😀 Emoji Added",   f"**{e.name}** {e}"))
    for e in removed: await send_log(guild, "emojilogs", box("🗑️ Emoji Removed", f"**{e.name}**"))

@bot.event
async def on_guild_stickers_update(guild, before, after):
    added   = [s for s in after  if s not in before]
    removed = [s for s in before if s not in after]
    for s in added:   await send_log(guild, "stickerlogs", box("🖼️ Sticker Added",   s.name))
    for s in removed: await send_log(guild, "stickerlogs", box("🗑️ Sticker Removed", s.name))

@bot.event
async def on_message(message):
    if message.author.bot: return

    # ── AI ────────────────────────────────────────────────────────────────────
    if message.guild:
        ai = get_ai_config(message.guild.id)
        ai_ch      = ai.get("channel")
        ai_mention = bot.user in message.mentions
        in_ai_ch   = ai_ch and str(message.channel.id) == str(ai_ch)

        if in_ai_ch or ai_mention:
            prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if prompt:
                sys_prompt = ai.get("system_prompt", "You are a helpful Discord bot. Be concise and friendly.")
                async with message.channel.typing():
                    try:
                        reply = await ask_groq([{"role": "user", "content": prompt}], sys_prompt)
                        # split if over 4000 chars
                        if len(reply) > 4000:
                            reply = reply[:4000] + "…"
                        await message.reply(embed=box("🤖 AI Response", reply))
                    except Exception as e:
                        await message.reply(embed=box("❌ AI Error", str(e)))
                return

    await bot.process_commands(message)

# ─────────────────────────────────────────────────────────────────────────────
#  SLASH COMMANDS
# ─────────────────────────────────────────────────────────────────────────────
@tree.command(name="setai", description="Set the channel where AI responds to all messages")
@app_commands.describe(
    channel="Channel for AI to respond in",
    system_prompt="Custom system prompt (server/bot owner only)"
)
@app_commands.default_permissions(administrator=True)
async def setai(interaction: discord.Interaction, channel: discord.TextChannel, system_prompt: str = None):
    ai = get_ai_config(interaction.guild.id)
    ai["channel"] = str(channel.id)
    if system_prompt and (interaction.user.id == OWNER_ID or interaction.guild.owner_id == interaction.user.id):
        ai["system_prompt"] = system_prompt
    set_ai_config(interaction.guild.id, ai)
    extra = "\nSystem prompt updated." if system_prompt else ""
    await interaction.response.send_message(embed=box(
        "✅ AI Channel Set",
        f"AI will respond in {channel.mention}.\nYou can also mention me anywhere.{extra}"
    ), ephemeral=True)

@tree.command(name="setprompt", description="Change the AI system prompt (server/bot owner only)")
@app_commands.describe(prompt="New system prompt")
async def setprompt(interaction: discord.Interaction, prompt: str):
    if interaction.user.id != OWNER_ID and interaction.guild.owner_id != interaction.user.id:
        return await interaction.response.send_message(
            embed=box("❌ No Permission", "Only the server/bot owner can change the AI prompt."), ephemeral=True)
    ai = get_ai_config(interaction.guild.id)
    ai["system_prompt"] = prompt
    set_ai_config(interaction.guild.id, ai)
    await interaction.response.send_message(embed=box("✅ Prompt Updated", f"*{prompt}*"), ephemeral=True)

# ─────────────────────────────────────────────────────────────────────────────
#  PREFIX COMMANDS — HELP
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="help")
async def help_cmd(ctx, *, section=None):
    sections = {
        "security": ("🛡️ Security",
            ".security .automod .antinuke .anticheck\n"
            ".whitelist add/remove/list/info/clear\n"
            ".extraowner add/remove/list\n"
            ".roleprotect add/remove/list\n"
            ".quarantine .quarantineadd\n"
            ".jail setup/remove/list  •  .jailuser\n"
            ".lockdown .unlockdown"),
        "utility": ("🔧 Utility",
            ".say .embed .poll .translate .remind .timer .calculator .quote\n"
            ".snipe .editsnipe .stealemoji .stealsticker\n"
            ".emojiinfo .stickerinfo .enlarge .serveremojis .serverstickers\n"
            ".copyavatar .copybanner .copyusername .copynickname\n"
            ".copyrole .copychannel .copycategory\n"
            ".firstmessage .membercount .inviteinfo\n"
            ".rolelist .roles .channels .categories .emojis .stickers\n"
            ".servericon .serverbanner\n"
            ".randommember .randomrole .randomchannel .randomemoji\n"
            ".qr .hash .reverse .base64encode .base64decode\n"
            ".urlencode .urldecode .timestamp\n"
            ".choose .coinflip .dice .randomnumber"),
        "mod": ("🔨 Moderation",
            ".ban .softban .tempban .unban .unbanall .kick\n"
            ".mute .unmute .unmuteall\n"
            ".warn .warnings .removewarn .clearwarns\n"
            ".nick .role .lock .unlock .hide .unhide\n"
            ".purge .purgebot .slowmode .unslowmode\n"
            ".inspect .list .massrole .massnick"),
        "info": ("📊 Information",
            ".help .afk .avatar .banner .userinfo .roleinfo\n"
            ".serverinfo .channelinfo .botinfo .ping .uptime\n"
            ".boosters .boostinfo .serverstats .memberstats\n"
            ".auditlog .createdat .joinedat .memberposition\n"
            ".badges .permissions .whois"),
        "logs": ("📝 Logging",
            ".setuplogs .setchannel .setuser\n"
            ".messagelogs .memberlogs .modlogs .voicelogs\n"
            ".boostlogs .joinlogs .leavelogs .rolelogs\n"
            ".nicknamelogs .channellogs .emojilogs\n"
            ".stickerlogs .securitylogs .antinukelogs"),
        "config": ("⚙️ Config",
            ".prefix .settings .setwelcome .setleave\n"
            ".autorole .autonick .verification\n"
            ".welcome .goodbye .resetconfig\n"
            "/setai  •  /setprompt"),
    }
    if section and section.lower() in sections:
        title, cmds = sections[section.lower()]
        await ctx.send(embed=box(title, cmds))
    else:
        fields = [(t, c, False) for _, (t, c) in sections.items()]
        await ctx.send(embed=box(
            "📖 Help",
            "Use `.help <section>` for details.\n"
            "Sections: `security` `utility` `mod` `info` `logs` `config`\n"
            "**Prefix:** `.`  |  AI: mention me or use `/setai`",
            fields=fields
        ))

# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY
# ─────────────────────────────────────────────────────────────────────────────
@bot.command()
async def ping(ctx):
    await ctx.send(embed=box("🏓 Pong!", f"Latency: **{round(bot.latency * 1000)}ms**"))

start_time = datetime.datetime.utcnow()

@bot.command()
async def uptime(ctx):
    delta = datetime.datetime.utcnow() - start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    await ctx.send(embed=box("⏱️ Uptime", f"`{h}h {m}m {s}s`"))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def say(ctx, *, message):
    await ctx.message.delete()
    await ctx.send(message)

@bot.command(name="embed")
@commands.has_permissions(manage_messages=True)
async def embed_cmd(ctx, title, *, desc=""):
    await ctx.send(embed=box(title, desc))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def poll(ctx, *, question):
    msg = await ctx.send(embed=box("📊 Poll", question))
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")

@bot.command()
async def translate(ctx, *, text):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text)}&langpair=auto|en"
        ) as r:
            data = await r.json()
            translated = data["responseData"]["translatedText"]
    await ctx.send(embed=box("🌐 Translation",
        f"**Original:** {text}\n**Translated:** {translated}"))

@bot.command()
async def remind(ctx, time: str, *, reminder):
    seconds = 0
    try:
        if "h" in time:   seconds = int(time.replace("h","")) * 3600
        elif "m" in time: seconds = int(time.replace("m","")) * 60
        elif "s" in time: seconds = int(time.replace("s",""))
        else: raise ValueError
    except ValueError:
        return await ctx.send(embed=box("❌ Format", "Use: `.remind 10m do something`"))
    await ctx.send(embed=box("⏰ Reminder Set", f"I'll remind you in **{time}**: {reminder}"))
    await asyncio.sleep(seconds)
    await ctx.send(f"{ctx.author.mention}", embed=box("⏰ Reminder!", reminder))

@bot.command(name="timer")
async def timer_cmd(ctx, time: str):
    seconds = 0
    try:
        if "h" in time:   seconds = int(time.replace("h","")) * 3600
        elif "m" in time: seconds = int(time.replace("m","")) * 60
        elif "s" in time: seconds = int(time.replace("s",""))
    except ValueError:
        return await ctx.send(embed=box("❌ Format", "Use: `.timer 10m`"))
    await ctx.send(embed=box("⏱️ Timer Started", f"Timer for **{time}** started!"))
    await asyncio.sleep(seconds)
    await ctx.send(f"{ctx.author.mention}", embed=box("⏰ Timer Done!", f"Your **{time}** timer has ended!"))

@bot.command()
async def calculator(ctx, *, expr):
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        await ctx.send(embed=box("🧮 Calculator", f"`{expr}` = **{result}**"))
    except:
        await ctx.send(embed=box("❌ Error", "Invalid expression."))

@bot.command()
async def quote(ctx, message_id: int):
    try:
        msg = await ctx.channel.fetch_message(message_id)
        await ctx.send(embed=box(
            f"💬 Quote from {msg.author}", msg.content,
            footer=msg.created_at.strftime("%Y-%m-%d %H:%M")))
    except:
        await ctx.send(embed=box("❌ Not Found", "Message not found."))

@bot.command()
async def snipe(ctx):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send(embed=box("👻 Snipe", "Nothing to snipe!"))
    author, content, ts = data
    await ctx.send(embed=box(
        f"👻 Sniped from {author}", content or "*[no text]*",
        footer=ts.strftime("%H:%M:%S"), thumbnail=str(author.display_avatar)))

@bot.command()
async def editsnipe(ctx):
    data = esnipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send(embed=box("📝 Edit Snipe", "Nothing to snipe!"))
    author, before, after, ts = data
    await ctx.send(embed=box(
        f"📝 Edit Sniped from {author}",
        f"**Before:** {before}\n**After:** {after}",
        footer=ts.strftime("%H:%M:%S")))

@bot.command()
@commands.has_permissions(manage_expressions=True)
async def stealemoji(ctx, emoji: discord.PartialEmoji):
    async with aiohttp.ClientSession() as s:
        async with s.get(str(emoji.url)) as r:
            data = await r.read()
    new = await ctx.guild.create_custom_emoji(name=emoji.name, image=data)
    await ctx.send(embed=box("✅ Emoji Stolen", f"Added {new} as `{new.name}`"))

@bot.command()
@commands.has_permissions(manage_expressions=True)
async def stealsticker(ctx):
    if not ctx.message.stickers:
        return await ctx.send(embed=box("❌ No Sticker", "Attach a sticker to your message."))
    sticker = await ctx.message.stickers[0].fetch()
    async with aiohttp.ClientSession() as s:
        async with s.get(sticker.url) as r:
            data = await r.read()
    new = await ctx.guild.create_sticker(
        name=sticker.name, description=sticker.description or "stolen",
        emoji="⭐", file=discord.File(fp=io.BytesIO(data), filename="sticker.png"))
    await ctx.send(embed=box("✅ Sticker Stolen", f"Added sticker **{new.name}**"))

@bot.command()
async def emojiinfo(ctx, emoji: discord.Emoji):
    await ctx.send(embed=box("😀 Emoji Info",
        f"**Name:** {emoji.name}\n**ID:** {emoji.id}\n**Animated:** {emoji.animated}\n**Server:** {emoji.guild}",
        image=str(emoji.url)))

@bot.command()
async def stickerinfo(ctx, sticker_id: int):
    try:
        s = await ctx.guild.fetch_sticker(sticker_id)
        await ctx.send(embed=box("🖼️ Sticker Info",
            f"**Name:** {s.name}\n**ID:** {s.id}\n**Description:** {s.description}"))
    except:
        await ctx.send(embed=box("❌ Not Found", "Sticker not found."))

@bot.command()
async def enlarge(ctx, emoji: discord.PartialEmoji):
    await ctx.send(embed=box("🔍 Enlarged Emoji", emoji.name, image=str(emoji.url)))

@bot.command()
async def serveremojis(ctx):
    emojis = " ".join(str(e) for e in ctx.guild.emojis) or "None"
    await ctx.send(embed=box("😀 Server Emojis", emojis[:4000]))

@bot.command()
async def serverstickers(ctx):
    stickers = "\n".join(s.name for s in ctx.guild.stickers) or "None"
    await ctx.send(embed=box("🖼️ Server Stickers", stickers))

@bot.command()
async def copyavatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=box(f"🖼️ {member}'s Avatar", "", image=str(member.display_avatar)))

@bot.command()
async def copybanner(ctx, member: discord.Member = None):
    member = member or ctx.author
    user = await bot.fetch_user(member.id)
    if not user.banner:
        return await ctx.send(embed=box("❌ No Banner", "User has no banner."))
    await ctx.send(embed=box(f"🖼️ {member}'s Banner", "", image=str(user.banner.url)))

@bot.command()
async def copyusername(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=box("📋 Username", f"`{member.name}`"))

@bot.command()
async def copynickname(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=box("📋 Nickname", f"`{member.nick or 'None'}`"))

@bot.command()
@commands.has_permissions(manage_roles=True)
async def copyrole(ctx, role: discord.Role):
    new = await ctx.guild.create_role(
        name=role.name, colour=role.colour,
        permissions=role.permissions, hoist=role.hoist, mentionable=role.mentionable)
    await ctx.send(embed=box("✅ Role Copied", f"Created {new.mention}"))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def copychannel(ctx, channel: discord.TextChannel):
    new = await channel.clone(name=channel.name)
    await ctx.send(embed=box("✅ Channel Copied", f"Created {new.mention}"))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def copycategory(ctx, category: discord.CategoryChannel):
    new = await category.clone(name=category.name)
    await ctx.send(embed=box("✅ Category Copied", f"Created **{new.name}**"))

@bot.command()
async def firstmessage(ctx):
    async for msg in ctx.channel.history(limit=1, oldest_first=True):
        await ctx.send(embed=box("📜 First Message",
            f"**Author:** {msg.author}\n**Content:** {msg.content}\n[Jump]({msg.jump_url})"))

@bot.command()
async def membercount(ctx):
    await ctx.send(embed=box("👥 Member Count", str(ctx.guild.member_count)))

@bot.command()
async def inviteinfo(ctx, invite: discord.Invite = None):
    if invite:
        await ctx.send(embed=box("🔗 Invite Info",
            f"**Code:** {invite.code}\n**Uses:** {invite.uses}\n**Inviter:** {invite.inviter}"))
    else:
        invites = await ctx.guild.invites()
        desc = "\n".join(f"`{i.code}` — {i.uses} uses ({i.inviter})" for i in invites[:10]) or "None"
        await ctx.send(embed=box("🔗 Server Invites", desc))

@bot.command()
async def rolelist(ctx):
    roles = "\n".join(r.mention for r in ctx.guild.roles[1:]) or "None"
    await ctx.send(embed=box("🎭 Roles", roles[:4000]))

@bot.command(name="roles")
async def roles_cmd(ctx):
    await rolelist(ctx)

@bot.command()
async def channels(ctx):
    chs = "\n".join(c.mention for c in ctx.guild.text_channels) or "None"
    await ctx.send(embed=box("📢 Channels", chs[:4000]))

@bot.command()
async def categories(ctx):
    cats = "\n".join(c.name for c in ctx.guild.categories) or "None"
    await ctx.send(embed=box("📁 Categories", cats[:4000]))

@bot.command()
async def emojis(ctx):
    await serveremojis(ctx)

@bot.command()
async def stickers(ctx):
    await serverstickers(ctx)

@bot.command()
async def servericon(ctx):
    await ctx.send(embed=box("🖼️ Server Icon", ctx.guild.name,
        image=str(ctx.guild.icon) if ctx.guild.icon else None))

@bot.command()
async def serverbanner(ctx):
    if not ctx.guild.banner:
        return await ctx.send(embed=box("❌ No Banner", "Server has no banner."))
    await ctx.send(embed=box("🖼️ Server Banner", ctx.guild.name, image=str(ctx.guild.banner)))

@bot.command()
async def randommember(ctx):
    m = random.choice(ctx.guild.members)
    await ctx.send(embed=box("🎲 Random Member", m.mention, thumbnail=str(m.display_avatar)))

@bot.command()
async def randomrole(ctx):
    r = random.choice(ctx.guild.roles)
    await ctx.send(embed=box("🎲 Random Role", r.mention))

@bot.command()
async def randomchannel(ctx):
    c = random.choice(ctx.guild.text_channels)
    await ctx.send(embed=box("🎲 Random Channel", c.mention))

@bot.command()
async def randomemoji(ctx):
    if not ctx.guild.emojis:
        return await ctx.send(embed=box("❌ No Emojis", "Server has no custom emojis."))
    e = random.choice(ctx.guild.emojis)
    await ctx.send(embed=box("🎲 Random Emoji", str(e)))

@bot.command()
async def qr(ctx, *, text):
    url = f"https://api.qrserver.com/v1/create-qr-code/?data={urllib.parse.quote(text)}&size=200x200"
    await ctx.send(embed=box("🔲 QR Code", text, image=url))

@bot.command(name="hash")
async def hash_cmd(ctx, *, text):
    h = hashlib.sha256(text.encode()).hexdigest()
    await ctx.send(embed=box("🔒 SHA-256 Hash", f"**Input:** `{text}`\n**Hash:** `{h}`"))

@bot.command()
async def reverse(ctx, *, text):
    await ctx.send(embed=box("🔄 Reversed", text[::-1]))

@bot.command()
async def base64encode(ctx, *, text):
    await ctx.send(embed=box("📦 Base64 Encoded", base64.b64encode(text.encode()).decode()))

@bot.command()
async def base64decode(ctx, *, text):
    try:
        await ctx.send(embed=box("📦 Base64 Decoded", base64.b64decode(text).decode()))
    except:
        await ctx.send(embed=box("❌ Error", "Invalid base64 string."))

@bot.command()
async def urlencode(ctx, *, text):
    await ctx.send(embed=box("🔗 URL Encoded", urllib.parse.quote(text)))

@bot.command()
async def urldecode(ctx, *, text):
    await ctx.send(embed=box("🔗 URL Decoded", urllib.parse.unquote(text)))

@bot.command()
async def timestamp(ctx):
    now = int(datetime.datetime.utcnow().timestamp())
    await ctx.send(embed=box("🕐 Timestamp", f"Unix: `{now}`\nDiscord: <t:{now}:F>"))

@bot.command()
async def choose(ctx, *, choices):
    opts = [c.strip() for c in choices.split(",")]
    await ctx.send(embed=box("🎲 Choose", f"I chose: **{random.choice(opts)}**"))

@bot.command()
async def coinflip(ctx):
    await ctx.send(embed=box("🪙 Coin Flip", random.choice(["Heads!", "Tails!"])))

@bot.command()
async def dice(ctx):
    await ctx.send(embed=box("🎲 Dice Roll", f"You rolled: **{random.randint(1, 6)}**"))

@bot.command()
async def randomnumber(ctx, minimum: int = 1, maximum: int = 100):
    await ctx.send(embed=box("🎲 Random Number",
        f"**{random.randint(minimum, maximum)}** (between {minimum}–{maximum})"))

# ─────────────────────────────────────────────────────────────────────────────
#  INFORMATION
# ─────────────────────────────────────────────────────────────────────────────
afk_users = {}

@bot.command()
async def afk(ctx, *, reason="AFK"):
    afk_users[ctx.author.id] = reason
    await ctx.send(embed=box("😴 AFK Set", f"{ctx.author.mention} is now AFK: {reason}"))

@bot.listen("on_message")
async def afk_listener(message):
    if message.author.bot: return
    if message.author.id in afk_users:
        del afk_users[message.author.id]
        await message.channel.send(embed=box("👋 Welcome Back!", f"{message.author.mention}, AFK removed."))
    for mention in message.mentions:
        if mention.id in afk_users:
            await message.channel.send(embed=box("😴 User AFK",
                f"{mention.mention} is AFK: {afk_users[mention.id]}"))

@bot.command()
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=box(f"🖼️ {member}'s Avatar", "", image=str(member.display_avatar)))

@bot.command()
async def banner(ctx, member: discord.Member = None):
    member = member or ctx.author
    user = await bot.fetch_user(member.id)
    if not user.banner:
        return await ctx.send(embed=box("❌ No Banner", "No banner found."))
    await ctx.send(embed=box(f"🖼️ {member}'s Banner", "", image=str(user.banner.url)))

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    fields = [
        ("ID",       str(m.id),                                              True),
        ("Nickname", m.nick or "None",                                       True),
        ("Top Role", m.top_role.mention,                                     True),
        ("Joined",   m.joined_at.strftime("%Y-%m-%d") if m.joined_at else "?", True),
        ("Created",  m.created_at.strftime("%Y-%m-%d"),                      True),
        ("Bot",      str(m.bot),                                             True),
    ]
    await ctx.send(embed=box(str(m), "", fields=fields, thumbnail=str(m.display_avatar)))

@bot.command()
async def roleinfo(ctx, role: discord.Role):
    fields = [
        ("ID",          str(role.id),                           True),
        ("Members",     str(len(role.members)),                 True),
        ("Mentionable", str(role.mentionable),                  True),
        ("Hoisted",     str(role.hoist),                        True),
        ("Created",     role.created_at.strftime("%Y-%m-%d"),   True),
    ]
    await ctx.send(embed=box(f"🎭 {role.name}", "", fields=fields))

@bot.command()
async def serverinfo(ctx):
    g = ctx.guild
    fields = [
        ("Owner",   str(g.owner),                          True),
        ("Members", str(g.member_count),                   True),
        ("Channels",str(len(g.channels)),                  True),
        ("Roles",   str(len(g.roles)),                     True),
        ("Emojis",  str(len(g.emojis)),                    True),
        ("Boosts",  str(g.premium_subscription_count),     True),
        ("Created", g.created_at.strftime("%Y-%m-%d"),     True),
    ]
    await ctx.send(embed=box(f"🏠 {g.name}", f"ID: {g.id}", fields=fields,
        thumbnail=str(g.icon) if g.icon else None))

@bot.command()
async def channelinfo(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    fields = [
        ("ID",       str(ch.id),                          True),
        ("Category", str(ch.category),                    True),
        ("NSFW",     str(ch.is_nsfw()),                   True),
        ("Slowmode", f"{ch.slowmode_delay}s",             True),
        ("Created",  ch.created_at.strftime("%Y-%m-%d"),  True),
    ]
    await ctx.send(embed=box(f"📢 #{ch.name}", ch.topic or "", fields=fields))

@bot.command()
async def botinfo(ctx):
    fields = [
        ("Name",    str(bot.user),                  True),
        ("ID",      str(bot.user.id),               True),
        ("Servers", str(len(bot.guilds)),            True),
        ("Ping",    f"{round(bot.latency*1000)}ms", True),
        ("Prefix",  ".",                            True),
    ]
    await ctx.send(embed=box("🤖 Bot Info", "", fields=fields,
        thumbnail=str(bot.user.display_avatar)))

@bot.command()
async def boosters(ctx):
    bl = ctx.guild.premium_subscribers
    if not bl:
        return await ctx.send(embed=box("💎 Boosters", "No boosters found."))
    await ctx.send(embed=box("💎 Boosters", "\n".join(m.mention for m in bl)))

@bot.command()
async def boostinfo(ctx):
    g = ctx.guild
    await ctx.send(embed=box("💎 Boost Info",
        f"**Level:** {g.premium_tier}\n"
        f"**Boosts:** {g.premium_subscription_count}\n"
        f"**Boosters:** {len(g.premium_subscribers)}"))

@bot.command()
async def serverstats(ctx):
    g = ctx.guild
    humans = sum(1 for m in g.members if not m.bot)
    bots   = sum(1 for m in g.members if m.bot)
    await ctx.send(embed=box("📊 Server Stats",
        f"**Total:** {g.member_count}\n**Humans:** {humans}\n**Bots:** {bots}\n"
        f"**Channels:** {len(g.channels)}\n**Roles:** {len(g.roles)}"))

@bot.command()
async def memberstats(ctx, member: discord.Member = None):
    m = member or ctx.author
    await ctx.send(embed=box(f"📊 {m}'s Stats",
        f"**Roles:** {len(m.roles)-1}\n"
        f"**Joined:** {m.joined_at.strftime('%Y-%m-%d') if m.joined_at else '?'}\n"
        f"**Created:** {m.created_at.strftime('%Y-%m-%d')}"))

@bot.command()
async def auditlog(ctx, limit: int = 5):
    if not ctx.author.guild_permissions.view_audit_log:
        return await ctx.send(embed=box("❌ No Permission", "You need View Audit Log."))
    entries = []
    async for entry in ctx.guild.audit_logs(limit=limit):
        entries.append(f"**{entry.action.name}** by {entry.user} — `{entry.created_at.strftime('%H:%M:%S')}`")
    await ctx.send(embed=box("📋 Audit Log", "\n".join(entries) or "No entries."))

@bot.command()
async def createdat(ctx, member: discord.Member = None):
    m = member or ctx.author
    await ctx.send(embed=box("📅 Account Created",
        f"{m.mention} created on **{m.created_at.strftime('%B %d, %Y')}**"))

@bot.command()
async def joinedat(ctx, member: discord.Member = None):
    m = member or ctx.author
    joined = m.joined_at.strftime('%B %d, %Y') if m.joined_at else "?"
    await ctx.send(embed=box("📅 Joined Server", f"{m.mention} joined on **{joined}**"))

@bot.command()
async def memberposition(ctx, member: discord.Member = None):
    m = member or ctx.author
    sorted_members = sorted(ctx.guild.members, key=lambda x: x.joined_at or datetime.datetime.utcnow())
    pos = sorted_members.index(m) + 1
    await ctx.send(embed=box("📊 Join Position", f"{m.mention} is member **#{pos}**"))

@bot.command()
async def badges(ctx, member: discord.Member = None):
    m = member or ctx.author
    user = await bot.fetch_user(m.id)
    flags = [f.name.replace("_", " ").title() for f in user.public_flags.all()] or ["None"]
    await ctx.send(embed=box(f"🏅 {m}'s Badges", "\n".join(flags)))

@bot.command()
async def permissions(ctx, member: discord.Member = None):
    m = member or ctx.author
    perms = [p.replace("_", " ").title() for p, v in m.guild_permissions if v]
    await ctx.send(embed=box(f"🔒 {m}'s Permissions", "\n".join(perms) or "None"))

@bot.command()
async def whois(ctx, member: discord.Member = None):
    await userinfo(ctx, member)

# ─────────────────────────────────────────────────────────────────────────────
#  MODERATION
# ─────────────────────────────────────────────────────────────────────────────
@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if member.top_role >= ctx.author.top_role and ctx.author.id != OWNER_ID:
        return await ctx.send(embed=box("❌ Hierarchy Error", "You can't ban someone with a higher or equal role."))
    await member.ban(reason=reason)
    await ctx.send(embed=box("🔨 Banned", f"{member} has been banned.\n**Reason:** {reason}"))
    await send_log(ctx.guild, "modlogs", box("🔨 Ban", f"{member} banned by {ctx.author}\nReason: {reason}"))

@bot.command()
@commands.has_permissions(ban_members=True)
async def softban(ctx, member: discord.Member, *, reason="Softban"):
    await member.ban(reason=reason, delete_message_days=7)
    await member.unban(reason="Softban")
    await ctx.send(embed=box("🔨 Softbanned", f"{member} softbanned — kicked + messages deleted."))

@bot.command()
@commands.has_permissions(ban_members=True)
async def tempban(ctx, member: discord.Member, time: str = "1h", *, reason="Tempban"):
    seconds = 3600
    try:
        if "d" in time:   seconds = int(time.replace("d","")) * 86400
        elif "h" in time: seconds = int(time.replace("h","")) * 3600
        elif "m" in time: seconds = int(time.replace("m","")) * 60
    except ValueError:
        return await ctx.send(embed=box("❌ Format", "Use: `.tempban @user 1h reason`"))
    await member.ban(reason=reason)
    await ctx.send(embed=box("⏳ Temp Banned", f"{member} banned for **{time}**.\n**Reason:** {reason}"))
    await asyncio.sleep(seconds)
    try:
        await ctx.guild.unban(member)
        await ctx.send(embed=box("✅ Temp Ban Expired", f"{member} has been unbanned."))
    except: pass

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user)
    await ctx.send(embed=box("✅ Unbanned", f"{user} has been unbanned."))

@bot.command()
@commands.has_permissions(ban_members=True)
async def unbanall(ctx):
    bans = [entry async for entry in ctx.guild.bans()]
    for b in bans:
        await ctx.guild.unban(b.user)
    await ctx.send(embed=box("✅ Unban All", f"Unbanned **{len(bans)}** users."))

@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if member.top_role >= ctx.author.top_role and ctx.author.id != OWNER_ID:
        return await ctx.send(embed=box("❌ Hierarchy Error", "You can't kick someone with a higher or equal role."))
    await member.kick(reason=reason)
    await ctx.send(embed=box("👢 Kicked", f"{member} has been kicked.\n**Reason:** {reason}"))
    await send_log(ctx.guild, "modlogs", box("👢 Kick", f"{member} kicked by {ctx.author}\nReason: {reason}"))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: str = "10m", *, reason="No reason"):
    seconds = 600
    try:
        if "d" in duration:   seconds = int(duration.replace("d","")) * 86400
        elif "h" in duration: seconds = int(duration.replace("h","")) * 3600
        elif "m" in duration: seconds = int(duration.replace("m","")) * 60
    except ValueError:
        return await ctx.send(embed=box("❌ Format", "Use: `.mute @user 10m reason`"))
    seconds = min(seconds, 2419200)  # Discord max timeout = 28 days
    until = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
    await ctx.send(embed=box("🔇 Muted", f"{member} muted for **{duration}**.\n**Reason:** {reason}"))
    await send_log(ctx.guild, "modlogs", box("🔇 Mute", f"{member} muted by {ctx.author} for {duration}"))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    await member.timeout(None)
    await ctx.send(embed=box("🔊 Unmuted", f"{member} has been unmuted."))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmuteall(ctx):
    count = 0
    for m in ctx.guild.members:
        if m.timed_out_until:
            try:
                await m.timeout(None)
                count += 1
            except: pass
    await ctx.send(embed=box("🔊 Unmuted All", f"Unmuted **{count}** members."))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason="No reason"):
    add_warn(ctx.guild.id, member.id, reason, ctx.author.id)
    count = len(get_warns(ctx.guild.id, member.id))
    await ctx.send(embed=box("⚠️ Warned",
        f"{member} has been warned. (Total: **{count}**)\n**Reason:** {reason}"))
    await send_log(ctx.guild, "modlogs", box("⚠️ Warn", f"{member} warned by {ctx.author}\nReason: {reason}"))

@bot.command()
async def warnings(ctx, member: discord.Member = None):
    m = member or ctx.author
    warns = get_warns(ctx.guild.id, m.id)
    if not warns:
        return await ctx.send(embed=box("✅ No Warnings", f"{m} has no warnings."))
    desc = "\n".join(f"**{i+1}.** {w['reason']} — <@{w['mod']}>" for i, w in enumerate(warns))
    await ctx.send(embed=box(f"⚠️ {m}'s Warnings", desc))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def removewarn(ctx, member: discord.Member):
    remove_last_warn(ctx.guild.id, member.id)
    await ctx.send(embed=box("✅ Warning Removed", f"Removed last warning from {member}."))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def clearwarns(ctx, member: discord.Member):
    clear_all_warns(ctx.guild.id, member.id)
    await ctx.send(embed=box("✅ Warnings Cleared", f"All warnings cleared for {member}."))

@bot.command()
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname=None):
    await member.edit(nick=nickname)
    await ctx.send(embed=box("✏️ Nickname Changed",
        f"{member}'s nickname set to `{nickname or 'reset'}`"))

@bot.command()
@commands.has_permissions(manage_roles=True)
async def role(ctx, member: discord.Member, role_obj: discord.Role):
    if role_obj in member.roles:
        await member.remove_roles(role_obj)
        await ctx.send(embed=box("🎭 Role Removed", f"Removed {role_obj.mention} from {member.mention}"))
    else:
        await member.add_roles(role_obj)
        await ctx.send(embed=box("🎭 Role Added", f"Added {role_obj.mention} to {member.mention}"))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    await ch.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(embed=box("🔒 Locked", f"{ch.mention} has been locked."))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    await ch.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send(embed=box("🔓 Unlocked", f"{ch.mention} has been unlocked."))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def hide(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    await ch.set_permissions(ctx.guild.default_role, view_channel=False)
    await ctx.send(embed=box("👁️ Hidden", f"{ch.mention} is now hidden."))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unhide(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    await ch.set_permissions(ctx.guild.default_role, view_channel=True)
    await ctx.send(embed=box("👁️ Unhidden", f"{ch.mention} is now visible."))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(embed=box("🗑️ Purge", f"Deleted **{len(deleted)-1}** messages."))
    await asyncio.sleep(3)
    try: await msg.delete()
    except: pass

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purgebot(ctx, amount: int):
    deleted = await ctx.channel.purge(limit=amount + 1, check=lambda m: m.author.bot)
    msg = await ctx.send(embed=box("🗑️ Purge Bots", f"Deleted **{len(deleted)}** bot messages."))
    await asyncio.sleep(3)
    try: await msg.delete()
    except: pass

@bot.command()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int):
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(embed=box("🐢 Slowmode", f"Set to **{seconds}s** in {ctx.channel.mention}"))

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unslowmode(ctx):
    await ctx.channel.edit(slowmode_delay=0)
    await ctx.send(embed=box("🐢 Slowmode Off", f"Removed slowmode from {ctx.channel.mention}"))

@bot.command()
@commands.has_permissions(moderate_members=True)
async def inspect(ctx, member: discord.Member):
    warns = get_warns(ctx.guild.id, member.id)
    sec = get_security(ctx.guild.id)
    fields = [
        ("Warnings",     str(len(warns)),                                    True),
        ("Jailed",       str(str(member.id) in sec.get("jailed",[])),        True),
        ("Quarantined",  str(str(member.id) in sec.get("quarantined",[])),   True),
        ("Whitelisted",  str(is_whitelisted(ctx.guild.id, member.id)),        True),
        ("Extra Owner",  str(is_extra_owner(ctx.guild.id, member.id)),        True),
    ]
    await ctx.send(embed=box(f"🔍 Inspect: {member}", "", fields=fields,
        thumbnail=str(member.display_avatar)))

@bot.command(name="list")
async def list_cmd(ctx):
    warns = load("warns").get(str(ctx.guild.id), {})
    if not warns:
        return await ctx.send(embed=box("📋 No Records", "No warnings on record."))
    desc = "\n".join(f"<@{uid}> — {len(ws)} warn(s)" for uid, ws in warns.items())
    await ctx.send(embed=box("📋 Warning List", desc))

@bot.command()
@commands.has_permissions(manage_roles=True)
async def massrole(ctx, role: discord.Role):
    count = 0
    for m in ctx.guild.members:
        if role not in m.roles:
            try:
                await m.add_roles(role)
                count += 1
            except: pass
    await ctx.send(embed=box("✅ Mass Role", f"Added {role.mention} to **{count}** members."))

@bot.command()
@commands.has_permissions(manage_nicknames=True)
async def massnick(ctx, *, nickname):
    count = 0
    for m in ctx.guild.members:
        if not m.bot:
            try:
                await m.edit(nick=nickname)
                count += 1
            except: pass
    await ctx.send(embed=box("✅ Mass Nick", f"Set nickname `{nickname}` for **{count}** members."))

# ─────────────────────────────────────────────────────────────────────────────
#  SECURITY
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="security")
@admin_check()
async def security_cmd(ctx):
    sec = get_security(ctx.guild.id)
    fields = [
        ("Antinuke",        "✅ On" if sec.get("antinuke") else "❌ Off", True),
        ("Automod",         "✅ On" if sec.get("automod")  else "❌ Off", True),
        ("Whitelisted",     str(len(sec.get("whitelist",  []))),          True),
        ("Extra Owners",    str(len(sec.get("extraowners",[]))),          True),
        ("Protected Roles", str(len(sec.get("roleprotect",[]))),          True),
        ("Jailed",          str(len(sec.get("jailed",     []))),          True),
        ("Quarantined",     str(len(sec.get("quarantined",[]))),          True),
    ]
    await ctx.send(embed=box("🛡️ Security Overview", "", fields=fields))

@bot.command()
@admin_check()
async def antinuke(ctx):
    sec = get_security(ctx.guild.id)
    sec["antinuke"] = not sec.get("antinuke", False)
    set_security(ctx.guild.id, sec)
    state = "✅ Enabled" if sec["antinuke"] else "❌ Disabled"
    await ctx.send(embed=box("🛡️ Anti-Nuke", f"Anti-Nuke is now **{state}**."))

@bot.command()
@admin_check()
async def automod(ctx):
    sec = get_security(ctx.guild.id)
    sec["automod"] = not sec.get("automod", False)
    set_security(ctx.guild.id, sec)
    state = "✅ Enabled" if sec["automod"] else "❌ Disabled"
    await ctx.send(embed=box("🤖 Automod", f"Automod is now **{state}**."))

@bot.command()
@admin_check()
async def anticheck(ctx):
    sec = get_security(ctx.guild.id)
    await ctx.send(embed=box("🔍 Anti-Check",
        f"Anti-Nuke: {'✅' if sec.get('antinuke') else '❌'}\n"
        f"Automod:   {'✅' if sec.get('automod')  else '❌'}"))

# ── Whitelist ─────────────────────────────────────────────────────────────────
@bot.group(name="whitelist")
@admin_check()
async def whitelist(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=box("📋 Whitelist", "Subcommands: `add` `remove` `list` `info` `clear`"))

@whitelist.command(name="add")
async def whitelist_add(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    sec.setdefault("whitelist", [])
    if str(member.id) not in sec["whitelist"]:
        sec["whitelist"].append(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Whitelisted", f"{member.mention} added to whitelist."))

@whitelist.command(name="remove")
async def whitelist_remove(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    wl = sec.get("whitelist", [])
    if str(member.id) in wl:
        wl.remove(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Removed", f"{member.mention} removed from whitelist."))

@whitelist.command(name="list")
async def whitelist_list(ctx):
    sec = get_security(ctx.guild.id)
    wl = sec.get("whitelist", [])
    await ctx.send(embed=box("📋 Whitelist", "\n".join(f"<@{i}>" for i in wl) or "Empty"))

@whitelist.command(name="info")
async def whitelist_info(ctx, member: discord.Member):
    status = is_whitelisted(ctx.guild.id, member.id)
    await ctx.send(embed=box("📋 Whitelist Info",
        f"{member.mention} is {'✅ whitelisted' if status else '❌ not whitelisted'}."))

@whitelist.command(name="clear")
async def whitelist_clear(ctx):
    sec = get_security(ctx.guild.id)
    sec["whitelist"] = []
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Whitelist Cleared", "All users removed from whitelist."))

# ── Extra Owners ──────────────────────────────────────────────────────────────
@bot.group(name="extraowner")
@owner_check()
async def extraowner(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=box("📋 Extra Owners", "Subcommands: `add` `remove` `list`"))

@extraowner.command(name="add")
async def extraowner_add(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    sec.setdefault("extraowners", [])
    if str(member.id) not in sec["extraowners"]:
        sec["extraowners"].append(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Extra Owner Added", f"{member.mention} is now an extra owner."))

@extraowner.command(name="remove")
async def extraowner_remove(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    eo = sec.get("extraowners", [])
    if str(member.id) in eo:
        eo.remove(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Removed", f"{member.mention} removed from extra owners."))

@extraowner.command(name="list")
async def extraowner_list(ctx):
    sec = get_security(ctx.guild.id)
    eo = sec.get("extraowners", [])
    await ctx.send(embed=box("📋 Extra Owners", "\n".join(f"<@{i}>" for i in eo) or "None"))

# ── Role Protect ──────────────────────────────────────────────────────────────
@bot.group(name="roleprotect")
@admin_check()
async def roleprotect(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=box("📋 Role Protect", "Subcommands: `add` `remove` `list`"))

@roleprotect.command(name="add")
async def rp_add(ctx, role: discord.Role):
    sec = get_security(ctx.guild.id)
    sec.setdefault("roleprotect", [])
    if str(role.id) not in sec["roleprotect"]:
        sec["roleprotect"].append(str(role.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Role Protected", f"{role.mention} is now protected."))

@roleprotect.command(name="remove")
async def rp_remove(ctx, role: discord.Role):
    sec = get_security(ctx.guild.id)
    rp = sec.get("roleprotect", [])
    if str(role.id) in rp:
        rp.remove(str(role.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Protection Removed", f"{role.mention} is no longer protected."))

@roleprotect.command(name="list")
async def rp_list(ctx):
    sec = get_security(ctx.guild.id)
    rp = sec.get("roleprotect", [])
    await ctx.send(embed=box("📋 Protected Roles", "\n".join(f"<@&{i}>" for i in rp) or "None"))

# ── Quarantine ────────────────────────────────────────────────────────────────
@bot.command()
@admin_check()
async def quarantine(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    sec.setdefault("quarantined", [])
    for channel in ctx.guild.channels:
        try: await channel.set_permissions(member, view_channel=False, send_messages=False)
        except: pass
    if str(member.id) not in sec["quarantined"]:
        sec["quarantined"].append(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("🔐 Quarantined",
        f"{member.mention} has been quarantined from all channels."))

@bot.command()
@admin_check()
async def quarantineadd(ctx, role: discord.Role):
    for channel in ctx.guild.channels:
        try: await channel.set_permissions(role, view_channel=False, send_messages=False)
        except: pass
    await ctx.send(embed=box("🔐 Role Quarantined",
        f"{role.mention} denied access to all channels."))

# ── Jail ──────────────────────────────────────────────────────────────────────
@bot.group(name="jail")
async def jail(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=box("🔒 Jail", "Subcommands: `setup <role>` `remove <user>` `list`\nTo jail a user: `.jailuser <user>`"))

@jail.command(name="setup")
@admin_check()
async def jail_setup(ctx, role: discord.Role):
    sec = get_security(ctx.guild.id)
    sec["jail_role"] = str(role.id)
    set_security(ctx.guild.id, sec)
    for channel in ctx.guild.channels:
        try: await channel.set_permissions(role, send_messages=False, view_channel=False)
        except: pass
    await ctx.send(embed=box("✅ Jail Setup", f"{role.mention} is now the jail role."))

@jail.command(name="remove")
@admin_check()
async def jail_remove(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    role_id = sec.get("jail_role")
    if role_id:
        role = ctx.guild.get_role(int(role_id))
        if role and role in member.roles:
            await member.remove_roles(role)
    jailed = sec.get("jailed", [])
    if str(member.id) in jailed:
        jailed.remove(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("✅ Unjailed", f"{member.mention} has been released."))

@jail.command(name="list")
async def jail_list(ctx):
    sec = get_security(ctx.guild.id)
    jailed = sec.get("jailed", [])
    await ctx.send(embed=box("🔒 Jailed Users",
        "\n".join(f"<@{i}>" for i in jailed) or "None"))

@bot.command(name="jailuser")
@admin_check()
async def jail_user(ctx, member: discord.Member):
    sec = get_security(ctx.guild.id)
    role_id = sec.get("jail_role")
    if not role_id:
        return await ctx.send(embed=box("❌ No Jail Role", "Set up jail first: `.jail setup <role>`"))
    role = ctx.guild.get_role(int(role_id))
    if role:
        await member.add_roles(role)
    sec.setdefault("jailed", [])
    if str(member.id) not in sec["jailed"]:
        sec["jailed"].append(str(member.id))
    set_security(ctx.guild.id, sec)
    await ctx.send(embed=box("🔒 Jailed", f"{member.mention} has been jailed."))

# ── Lockdown ──────────────────────────────────────────────────────────────────
@bot.command()
@admin_check()
async def lockdown(ctx):
    for channel in ctx.guild.text_channels:
        try: await channel.set_permissions(ctx.guild.default_role, send_messages=False)
        except: pass
    await ctx.send(embed=box("🔒 Server Lockdown", "All channels have been locked."))

@bot.command()
@admin_check()
async def unlockdown(ctx):
    for channel in ctx.guild.text_channels:
        try: await channel.set_permissions(ctx.guild.default_role, send_messages=True)
        except: pass
    await ctx.send(embed=box("🔓 Lockdown Lifted", "All channels have been unlocked."))

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
LOG_TYPES = [
    "messagelogs", "memberlogs", "modlogs", "voicelogs", "boostlogs",
    "joinlogs", "leavelogs", "rolelogs", "nicknamelogs", "channellogs",
    "emojilogs", "stickerlogs", "securitylogs", "antinukelogs"
]

@bot.command()
@admin_check()
async def setuplogs(ctx):
    await ctx.send(embed=box("📝 Setup Logs",
        "Use `.setchannel #channel` to route all logs to one channel,\n"
        "or set each individually:\n" +
        "  ".join(f"`.{t}`" for t in LOG_TYPES)))

@bot.command()
@admin_check()
async def setchannel(ctx, channel: discord.TextChannel):
    lc = get_log_channels(ctx.guild.id)
    for lt in LOG_TYPES:
        lc[lt] = str(channel.id)
    set_log_channels(ctx.guild.id, lc)
    await ctx.send(embed=box("✅ Log Channel Set", f"All logs → {channel.mention}"))

@bot.command()
@admin_check()
async def setuser(ctx, member: discord.Member):
    await ctx.send(embed=box("ℹ️ Set User",
        f"Log user filter noted for {member.mention}. "
        f"Check modlogs for this user's recorded actions."))

# generate individual log-channel commands dynamically
def make_log_cmd(log_type):
    @bot.command(name=log_type)
    @admin_check()
    async def log_cmd(ctx, channel: discord.TextChannel):
        lc = get_log_channels(ctx.guild.id)
        lc[log_type] = str(channel.id)
        set_log_channels(ctx.guild.id, lc)
        await ctx.send(embed=box("✅ Log Set", f"`{log_type}` → {channel.mention}"))
    log_cmd.__name__ = log_type
    return log_cmd

for _lt in LOG_TYPES:
    make_log_cmd(_lt)

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
@bot.command()
async def prefix(ctx):
    await ctx.send(embed=box("⚙️ Prefix", "Current prefix: **.**"))

@bot.command()
@admin_check()
async def settings(ctx):
    cfg = get_config(ctx.guild.id)
    ai  = get_ai_config(ctx.guild.id)
    fields = [
        ("Welcome Channel", f"<#{cfg['welcome_channel']}>" if cfg.get("welcome_channel") else "Not Set", True),
        ("Leave Channel",   f"<#{cfg['leave_channel']}>"   if cfg.get("leave_channel")   else "Not Set", True),
        ("Auto Role",       f"<@&{cfg['autorole']}>"       if cfg.get("autorole")         else "Not Set", True),
        ("Auto Nick",       cfg.get("autonick", "Not Set"),                                               True),
        ("AI Channel",      f"<#{ai['channel']}>"          if ai.get("channel")           else "Not Set", True),
        ("AI Prompt",       ai.get("system_prompt", "Default")[:50] + "…" if len(ai.get("system_prompt","Default")) > 50 else ai.get("system_prompt","Default"), True),
    ]
    await ctx.send(embed=box("⚙️ Server Settings", "", fields=fields))

@bot.command()
@admin_check()
async def setwelcome(ctx, channel: discord.TextChannel):
    cfg = get_config(ctx.guild.id)
    cfg["welcome_channel"] = str(channel.id)
    set_config(ctx.guild.id, cfg)
    await ctx.send(embed=box("✅ Welcome Channel", f"Set to {channel.mention}"))

@bot.command()
@admin_check()
async def setleave(ctx, channel: discord.TextChannel):
    cfg = get_config(ctx.guild.id)
    cfg["leave_channel"] = str(channel.id)
    set_config(ctx.guild.id, cfg)
    await ctx.send(embed=box("✅ Leave Channel", f"Set to {channel.mention}"))

@bot.command()
@admin_check()
async def autorole(ctx, role: discord.Role):
    cfg = get_config(ctx.guild.id)
    cfg["autorole"] = str(role.id)
    set_config(ctx.guild.id, cfg)
    await ctx.send(embed=box("✅ Auto Role", f"New members will get {role.mention}"))

@bot.command()
@admin_check()
async def autonick(ctx, *, nickname):
    cfg = get_config(ctx.guild.id)
    cfg["autonick"] = nickname
    set_config(ctx.guild.id, cfg)
    await ctx.send(embed=box("✅ Auto Nick", f"New members will be nicknamed `{nickname}`"))

@bot.command()
@admin_check()
async def verification(ctx):
    await ctx.send(embed=box("🔒 Verification",
        "Use Discord's built-in verification in Server Settings → Safety Setup."))

@bot.command()
@admin_check()
async def welcome(ctx, *, message=None):
    cfg = get_config(ctx.guild.id)
    if message:
        cfg["welcome_msg"] = message
        set_config(ctx.guild.id, cfg)
        await ctx.send(embed=box("✅ Welcome Message",
            f"Set! Use `{{user}}` for mention, `{{server}}` for server name.\n**Preview:** {message}"))
    else:
        await ctx.send(embed=box("📋 Welcome Message", cfg.get("welcome_msg", "Not set")))

@bot.command()
@admin_check()
async def goodbye(ctx, *, message=None):
    cfg = get_config(ctx.guild.id)
    if message:
        cfg["goodbye_msg"] = message
        set_config(ctx.guild.id, cfg)
        await ctx.send(embed=box("✅ Goodbye Message", f"Set: {message}"))
    else:
        await ctx.send(embed=box("📋 Goodbye Message", cfg.get("goodbye_msg", "Not set")))

@bot.command()
@admin_check()
async def resetconfig(ctx):
    set_config(ctx.guild.id, {})
    set_log_channels(ctx.guild.id, {})
    set_security(ctx.guild.id, {})
    set_ai_config(ctx.guild.id, {})
    await ctx.send(embed=box("✅ Config Reset", "All server configuration has been reset."))

# ─────────────────────────────────────────────────────────────────────────────
#  ERROR HANDLER
# ─────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=box("❌ Missing Permissions",
            f"You need: `{'`, `'.join(error.missing_permissions)}`"))
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(embed=box("❌ Member Not Found", "I couldn't find that member."))
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send(embed=box("❌ Role Not Found", "I couldn't find that role."))
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(embed=box("❌ Channel Not Found", "I couldn't find that channel."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=box("❌ Bad Argument", str(error)))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=box("❌ Missing Argument", f"Required: `{error.param.name}`"))
    elif isinstance(error, commands.CommandNotFound):
        pass  # silently ignore unknown commands
    elif isinstance(error, commands.NotOwner):
        await ctx.send(embed=box("❌ Owner Only", "This command is restricted to the server/bot owner."))
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(embed=box("❌ No Permission", "You don't have permission to use this command."))
    else:
        print(f"Unhandled error in '{ctx.command}': {error}")

# ─────────────────────────────────────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
