import discord
import os
import cassiopeia as cass
import asyncio
from discord.ext import tasks
import requests
from dotenv import load_dotenv

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()

# Désactivation du cache pour obtenir des données fraîches à chaque appel
cass.apply_settings({"cache": {"expiration": 0}})

mapping: dict[str, str] = {
    "IRON": 0,
    "BRONZE": 1,
    "SILVER": 2,
    "GOLD": 3,
    "PLATINUM": 4,
    "EMERALD": 5,
    "DIAMOND": 6,
    "MASTER": 7,
    "GRANDMASTER": 8,
    "CHALLENGER": 9,
    "I": 300,
    "II": 200,
    "III": 100,
    "IV": 0,
}

# -------------------------
# CONSTANTES & CONFIGURATION (chargées depuis le .env)
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
PLAYERS_FILE = os.getenv("PLAYERS_FILE", "players.txt")
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID"))

# Configuration de Cassiopeia
cass.set_riot_api_key(RIOT_API_KEY)
# Vous pouvez définir d'autres paramètres (région par défaut, etc.) si nécessaire

# -------------------------
# VARIABLE GLOBALE
# -------------------------
# Dictionnaire des joueurs enregistrés.
# Clé : (name, tag) ; Valeur : objet Summoner (Cassiopeia)
players: dict[tuple[str, str], cass.Summoner] = {}

# Dictionnaire global pour suivre les parties actives.
# Clé : (name, tag) ; Valeur : (game_id, champion, lp_initial)
active_games = {}


# -------------------------
# FONCTIONS DE GESTION DES JOUEURS
# -------------------------
def load_players():
    """
    Charge les joueurs depuis le fichier texte dans le dictionnaire global 'players'.
    Chaque ligne doit être au format : name,tag,server
    """
    global players
    if os.path.exists(PLAYERS_FILE):
        with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) == 3:
                    name, tag, server = parts
                    summoner = get_player_data(name, tag, server)
                    if summoner:
                        players[(name, tag)] = summoner
        print(f"{len(players)} joueurs chargés depuis {PLAYERS_FILE}.")
    else:
        print(
            "Aucun fichier de joueurs trouvé. Un nouveau sera créé lors du premier enregistrement."
        )


def save_player(name: str, tag: str, server: str):
    """
    Ajoute un joueur dans le fichier texte.
    """
    with open(PLAYERS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{name},{tag},{server}\n")


def get_player_data(name: str, tag: str, server: str):
    """
    Récupère les informations d'un joueur via l'API Riot en utilisant Cassiopeia.
    """
    try:
        # La méthode get_account() prend en compte le nom, la région et le "tag" (à gérer localement).
        summoner = cass.get_account(name=name, region=server, tagline=tag).summoner
        return summoner
    except Exception as e:
        print(f"Erreur lors de la récupération du joueur {name} sur {server}: {e}")
        return None


async def player(name: str, tag: str, server: str):
    """
    Version asynchrone de la récupération des informations d'un joueur.
    L'appel bloquant est exécuté dans un thread séparé pour ne pas bloquer l'event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_player_data, name, tag, server)


def get_solo_lp(summoner: cass.Summoner):
    """
    Récupère le nombre de LP (points) pour le mode classé Solo/Duo d'un summoner
    en effectuant directement une requête à l'API Riot.

    Retourne un tuple (lp, tier, division) si trouvé, sinon None.

    Pour récupérer les données, on utilise l'endpoint suivant :
      GET https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summonerId}?api_key={RIOT_API_KEY}

    Remarque :
      - `summoner.id` doit contenir l'identifiant crypté du summoner.
      - `summoner.region` doit permettre de déterminer la région (exemple : "euw1", "na1", etc.).
    """
    try:
        summoner_id = summoner.id
        region = (
            summoner.region.value
            if hasattr(summoner.region, "value")
            else summoner.region
        )
        url = f"https://{region}1.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}?api_key={RIOT_API_KEY}"
        response = requests.get(url)
        if response.status_code != 200:
            print(
                f"Erreur lors de la récupération des LP pour {summoner.name} (HTTP {response.status_code})"
            )
            return None

        data = response.json()
        for entry in data:
            if entry.get("queueType") == "RANKED_SOLO_5x5":
                league_points = entry.get("leaguePoints")
                tier = entry.get("tier")
                division = entry.get("rank")
                return (league_points, tier, division)
    except Exception as e:
        print(f"Erreur lors de la récupération des LP pour {summoner}: {e}")
    return None


# -------------------------
# INITIALISATION DU BOT DISCORD AVEC PYCORD
# -------------------------
intents = discord.Intents.default()
bot = discord.Bot(intents=intents)


@bot.event
async def on_ready():
    print(f"Bot connecté en tant que {bot.user}")
    load_players()
    print("Joueurs enregistrés :", [f"{name}#{tag}" for (name, tag) in players.keys()])
    check_games.start()  # Démarre la tâche périodique


# -------------------------
# COMMANDE SLASH : Enregistrer un joueur
# -------------------------
@bot.slash_command(name="register", description="Enregistre un joueur dans la base.")
async def register(ctx: discord.ApplicationContext, name: str, tag: str, server: str):
    """
    Enregistre un joueur.
    Exemple d'utilisation : /register name:Faker tag:01 server:EUW
    """
    p_data = await player(name, tag, server)
    if p_data:
        players[(name, tag)] = p_data
        save_player(name, tag, server)
        await ctx.respond(f"Joueur {name}#{tag} sur {server} enregistré avec succès !")
    else:
        await ctx.respond(
            "Erreur lors de l'enregistrement du joueur. Veuillez vérifier le nom et le serveur.",
            ephemeral=True,
        )


# -------------------------
# COMMANDE SLASH : Liste des comptes enregistrées
# -------------------------
@bot.slash_command(
    name="listaccounts",
    description="Affiche la liste des comptes enregistrées avec leur Elo, wins et losses.",
)
async def listaccounts(ctx: discord.ApplicationContext):
    """
    Affiche la liste des comptes enregistrées.
    Pour chaque compte, le pseudo est affiché en sous-titre et l'elo, le nombre de wins et losses sur une seule ligne.
    """
    embed = discord.Embed(title="Comptes enregistrées", color=discord.Color.blurple())
    fields = (
        []
    )  # On stocke pour chaque compte : (score numérique, pseudo, chaîne à afficher)

    for (name, tag), summoner in players.items():
        try:
            league_entries = summoner.league_entries
            solo_entry = None
            for entry in league_entries:
                if entry.queue.value == "RANKED_SOLO_5x5" or (
                    hasattr(entry.queue, "id") and entry.queue.id == 420
                ):
                    solo_entry = entry
                    break

            if solo_entry:
                # Construction de la chaîne d'elo
                elo_str = f"{solo_entry.tier} {solo_entry.division} - {solo_entry.league_points} LP"
                wins = solo_entry.wins
                losses = solo_entry.losses
                # Calcul d'une valeur numérique pour le tri (LP + bonus en fonction du tier et de la division)
                score = (
                    solo_entry.league_points
                    + mapping[solo_entry.division.value]
                    + (mapping[solo_entry.tier.value] * 400)
                )
            else:
                elo_str = "Non classé"
                wins = "N/A"
                losses = "N/A"
                score = -1
        except Exception as e:
            elo_str = "Erreur"
            wins = "Erreur"
            losses = "Erreur"
            score = -1
            print(f"Erreur lors de la récupération des données pour {name}#{tag}: {e}")

        pseudo = f"{name}"
        value_line = f"Elo: {elo_str} | Wins: {wins} | Losses: {losses}"
        fields.append((score, pseudo, value_line))

    # Tri des comptes par ordre décroissant de score
    sorted_fields = sorted(fields, key=lambda x: x[0], reverse=True)
    for _, pseudo, value_line in sorted_fields:
        embed.add_field(name=pseudo, value=value_line, inline=False)

    try:
        await ctx.respond(embed=embed)
    except Exception as e:
        await ctx.send(embed=embed)


# -------------------------
# TÂCHE DE VÉRIFICATION DES PARTIES
# -------------------------
@tasks.loop(seconds=60)
async def check_games():
    """
    Vérifie toutes les 60 secondes l'état des parties pour chaque joueur enregistré.
      - Si un joueur est dans une partie classée Solo/Duo et n'est pas encore enregistré dans active_games,
        on enregistre son état (ID de partie, champion joué et LP initial) et on envoie un embed dans Discord.
      - Si le joueur était enregistré en jeu et qu'il n'est plus en partie, on rafraîchit l'objet summoner,
        on récupère ses LP actuels, on calcule la différence et on envoie un embed indiquant s'il a gagné ou perdu des points.
    """
    for (name, tag), summoner in players.items():
        try:
            current_game = summoner.current_match()
        except Exception as e:
            current_game = None

        key = (name, tag)
        # Si le joueur est en partie et que c'est une partie classée Solo/Duo (queue id 420)
        if current_game is not None and current_game.queue.id == 420:
            if key not in active_games:
                rank = get_solo_lp(summoner)
                if rank is None:
                    continue
                # Calcul du LP initial
                lp_initial = rank[0] + mapping[rank[2]] + (mapping[rank[1]] * 400)
                # Récupération du champion joué par le joueur
                champ = None
                for participant in current_game.participants:
                    if participant.summoner == summoner:
                        champ = participant.champion.name
                        break
                active_games[key] = (current_game.id, champ, lp_initial)
                # Construction de l'embed pour annoncer le début de la partie
                champ_icon_url = f"https://ddragon.leagueoflegends.com/cdn/13.6.1/img/champion/{champ}.png"
                embed = discord.Embed(
                    title="Partie lancée",
                    description=f"Joueur **{name}** a lancé une partie classée Solo/Duo.",
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Champion", value=champ, inline=True)
                embed.add_field(
                    name="Rank",
                    value=f"{rank[1]} {rank[2]} - {rank[0]} LP",
                    inline=True,
                )
                embed.set_thumbnail(url=champ_icon_url)
                channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                if channel:
                    await channel.send(embed=embed)
        else:
            # Le joueur n'est plus en partie (ou la partie n'est pas classée Solo/Duo)
            if key in active_games:
                game_id, champ, lp_initial = active_games[key]
                server_str = (
                    summoner.region.value
                    if hasattr(summoner.region, "value")
                    else str(summoner.region)
                )
                refreshed_summoner = await asyncio.get_event_loop().run_in_executor(
                    None, get_player_data, name, tag, server_str
                )
                if refreshed_summoner is not None:
                    players[key] = refreshed_summoner
                    rank = get_solo_lp(refreshed_summoner)
                    if rank is None:
                        continue
                    lp_final = rank[0] + mapping[rank[2]] + (mapping[rank[1]] * 400)
                    lp_diff = lp_final - lp_initial
                    champ_icon_url = f"https://ddragon.leagueoflegends.com/cdn/13.6.1/img/champion/{champ}.png"
                    color = (
                        discord.Color.green() if lp_diff >= 0 else discord.Color.red()
                    )
                    embed = discord.Embed(
                        title="Partie terminée",
                        description=f"Joueur **{name}** a terminé sa partie sur **{champ}**.",
                        color=color,
                    )
                    if lp_diff > 0:
                        embed.add_field(
                            name="Résultat", value=f"Gagné {lp_diff} LP", inline=False
                        )
                    elif lp_diff < 0:
                        embed.add_field(
                            name="Résultat",
                            value=f"Perdu {abs(lp_diff)} LP",
                            inline=False,
                        )
                    else:
                        embed.add_field(
                            name="Résultat",
                            value="Aucun changement de LP",
                            inline=False,
                        )
                    embed.add_field(
                        name="Rank",
                        value=f"{rank[1]} {rank[2]} - {rank[0]} LP",
                        inline=True,
                    )
                    embed.set_thumbnail(url=champ_icon_url)
                    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                    if channel:
                        await channel.send(embed=embed)
                del active_games[key]


# -------------------------
# LANCEMENT DU BOT
# -------------------------
bot.run(TOKEN)
