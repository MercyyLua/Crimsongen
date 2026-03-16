import os
import re
import json
import sqlite3
import random
from datetime import date
import discord
from discord import app_commands
from discord.ext import commands

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD TOKEN MISSING")

DB_PATH = "steam_bot.db"
STOCK_PATH = "stock.json"

MEMBER_ROLE_ID = 1471512804535046237
BOOSTER_ROLE_ID = 1469733875709378674
BOOSTER_ROLE_2_ID = 1471590464279810210
STAFF_ROLE_ID = 1471515890225774663
STAFF_ROLE_2_ID = 1474815528538472538
STAFF_ROLE_3_ID = 1471918887934361690
PROTECTED_GUILD_ID = 1463580079819849834
# =========================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= DATABASE =================
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        cur = con.cursor()

        # Stock stored in DB — persists across deploys
        cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            games    TEXT NOT NULL,
            UNIQUE(username, password)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS gens (
            user_id INTEGER,
            day TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            account TEXT,
            reason TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            owner_id INTEGER,
            code TEXT UNIQUE
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_uses (
            user_id INTEGER UNIQUE
        )
        """)

        # Migrate existing stock.json into DB if present
        if os.path.exists(STOCK_PATH):
            try:
                with open(STOCK_PATH, "r", encoding="utf-8") as f:
                    old = json.load(f)
                migrated = 0
                for a in old:
                    try:
                        cur.execute(
                            "INSERT OR IGNORE INTO stock (username, password, games) VALUES (?,?,?)",
                            (a["username"], a["password"], a["games"])
                        )
                        migrated += 1
                    except Exception:
                        pass
                con.commit()
                os.rename(STOCK_PATH, STOCK_PATH + ".migrated")
                print(f"✅ Migrated {migrated} accounts from stock.json to DB")
            except Exception as e:
                print(f"⚠️ Migration skipped: {e}")


def load_stock() -> list:
    """Load all accounts from DB as list of dicts."""
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT username, password, games FROM stock")
        return [{"username": r[0], "password": r[1], "games": r[2]} for r in cur.fetchall()]


def save_stock(stock: list):
    """Replace entire stock (used for bulk operations)."""
    with db() as con:
        con.execute("DELETE FROM stock")
        cur = con.cursor()
        for a in stock:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO stock (username, password, games) VALUES (?,?,?)",
                    (a["username"], a["password"], a["games"])
                )
            except Exception:
                pass
        con.commit()


def add_accounts_to_stock(accounts: list) -> int:
    """Add accounts to DB, skip duplicates. Returns count added."""
    added = 0
    with db() as con:
        cur = con.cursor()
        for acc in accounts:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO stock (username, password, games) VALUES (?,?,?)",
                    (acc["username"], acc["password"], acc["games"])
                )
                if cur.rowcount > 0:
                    added += 1
            except Exception:
                pass
        con.commit()
    return added

# ================= HELPERS =================
def has_role(member, role_id):
    return any(r.id == role_id for r in member.roles)

def base_limit(member):
    boosts = 0
    if has_role(member, BOOSTER_ROLE_ID):
        boosts += 1
    if has_role(member, BOOSTER_ROLE_2_ID):
        boosts += 1
    if boosts == 1:
        return 4
    if boosts >= 2:
        return 6
    return 2

def has_referral(user_id):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM referral_uses WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def daily_limit(member):
    if any(has_role(member, r) for r in (STAFF_ROLE_ID, STAFF_ROLE_2_ID, STAFF_ROLE_3_ID)):
        return 999
    limit = base_limit(member)
    if has_referral(member.id):
        limit += 1
    return limit

def used_today(user_id):
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM gens WHERE user_id=? AND day=?",
            (user_id, date.today().isoformat())
        )
        return cur.fetchone()[0]

def staff_only(interaction: discord.Interaction):
    return any(has_role(interaction.user, r) for r in (STAFF_ROLE_ID, STAFF_ROLE_2_ID, STAFF_ROLE_3_ID))

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
    else:
        try:
            await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        except Exception:
            await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)

# ================= FILE PARSER =================
def is_credential_line(line: str) -> bool:
    line = line.strip()
    if ":" not in line:
        return False
    user, _ = line.split(":", 1)
    user = user.strip()
    if not user or " " in user:
        return False
    return True

def normalise_games(games_str: str) -> str:
    """Normalise game separators to comma-separated, clean whitespace."""
    # Replace common separators with comma
    for sep in [" | ", " / ", " + ", "\n", " & ", " ; "]:
        games_str = games_str.replace(sep, ", ")
    # Clean up multiple commas/spaces
    parts = [g.strip() for g in games_str.split(",") if g.strip()]
    return ", ".join(parts)

def parse_file(text: str):
    """
    Supports many formats:
      Format 1 (inline, one game per line):  user:pass - Game
      Format 2 (inline, multi-game):         user:pass | Game1 | Game2
      Format 3 (block):                      Game1\nGame2\nuser:pass
      Format 4 (labeled):                    Username: user\nPassword: pass\nGames: g1,g2
      Handles same user:pass repeated across multiple lines — merges games.
    Returns list of (username, password, games_string)
    """
    # ── Pass 1: collect raw (user, pass, [games]) grouped by credentials ──
    creds_map: dict[str, list[str]] = {}   # "user:pass" -> [game, game, ...]
    order: list[str] = []                  # preserve insertion order

    def add_entry(user: str, pwd: str, games_raw: str):
        key = f"{user}:{pwd}"
        games_list = [g.strip() for g in re.split(r"[,|/&;]", games_raw) if g.strip()]
        if key not in creds_map:
            creds_map[key] = []
            order.append(key)
        for g in games_list:
            if g not in creds_map[key]:
                creds_map[key].append(g)

    lines = [l.rstrip() for l in text.splitlines()]
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # ── Format 4: labeled block ──────────────────────────────
        if re.match(r"^(username|user)\s*:", line, re.I):
            label_block: dict[str, str] = {}
            while i < len(lines) and lines[i].strip():
                l = lines[i].strip()
                if ":" in l:
                    k, v = l.split(":", 1)
                    label_block[k.strip().lower()] = v.strip()
                i += 1
            user  = label_block.get("username") or label_block.get("user", "")
            pwd   = label_block.get("password") or label_block.get("pass", "")
            games = label_block.get("games") or label_block.get("game", "")
            if user and pwd and games:
                add_entry(user, pwd, games)
            continue

        # ── Normalise inline separator (dash variants → pipe) ────
        norm = line
        for sep in [" \u2013 ", " \u2014 ", " \u2012 ", " - ", " – ", " — "]:
            if sep in norm:
                norm = norm.replace(sep, "|", 1)
                break
        norm = re.sub(r"(?i)\bgames?:", "", norm).strip()

        # ── Format 1 & 2: creds | game(s) ────────────────────────
        if "|" in norm:
            parts      = norm.split("|")
            creds_part = parts[0].strip()
            games_part = ", ".join(p.strip() for p in parts[1:] if p.strip())
            if ":" in creds_part and games_part:
                user, pwd = creds_part.split(":", 1)
                user, pwd = user.strip(), pwd.strip()
                if user and pwd:
                    add_entry(user, pwd, games_part)
                    i += 1
                    continue

        # ── Format 3: block (game lines then creds line) ─────────
        block_lines: list[str] = []
        j = i
        while j < len(lines) and lines[j].strip():
            block_lines.append(lines[j].strip())
            j += 1

        if not block_lines:
            i += 1
            continue

        cred_index = None
        for k, bl in enumerate(block_lines):
            if is_credential_line(bl):
                cred_index = k
                break

        if cred_index is None:
            i = j
            continue

        cred_line  = block_lines[cred_index]
        game_lines = [bl for bl in block_lines[:cred_index] if bl]
        post_lines = block_lines[cred_index + 1:]
        for pl in post_lines:
            if not is_credential_line(pl) and pl:
                game_lines.append(pl)

        user, pwd = cred_line.split(":", 1)
        user, pwd = user.strip(), pwd.strip()

        if not pwd and post_lines:
            pwd = post_lines[0].strip()
            game_lines = [bl for bl in block_lines[:cred_index] if bl]

        if not user or not pwd or not game_lines:
            i = j
            continue

        add_entry(user, pwd, ", ".join(game_lines))
        i = j

    # ── Pass 2: build final results ──────────────────────────────
    results = []
    for key in order:
        user, pwd = key.split(":", 1)
        games = creds_map[key]
        if games:
            results.append((user, pwd, ", ".join(games)))

    return results

# ================= EVENTS =================
@bot.event
async def on_member_join(member: discord.Member):
    if not member.bot: return
    if member.guild.id != PROTECTED_GUILD_ID: return
    guild = member.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.bot_add):
        adder = entry.user
        # Ban the bot
        try: await guild.ban(member, reason="Unauthorised bot — not allowed in this server", delete_message_days=0)
        except Exception: pass
        # Ban whoever added it
        if not adder.bot:
            try: await guild.ban(adder, reason="Added an unauthorised bot to the server", delete_message_days=0)
            except Exception: pass
        break


    init_db()
    await bot.tree.sync()
    await bot.change_presence(
        activity=discord.Streaming(name="Crimson Gen", url="https://twitch.tv/crimsongen"),
        status=discord.Status.streaming
    )
    print(f"✅ Logged in as {bot.user}")

# ================= PAGINATION VIEW =================
class GameView(discord.ui.View):
    def __init__(self, user_id, pages):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.pages = pages
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ These buttons are not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self.update()
        await interaction.response.edit_message(content=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self.update()
        await interaction.response.edit_message(content=self.pages[self.index], view=self)

    def update(self):
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index == len(self.pages) - 1

# ================= USER COMMANDS =================

@bot.tree.command(name="steamaccount", description="Generate a Steam account for a game")
async def steamaccount(interaction: discord.Interaction, game: str):
    await interaction.response.defer(ephemeral=True)

    used  = used_today(interaction.user.id)
    limit = daily_limit(interaction.user)

    if used >= limit:
        await interaction.followup.send(f"❌ Daily limit reached ({used}/{limit} today).", ephemeral=True)
        return

    stock = load_stock()
    game_lower = game.lower().strip()

    # Match accounts that have this game — check each individual game in the list
    def has_game(acc):
        for g in acc["games"].split(","):
            if game_lower in g.strip().lower():
                return True
        return False

    matches = [a for a in stock if has_game(a)]

    if not matches:
        # Try fuzzy — check if any word in game name matches
        words = [w for w in game_lower.split() if len(w) > 2]
        if words:
            matches = [
                a for a in stock
                if any(w in a["games"].lower() for w in words)
            ]

    if not matches:
        # Show available games as hint
        all_games = sorted({
            g.strip()
            for a in stock
            for g in a["games"].split(",")
            if g.strip()
        })
        hint = ", ".join(all_games[:10]) + ("..." if len(all_games) > 10 else "")
        await interaction.followup.send(
            f"❌ No accounts found for **{game}**.\n"
            f"Available games: {hint}\n"
            f"Use `/listgames` to see the full list.",
            ephemeral=True
        )
        return

    acc  = random.choice(matches)
    user = acc["username"]
    pwd  = acc["password"]
    games = acc["games"]

    with db() as con:
        con.execute("INSERT INTO gens VALUES (?,?)", (interaction.user.id, date.today().isoformat()))

    embed = discord.Embed(
        title="🎮 Steam Account Generated",
        description="Please change the password after logging in. Do not share this account.",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1470798856085307423/1471984801266532362/IMG_7053.gif")
    embed.add_field(name="🔐 Credentials", value=f"`{user}:{pwd}`", inline=False)
    embed.add_field(name="🎮 Games on Account", value=games[:1024] if len(games) < 1024 else games[:1021] + "...", inline=False)
    embed.add_field(name="📊 Daily Usage", value=f"`{used + 1}/{limit}`", inline=True)
    embed.set_footer(text="Crimson Gen  ·  Steam Accounts")

    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send("✅ Account sent to your DMs!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ Couldn't DM you. Enable DMs from server members.\n\n**Account:** `{user}:{pwd}`",
            ephemeral=True
        )


@bot.tree.command(name="listgames", description="View available games in stock")
async def listgames(interaction: discord.Interaction):
    stock = load_stock()
    # Count stock per game
    game_counts: dict[str, int] = {}
    for acc in stock:
        for g in acc["games"].split(","):
            g = g.strip()
            if g:
                game_counts[g] = game_counts.get(g, 0) + 1

    if not game_counts:
        await interaction.response.send_message("❌ No games in stock.", ephemeral=True)
        return

    sorted_games = sorted(game_counts.items(), key=lambda x: x[0].lower())
    pages = []
    chunk = []
    for name, count in sorted_games:
        chunk.append(f"• **{name}** — `{count}`")
        if len(chunk) == 15:
            pages.append("🎮 **Available Games**\n" + "\n".join(chunk))
            chunk = []
    if chunk:
        pages.append("🎮 **Available Games**\n" + "\n".join(chunk))

    view = GameView(interaction.user.id, pages)
    view.update()
    await interaction.response.send_message(pages[0], view=view, ephemeral=True)


@bot.tree.command(name="search", description="Search stock for a specific game")
async def search(interaction: discord.Interaction, game: str):
    stock = load_stock()
    game_lower = game.lower().strip()

    count = sum(
        1 for a in stock
        if any(game_lower in g.strip().lower() for g in a["games"].split(","))
    )

    if count == 0:
        await interaction.response.send_message(
            f"❌ No accounts found for **{game}**. Use `/listgames` to see what's available.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"🔍 **{game}** — `{count}` account(s) in stock",
            ephemeral=True
        )


@bot.tree.command(name="stock", description="View total available accounts")
async def stock_cmd(interaction: discord.Interaction):
    stock = load_stock()
    total = len(stock)

    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM reports")
        reported = cur.fetchone()[0]

    available = total - reported

    embed = discord.Embed(title="📦 Stock", color=discord.Color.blue())
    embed.add_field(name="✅ Available", value=f"**{available}** account(s)", inline=False)
    embed.add_field(name="🚨 Reported", value=f"**{reported}** account(s)", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mystats", description="View your stats")
async def mystats(interaction: discord.Interaction):
    used = used_today(interaction.user.id)
    limit = daily_limit(interaction.user)
    referral = has_referral(interaction.user.id)

    await interaction.response.send_message(
        f"📊 **Your Stats**\n"
        f"Gens today: **{used}/{limit}**\n"
        f"Referral bonus: **{'Yes' if referral else 'No'}**",
        ephemeral=True
    )


@bot.tree.command(name="topusers", description="Top users today")
async def topusers(interaction: discord.Interaction):
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT user_id, COUNT(*) FROM gens "
            "WHERE day=? GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 10",
            (date.today().isoformat(),)
        )
        rows = cur.fetchall()

    if not rows:
        await interaction.response.send_message("❌ No gens today.")
        return

    msg = "🏆 **Top Users Today**\n"
    for i, (uid, count) in enumerate(rows, 1):
        msg += f"{i}. <@{uid}> — {count}\n"

    await interaction.response.send_message(msg)

# ================= REFERRALS =================

@bot.tree.command(name="referral_create", description="Create your referral code")
async def referral_create(interaction: discord.Interaction):
    code = "".join(str(random.randint(0, 9)) for _ in range(8))
    with db() as con:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO referrals VALUES (?,?)", (interaction.user.id, code))
    await interaction.response.send_message(f"🎁 **Your Referral Code:** `{code}`", ephemeral=True)


@bot.tree.command(name="refer", description="Redeem a referral code")
async def refer(interaction: discord.Interaction, code: str):
    if not code.isdigit() or len(code) != 8:
        await interaction.response.send_message("❌ Invalid code.", ephemeral=True)
        return

    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT owner_id FROM referrals WHERE code=?", (code,))
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message("❌ Code not found.", ephemeral=True)
            return
        cur.execute("INSERT OR IGNORE INTO referral_uses VALUES (?)", (interaction.user.id,))

    await interaction.response.send_message("✅ Referral redeemed! +1 daily gen.", ephemeral=True)


@bot.tree.command(name="boostinfo", description="Boost perks info")
async def boostinfo(interaction: discord.Interaction):
    await interaction.response.send_message(
        "💎 **Boost Perks**\n"
        "No boost: 2/day\n"
        "1 boost: 4/day\n"
        "2 boosts: 6/day\n"
        "+ Referral bonus",
        ephemeral=True
    )


@bot.tree.command(name="report", description="Report a bad account")
async def report(interaction: discord.Interaction, account: str, reason: str = "Invalid"):
    with db() as con:
        con.execute("INSERT INTO reports VALUES (?,?)", (account, reason))
    await interaction.response.send_message("🚨 Report submitted.", ephemeral=True)

# ================= STAFF COMMANDS =================

@bot.tree.command(name="restock", description="Upload a file to restock accounts")
@app_commands.check(staff_only)
async def restock(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8", errors="ignore")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to read file: {e}", ephemeral=True)
        return

    parsed = parse_file(text)
    if not parsed:
        await interaction.followup.send(
            "❌ No valid accounts found in file.\n"
            "Supported formats:\n"
            "• `user:pass - Game1, Game2`\n"
            "• `user:pass | Game1 | Game2`\n"
            "• Block: games on lines above `user:pass`",
            ephemeral=True
        )
        return

    accounts = [{"username": u, "password": p, "games": g} for u, p, g in parsed]
    added    = add_accounts_to_stock(accounts)

    # Count per individual game across all added accounts
    game_counts: dict[str, int] = {}
    for acc in accounts:
        for g in acc["games"].split(","):
            g = g.strip()
            if g:
                game_counts[g] = game_counts.get(g, 0) + 1

    embed = discord.Embed(title="🔄 Restock Complete", color=discord.Color.green())
    embed.add_field(name="📥 Parsed",    value=f"`{len(parsed)}`",  inline=True)
    embed.add_field(name="✅ New Added", value=f"`{added}`",         inline=True)
    embed.add_field(name="♻️ Dupes Skipped", value=f"`{len(parsed) - added}`", inline=True)

    if game_counts:
        sorted_games = sorted(game_counts.items(), key=lambda x: x[0].lower())
        # Split into chunks if too many games
        chunk_size = 15
        chunks = [sorted_games[i:i+chunk_size] for i in range(0, len(sorted_games), chunk_size)]
        for idx, chunk in enumerate(chunks):
            field_val = "\n".join(f"• **{g}** — `{c}`" for g, c in chunk)
            embed.add_field(
                name=f"🎮 Games ({idx+1}/{len(chunks)})" if len(chunks) > 1 else "🎮 Games Restocked",
                value=field_val[:1024],
                inline=False
            )

    embed.set_footer(text=f"Crimson Gen  ·  Restock  ·  {added} new account(s) added")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="removeaccount", description="Remove an account")
@app_commands.check(staff_only)
async def removeaccount(interaction: discord.Interaction, account: str):
    stock = load_stock()
    new_stock = [a for a in stock if f"{a['username']}:{a['password']}" != account]
    removed = len(stock) - len(new_stock)
    save_stock(new_stock)
    await interaction.response.send_message(f"🗑️ Removed **{removed}** account(s).", ephemeral=True)


@bot.tree.command(name="accountinfo", description="View account info")
@app_commands.check(staff_only)
async def accountinfo(interaction: discord.Interaction, account: str):
    stock = load_stock()
    found = next((a for a in stock if f"{a['username']}:{a['password']}" == account), None)
    if not found:
        await interaction.response.send_message("❌ Account not found.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"ℹ️ **Account Info**\nGames: `{found['games']}`",
        ephemeral=True
    )


@bot.tree.command(name="reportedaccounts", description="View reported accounts")
@app_commands.check(staff_only)
async def reportedaccounts(interaction: discord.Interaction):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT account, reason FROM reports")
        rows = cur.fetchall()

    if not rows:
        await interaction.response.send_message("✅ No reports.")
        return

    msg = "🚨 **Reported Accounts**\n"
    for acc, reason in rows:
        msg += f"`{acc}` — {reason}\n"
    await interaction.response.send_message(msg)


@bot.tree.command(name="resetreport", description="Clear report for account")
@app_commands.check(staff_only)
async def resetreport(interaction: discord.Interaction, account: str):
    with db() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM reports WHERE account=?", (account,))
    await interaction.response.send_message("✅ Report cleared.", ephemeral=True)


@bot.tree.command(name="resetallreports", description="Clear all reports")
@app_commands.check(staff_only)
async def resetallreports(interaction: discord.Interaction):
    with db() as con:
        con.execute("DELETE FROM reports")
    await interaction.response.send_message("✅ All reports cleared.", ephemeral=True)


@bot.tree.command(name="globalstats", description="View bot stats")
@app_commands.check(staff_only)
async def globalstats(interaction: discord.Interaction):
    stock = load_stock()
    total = len(stock)

    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM reports")
        reported = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM gens")
        gens = cur.fetchone()[0]

    await interaction.response.send_message(
        f"🌍 **Global Stats**\n"
        f"Total accounts: **{total}**\n"
        f"Reported: **{reported}**\n"
        f"Total gens: **{gens}**",
        ephemeral=True
    )

@bot.tree.command(name="downloadstock", description="Download all stock as a TXT file")
@app_commands.check(staff_only)
async def downloadstock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    stock = load_stock()
    if not stock:
        await interaction.followup.send("❌ Stock is empty.", ephemeral=True)
        return

    import io
    lines = []
    for acc in stock:
        lines.append(f"{acc['username']}:{acc['password']} – {acc['games']}")
    
    data = "\n".join(lines).encode("utf-8")
    file = discord.File(io.BytesIO(data), filename="stock.txt")

    game_counts: dict[str, int] = {}
    for acc in stock:
        for g in acc["games"].split(","):
            g = g.strip()
            if g:
                game_counts[g] = game_counts.get(g, 0) + 1

    embed = discord.Embed(title="📦 Stock Download", color=discord.Color.blue())
    embed.add_field(name="✅ Total Accounts", value=f"`{len(stock)}`",      inline=True)
    embed.add_field(name="🎮 Unique Games",   value=f"`{len(game_counts)}`", inline=True)
    embed.set_footer(text="Crimson Gen  ·  Staff Only")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


# ================= START BOT =================
bot.run(TOKEN)
