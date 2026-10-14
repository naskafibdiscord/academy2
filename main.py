import asyncio
import hashlib
import io
import json
import os
import re
import time
import unicodedata
import aiohttp
from PIL import Image, ImageDraw, ImageFont

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "data.json")

# ---------- Stockage (data.json) ----------
def load():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

STATE_FILE = os.path.join(DATA_DIR, "sync_state.json")

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def code_hash():
    """Empreinte du code : change dès que main.py est modifié."""
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def default_responses():
    return [
        {
            "id": "acceptee",
            "name": "Candidature acceptée",
            "text": (
                "Bonjour,\n"
                "Nous avons le plaisir de vous informer que votre candidature pour rejoindre le BCSO a été acceptée !\n"
                "Votre profil et vos motivations ont retenu toute notre attention. Nous vous invitons à vous manifester "
                "dès que possible afin d'organiser votre intégration et de planifier vos premières étapes au sein du service.\n"
                "Félicitations et bienvenue parmi nous !\n"
                "Cordialement,\n"
                "L'équipe de direction du BCSO"
            ),
        },
        {
            "id": "refusee",
            "name": "Candidature refusée",
            "text": (
                "Bonjour,\n"
                "Nous vous remercions pour votre intérêt envers le BCSO.\n"
                "Après étude de votre dossier, nous avons le regret de vous informer que votre candidature n'a pas été "
                "retenue pour cette session. Les places étant limitées, nous avons dû effectuer une sélection stricte.\n"
                "Nous vous souhaitons une bonne continuation et vous encourageons à postuler de nouveau lors de prochains recrutements.\n"
                "Cordialement,\n"
                "La direction du BCSO"
            ),
        },
    ]

def default_config():
    return {
        "title": "🎫 Support",
        "description": "Choisissez un type de ticket dans le menu ci-dessous.",
        "types": [],
        "categoryId": None,
        "staffRoleId": None,
        "responses": default_responses(),
        "blacklist": {},
        "welcome": {
            "enabled": False,
            "channelId": None,
            "background": None,
            "message": (
                "🚨 **NOUVEAU RECRUE AU BLOCS / BCSO** 🚨\n\n"
                "Bienvenue à toi, {user} !\n"
                "Tu viens de fouler le sol de Blaine County. Pense à consulter les règlements et à te rapprocher de l'administration pour débuter ton parcours.\n\n"
                "*« To Protect and to Serve »*"
            )
        }
    }

def get_guild(guild_id):
    g = default_config()
    data = load().get(str(guild_id), {})
    g.update(data)
    if "welcome" in data:
        g["welcome"] = {**default_config()["welcome"], **data["welcome"]}
    return g

def update_guild(guild_id, fn):
    db = load()
    g = default_config()
    g.update(db.get(str(guild_id), {}))
    fn(g)
    db[str(guild_id)] = g
    save(db)
    return g

def slug(text):
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "ticket"

# ---------- Panel (menu déroulant) ----------
class TicketSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            custom_id="ticket_select",
            placeholder="Choisissez un type de ticket",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        g = get_guild(guild.id)

        if g["types"]:
            try:
                await interaction.message.edit(view=build_panel_view(g))
            except discord.HTTPException:
                pass

        type_id = self.values[0]
        ticket_type = next((t for t in g["types"] if t["id"] == type_id), None)
        if not ticket_type:
            return await interaction.followup.send("❌ Ce type n'existe plus.", ephemeral=True)

        topic = f"ticket:{interaction.user.id}:{type_id}"
        existing = discord.utils.find(lambda c: c.topic == topic, guild.text_channels)
        if existing:
            return await interaction.followup.send(
                f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True
            )

        perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: perms,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
            ),
        }
        staff_role = guild.get_role(g["staffRoleId"]) if g["staffRoleId"] else None
        if staff_role:
            overwrites[staff_role] = perms

        category = guild.get_channel(g["categoryId"]) if g["categoryId"] else None

        try:
            channel = await guild.create_text_channel(
                name=f"{slug(ticket_type['name'])}-{slug(interaction.user.name)}"[:90],
                category=category,
                topic=topic,
                overwrites=overwrites,
            )
        except discord.HTTPException:
            return await interaction.followup.send(
                "❌ Impossible de créer le ticket (vérifie la permission « Gérer les salons » du bot).",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"{ticket_type['emoji']} {ticket_type['name']}".strip(),
            description=f"Bienvenue {interaction.user.mention}, explique ta demande. Le staff va te répondre.",
            color=0x57F287,
        )
        await channel.send(
            content=staff_role.mention if staff_role else None,
            embed=embed,
            view=CloseView(),
        )
        recruit = is_recruitment(ticket_type)
        print(f"🎫 Ticket « {ticket_type['name']} » ouvert par {interaction.user} | formulaire recrutement : {recruit}")
        if recruit:
            try:
                await send_recruitment_form(channel, interaction.user)
            except Exception as e:
                print(f"❌ Formulaire de recrutement impossible : {e}")
        await interaction.followup.send(f"✅ Ticket créé : {channel.mention}", ephemeral=True)

def build_panel_view(g):
    options = [
        discord.SelectOption(
            label=t["name"],
            value=t["id"],
            description=t["description"] or None,
            emoji=t["emoji"] or None,
        )
        for t in g["types"]
    ]
    view = discord.ui.View(timeout=None)
    view.add_item(TicketSelect(options))
    return view

def build_panel_embed(g):
    return discord.Embed(title=g["title"], description=g["description"], color=0x5865F2)

# ---------- Bouton fermer ----------
class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = get_guild(interaction.guild_id)
        parts = (interaction.channel.topic or "").split(":")
        owner_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        is_staff = (
            (g["staffRoleId"] and interaction.user.get_role(g["staffRoleId"]) is not None)
            or interaction.user.guild_permissions.manage_channels
        )
        if interaction.user.id != owner_id and not is_staff:
            return await interaction.response.send_message("❌ Tu ne peux pas fermer ce ticket.", ephemeral=True)

        await interaction.response.send_message("🔒 Ticket fermé, suppression dans 5 secondes...")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except discord.HTTPException:
            pass

# ---------- Modal apparence ----------
class ApparenceModal(discord.ui.Modal, title="Apparence du panel"):
    def __init__(self, g):
        super().__init__()
        self.titre = discord.ui.TextInput(label="Titre", default=g["title"], max_length=256)
        self.desc = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=g["description"],
            max_length=4000,
        )
        self.add_item(self.titre)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction):
        def apply(g):
            g["title"] = self.titre.value
            g["description"] = self.desc.value

        update_guild(interaction.guild_id, apply)
        await interaction.response.send_message(
            "✅ Apparence mise à jour. Refais `/ticket_config envoyer` pour mettre à jour le panel.",
            ephemeral=True,
        )

# ---------- Commande /ticket_config ----------
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
class TicketConfig(app_commands.Group, name="ticket_config", description="Configurer le système de tickets"):

    @app_commands.command(name="apparence", description="Modifier le titre et la description du panel")
    async def apparence(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApparenceModal(get_guild(interaction.guild_id)))

    @app_commands.command(name="type_ajouter", description="Ajouter un type de ticket au menu")
    @app_commands.describe(nom="Ex : Recrutement", description="Petite description sous le nom", emoji="Ex : 📝")
    async def type_ajouter(
        self,
        interaction: discord.Interaction,
        nom: app_commands.Range[str, 1, 80],
        description: app_commands.Range[str, 0, 100] = "",
        emoji: str = "",
    ):
        g = get_guild(interaction.guild_id)
        if len(g["types"]) >= 25:
            return await interaction.response.send_message("❌ Maximum 25 types.", ephemeral=True)
        new_type = {
            "id": format(int(time.time() * 1000), "x"),
            "name": nom,
            "description": description,
            "emoji": emoji,
        }
        update_guild(interaction.guild_id, lambda x: x["types"].append(new_type))
        await interaction.response.send_message(
            f"✅ Type **{nom}** ajouté. Refais `/ticket_config envoyer` pour mettre à jour le panel.",
            ephemeral=True,
        )

    @app_commands.command(name="type_retirer", description="Retirer un type de ticket")
    @app_commands.describe(nom="Type à retirer")
    async def type_retirer(self, interaction: discord.Interaction, nom: str):
        g = get_guild(interaction.guild_id)
        if not any(t["id"] == nom for t in g["types"]):
            return await interaction.response.send_message("❌ Type introuvable.", ephemeral=True)
        update_guild(interaction.guild_id, lambda x: x.update(types=[t for t in x["types"] if t["id"] != nom]))
        await interaction.response.send_message(
            "🗑️ Type retiré. Refais `/ticket_config envoyer` pour mettre à jour le panel.", ephemeral=True
        )

    @type_retirer.autocomplete("nom")
    async def type_retirer_autocomplete(self, interaction: discord.Interaction, current: str):
        g = get_guild(interaction.guild_id)
        return [
            app_commands.Choice(name=t["name"][:100], value=t["id"])
            for t in g["types"]
            if current.lower() in t["name"].lower()
        ][:25]

    @app_commands.command(name="categorie", description="Catégorie où les tickets seront créés")
    @app_commands.describe(categorie="La catégorie")
    async def categorie(self, interaction: discord.Interaction, categorie: discord.CategoryChannel):
        update_guild(interaction.guild_id, lambda x: x.update(categoryId=categorie.id))
        await interaction.response.send_message(
            f"✅ Les tickets seront créés dans **{categorie.name}**.", ephemeral=True
        )

    @app_commands.command(name="role_staff", description="Rôle qui peut voir et gérer les tickets")
    @app_commands.describe(role="Le rôle staff")
    async def role_staff(self, interaction: discord.Interaction, role: discord.Role):
        update_guild(interaction.guild_id, lambda x: x.update(staffRoleId=role.id))
        await interaction.response.send_message(f"✅ Rôle staff : {role.mention}.", ephemeral=True)

    @app_commands.command(name="envoyer", description="Envoyer le panel de tickets dans un salon")
    @app_commands.describe(salon="Salon du panel")
    async def envoyer(self, interaction: discord.Interaction, salon: discord.TextChannel):
        g = get_guild(interaction.guild_id)
        if not g["types"]:
            return await interaction.response.send_message(
                "❌ Ajoute au moins un type avec `/ticket_config type_ajouter`.", ephemeral=True
            )
        await salon.send(embed=build_panel_embed(g), view=build_panel_view(g))
        await interaction.response.send_message(f"✅ Panel envoyé dans {salon.mention}.", ephemeral=True)

    @app_commands.command(name="voir", description="Voir la configuration actuelle")
    async def voir(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        embed = discord.Embed(title="⚙️ Configuration des tickets", color=0x5865F2)
        embed.add_field(name="Titre", value=g["title"], inline=False)
        embed.add_field(name="Description", value=g["description"][:1000], inline=False)
        types = "\n".join(f"{t['emoji']} {t['name']}".strip() for t in g["types"]) or "Aucun"
        embed.add_field(name="Types", value=types, inline=False)
        embed.add_field(name="Catégorie", value=f"<#{g['categoryId']}>" if g["categoryId"] else "Non définie")
        embed.add_field(name="Rôle staff", value=f"<@&{g['staffRoleId']}>" if g["staffRoleId"] else "Non défini")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- Système de Message de Bienvenue (/bienvenu_config) ----------
class BienvenueModal(discord.ui.Modal, title="Configuration du Message de Bienvenue"):
    def __init__(self, current_msg):
        super().__init__()
        self.message_input = discord.ui.TextInput(
            label="Message de bienvenue",
            style=discord.TextStyle.paragraph,
            default=current_msg,
            max_length=1500,
            placeholder="Utilise {user} pour mentionner le membre."
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        def apply(g):
            g["welcome"]["message"] = self.message_input.value
        update_guild(interaction.guild_id, apply)
        await interaction.response.send_message("✅ Texte de bienvenue mis à jour avec succès !", ephemeral=True)

@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
class BienvenuConfig(app_commands.Group, name="bienvenu_config", description="Configurer le système de bienvenue BCSO"):

    @app_commands.command(name="salon", description="Définir le salon où envoyer les messages de bienvenue")
    @app_commands.describe(salon="Le salon textuel")
    async def salon(self, interaction: discord.Interaction, salon: discord.TextChannel):
        update_guild(interaction.guild_id, lambda x: x["welcome"].__setitem__("channelId", salon.id))
        await interaction.response.send_message(f"✅ Salon de bienvenue défini sur {salon.mention}.", ephemeral=True)

    @app_commands.command(name="activer", description="Activer ou désactiver le système de bienvenue")
    @app_commands.describe(etat="True pour activer, False pour désactiver")
    async def activer(self, interaction: discord.Interaction, etat: bool):
        update_guild(interaction.guild_id, lambda x: x["welcome"].__setitem__("enabled", etat))
        status = "activé 🟢" if etat else "désactivé 🔴"
        await interaction.response.send_message(f"✅ Le système de bienvenue est désormais {status}.", ephemeral=True)

    @app_commands.command(name="texte", description="Modifier le message textuel de bienvenue")
    async def texte(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        await interaction.response.send_modal(BienvenueModal(g["welcome"]["message"]))

    @app_commands.command(name="fond", description="Définir une image d'arrière-plan personnalisée (URL directe de l'image)")
    @app_commands.describe(url="Lien direct vers l'image (laisse vide pour supprimer)")
    async def fond(self, interaction: discord.Interaction, url: str = None):
        update_guild(interaction.guild_id, lambda x: x["welcome"].__setitem__("background", url))
        if url:
            await interaction.response.send_message("✅ Image d'arrière-plan mise à jour avec succès !", ephemeral=True)
        else:
            await interaction.response.send_message("🗑️ Image d'arrière-plan réinitialisée (fond BCSO par défaut).", ephemeral=True)

    @app_commands.command(name="voir", description="Voir la configuration actuelle du système de bienvenue")
    async def voir(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        w = g["welcome"]
        embed = discord.Embed(title="⚙️ Configuration - Bienvenue BCSO", color=0xC9A227)
        embed.add_field(name="État", value="Activé 🟢" if w["enabled"] else "Désactivé 🔴", inline=True)
        embed.add_field(name="Salon", value=f"<#{w['channelId']}>" if w["channelId"] else "Non défini", inline=True)
        embed.add_field(name="Fond personnalisé", value="Défini (URL)" if w["background"] else "Défaut BCSO", inline=True)
        embed.add_field(name="Message", value=w["message"][:1000], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def generate_welcome_card(member, bg_url):
    """Génère une image de bienvenue stylisée BCSO avec l'avatar du membre"""
    width, height = 900, 350
    card = Image.new("RGBA", (width, height), (20, 24, 33, 255))
    draw = ImageDraw.Draw(card)

    bg_loaded = False
    if bg_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(bg_url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        bg_img = Image.open(io.BytesIO(data)).convert("RGBA")
                        bg_img = bg_img.resize((width, height))
                        card.paste(bg_img, (0, 0))
                        bg_loaded = True
        except Exception:
            pass

    if not bg_loaded:
        for x in range(width):
            color = int(20 + (x / width) * 15)
            draw.line([(x, 0), (x, height)], fill=(color, color + 5, color + 15, 255))
        draw.rectangle([0, 0, width - 1, height - 1], outline=(201, 162, 39, 255), width=4)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 140))
    card = Image.alpha_composite(card, overlay)
    draw = ImageDraw.Draw(card)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(str(member.display_avatar.url)) as resp:
                if resp.status == 200:
                    avatar_data = await resp.read()
                    avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA")
                    avatar = avatar.resize((160, 160))

                    mask = Image.new("L", (160, 160), 0)
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse((0, 0, 160, 160), fill=255)

                    draw.ellipse((68, 68, 232, 232), fill=(201, 162, 39, 255))
                    card.paste(avatar, (74, 74), mask)
    except Exception:
        pass

    try:
        font_title = ImageFont.truetype("arial.ttf", 36)
        font_sub = ImageFont.truetype("arial.ttf", 22)
    except IOError:
        font_title = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    draw.text((270, 90), "BIENVENUE AU BCSO", fill=(201, 162, 39, 255), font=font_title)
    draw.text((270, 145), f"{member.name}#{member.discriminator}" if member.discriminator != "0" else member.name, fill=(255, 255, 255, 255), font=font_sub)
    draw.text((270, 180), f"Membre n° {member.guild.member_count}", fill=(180, 190, 200, 255), font=font_sub)

    buffer = io.BytesIO()
    card.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# ---------- Réponses types : /reponse_config et /reponse ----------
def render_response(text, member):
    if "{user}" in text:
        return text.replace("{user}", member.mention)
    match = re.search(r"bonjour", text, re.IGNORECASE)
    if match:
        return text[: match.end()] + " " + member.mention + text[match.end():]
    return f"{member.mention}\n{text}"

async def response_choices(interaction: discord.Interaction, current: str):
    g = get_guild(interaction.guild_id)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in g["responses"]
        if current.lower() in r["name"].lower()
    ][:25]

class ReponseModal(discord.ui.Modal):
    def __init__(self, existing=None):
        super().__init__(title="Modifier la réponse" if existing else "Nouvelle réponse")
        self.existing_id = existing["id"] if existing else None
        self.nom = discord.ui.TextInput(
            label="Nom de la réponse",
            placeholder="Ex : Candidature acceptée",
            default=existing["name"] if existing else None,
            max_length=50,
        )
        self.texte = discord.ui.TextInput(
            label="Texte du message",
            style=discord.TextStyle.paragraph,
            placeholder="Bonjour,\n...",
            default=existing["text"] if existing else None,
            max_length=1900,
        )
        self.add_item(self.nom)
        self.add_item(self.texte)

    async def on_submit(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        if not self.existing_id and len(g["responses"]) >= 25:
            return await interaction.response.send_message("❌ Maximum 25 réponses.", ephemeral=True)

        def apply(x):
            if self.existing_id:
                for r in x["responses"]:
                    if r["id"] == self.existing_id:
                        r["name"] = self.nom.value
                        r["text"] = self.texte.value
            else:
                x["responses"].append(
                    {"id": format(int(time.time() * 1000), "x"), "name": self.nom.value, "text": self.texte.value}
                )

        update_guild(interaction.guild_id, apply)
        await interaction.response.send_message(
            f"✅ Réponse **{self.nom.value}** enregistrée. Utilise `/reponse` pour l'envoyer.", ephemeral=True
        )

@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
class ReponseConfig(app_commands.Group, name="reponse_config", description="Configurer les réponses types"):

    @app_commands.command(name="ajouter", description="Créer une nouvelle réponse type")
    async def ajouter(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReponseModal())

    @app_commands.command(name="modifier", description="Modifier une réponse type")
    @app_commands.describe(nom="Réponse à modifier")
    async def modifier(self, interaction: discord.Interaction, nom: str):
        g = get_guild(interaction.guild_id)
        r = next((r for r in g["responses"] if r["id"] == nom), None)
        if not r:
            return await interaction.response.send_message("❌ Réponse introuvable.", ephemeral=True)
        await interaction.response.send_modal(ReponseModal(r))

    @modifier.autocomplete("nom")
    async def modifier_autocomplete(self, interaction: discord.Interaction, current: str):
        return await response_choices(interaction, current)

    @app_commands.command(name="supprimer", description="Supprimer une réponse type")
    @app_commands.describe(nom="Réponse à supprimer")
    async def supprimer(self, interaction: discord.Interaction, nom: str):
        g = get_guild(interaction.guild_id)
        if not any(r["id"] == nom for r in g["responses"]):
            return await interaction.response.send_message("❌ Réponse introuvable.", ephemeral=True)
        update_guild(interaction.guild_id, lambda x: x.update(responses=[r for r in x["responses"] if r["id"] != nom]))
        await interaction.response.send_message("🗑️ Réponse supprimée.", ephemeral=True)

    @supprimer.autocomplete("nom")
    async def supprimer_autocomplete(self, interaction: discord.Interaction, current: str):
        return await response_choices(interaction, current)

    @app_commands.command(name="voir", description="Voir les réponses types enregistrées")
    async def voir(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        if not g["responses"]:
            return await interaction.response.send_message(
                "Aucune réponse. Crée-en une avec `/reponse_config ajouter`.", ephemeral=True
            )
        embed = discord.Embed(title="📨 Réponses types", color=0x5865F2)
        for r in g["responses"]:
            preview = r["text"][:200] + ("…" if len(r["text"]) > 200 else "")
            embed.add_field(name=r["name"], value=preview, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ResponseSelect(discord.ui.Select):
    def __init__(self, responses):
        super().__init__(
            placeholder="1️⃣ Choisis le texte à envoyer",
            options=[discord.SelectOption(label=r["name"][:100], value=r["id"]) for r in responses],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_id = self.values[0]
        await interaction.response.defer()

class MemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="2️⃣ Choisis la personne à mentionner")

    async def callback(self, interaction: discord.Interaction):
        self.view.member = self.values[0]
        await interaction.response.defer()

class SendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Envoyer", emoji="📨", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not view.selected_id or not view.member:
            return await interaction.response.send_message(
                "❌ Choisis un texte ET une personne avant d'envoyer.", ephemeral=True
            )
        g = get_guild(interaction.guild_id)
        r = next((r for r in g["responses"] if r["id"] == view.selected_id), None)
        if not r:
            return await interaction.response.send_message("❌ Ce texte n'existe plus.", ephemeral=True)
        try:
            await interaction.channel.send(
                render_response(r["text"], view.member),
                allowed_mentions=discord.AllowedMentions(users=[view.member]),
            )
        except discord.HTTPException:
            return await interaction.response.send_message(
                "❌ Impossible d'envoyer ici (vérifie les permissions du bot dans ce salon).", ephemeral=True
            )
        view.stop()
        await interaction.response.edit_message(content="✅ Message envoyé !", view=None)

class ResponsePanel(discord.ui.View):
    def __init__(self, responses):
        super().__init__(timeout=300)
        self.selected_id = None
        self.member = None
        self.add_item(ResponseSelect(responses))
        self.add_item(MemberSelect())
        self.add_item(SendButton())

@app_commands.command(name="reponse", description="Ouvrir le panel pour envoyer une réponse type")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def reponse(interaction: discord.Interaction):
    g = get_guild(interaction.guild_id)
    if not g["responses"]:
        return await interaction.response.send_message(
            "❌ Aucune réponse. Crée-en une avec `/reponse_config ajouter`.", ephemeral=True
        )
    await interaction.response.send_message(
        "📨 **Réponses types** : choisis le texte, la personne à mentionner, puis clique sur Envoyer.",
        view=ResponsePanel(g["responses"]),
        ephemeral=True,
    )

# ---------- Blacklist : /blacklist, /unblacklist, /blacklist_liste ----------
BLACKLIST_ROLE_NAME = "blacklist"

async def get_blacklist_role(guild, create=True):
    role = discord.utils.find(lambda r: r.name.lower() == BLACKLIST_ROLE_NAME, guild.roles)
    if not role and create:
        role = await guild.create_role(name=BLACKLIST_ROLE_NAME, reason="Système de blacklist")
    return role

async def hide_channels(guild, role):
    """Refuse « Voir le salon » au rôle blacklist sur tous les salons et catégories."""
    count = 0
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        if overwrite.view_channel is False:
            continue
        overwrite.view_channel = False
        try:
            await channel.set_permissions(role, overwrite=overwrite, reason="Blacklist : salon masqué")
            count += 1
        except discord.HTTPException as e:
            print(f"❌ Impossible de masquer #{channel.name} : {e}")
    return count

def clean_id(text):
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None

@app_commands.command(name="blacklist", description="Blacklister quelqu'un définitivement (même s'il quitte le serveur)")
@app_commands.describe(identifiant="ID Discord de la personne", raison="Raison (optionnel)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def blacklist(interaction: discord.Interaction, identifiant: str, raison: str = "Non précisée"):
    uid = clean_id(identifiant)
    if not uid:
        return await interaction.response.send_message("❌ Identifiant invalide (chiffres uniquement).", ephemeral=True)
    g = get_guild(interaction.guild_id)
    if str(uid) in g["blacklist"]:
        return await interaction.response.send_message("⚠️ Cette personne est déjà blacklistée.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        user = await interaction.client.fetch_user(uid)
    except discord.NotFound:
        return await interaction.followup.send("❌ Aucun compte Discord avec cet identifiant.", ephemeral=True)

    entry = {
        "name": str(user),
        "reason": raison[:500],
        "by": interaction.user.id,
        "date": time.strftime("%d/%m/%Y"),
    }
    update_guild(interaction.guild_id, lambda x: x["blacklist"].__setitem__(str(uid), entry))

    guild = interaction.guild
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            member = None

    try:
        role = await get_blacklist_role(guild)
        await hide_channels(guild, role)
    except discord.Forbidden:
        role = None

    if role is None:
        status = (
            "⚠️ Enregistré, mais je n'ai pas pu préparer le rôle : "
            "donne-moi « Gérer les rôles » et « Gérer les salons »."
        )
    elif member:
        try:
            await member.add_roles(role, reason=f"Blacklist par {interaction.user}")
            status = f"Le rôle **{role.name}** a été donné à {member.mention} : il ne voit plus aucun salon."
        except discord.Forbidden:
            status = (
                "⚠️ Enregistré, mais je n'ai pas pu donner le rôle : "
                "mon rôle doit être **au-dessus** du rôle blacklist."
            )
    else:
        status = "La personne n'est pas sur le serveur : le rôle lui sera donné automatiquement si elle revient."

    await interaction.followup.send(
        f"🚫 **{user}** (`{uid}`) est blacklisté(e).\nRaison : {entry['reason']}\n{status}", ephemeral=True
    )

@app_commands.command(name="unblacklist", description="Retirer quelqu'un de la blacklist")
@app_commands.describe(identifiant="ID Discord de la personne")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def unblacklist(interaction: discord.Interaction, identifiant: str):
    uid = clean_id(identifiant)
    g = get_guild(interaction.guild_id)
    if not uid or str(uid) not in g["blacklist"]:
        return await interaction.response.send_message("❌ Cette personne n'est pas blacklistée.", ephemeral=True)
    update_guild(interaction.guild_id, lambda x: x["blacklist"].pop(str(uid), None))

    member = interaction.guild.get_member(uid)
    role = await get_blacklist_role(interaction.guild, create=False)
    if member and role and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Retrait blacklist par {interaction.user}")
        except discord.Forbidden:
            pass
    await interaction.response.send_message(f"✅ `{uid}` a été retiré(e) de la blacklist.", ephemeral=True)

@app_commands.command(name="blacklist_liste", description="Voir toutes les personnes blacklistées")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def blacklist_liste(interaction: discord.Interaction):
    g = get_guild(interaction.guild_id)
    if not g["blacklist"]:
        return await interaction.response.send_message("La blacklist est vide.", ephemeral=True)
    lines = [
        f"• <@{uid}> (`{uid}`) — {e['reason']} _({e['date']})_" for uid, e in g["blacklist"].items()
    ]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await interaction.response.send_message(
        embed=discord.Embed(title=f"🚫 Blacklist ({len(lines)})", description=text, color=0xED4245),
        ephemeral=True,
    )

# ---------- Formulaire de recrutement (ticket « Recrutement ») ----------
RECRUIT_COLOR = 0xC9A227
CHECK = "☐ Oui   ☐ Non"

RECRUIT_SECTIONS = [
    ("I. INFORMATIONS PERSONNELLES", [
        "**1.** Nom et prénom RP :",
        "**2.** Âge RP :",
        "**3.** Âge réel :",
        "**4.** Disponibilités (jours et horaires) :",
        "**5.** Identifiant Discord :",
        "**6.** Depuis combien de temps jouez-vous sur le serveur ?",
    ]),
    ("II. EXPÉRIENCE ET MOTIVATIONS", [
        "**7.** Avez-vous déjà été membre des forces de l'ordre sur un autre serveur RP ? Si oui, précisez votre expérience.",
        "**8.** Pourquoi souhaitez-vous rejoindre la BCSO ?",
        "**9.** Quelles sont vos principales qualités pour exercer le métier de shérif adjoint ?",
        "**10.** Quels sont vos défauts et comment comptez-vous les améliorer ?",
        "**11.** Que pouvez-vous apporter à la BCSO et à ses effectifs ?",
        "**12.** Pourquoi devrions-nous vous choisir plutôt qu'un autre candidat ?",
    ]),
    ("III. CONNAISSANCES DU ROLEPLAY", [
        "**13.** Expliquez avec vos propres mots ce qu'est le RP.",
        "**14.** Qu'est-ce que le Fear RP ?",
        "**15.** Qu'est-ce que le PowerGaming ?",
        "**16.** Qu'est-ce que le MetaGaming ?",
        "**17.** Que signifie le terme « No Pain RP » ?",
        "**18.** Quelle est la différence entre une action RP et une action HRP ?",
    ]),
    ("IV. MISES EN SITUATION", [
        "**19.** Vous contrôlez un véhicule dont le conducteur refuse d'obtempérer. Quelle est votre procédure ?",
        "**20.** Lors d'une interpellation, un individu vous insulte et vous provoque. Comment réagissez-vous ?",
        "**21.** Vous constatez qu'un collègue ne respecte pas le règlement du serveur. Que faites-vous ?",
        "**22.** Vous êtes seul face à plusieurs individus armés. Comment gérez-vous la situation en RP ?",
        "**23.** Un supérieur vous donne un ordre que vous estimez contraire au règlement. Quelle est votre réaction ?",
        "**24.** Vous arrivez sur une scène où plusieurs témoins donnent des versions différentes des faits. Comment procédez-vous ?",
    ]),
    ("V. ENGAGEMENT ET RÈGLEMENT", [
        f"**25.** Êtes-vous prêt à respecter la hiérarchie de la BCSO ?\n{CHECK}",
        f"**26.** Vous engagez-vous à respecter le règlement du serveur et les règles RP ?\n{CHECK}",
        f"**27.** Êtes-vous prêt à suivre une formation avant de prendre vos fonctions ?\n{CHECK}",
        f"**28.** Acceptez-vous de faire preuve de professionnalisme, de neutralité et de respect envers les citoyens et vos collègues ?\n{CHECK}",
        "**29.** Avez-vous des sanctions ou antécédents RP susceptibles d'être utiles à l'évaluation de votre candidature ? Si oui, précisez.",
    ]),
    ("VI. DÉCLARATION DU CANDIDAT", [
        "*Je certifie que les informations fournies dans ce formulaire sont exactes. Je m'engage à respecter les règles du "
        "serveur, les procédures internes de la BCSO et la hiérarchie de l'institution.*",
        "**Nom du candidat :**\n**Date de candidature :**\n**Signature :**",
    ]),
    ("VII. RÉSERVÉ À L'ADMINISTRATION BCSO", [
        "**Décision finale :**\n☐ Candidature acceptée\n☐ Entretien requis\n☐ Candidature refusée",
        "**Évaluateur :**\n**Observations :**\n**Note finale :** …… / 20\n**Date de décision :**",
    ]),
]

def is_recruitment(ticket_type):
    name = slug(ticket_type["name"])
    return any(word in name for word in ("recrut", "candidat"))

async def send_recruitment_form(channel, user):
    embeds = [discord.Embed(
        title="🚔 BLAINE COUNTY SHERIFF'S OFFICE",
        description="**FORMULAIRE DE RECRUTEMENT**\n\n*« Servir et protéger les citoyens de Blaine County »*",
        color=RECRUIT_COLOR,
    )]
    for title, lines in RECRUIT_SECTIONS:
        embeds.append(discord.Embed(title=title, description="\n\n".join(lines), color=RECRUIT_COLOR))
    for emb in embeds:
        await channel.send(embed=emb)

# ---------- Système MP d'Annonce PA (/mp_pa) ----------
class TimeSelect(discord.ui.Select):
    def __init__(self):
        times = []
        for h in range(20, 24):
            for m in (0, 15, 30, 45):
                times.append(f"{h:02d}h{m:02d}")

        options = [discord.SelectOption(label=t, value=t) for t in times]
        super().__init__(placeholder="⏰ Choisissez l'heure du rendez-vous", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_time = self.values[0]
        await interaction.response.send_message(
            f"🚀 Envoi des MP en cours pour l'horaire : **{selected_time}**...",
            ephemeral=True
        )

        message_text = (
            f"🚨 **[BCSO] RECRUTEMENT OUVERT — BCSO ACADEMY (PA)** 🚨\n\n"
            f"📢 **RENDEZ-VOUS CE SOIR À {selected_time} AU POSTE DU BCSO (SANDY SHORES)** 📢\n\n"
            f"*« To Protect and to Serve » — Servir et protéger le Comté de Blaine*\n\n"
            f"Vous avez le sens du devoir, de la justice et de l'esprit d'équipe ? Le Blaine County Sheriff's Office ouvre les portes de son académie de police pour former la prochaine génération d'adjoints du Sheriff !\n\n"
            f"📅 **Rendez-vous & Inscriptions :**\n"
            f"- 🏢 **Lieu :** Poste du BCSO de Sandy Shores\n"
            f"- 🕗 **Heure :** Ce soir à {selected_time}\n\n"
            f"📋 **Profil recherché :**\n"
            f"- ⚖️ **Rigueur & Discipline :** Savoir garder son calme et respecter la chaîne de commandement.\n"
            f"- 🚗 **Conduite & Sécurité :** Être titulaire du permis de conduire.\n"
            f"- 📜 **Casier judiciaire :** Vierge de toute condamnation grave.\n"
            f"- 🗣️ **Communication :** Être capable de s'exprimer clairement et avec sang-froid en toutes situations.\n\n"
            f"📝 **Comment postuler ?**\n"
            f"1. **Déposez votre dossier sur Discord :** Ouvrez un ticket dans la section dédiée sur le Discord BCSO ACADEMY.\n"
            f"2. **Attendez la validation :** Vérifiez si votre candidature est acceptée ou refusée.\n"
            f"3. **Entretien oral :** Si votre candidature est acceptée, rendez-vous à l'heure et au lieu indiqués ci-dessus pour passer votre entretien oral avec la Supervision."
        )

        guild = interaction.guild
        success = 0
        failed = 0

        for member in guild.members:
            if member.bot:
                continue
            try:
                await member.send(message_text)
                success += 1
                await asyncio.sleep(0.5)
            except Exception:
                failed += 1

        await interaction.followup.send(
            f"✅ **Annonce envoyée !**\n"
            f"• **Envoyés avec succès :** {success}\n"
            f"• **Bloqués/Échecs (MP fermés) :** {failed}",
            ephemeral=True
        )

class TimeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(TimeSelect())

@app_commands.command(name="mp_pa", description="Envoyer le message de recrutement PA à tous les membres en MP")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def mp_pa(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🕒 **Sélectionnez l'heure de l'événement** dans le menu déroulant ci-dessous :",
        view=TimeView(),
        ephemeral=True
    )

# ---------- Configuration du Bot ----------
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(TicketConfig())
        self.tree.add_command(ReponseConfig())
        self.tree.add_command(reponse)
        self.tree.add_command(blacklist)
        self.tree.add_command(unblacklist)
        self.tree.add_command(blacklist_liste)
        self.tree.add_command(mp_pa)
        self.tree.add_command(BienvenuConfig())

        self.add_view(CloseView())
        db = load()
        for guild_id_str in db:
            try:
                g = get_guild(int(guild_id_str))
                if g["types"]:
                    self.add_view(build_panel_view(g))
            except Exception:
                pass

    async def on_ready(self):
        print(f"🤖 Connecté en tant que {self.user} (ID: {self.user.id})")
        
        # Remplace par l'ID de ton serveur Discord
        MY_GUILD = discord.Object(id=123456789012345678) # ⚠️ Mets ton ID de serveur ici
        
        try:
            self.tree.copy_global_to(guild=MY_GUILD)
            await self.tree.sync(guild=MY_GUILD)
            print("✅ Commandes slash synchronisées instantanément sur le serveur de test.")
        except Exception as e:
            print(f"⚠️ Erreur lors de la synchronisation de la guilde : {e}")
            await self.tree.sync()
            print("✅ Commandes slash synchronisées globalement.")

    async def on_member_join(self, member):
        guild = member.guild
        g = get_guild(guild.id)

        if str(member.id) in g["blacklist"]:
            role = await get_blacklist_role(guild, create=False)
            if role:
                try:
                    await member.add_roles(role, reason="Retour d'une personne blacklistée")
                    print(f"🚫 Rôle blacklist réappliqué à {member} ({member.id})")
                except discord.HTTPException:
                    pass

        w = g["welcome"]
        if w["enabled"] and w["channelId"]:
            channel = guild.get_channel(w["channelId"])
            if channel:
                try:
                    img_buffer = await generate_welcome_card(member, w["background"])
                    file = discord.File(fp=img_buffer, filename="welcome_bcso.png")

                    formatted_text = w["message"].replace("{user}", member.mention)

                    embed = discord.Embed(color=0xC9A227)
                    embed.set_image(url="attachment://welcome_bcso.png")

                    await channel.send(content=formatted_text, embed=embed, file=file)
                except Exception as e:
                    print(f"❌ Erreur lors de l'envoi du message de bienvenue : {e}")

bot = MyBot()

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ Erreur : Le token DISCORD_TOKEN est introuvable !")
    else:
        bot.run(TOKEN)
