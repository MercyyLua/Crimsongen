import os
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
# =========================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= STOCK JSON =================
def load_stock() -> list:
    if not os.path.exists(STOCK_PATH):
        return []
    with open(STOCK_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_stock(stock: list):
    with open(STOCK_PATH, "w", encoding="utf-8") as f:
        json.dump(stock, f, indent=2, ensure_ascii=False)

def add_accounts_to_stock(accounts: list):
    """accounts is a list of {"username": ..., "password": ..., "games": ...}"""
    stock = load_stock()
    existing = {f"{a['username']}:{a['password']}" for a in stock}
    added = 0
    for acc in accounts:
        key = f"{acc['username']}:{acc['password']}"
        if key not in existing:
            stock.append(acc)
            existing.add(key)
            added += 1
    save_stock(stock)
    return added

# ================= DATABASE (gens/reports/referrals only) =================
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        cur = con.cursor()

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

def parse_file(text: str):
    """
    Supports:
      Format 1 (inline):  user:pass – Game Name
      Format 2 (block):   Game1\nGame2\nuser:pass
    Returns list of (username, password, games)
    """
    results = []
    lines = [l.rstrip() for l in text.splitlines()]
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Inline format
        normalised = line.replace(" \u2013 ", "|").replace(" \u2014 ", "|").replace(" - ", "|").replace(" | ", "|")
        normalised = normalised.replace("GAMES:", "").replace("Games:", "").replace("games:", "").strip()

        if "|" in normalised and ":" in normalised.split("|")[0]:
            parts = normalised.split("|", 1)
            creds = parts[0].strip()
            games = parts[1].strip()
            if ":" in creds and games:
                user, pwd = creds.split(":", 1)
                if user.strip() and pwd.strip():
                    results.append((user.strip(), pwd.strip(), games.strip()))
                    i += 1
                    continue

        # Block format
        block_lines = []
        while i < len(lines) and lines[i].strip():
            block_lines.append(lines[i].strip())
            i += 1

        if not block_lines:
            i += 1
            continue

        cred_index = None
        for j, bl in enumerate(block_lines):
            if is_credential_line(bl):
                cred_index = j
                break

        if cred_index is None:
            continue

        game_lines = [bl for bl in block_lines[:cred_index] if bl]
        cred_line = block_lines[cred_index]

        user, pwd = cred_line.split(":", 1)
        user = user.strip()
        pwd = pwd.strip()

        if not pwd and cred_index + 1 < len(block_lines):
            pwd = block_lines[cred_index + 1].strip()

        if not user or not pwd:
            continue

        games = ", ".join(game_lines) if game_lines else None
        if not games:
            continue

        results.append((user, pwd, games))

    return results

# ================= EVENTS =================
@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    await bot.change_presence(
        activity=discord.Game(name="🎮 Generating Steam accounts"),
        status=discord.Status.invisible
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

    used = used_today(interaction.user.id)
    limit = daily_limit(interaction.user)

    if used >= limit:
        await interaction.followup.send(f"❌ Daily limit reached ({limit}/day).", ephemeral=True)
        return

    stock = load_stock()
    matches = [a for a in stock if game.lower() in a["games"].lower()]

    if not matches:
        await interaction.followup.send("❌ No accounts available for that game.", ephemeral=True)
        return

    acc = random.choice(matches)
    user, pwd, games = acc["username"], acc["password"], acc["games"]

    with db() as con:
        con.execute("INSERT INTO gens VALUES (?,?)", (interaction.user.id, date.today().isoformat()))

    embed = discord.Embed(
        title="🎮 Generated Steam Account",
        description="Crimson Gen has agreed to only distribute accounts they own. Crimson Gen takes no responsibility for what you do with these accounts.",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1470798856085307423/1471984801266532362/IMG_7053.gif")
    embed.add_field(name="🔐 Account Details", value=f"`{user}:{pwd}`", inline=False)
    embed.add_field(name="🎮 Games", value=games if len(games) < 1024 else games[:1021] + "...", inline=False)
    embed.set_footer(text="Enjoy! ❤️")

    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send("✅ Account sent to your DMs!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ Couldn't send DM. Please enable DMs from server members.\n\n**Account:** `{user}:{pwd}`",
            ephemeral=True
        )


@bot.tree.command(name="listgames", description="View available games")
async def listgames(interaction: discord.Interaction):
    stock = load_stock()
    games = sorted({
        g.strip()
        for acc in stock
        for g in acc["games"].split(",")
        if g.strip()
    })

    if not games:
        await interaction.response.send_message("❌ No games available.")
        return

    pages = []
    for i in range(0, len(games), 15):
        pages.append("🎮 **Available Games**\n" + "\n".join(games[i:i + 15]))

    view = GameView(interaction.user.id, pages)
    view.update()
    await interaction.response.send_message(pages[0], view=view)


@bot.tree.command(name="search", description="Search stock for a game")
async def search(interaction: discord.Interaction, game: str):
    stock = load_stock()
    count = sum(1 for a in stock if game.lower() in a["games"].lower())
    await interaction.response.send_message(f"🔍 **{game}** stock: **{count}**")


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

    if not staff_only(interaction):
        await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
        return

    try:
        text = (await file.read()).decode("utf-8", errors="ignore")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to read file: {e}", ephemeral=True)
        return

    parsed = parse_file(text)
    if not parsed:
        await interaction.followup.send("❌ No valid accounts found in file.", ephemeral=True)
        return

    accounts = [{"username": u, "password": p, "games": g} for u, p, g in parsed]
    added = add_accounts_to_stock(accounts)

    game_counts = {}
    for acc in accounts:
        for g in acc["games"].split(","):
            g = g.strip()
            if g:
                game_counts[g] = game_counts.get(g, 0) + 1

    embed = discord.Embed(title="🔄 Restock Complete", color=discord.Color.green())
    stock_lines = "\n".join(
        f"**{game}:** `{count}` added"
        for game, count in sorted(game_counts.items(), key=lambda x: x[0].lower())
    )
    embed.add_field(name="📦 Games Added", value=stock_lines or "None", inline=False)
    embed.set_footer(text=f"✅ {added} new account(s) added to stock.json")
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

# ================= START BOT =================
bot.run(TOKEN)
