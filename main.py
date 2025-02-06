import discord
import os
import cassiopeia as cass
import asyncio
from discord.ext import tasks
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

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
    "RenataGlasc": "Renata",
    "Wukong": "MonkeyKing",
    "LeBlanc": "Leblanc",
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

# -------------------------
# VARIABLES GLOBALES
# -------------------------
# Dictionnaire des joueurs enregistrés.
# Clé : (name, tag) ; Valeur : objet Summoner (Cassiopeia)
players: dict[tuple[str, str], cass.Summoner] = {}

# Dictionnaire global pour suivre les parties actives.
# Clé : (name, tag) ; Valeur : (game_id, champion, lp_initial, game_start_time)
active_games = {}

# Dictionnaire global pour stocker les statistiques des parties terminées durant la période de 24h.
# Clé : (name, tag) ; Valeur : dict contenant :
#    "wins": int, "losses": int, "lp_diff_total": int,
#    "total_kills": int, "total_deaths": int, "total_assists": int,
#    "total_cs_min": float, "games": int
daily_recap = {}


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
        summoner = cass.get_account(name=name, region=server, tagline=tag).summoner
        return summoner
    except Exception as e:
        print(f"Erreur lors de la récupération du joueur {name} sur {server}: {e}")
        return None


async def player(name: str, tag: str, server: str):
    """
    Version asynchrone de la récupération des informations d'un joueur.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_player_data, name, tag, server)


def get_solo_lp(summoner: cass.Summoner):
    """
    Récupère le nombre de LP pour le mode classé Solo/Duo d'un summoner.
    Retourne un tuple (league_points, tier, division) si trouvé, sinon None.
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


# -------------------------
# FONCTION POUR ATTENDRE JUSQU'À 10H
# -------------------------
async def start_daily_recap():
    """
    Attend jusqu'à 10h du jour actuel si 10h n'est pas encore passé,
    sinon attend jusqu'à 10h le lendemain.
    """
    now = datetime.now()
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    print(
        f"Attente de {wait_seconds:.0f} secondes jusqu'à {target.strftime('%d/%m/%Y %H:%M:%S')}"
    )
    await asyncio.sleep(wait_seconds)
    daily_recap_task.start()


@bot.event
async def on_ready():
    print(f"Bot connecté en tant que {bot.user}")
    load_players()
    print("Joueurs enregistrés :", [f"{name}#{tag}" for (name, tag) in players.keys()])
    check_games.start()  # Démarre la tâche de vérification des parties
    await start_daily_recap()  # Lance la tâche de récapitulatif quotidien


# -------------------------
# COMMANDE SLASH : Enregistrer un joueur
# -------------------------
@bot.slash_command(name="register", description="Enregistre un joueur dans la base.")
async def register(ctx: discord.ApplicationContext, name: str, tag: str, server: str):
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
    embed = discord.Embed(title="Comptes enregistrées", color=discord.Color.blurple())
    fields = []
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
                elo_str = f"{solo_entry.tier} {solo_entry.division} - {solo_entry.league_points} LP"
                wins = solo_entry.wins
                losses = solo_entry.losses
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
            print(f"Erreur pour {name}#{tag}: {e}")
        pseudo = f"{name}"
        value_line = f"Elo: {elo_str} | Wins: {wins} | Losses: {losses}"
        fields.append((score, pseudo, value_line))
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
    Vérifie toutes les 60 secondes l'état des parties.
    Si un joueur est en game (queue id 420) et n'est pas encore dans active_games,
    enregistre son ID de partie, champion joué, LP initial et l'heure de lancement.
    Si le joueur était en game et ne l'est plus, récupère ses stats, calcule le LP difference,
    récupère le KDA et le CS/min, puis envoie un embed avec ces infos.
    Les stats de la partie sont ajoutées dans daily_recap pour le récap quotidien.
    """
    for (name, tag), summoner in players.items():
        try:
            current_game = summoner.current_match()
        except Exception:
            current_game = None

        key = (name, tag)
        if current_game is not None and current_game.queue.id == 420:
            if key not in active_games:
                rank = get_solo_lp(summoner)
                if rank is None:
                    continue
                lp_initial = rank[0] + mapping[rank[2]] + (mapping[rank[1]] * 400)
                champ = None
                for participant in current_game.participants:
                    if participant.summoner == summoner:
                        champ = participant.champion.name
                        break
                game_start_time = current_game.creation
                # ajouter 1 h
                game_start_time = game_start_time + timedelta(hours=1)
                active_games[key] = (
                    current_game.id,
                    champ,
                    lp_initial,
                    game_start_time,
                    rank,
                )
                champ_icon_name = (
                    champ.replace(" ", "").replace("'", "").replace(".", "")
                )
                if champ_icon_name in mapping:
                    champ_icon_name = mapping[champ_icon_name]
                champ_icon_url = f"https://ddragon.leagueoflegends.com/cdn/15.3.1/img/champion/{champ_icon_name}.png"
                embed = discord.Embed(
                    title="Partie lancée",
                    description=f"**{name}** a lancé une partie classée Solo/Duo.",
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Champion", value=champ, inline=True)
                embed.add_field(
                    name="Rank",
                    value=f"{rank[1]} {rank[2]} - {rank[0]} LP",
                    inline=True,
                )
                embed.set_footer(
                    text=f"{game_start_time.strftime('%d/%m/%Y %H:%M:%S')}"
                )
                try:
                    response = requests.get(champ_icon_url)
                    response.raise_for_status()
                    embed.set_thumbnail(url=champ_icon_url)
                except requests.exceptions.HTTPError as errh:
                    print(f"Erreur HTTP pour l'icône: {errh}")
                channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                if channel:
                    await channel.send(embed=embed)
        else:
            if key in active_games:
                game_id, champ, lp_initial, game_start_time, initial_rank = (
                    active_games[key]
                )
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
                    champ_icon_name = (
                        champ.replace(" ", "").replace("'", "").replace(".", "")
                    )
                    if champ_icon_name in mapping:
                        champ_icon_name = mapping[champ_icon_name]
                    champ_icon_url = f"https://ddragon.leagueoflegends.com/cdn/15.3.1/img/champion/{champ_icon_name}.png"
                    color = (
                        discord.Color.green() if lp_diff >= 0 else discord.Color.red()
                    )
                    embed = discord.Embed(
                        title="Partie terminée",
                        description=f"**{name}** a terminé sa partie sur **{champ}**.",
                        color=color,
                    )
                    match = cass.get_match(game_id, region=server_str)
                    for participant in match.participants:
                        if participant.summoner == refreshed_summoner:
                            break
                    kill, death, assist = (
                        participant.stats.kills,
                        participant.stats.deaths,
                        participant.stats.assists,
                    )
                    cs = (
                        participant.stats.total_minions_killed
                        + participant.stats.neutral_minions_killed
                    )
                    if isinstance(match.duration, timedelta):
                        duration_seconds = match.duration.total_seconds()
                    else:
                        duration_seconds = match.duration
                    minutes = int(duration_seconds // 60)
                    seconds = int(duration_seconds % 60)
                    cs_per_minute = (
                        cs / (duration_seconds / 60) if duration_seconds > 0 else 0
                    )

                    CSmin = (
                        f"\nCS/Min: {cs_per_minute:.1f}" if cs_per_minute > 4 else ""
                    )
                    if lp_diff > 0:
                        embed.add_field(
                            name="Résultat",
                            value=f"Gagné {lp_diff} LP ({kill}/{death}/{assist}){CSmin}",
                            inline=False,
                        )
                    elif lp_diff <= 0:
                        embed.add_field(
                            name="Résultat",
                            value=f"Perdu {abs(lp_diff)} LP ({kill}/{death}/{assist}){CSmin}",
                            inline=False,
                        )
                    embed.add_field(
                        name="Rank",
                        value=f"{rank[1]} {rank[2]} - {rank[0]} LP",
                        inline=True,
                    )
                    embed.set_thumbnail(url=champ_icon_url)
                    # Ajout du footer avec la durée de la partie, le CS par minute, et la date/heure de lancement
                    embed.set_footer(
                        text=f"Durée: {minutes}m {seconds}s | Lancement: {game_start_time.strftime('%d/%m/%Y %H:%M:%S')}"
                    )
                    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                    if channel:
                        await channel.send(embed=embed)

                    # Mise à jour du récap quotidien
                    recap_key = key  # Utilisation de (name, tag) comme clef
                    # Lors du premier match de la période, on enregistre également le rank de départ
                    if recap_key not in daily_recap:
                        daily_recap[recap_key] = {
                            "wins": 0,
                            "losses": 0,
                            "lp_diff_total": 0,
                            "total_kills": 0,
                            "total_deaths": 0,
                            "total_assists": 0,
                            "total_cs_min": 0.0,
                            "games": 0,
                            "start_rank": f"{initial_rank[1]} {initial_rank[2]} - {initial_rank[0]} LP",  # Rank d'il y a 24h (du premier match enregistré)
                        }
                    entry = daily_recap[recap_key]
                    if lp_diff > 0:
                        entry["wins"] += 1
                    elif lp_diff < 0:
                        entry["losses"] += 1
                    entry["lp_diff_total"] += lp_diff
                    entry["total_kills"] += kill
                    entry["total_deaths"] += death
                    entry["total_assists"] += assist
                    entry["total_cs_min"] += cs_per_minute
                    entry["games"] += 1
                    del active_games[key]


# -------------------------
# TÂCHE DE RÉCAPITULATIF QUOTIDIEN (24h)
# -------------------------
@tasks.loop(hours=24)
async def daily_recap_task():
    """
    Envoie un récapitulatif quotidien pour chaque compte ayant joué depuis le dernier récap.
    Pour chaque compte, affiche :
      pseudo - LPdiff
      rank_d'ilya24h -> rank_actuel (nombre games: wins / losses) | Avg KDA: ... | Avg CS/Min: ...
    Puis réinitialise le dictionnaire daily_recap.
    """
    if not daily_recap:
        print("No recap today")
        return

    embed = discord.Embed(title="Récapitulatif quotidien", color=discord.Color.gold())

    for (name, tag), stats in daily_recap.items():
        games = stats["games"]
        lp_diff_total = stats["lp_diff_total"]

        avg_kda = (stats["total_kills"] + stats["total_assists"]) / (
            stats["total_deaths"] if stats["total_deaths"] > 0 else 1
        )
        avg_cs_min = stats["total_cs_min"] / games

        summoner = players.get((name, tag))
        if summoner:
            current_rank_data = get_solo_lp(summoner)
            if current_rank_data:
                current_rank = f"{current_rank_data[1]} {current_rank_data[2]} - {current_rank_data[0]} LP"
            else:
                current_rank = "N/A"
        else:
            current_rank = "N/A"

        start_rank = stats.get("start_rank", "N/A")
        avg_cs_min_str = f" | Avg CS/Min: {avg_cs_min:.1f}" if avg_cs_min > 4 else ""
        recap_field_name = f"{name} | {lp_diff_total} LP"
        recap_field_value = (
            f"{start_rank} -> {current_rank}\n"
            f"({games} games: {stats['wins']} wins / {stats['losses']} losses) | "
            f"Avg KDA: {avg_kda:.2f}{avg_cs_min_str}"
        )

        embed.add_field(name=recap_field_name, value=recap_field_value, inline=False)

    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)

    daily_recap.clear()


# -------------------------
# LANCEMENT DU BOT
# -------------------------
bot.run(TOKEN)
