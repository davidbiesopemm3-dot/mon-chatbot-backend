"""
CHATBOT SERVEUR - Chez Ella (WhatsApp + site web)
===================================================================
Version 3 :
- Catalogue lu dans Supabase (table "produits"), envoi des photos
- Prise de commande complète : nom, commune + adresse, téléphone, jour de livraison,
  frais de livraison selon la commune (table "zones_livraison"), récapitulatif
- Paiement Orange Money / Wave : le bot demande la capture d'écran et la transfère
  au propriétaire, qui confirme avec "#N ok"
- Le bot passe la main au propriétaire sur événement :
  capture de paiement, demande d'un humain, question sans réponse
- Réponses en texte simple : pas d'émojis, pas d'astérisques, un seul bonjour
"""

import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta

import anthropic
import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Permet au site web de communiquer avec ce serveur


# ============================================
# CONFIGURATION (variables d'environnement Render)
# ============================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Numéro WhatsApp perso du propriétaire, avec indicatif, sans le + (ex : 33612345678)
NUMERO_PROPRIETAIRE = os.environ.get("NUMERO_PROPRIETAIRE", "").replace("+", "").replace(" ", "")

# Numéros de paiement de la boutique
PAIEMENT_ORANGE_MONEY = os.environ.get("PAIEMENT_ORANGE_MONEY", "")
PAIEMENT_WAVE = os.environ.get("PAIEMENT_WAVE", "")

MODELE = "claude-sonnet-4-6"
GRAPH_BASE = "https://graph.facebook.com/v21.0"
GRAPH_URL = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
MAX_PHOTOS_PAR_REPONSE = 4
TAILLE_HISTORIQUE = 16                     # nombre de messages gardés par cliente
DUREE_CONVERSATION = timedelta(hours=24)   # après 24 h de silence, on repart de zéro

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

if not DATABASE_URL:
    print("ATTENTION : DATABASE_URL n'est pas définie, la base de données ne marchera pas.")
if not NUMERO_PROPRIETAIRE:
    print("ATTENTION : NUMERO_PROPRIETAIRE n'est pas défini, les captures et résumés ne seront transmis à personne.")


# ============================================
# INFORMATIONS DE LA BOUTIQUE
# ============================================
INFOS_BOUTIQUE_ELLA = f"""
Nom de la boutique : Chez Ella (Prêt-à-porter féminin)
Horaires : du lundi au samedi, de 9h à 19h
Livraison : partout à Abidjan, sous 48h. Les frais dépendent de la commune (outil frais_livraison).
Paiement accepté : Orange Money, Wave, ou espèces à la livraison
Numéro Orange Money : {PAIEMENT_ORANGE_MONEY or "à demander à une conseillère"}
Numéro Wave : {PAIEMENT_WAVE or "à demander à une conseillère"}
Adresse : Marcory, Abidjan
"""

INSTRUCTIONS_SYSTEME = f"""
Tu es la vendeuse de la boutique Chez Ella et tu discutes avec des clientes sur WhatsApp.
Réponds de façon courte, chaleureuse et professionnelle.

Informations sur la boutique :
{INFOS_BOUTIQUE_ELLA}

Salutations :
- Dis bonjour uniquement dans ton tout premier message de la conversation.
- Si tu as déjà répondu à la cliente plus haut dans la conversation, ne dis plus bonjour : va droit au but.

Articles :
- Les articles, prix, tailles et stocks sont UNIQUEMENT dans la base. Pour toute question sur un article,
  utilise rechercher_produits. N'invente jamais un article, un prix ou une taille.
- Si la cliente veut voir un article, utilise envoyer_photo (3 photos maximum à la fois).
  Les photos partent automatiquement : ne mets jamais d'adresse de photo dans ton texte.

Prise de commande, dans cet ordre :
1. Fais confirmer à la cliente le ou les articles, la taille et la quantité.
2. Demande-lui en UN SEUL message : son nom, sa commune et son adresse de livraison,
   un numéro à appeler pour le livreur, et le jour de livraison souhaité.
3. Demande comment elle veut payer : Orange Money, Wave, ou espèces à la livraison.
4. Quand tu as toutes les informations, appelle enregistrer_commande. Le récapitulatif complet
   est envoyé automatiquement à la cliente : ne le réécris pas.
5. Juste après :
   - Orange Money ou Wave : indique le montant total et le numéro de paiement, puis demande-lui
     d'envoyer ici la capture d'écran du paiement. C'est obligatoire.
   - Espèces : dis-lui qu'elle paiera le livreur à la réception.
Si enregistrer_commande renvoie une erreur (taille ou stock indisponible, commune inconnue),
explique-le simplement à la cliente et propose une solution.
Ne confirme jamais toi-même qu'un paiement est reçu : c'est une conseillère qui vérifie.

Passer la main à une conseillère (outil passer_a_un_humain) :
- si la cliente demande à parler à une personne,
- si tu ne connais pas la réponse à sa question,
- si elle se plaint ou si la situation est délicate.
Dans ce cas, dis-lui simplement qu'une conseillère va lui répondre très vite.

Format (WhatsApp) :
- Texte simple, comme un SMS écrit par une vendeuse. Jamais d'émojis.
- Jamais de Markdown : pas d'astérisques, pas de dièses, pas de tirets de liste.
- 3 à 4 lignes maximum. Prix écrits ainsi : 12 000 FCFA.

Ne mentionne jamais que tu es une intelligence artificielle.
"""


# ============================================
# BASE DE DONNÉES (Supabase / PostgreSQL)
# ============================================
def requete_sql(sql, params=()):
    """Exécute une requête et renvoie les lignes (liste de dictionnaires)."""
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10, sslmode="require")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            lignes = [dict(l) for l in cur.fetchall()] if cur.description else []
        conn.commit()
        return lignes
    finally:
        conn.close()


def formater_prix(prix):
    return f"{int(prix):,}".replace(",", " ") + " FCFA"


def normaliser(texte):
    """'Port-Bouët' -> 'portbouet' : pour comparer les communes sans se soucier des accents."""
    texte = unicodedata.normalize("NFD", (texte or "").lower())
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", texte)


def rechercher_produits(categorie=None, couleur=None, taille=None, prix_max=None, mot_cle=None):
    conditions = ["actif = true", "stock > 0"]
    params = []
    if categorie:
        # "jupes" trouve "jupe"
        conditions.append("(categorie ilike %s or %s ilike '%%' || categorie || '%%')")
        params += [f"%{categorie.strip()}%", categorie.strip()]
    if couleur:
        # "noire" trouve "noir"
        conditions.append("(couleur ilike %s or %s ilike '%%' || couleur || '%%')")
        params += [f"%{couleur.strip()}%", couleur.strip()]
    if taille:
        conditions.append("%s = any(tailles)")
        params.append(taille.strip().upper())
    if prix_max:
        conditions.append("prix <= %s")
        params.append(int(prix_max))
    if mot_cle:
        conditions.append("(nom ilike %s or description ilike %s)")
        params += [f"%{mot_cle.strip()}%"] * 2

    lignes = requete_sql(
        "select id, nom, categorie, couleur, tailles, prix, stock, description, "
        "(photo_url is not null and photo_url <> '') as a_une_photo "
        f"from public.produits where {' and '.join(conditions)} order by prix limit 10",
        params,
    )
    for ligne in lignes:
        ligne["prix"] = formater_prix(ligne["prix"])
    return lignes


def obtenir_produit(produit_id):
    lignes = requete_sql(
        "select id, nom, prix, stock, tailles, photo_url from public.produits where id = %s and actif = true",
        (int(produit_id),),
    )
    return lignes[0] if lignes else None


def trouver_zone(commune):
    """Cherche la commune dans la table zones_livraison (sans tenir compte des accents)."""
    cible = normaliser(commune)
    if not cible:
        return None
    for zone in requete_sql("select commune, frais from public.zones_livraison"):
        nom = normaliser(zone["commune"])
        if nom == cible or (len(cible) >= 4 and (nom in cible or cible in nom)):
            return zone
    return None


def derniere_commande(numero):
    lignes = requete_sql(
        "select * from public.commandes where numero_whatsapp = %s order by id desc limit 1",
        (numero,),
    )
    return lignes[0] if lignes else None


def changer_statut_commande(commande_id, statut):
    requete_sql("update public.commandes set statut = %s where id = %s", (statut, commande_id))


def diminuer_stock(articles):
    for article in articles:
        requete_sql(
            "update public.produits set stock = greatest(stock - %s, 0) where id = %s",
            (article["quantite"], article["produit_id"]),
        )


def remettre_stock(articles):
    for article in articles:
        requete_sql(
            "update public.produits set stock = stock + %s where id = %s",
            (article["quantite"], article["produit_id"]),
        )


def construire_recapitulatif(commande_id, lignes, commune, frais, total, nom, adresse, telephone, jour, mode):
    noms_modes = {"orange_money": "Orange Money", "wave": "Wave", "cash": "Espèces à la livraison"}
    texte = f"Récapitulatif de votre commande n°{commande_id}\n\n"
    for l in lignes:
        taille = f", taille {l['taille']}" if l["taille"] else ""
        texte += f"{l['nom']}{taille} x{l['quantite']} : {formater_prix(l['montant'])}\n"
    texte += f"Livraison {commune} : {formater_prix(frais)}\n"
    texte += f"Total : {formater_prix(total)}\n\n"
    texte += f"Nom : {nom}\n"
    texte += f"Adresse : {commune}, {adresse}\n"
    texte += f"Téléphone : {telephone}\n"
    texte += f"Jour de livraison : {jour}\n"
    texte += f"Paiement : {noms_modes.get(mode, mode)}"
    return texte


# ============================================
# OUTILS DONNÉS À CLAUDE
# ============================================
OUTIL_RECHERCHE = {
    "name": "rechercher_produits",
    "description": "Cherche les articles disponibles en stock. Tous les critères sont facultatifs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "categorie": {"type": "string", "description": "Type de vêtement au singulier : robe, jupe, haut, pantalon, ensemble..."},
            "couleur": {"type": "string", "description": "Couleur au masculin singulier : noir, rouge, bleu..."},
            "taille": {"type": "string", "description": "Taille : XS, S, M, L, XL..."},
            "prix_max": {"type": "integer", "description": "Budget maximum en FCFA"},
            "mot_cle": {"type": "string", "description": "Mot à chercher dans le nom ou la description (ex : wax)"},
        },
    },
}

OUTIL_PHOTO = {
    "name": "envoyer_photo",
    "description": "Envoie à la cliente la photo d'un article, avec son nom et son prix en légende.",
    "input_schema": {
        "type": "object",
        "properties": {"produit_id": {"type": "integer", "description": "Champ id renvoyé par rechercher_produits"}},
        "required": ["produit_id"],
    },
}

OUTIL_FRAIS = {
    "name": "frais_livraison",
    "description": "Donne les frais de livraison pour une commune d'Abidjan.",
    "input_schema": {
        "type": "object",
        "properties": {"commune": {"type": "string", "description": "Commune, ex : Cocody, Yopougon, Marcory"}},
        "required": ["commune"],
    },
}

OUTIL_COMMANDE = {
    "name": "enregistrer_commande",
    "description": (
        "Enregistre la commande quand la cliente a confirmé ses articles et donné toutes ses informations. "
        "Envoie automatiquement le récapitulatif à la cliente et prévient la gérante."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "articles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "produit_id": {"type": "integer"},
                        "taille": {"type": "string"},
                        "quantite": {"type": "integer"},
                    },
                    "required": ["produit_id", "quantite"],
                },
            },
            "nom": {"type": "string"},
            "commune": {"type": "string"},
            "adresse": {"type": "string", "description": "Quartier, rue, repère"},
            "telephone": {"type": "string", "description": "Numéro à appeler pour le livreur"},
            "jour_livraison": {"type": "string", "description": "Jour ou créneau souhaité, ex : samedi 19 octobre matin"},
            "mode_paiement": {"type": "string", "enum": ["orange_money", "wave", "cash"]},
        },
        "required": ["articles", "nom", "commune", "adresse", "telephone", "jour_livraison", "mode_paiement"],
    },
}

OUTIL_HUMAIN = {
    "name": "passer_a_un_humain",
    "description": "Passe la conversation à une conseillère (la gérante), qui répondra elle-même à la cliente.",
    "input_schema": {
        "type": "object",
        "properties": {"raison": {"type": "string", "description": "Pourquoi, en une phrase"}},
        "required": ["raison"],
    },
}

OUTILS_WHATSAPP = [OUTIL_RECHERCHE, OUTIL_PHOTO, OUTIL_FRAIS, OUTIL_COMMANDE, OUTIL_HUMAIN]
OUTILS_SITE = [OUTIL_RECHERCHE, OUTIL_PHOTO, OUTIL_FRAIS]


def outil_enregistrer_commande(ctx, articles, nom, commune, adresse, telephone, jour_livraison, mode_paiement):
    lignes = []
    sous_total = 0
    for article in articles:
        produit = obtenir_produit(article["produit_id"])
        if not produit:
            return {"ok": False, "erreur": f"Article {article['produit_id']} introuvable."}
        quantite = max(1, int(article.get("quantite") or 1))
        taille = (article.get("taille") or "").strip().upper()
        if produit["tailles"] and taille not in produit["tailles"]:
            return {"ok": False, "erreur": f"Taille {taille or '?'} indisponible pour {produit['nom']}. "
                                           f"Tailles disponibles : {', '.join(produit['tailles'])}."}
        if produit["stock"] < quantite:
            return {"ok": False, "erreur": f"Il ne reste que {produit['stock']} pièce(s) de {produit['nom']}."}
        montant = produit["prix"] * quantite
        sous_total += montant
        lignes.append({
            "produit_id": produit["id"], "nom": produit["nom"], "taille": taille,
            "quantite": quantite, "prix_unitaire": produit["prix"], "montant": montant,
        })

    zone = trouver_zone(commune)
    if not zone:
        return {"ok": False, "erreur": f"La commune '{commune}' n'est pas dans nos zones de livraison. "
                                       "Demande à la cliente de préciser sa commune, ou passe la main à une conseillère."}

    frais = zone["frais"]
    total = sous_total + frais
    statut = "a_payer_livraison" if mode_paiement == "cash" else "en_attente_paiement"

    commande_id = requete_sql(
        "insert into public.commandes (numero_whatsapp, nom, telephone, commune, adresse, jour_livraison, "
        "mode_paiement, articles, sous_total, frais_livraison, total, statut) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (ctx["numero"], nom, telephone, zone["commune"], adresse, jour_livraison, mode_paiement,
         json.dumps(lignes, ensure_ascii=False), sous_total, frais, total, statut),
    )[0]["id"]

    if mode_paiement == "cash":
        diminuer_stock(lignes)

    recap = construire_recapitulatif(commande_id, lignes, zone["commune"], frais, total,
                                     nom, adresse, telephone, jour_livraison, mode_paiement)
    ctx["envoyer_texte"](recap)

    suite = ("Paiement en espèces à la livraison."
             if mode_paiement == "cash" else "En attente de la capture de paiement.")
    ticket = obtenir_ticket(ctx["numero"])
    envoyer_au_proprio(
        f"Nouvelle commande n°{commande_id} - cliente #{ticket} (+{ctx['numero']})\n\n{recap}\n\n{suite}",
        ctx["numero"],
    )
    return {"ok": True, "commande_id": commande_id, "total": formater_prix(total),
            "recapitulatif_deja_envoye": True}


def executer_outil(nom, entree, ctx):
    try:
        if nom == "rechercher_produits":
            produits = rechercher_produits(**entree)
            return {"nombre": len(produits), "produits": produits}

        if nom == "envoyer_photo":
            if ctx["photos"] >= MAX_PHOTOS_PAR_REPONSE:
                return {"ok": False, "raison": "Limite de photos atteinte pour ce message."}
            produit = obtenir_produit(entree["produit_id"])
            if not produit or not produit.get("photo_url"):
                return {"ok": False, "raison": "Pas de photo pour cet article."}
            ok = ctx["envoyer_photo"](produit["photo_url"], f"{produit['nom']} - {formater_prix(produit['prix'])}")
            if ok:
                ctx["photos"] += 1
            return {"ok": ok}

        if nom == "frais_livraison":
            zone = trouver_zone(entree.get("commune", ""))
            if not zone:
                return {"trouve": False}
            return {"trouve": True, "commune": zone["commune"], "frais": formater_prix(zone["frais"])}

        if nom == "enregistrer_commande":
            return outil_enregistrer_commande(ctx, **entree)

        if nom == "passer_a_un_humain":
            ctx["passer_la_main"] = entree.get("raison", "Demande de la cliente")
            return {"ok": True, "consigne": "Dis à la cliente qu'une conseillère va lui répondre très vite."}

        return {"ok": False, "raison": f"Outil inconnu : {nom}"}
    except Exception as e:
        print(f"Erreur outil {nom} : {e}")
        return {"ok": False, "raison": "Erreur technique."}


def generer_reponse_claude(historique, ctx, outils):
    """Envoie la conversation à Claude, qui peut utiliser les outils avant de répondre."""
    messages = list(historique)
    for _ in range(8):
        reponse = client.messages.create(
            model=MODELE, max_tokens=700, system=INSTRUCTIONS_SYSTEME, tools=outils, messages=messages,
        )
        if reponse.stop_reason != "tool_use":
            return "".join(b.text for b in reponse.content if b.type == "text")

        messages.append({"role": "assistant", "content": reponse.content})
        resultats = []
        for bloc in reponse.content:
            if bloc.type == "tool_use":
                resultat = executer_outil(bloc.name, bloc.input, ctx)
                resultats.append({
                    "type": "tool_result", "tool_use_id": bloc.id,
                    "content": json.dumps(resultat, ensure_ascii=False, default=str),
                })
        messages.append({"role": "user", "content": resultats})
    return "Je vérifie cela et je reviens vers vous très vite."


def texte_conversation(historique):
    return "\n".join(
        f"{'Cliente' if m['role'] == 'user' else 'Boutique'} : {m['content']}" for m in historique
    )


def resumer_conversation(historique):
    reponse = client.messages.create(
        model=MODELE, max_tokens=200,
        system=("Tu résumes une conversation entre une cliente et une boutique de vêtements, pour la gérante. "
                "En 2 ou 3 phrases : ce que la cliente cherche (article, taille, budget) et sa question en suspens. "
                "Texte simple, sans émojis ni Markdown."),
        messages=[{"role": "user", "content": texte_conversation(historique)}],
    )
    return "".join(b.text for b in reponse.content if b.type == "text").strip()


def reformuler_consigne(consigne, historique):
    reponse = client.messages.create(
        model=MODELE, max_tokens=300,
        system=("Tu es la vendeuse de la boutique Chez Ella. La gérante te donne une consigne pour répondre "
                "à une cliente. Écris le message à envoyer : court, chaleureux, texte simple, sans émojis ni "
                "Markdown, sans dire bonjour. N'ajoute aucune information absente de la consigne. "
                "Ne mentionne ni la gérante ni la consigne. Réponds uniquement avec le message."),
        messages=[{"role": "user",
                   "content": f"Conversation :\n{texte_conversation(historique)}\n\nConsigne de la gérante : {consigne}"}],
    )
    return "".join(b.text for b in reponse.content if b.type == "text").strip()


# ============================================
# NETTOYAGE DU TEXTE (pas d'émojis, pas de Markdown)
# ============================================
MOTIF_EMOJIS = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def nettoyer_texte(texte):
    texte = MOTIF_EMOJIS.sub("", texte)
    texte = re.sub(r"(?m)^\s*(?:[-•*]|\d+[.)])\s+", "", texte)  # puces et numéros de liste
    texte = re.sub(r"[*_#`]", "", texte)                         # **gras**, _italique_, # titres
    texte = re.sub(r"[ \t]{2,}", " ", texte)
    texte = "\n".join(ligne.strip() for ligne in texte.split("\n"))
    texte = re.sub(r"\n{3,}", "\n\n", texte)
    return texte.strip()


# ============================================
# ENVOI WHATSAPP
# ============================================
def appel_whatsapp(numero, contenu):
    """Envoie un message. Renvoie (succès, identifiant du message)."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero, **contenu}
    try:
        reponse = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=15)
        print(f"Envoi WhatsApp à {numero} : {reponse.status_code} - {reponse.text}")
        if reponse.status_code == 200:
            ids = reponse.json().get("messages", [])
            return True, (ids[0].get("id") if ids else None)
    except Exception as e:
        print(f"Erreur envoi WhatsApp : {e}")
    return False, None


def envoyer_message_whatsapp(numero, texte):
    return appel_whatsapp(numero, {"type": "text", "text": {"body": texte}})


def envoyer_image_whatsapp(numero, url_image, legende=""):
    return appel_whatsapp(numero, {"type": "image", "image": {"link": url_image, "caption": legende}})


def recopier_media(media_id, nom_fichier):
    """
    Télécharge une image reçue d'une cliente et la renvoie chez Meta,
    pour obtenir un identifiant qu'on peut transférer au propriétaire.
    """
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    info = requests.get(f"{GRAPH_BASE}/{media_id}", headers=headers, timeout=15).json()
    fichier = requests.get(info["url"], headers=headers, timeout=30)
    type_mime = info.get("mime_type", "image/jpeg")
    envoi = requests.post(
        f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/media",
        headers=headers,
        data={"messaging_product": "whatsapp", "type": type_mime},
        files={"file": (nom_fichier, fichier.content, type_mime)},
        timeout=30,
    )
    return envoi.json()["id"]


# ============================================
# MÉMOIRE DES CONVERSATIONS (en mémoire vive, effacée si Render redémarre)
# Les commandes, elles, sont enregistrées dans Supabase.
# ============================================
verrou = threading.RLock()
conversations = {}          # numéro cliente -> état de la conversation
tickets = {}                # numéro de ticket -> numéro cliente
tickets_par_numero = {}     # numéro cliente -> numéro de ticket
messages_vers_proprio = {}  # id du message envoyé au propriétaire -> numéro cliente
file_attente_proprio = []   # messages non délivrés au propriétaire (sa fenêtre de 24 h était fermée)
messages_traites = set()    # anti-doublon
compteur_tickets = {"n": 0}


def obtenir_conversation(numero):
    maintenant = datetime.now()
    conv = conversations.get(numero)
    if conv is None or maintenant - conv["derniere_activite"] > DUREE_CONVERSATION:
        conv = {"historique": [], "mode": "bot", "derniere_activite": maintenant}
        conversations[numero] = conv
    return conv


def obtenir_ticket(numero):
    """Chaque cliente reçoit un numéro court (#1, #2...) pour que le propriétaire puisse lui répondre."""
    with verrou:
        if numero not in tickets_par_numero:
            compteur_tickets["n"] += 1
            tickets_par_numero[numero] = compteur_tickets["n"]
            tickets[compteur_tickets["n"]] = numero
        return tickets_par_numero[numero]


def ajouter_historique(conv, role, texte):
    historique = conv["historique"]
    if historique and historique[-1]["role"] == role:
        historique[-1]["content"] += "\n" + texte
    else:
        historique.append({"role": role, "content": texte})
    del historique[:-TAILLE_HISTORIQUE]
    while historique and historique[0]["role"] != "user":
        historique.pop(0)


# ============================================
# MESSAGES AU PROPRIÉTAIRE
# ============================================
def envoyer_au_proprio(contenu, numero_client=None):
    """Texte ou image pour le propriétaire. S'il ne passe pas, on le garde en file d'attente."""
    if not NUMERO_PROPRIETAIRE:
        print(f"(Pas de NUMERO_PROPRIETAIRE) Message non transmis : {contenu}")
        return False
    if isinstance(contenu, str):
        contenu = {"type": "text", "text": {"body": contenu}}
    ok, id_message = appel_whatsapp(NUMERO_PROPRIETAIRE, contenu)
    with verrou:
        if ok and id_message and numero_client:
            messages_vers_proprio[id_message] = numero_client
        if not ok:
            file_attente_proprio.append((contenu, numero_client))
            print("Message au propriétaire mis en file d'attente : il doit écrire au bot pour la débloquer.")
    return ok


def vider_file_attente():
    with verrou:
        en_attente = list(file_attente_proprio)
        file_attente_proprio.clear()
    for contenu, numero_client in en_attente:
        envoyer_au_proprio(contenu, numero_client)
    return len(en_attente)


def passer_la_main(numero_client, raison, prevenir_client=True):
    ticket = obtenir_ticket(numero_client)
    with verrou:
        conv = obtenir_conversation(numero_client)
        conv["mode"] = "humain"
        historique = list(conv["historique"])
    try:
        resume = resumer_conversation(historique)
    except Exception as e:
        print(f"Erreur résumé : {e}")
        resume = texte_conversation(historique)[-800:]
    if prevenir_client:
        envoyer_message_whatsapp(numero_client, "Je transmets votre demande à une conseillère, elle vous répond très vite.")
    envoyer_au_proprio(
        f"Cliente #{ticket} (+{numero_client})\nRaison : {raison}\n\n{resume}\n\n"
        f"Pour lui répondre : réponds à ce message, ou commence par #{ticket}.\n"
        f"Pour rendre la main au bot : #{ticket} bot",
        numero_client,
    )


AIDE_PROPRIETAIRE = (
    "Commandes :\n"
    "#3 suivi de ton message : le bot le transmet à la cliente n°3\n"
    "#3 ok : paiement vérifié, la commande est confirmée\n"
    "#3 annule : commande annulée\n"
    "#3 bot : le bot reprend la conversation\n"
    "Tu peux aussi répondre directement (glisser) à un message de cliente."
)


def traiter_message_proprietaire(texte, id_message_cite):
    nb_debloques = vider_file_attente()

    numero_client = None
    with verrou:
        if id_message_cite:
            numero_client = messages_vers_proprio.get(id_message_cite)
    correspondance = re.match(r"^\s*#(\d+)\s*(.*)$", texte, re.S)
    if correspondance:
        with verrou:
            numero_client = tickets.get(int(correspondance.group(1)))
        texte = correspondance.group(2).strip()

    if not numero_client:
        # "ok", "merci"... servent juste à garder la fenêtre de 24 h ouverte : pas de réponse
        if nb_debloques == 0 and normaliser(texte) not in ("ok", "okay", "merci", "oui"):
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, AIDE_PROPRIETAIRE)
        return

    ticket = obtenir_ticket(numero_client)
    with verrou:
        conv = obtenir_conversation(numero_client)
    commande_cle = normaliser(texte)

    # ---- Paiement vérifié ----
    if commande_cle in ("ok", "valide", "confirme", "paye"):
        commande = derniere_commande(numero_client)
        if commande and commande["statut"] in ("paiement_a_verifier", "en_attente_paiement"):
            changer_statut_commande(commande["id"], "payee")
            diminuer_stock(commande["articles"])
            message = (f"Paiement bien reçu, merci. Votre commande n°{commande['id']} est confirmée. "
                       f"Livraison prévue {commande['jour_livraison']} à {commande['commune']}.")
            envoyer_message_whatsapp(numero_client, message)
            with verrou:
                ajouter_historique(conv, "assistant", message)
                conv["mode"] = "bot"
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE,
                                     f"Commande n°{commande['id']} confirmée, la cliente #{ticket} est prévenue. "
                                     "Le bot reprend la conversation.")
            return
        commande_cle = "bot"  # pas de paiement en attente : on rend simplement la main au bot

    # ---- Commande annulée ----
    if commande_cle in ("annule", "annuler"):
        commande = derniere_commande(numero_client)
        if commande and commande["statut"] not in ("payee", "annulee"):
            changer_statut_commande(commande["id"], "annulee")
            if commande["statut"] == "a_payer_livraison":
                remettre_stock(commande["articles"])  # le stock avait été retiré à la commande
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE,
                                     f"Commande n°{commande['id']} annulée. Écris #{ticket} suivi d'un message "
                                     "si tu veux expliquer pourquoi à la cliente.")
        else:
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"Aucune commande à annuler pour #{ticket}.")
        return

    # ---- Rendre la main au bot ----
    if commande_cle in ("bot", "fin"):
        with verrou:
            conv["mode"] = "bot"
        envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"C'est noté, le bot reprend la conversation #{ticket}.")
        return

    if not texte:
        return

    # ---- Message à transmettre à la cliente ----
    with verrou:
        conv["mode"] = "humain"
        historique = list(conv["historique"])
    try:
        message = nettoyer_texte(reformuler_consigne(texte, historique))
    except Exception as e:
        print(f"Erreur reformulation : {e}")
        message = nettoyer_texte(texte)

    ok, _ = envoyer_message_whatsapp(numero_client, message)
    if ok:
        with verrou:
            ajouter_historique(conv, "assistant", message)
        envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"Envoyé à #{ticket} : {message}")
    else:
        envoyer_message_whatsapp(NUMERO_PROPRIETAIRE,
                                 f"Impossible d'écrire à #{ticket} : elle n'a pas écrit depuis plus de 24 h.")


# ============================================
# MESSAGES DES CLIENTES
# ============================================
def traiter_message_client(numero, texte):
    with verrou:
        conv = obtenir_conversation(numero)
        conv["derniere_activite"] = datetime.now()
        ajouter_historique(conv, "user", texte)
        mode = conv["mode"]
        historique = list(conv["historique"])

    # Une conseillère a la main : on lui transmet le message, le bot se tait
    if mode == "humain":
        ticket = obtenir_ticket(numero)
        envoyer_au_proprio(f"#{ticket} (+{numero}) : {texte}", numero)
        return

    def envoyer_photo(url, legende):
        ok, _ = envoyer_image_whatsapp(numero, url, legende)
        return ok

    def envoyer_texte(message):
        envoyer_message_whatsapp(numero, message)
        with verrou:
            ajouter_historique(conv, "assistant", message)

    ctx = {"numero": numero, "photos": 0, "passer_la_main": None,
           "envoyer_photo": envoyer_photo, "envoyer_texte": envoyer_texte}

    try:
        reponse = nettoyer_texte(generer_reponse_claude(historique, ctx, OUTILS_WHATSAPP))
    except Exception as e:
        print(f"Erreur Claude : {e}")
        reponse = "Désolée, petit souci technique. Une conseillère va vous recontacter très vite."
        ctx["passer_la_main"] = "Erreur technique du bot"

    if reponse:
        envoyer_message_whatsapp(numero, reponse)
        with verrou:
            ajouter_historique(conv, "assistant", reponse)

    if ctx["passer_la_main"]:
        passer_la_main(numero, ctx["passer_la_main"], prevenir_client=False)


def traiter_media_client(numero, type_media, media):
    """La cliente envoie une image ou un document : capture de paiement, ou autre chose."""
    ticket = obtenir_ticket(numero)
    with verrou:
        conv = obtenir_conversation(numero)
        conv["derniere_activite"] = datetime.now()
        ajouter_historique(conv, "user", "(La cliente a envoyé une image)")
        conv["mode"] = "humain"

    commande = None
    try:
        commande = derniere_commande(numero)
    except Exception as e:
        print(f"Erreur lecture commande : {e}")

    if commande and commande["statut"] == "en_attente_paiement":
        changer_statut_commande(commande["id"], "paiement_a_verifier")
        message_cliente = "Merci, nous avons bien reçu votre capture. Nous vérifions le paiement et revenons vers vous très vite."
        legende = (f"Paiement commande n°{commande['id']} - cliente #{ticket} (+{numero})\n"
                   f"Total attendu : {formater_prix(commande['total'])}\n"
                   f"Si le paiement est bon : #{ticket} ok\n"
                   f"Sinon : #{ticket} suivi de ton message")
    else:
        message_cliente = "Merci, je transmets votre image à une conseillère, elle vous répond très vite."
        legende = (f"Image reçue de la cliente #{ticket} (+{numero})\n"
                   f"Pour lui répondre : #{ticket} suivi de ton message\n"
                   f"Pour rendre la main au bot : #{ticket} bot")

    envoyer_message_whatsapp(numero, message_cliente)
    with verrou:
        ajouter_historique(conv, "assistant", message_cliente)

    nom_fichier = media.get("filename") or ("capture.jpg" if type_media == "image" else "document")
    try:
        nouvel_id = recopier_media(media["id"], nom_fichier)
        contenu = {"type": type_media, type_media: {"id": nouvel_id, "caption": legende}}
        if type_media == "document":
            contenu["document"]["filename"] = nom_fichier
        envoyer_au_proprio(contenu, numero)
    except Exception as e:
        print(f"Erreur transfert image : {e}")
        envoyer_au_proprio(legende + "\n(L'image n'a pas pu être transférée, demande-la à la cliente.)", numero)


def traiter_message_entrant(message):
    """Aiguille un message WhatsApp entrant (appelé dans un fil séparé)."""
    try:
        numero = message.get("from")
        type_message = message.get("type")
        texte = message.get("text", {}).get("body", "").strip() if type_message == "text" else ""
        id_cite = message.get("context", {}).get("id")

        if NUMERO_PROPRIETAIRE and numero == NUMERO_PROPRIETAIRE:
            if texte:
                traiter_message_proprietaire(texte, id_cite)
            else:
                vider_file_attente()
            return

        if texte:
            traiter_message_client(numero, texte)
        elif type_message in ("image", "document"):
            traiter_media_client(numero, type_message, message.get(type_message, {}))
        elif type_message in ("audio", "video", "sticker"):
            envoyer_message_whatsapp(
                numero, "Je ne peux lire que les messages écrits pour le moment. Pouvez-vous m'écrire votre demande ?")
    except Exception as e:
        print(f"Erreur traitement message : {e}")


# ============================================
# ROUTES
# ============================================
@app.route("/")
def home():
    return "Le serveur du chatbot Chez Ella est en ligne !"


@app.route("/message", methods=["POST"])
def chat():
    """Route du site web : renvoie le texte et les photos (pas de prise de commande ici)."""
    try:
        data = request.get_json() or {}
        user_message = data.get("message", "")
        if not user_message:
            return jsonify({"reponse": "Veuillez entrer un message."}), 400

        photos = []

        def envoyer_photo(url, legende):
            photos.append({"url": url, "legende": legende})
            return True

        ctx = {"numero": "site", "photos": 0, "passer_la_main": None,
               "envoyer_photo": envoyer_photo, "envoyer_texte": lambda m: None}
        reponse_texte = nettoyer_texte(
            generer_reponse_claude([{"role": "user", "content": user_message}], ctx, OUTILS_SITE))
        return jsonify({"reponse": reponse_texte, "photos": photos})
    except Exception as e:
        print(f"Erreur : {e}")
        return jsonify({"reponse": "Désolé, une erreur technique est survenue."}), 500


@app.route("/webhook", methods=["GET"])
def verifier_webhook():
    """Meta appelle cette route une seule fois, pour vérifier que le webhook t'appartient."""
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        print("Webhook vérifié avec succès !")
        return request.args.get("hub.challenge"), 200
    print("Échec de la vérification du webhook.")
    return "Erreur de vérification", 403


@app.route("/webhook", methods=["POST"])
def recevoir_message_whatsapp():
    """
    On répond tout de suite "ok" à Meta et on traite le message dans un fil séparé :
    sinon Meta croit à un échec et renvoie le même message plusieurs fois.
    """
    try:
        data = request.get_json() or {}
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []) or []:
                    id_message = message.get("id")
                    with verrou:
                        if id_message in messages_traites:
                            continue
                        messages_traites.add(id_message)
                        if len(messages_traites) > 5000:
                            messages_traites.clear()
                    print(f"Message reçu de {message.get('from')} ({message.get('type')})")
                    threading.Thread(target=traiter_message_entrant, args=(message,), daemon=True).start()
    except Exception as e:
        print(f"Erreur webhook : {e}")
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
