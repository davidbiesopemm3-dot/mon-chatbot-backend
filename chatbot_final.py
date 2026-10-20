"""
CHATBOT SERVEUR - Chez Ella (WhatsApp + site web)
===================================================================
Version 5 :
- Catalogue lu dans Supabase (table "produits"), envoi des photos
- Prise de commande complète, frais de livraison par commune, récapitulatif
- Paiement Orange Money / Wave : capture transférée au propriétaire, qui confirme avec "#N ok"
- Passage de relais au propriétaire sur événement (capture, demande d'un humain, question sans réponse)
- Toute la mémoire (conversations, tickets, messages en attente) est rangée dans Supabase :
  rien ne se perd quand Render se met en veille ou redémarre
- Le propriétaire peut demander "commandes" (liste) ou "commande 12" (détail + capture)
- Les captures de paiement sont enregistrées dans Supabase (table "captures_paiement")
- Les heures (heure d'Abidjan) apparaissent dans les récapitulatifs et la liste
"""

import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

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

# Numéros de paiement de la boutique (affichés aux clientes)
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
4. Quand tu as toutes les informations, appelle OBLIGATOIREMENT enregistrer_commande.
   Sans cet appel, la commande n'existe pas et la boutique n'est pas prévenue.
   Le récapitulatif complet est envoyé automatiquement à la cliente : ne le réécris pas.
5. Juste après :
   - Orange Money ou Wave : indique le montant total et le numéro de paiement, puis demande-lui
     d'envoyer ici la capture d'écran du paiement. C'est obligatoire.
   - Espèces : dis-lui qu'elle paiera le livreur à la réception.
   - Si enregistrer_commande indique capture_deja_recue, ne redemande pas la capture :
     dis simplement qu'une conseillère vérifie le paiement.
Si enregistrer_commande renvoie une erreur (taille ou stock indisponible, commune inconnue),
explique-le simplement à la cliente et propose une solution.
Ne confirme jamais toi-même qu'un paiement est reçu : c'est une conseillère qui vérifie.
Si la cliente envoie une image alors qu'aucune commande n'est enregistrée, remercie-la et
continue normalement la prise de commande (l'image sera rattachée automatiquement à sa commande).

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


def heure_abidjan(moment):
    """Abidjan est à l'heure GMT (UTC+0) toute l'année."""
    return moment.astimezone(timezone.utc).strftime("%d/%m/%Y à %Hh%M")


def normaliser(texte):
    """'Port-Bouët' -> 'portbouet' : pour comparer sans se soucier des accents, espaces et majuscules."""
    texte = unicodedata.normalize("NFD", (texte or "").lower())
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", texte)


# ---------- Produits ----------
def rechercher_produits(categorie=None, couleur=None, taille=None, prix_max=None, mot_cle=None):
    conditions = ["actif = true", "stock > 0"]
    params = []
    if categorie:
        conditions.append("(categorie ilike %s or %s ilike '%%' || categorie || '%%')")  # "jupes" trouve "jupe"
        params += [f"%{categorie.strip()}%", categorie.strip()]
    if couleur:
        conditions.append("(couleur ilike %s or %s ilike '%%' || couleur || '%%')")      # "noire" trouve "noir"
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


def diminuer_stock(articles):
    for a in articles:
        requete_sql("update public.produits set stock = greatest(stock - %s, 0) where id = %s",
                    (a["quantite"], a["produit_id"]))


def remettre_stock(articles):
    for a in articles:
        requete_sql("update public.produits set stock = stock + %s where id = %s",
                    (a["quantite"], a["produit_id"]))


# ---------- Livraison ----------
def trouver_zone(commune):
    """Cherche la commune dans zones_livraison (sans tenir compte des accents)."""
    cible = normaliser(commune)
    if not cible:
        return None
    for zone in requete_sql("select commune, frais from public.zones_livraison"):
        nom = normaliser(zone["commune"])
        if nom == cible or (len(cible) >= 4 and (nom in cible or cible in nom)):
            return zone
    return None


# ---------- Commandes ----------
def derniere_commande(numero):
    lignes = requete_sql(
        "select * from public.commandes where numero_whatsapp = %s order by id desc limit 1", (numero,))
    return lignes[0] if lignes else None


def commande_par_id(commande_id):
    lignes = requete_sql("select * from public.commandes where id = %s", (int(commande_id),))
    return lignes[0] if lignes else None


def changer_statut_commande(commande_id, statut):
    requete_sql("update public.commandes set statut = %s where id = %s", (statut, commande_id))


NOMS_PAIEMENT = {"orange_money": "Orange Money", "wave": "Wave", "cash": "Espèces à la livraison"}


def construire_recapitulatif(commande):
    texte = f"Récapitulatif de la commande n°{commande['id']}\n"
    if commande.get("created_at"):
        texte += f"Passée le {heure_abidjan(commande['created_at'])}\n"
    texte += "\n"
    for l in commande["articles"]:
        taille = f", taille {l['taille']}" if l.get("taille") else ""
        texte += f"{l['nom']}{taille} x{l['quantite']} : {formater_prix(l['montant'])}\n"
    texte += f"Livraison {commande['commune']} : {formater_prix(commande['frais_livraison'])}\n"
    texte += f"Total : {formater_prix(commande['total'])}\n\n"
    texte += f"Nom : {commande['nom']}\n"
    texte += f"Adresse : {commande['commune']}, {commande['adresse']}\n"
    texte += f"Téléphone : {commande['telephone']}\n"
    texte += f"Jour de livraison : {commande['jour_livraison']}\n"
    texte += f"Paiement : {NOMS_PAIEMENT.get(commande['mode_paiement'], commande['mode_paiement'])}"
    return texte


# ---------- Captures de paiement ----------
def enregistrer_capture(commande_id, numero, type_mime, nom_fichier, contenu):
    requete_sql(
        "insert into public.captures_paiement (commande_id, numero_whatsapp, type_mime, nom_fichier, contenu) "
        "values (%s, %s, %s, %s, %s)",
        (commande_id, numero, type_mime, nom_fichier, psycopg2.Binary(contenu)),
    )


def captures_de_commande(commande_id, maximum=3):
    """Les dernières captures de la commande (3 au plus), de la plus ancienne à la plus récente."""
    lignes = requete_sql(
        "select type_mime, nom_fichier, contenu, created_at from public.captures_paiement "
        "where commande_id = %s order by id desc limit %s", (commande_id, maximum))
    return list(reversed(lignes))


# ---------- Clientes (mémoire des conversations) ----------
def creer_cliente_si_besoin(numero):
    # "where not exists" évite de consommer un numéro de ticket quand la cliente existe déjà
    requete_sql("insert into public.clientes (numero_whatsapp) select %s "
                "where not exists (select 1 from public.clientes where numero_whatsapp = %s) "
                "on conflict (numero_whatsapp) do nothing", (numero, numero))


def charger_conversation(numero):
    """Lit la conversation de la cliente dans Supabase (la crée si besoin)."""
    creer_cliente_si_besoin(numero)
    ligne = requete_sql(
        "select ticket, mode, historique, derniere_activite from public.clientes where numero_whatsapp = %s",
        (numero,),
    )[0]
    conv = {"numero": numero, "ticket": ligne["ticket"], "mode": ligne["mode"],
            "historique": ligne["historique"] or []}
    # Après 24 h de silence, on repart d'une conversation neuve
    if datetime.now(timezone.utc) - ligne["derniere_activite"] > DUREE_CONVERSATION:
        conv["historique"] = []
        conv["mode"] = "bot"
    return conv


def sauver_conversation(conv, activite=False):
    sql = "update public.clientes set historique = %s, mode = %s"
    if activite:
        sql += ", derniere_activite = now()"
    requete_sql(sql + " where numero_whatsapp = %s",
                (json.dumps(conv["historique"], ensure_ascii=False), conv["mode"], conv["numero"]))


def obtenir_ticket(numero):
    """Chaque cliente a un numéro court et permanent (#1, #2...) pour que le propriétaire lui réponde."""
    creer_cliente_si_besoin(numero)
    return requete_sql("select ticket from public.clientes where numero_whatsapp = %s", (numero,))[0]["ticket"]


def numero_par_ticket(ticket):
    lignes = requete_sql("select numero_whatsapp from public.clientes where ticket = %s", (int(ticket),))
    return lignes[0]["numero_whatsapp"] if lignes else None


def ajouter_historique(conv, role, texte):
    historique = conv["historique"]
    if historique and historique[-1]["role"] == role:
        historique[-1]["content"] += "\n" + texte
    else:
        historique.append({"role": role, "content": texte})
    del historique[:-TAILLE_HISTORIQUE]
    while historique and historique[0]["role"] != "user":
        historique.pop(0)


# Un verrou par cliente : deux messages d'une même cliente sont traités l'un après l'autre
_verrous = {}
_verrou_global = threading.Lock()


def verrou_cliente(numero):
    with _verrou_global:
        if numero not in _verrous:
            _verrous[numero] = threading.Lock()
        return _verrous[numero]


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
        "Envoie automatiquement le récapitulatif à la cliente et prévient la gérante. "
        "Obligatoire : sans cet appel, la commande n'existe pas."
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
        lignes.append({"produit_id": produit["id"], "nom": produit["nom"], "taille": taille,
                       "quantite": quantite, "prix_unitaire": produit["prix"], "montant": montant})

    zone = trouver_zone(commune)
    if not zone:
        return {"ok": False, "erreur": f"La commune '{commune}' n'est pas dans nos zones de livraison. "
                                       "Demande à la cliente de préciser sa commune, ou passe la main à une conseillère."}

    frais = zone["frais"]
    total = sous_total + frais
    statut = "a_payer_livraison" if mode_paiement == "cash" else "en_attente_paiement"

    commande = requete_sql(
        "insert into public.commandes (numero_whatsapp, nom, telephone, commune, adresse, jour_livraison, "
        "mode_paiement, articles, sous_total, frais_livraison, total, statut) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *",
        (ctx["numero"], nom, telephone, zone["commune"], adresse, jour_livraison, mode_paiement,
         json.dumps(lignes, ensure_ascii=False), sous_total, frais, total, statut),
    )[0]

    if mode_paiement == "cash":
        diminuer_stock(lignes)

    recap = construire_recapitulatif(commande)
    ctx["envoyer_texte"](recap)

    ticket = obtenir_ticket(ctx["numero"])
    suite = ("Paiement en espèces à la livraison." if mode_paiement == "cash"
             else f"En attente de la capture de paiement. Quand elle arrive et que c'est bon : #{ticket} ok")
    # La cliente avait peut-être envoyé sa capture AVANT de commander : on la rattache
    orphelines = []
    if mode_paiement != "cash":
        orphelines = requete_sql(
            "update public.captures_paiement set commande_id = %s "
            "where numero_whatsapp = %s and commande_id is null and created_at > now() - interval '24 hours' "
            "returning type_mime, nom_fichier, contenu, created_at",
            (commande["id"], ctx["numero"]),
        )
    if orphelines:
        changer_statut_commande(commande["id"], "paiement_a_verifier")
        commande["statut"] = "paiement_a_verifier"
        for cap in orphelines:
            envoyer_capture_au_proprio(commande, ticket, ctx["numero"], bytes(cap["contenu"]),
                                       cap["type_mime"], cap["nom_fichier"], cap["created_at"])
    else:
        envoyer_au_proprio(f"Nouvelle commande n°{commande['id']} - cliente #{ticket} (+{ctx['numero']})\n\n"
                           f"{recap}\n\n{suite}", ctx["numero"])
    return {"ok": True, "commande_id": commande["id"], "total": formater_prix(total),
            "recapitulatif_deja_envoye": True, "capture_deja_recue": bool(orphelines)}


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


def etat_commande_cliente(numero):
    """Une phrase sur la commande en cours de la cliente, ajoutée aux consignes de Claude."""
    try:
        commande = derniere_commande(numero)
    except Exception:
        return ""
    if not commande or datetime.now(timezone.utc) - commande["created_at"] > timedelta(days=7):
        return ""
    n, total = commande["id"], formater_prix(commande["total"])
    etats = {
        "en_attente_paiement": f"La commande n°{n} ({total}) est enregistrée et attend la capture de paiement.",
        "paiement_a_verifier": (f"La commande n°{n} ({total}) : la capture de paiement est reçue, une conseillère "
                                "la vérifie. Si la cliente demande, dis que la vérification est en cours, "
                                "sans confirmer le paiement toi-même."),
        "a_payer_livraison": f"La commande n°{n} ({total}) est confirmée, paiement en espèces à la livraison.",
        "payee": f"La commande n°{n} ({total}) est payée et confirmée, livraison {commande['jour_livraison']}.",
        "annulee": f"La commande n°{n} a été annulée.",
    }
    texte = etats.get(commande["statut"], "")
    return f"\n\nÉtat actuel de la cliente : {texte}" if texte else ""


def generer_reponse_claude(historique, ctx, outils):
    """Envoie la conversation à Claude, qui peut utiliser les outils avant de répondre."""
    messages = list(historique)
    consignes = INSTRUCTIONS_SYSTEME + (etat_commande_cliente(ctx["numero"]) if ctx["numero"] != "site" else "")
    for _ in range(8):
        reponse = client.messages.create(
            model=MODELE, max_tokens=700, system=consignes, tools=outils, messages=messages,
        )
        if reponse.stop_reason != "tool_use":
            return "".join(b.text for b in reponse.content if b.type == "text")

        messages.append({"role": "assistant", "content": reponse.content})
        resultats = []
        for bloc in reponse.content:
            if bloc.type == "tool_use":
                resultat = executer_outil(bloc.name, bloc.input, ctx)
                resultats.append({"type": "tool_result", "tool_use_id": bloc.id,
                                  "content": json.dumps(resultat, ensure_ascii=False, default=str)})
        messages.append({"role": "user", "content": resultats})
    return "Je vérifie cela et je reviens vers vous très vite."


def texte_conversation(historique):
    return "\n".join(f"{'Cliente' if m['role'] == 'user' else 'Boutique'} : {m['content']}" for m in historique)


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
    return appel_whatsapp(numero, {"type": "text", "text": {"body": texte[:4000]}})


def envoyer_image_whatsapp(numero, url_image, legende=""):
    return appel_whatsapp(numero, {"type": "image", "image": {"link": url_image, "caption": legende}})


def telecharger_media(media_id):
    """Télécharge une image envoyée par une cliente. Renvoie (contenu, type)."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    info = requests.get(f"{GRAPH_BASE}/{media_id}", headers=headers, timeout=15).json()
    fichier = requests.get(info["url"], headers=headers, timeout=30)
    fichier.raise_for_status()
    return fichier.content, info.get("mime_type", "image/jpeg")


def televerser_media(contenu, type_mime, nom_fichier):
    """Envoie un fichier chez Meta pour pouvoir le transférer sur WhatsApp. Renvoie son identifiant."""
    envoi = requests.post(
        f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/media",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        data={"messaging_product": "whatsapp", "type": type_mime},
        files={"file": (nom_fichier, contenu, type_mime)},
        timeout=30,
    )
    return envoi.json()["id"]


def contenu_media(type_mime, id_media, legende, nom_fichier):
    """Prépare un message image (ou document pour un PDF) à partir d'un identifiant Meta."""
    type_media = "image" if type_mime.startswith("image/") else "document"
    contenu = {"type": type_media, type_media: {"id": id_media, "caption": legende}}
    if type_media == "document":
        contenu["document"]["filename"] = nom_fichier
    return contenu


# ============================================
# MESSAGES AU PROPRIÉTAIRE
# ============================================
def envoyer_au_proprio(contenu, numero_client=None, depuis_file=False):
    """
    Envoie un texte ou une image au propriétaire.
    S'il ne passe pas (fenêtre de 24 h fermée), il est rangé dans Supabase jusqu'à ce que le propriétaire écrive.
    """
    if not NUMERO_PROPRIETAIRE:
        print(f"(Pas de NUMERO_PROPRIETAIRE) Message non transmis : {contenu}")
        return False
    if isinstance(contenu, str):
        contenu = {"type": "text", "text": {"body": contenu[:4000]}}
    ok, id_message = appel_whatsapp(NUMERO_PROPRIETAIRE, contenu)
    try:
        if ok and id_message and numero_client:
            requete_sql("insert into public.messages_proprio (id_message, numero_whatsapp) values (%s, %s) "
                        "on conflict do nothing", (id_message, numero_client))
        if not ok and not depuis_file:
            requete_sql("insert into public.file_attente_proprio (contenu, numero_whatsapp) values (%s, %s)",
                        (json.dumps(contenu, ensure_ascii=False), numero_client))
            print("Message au propriétaire mis en file d'attente : il doit écrire au bot pour la débloquer.")
    except Exception as e:
        print(f"Erreur enregistrement message propriétaire : {e}")
    return ok


def vider_file_attente():
    """Le propriétaire vient d'écrire : sa fenêtre de 24 h est ouverte, on envoie tout ce qui attendait."""
    en_attente = requete_sql("select id, contenu, numero_whatsapp from public.file_attente_proprio order by id")
    envoyes = 0
    for ligne in en_attente:
        if envoyer_au_proprio(ligne["contenu"], ligne["numero_whatsapp"], depuis_file=True):
            requete_sql("delete from public.file_attente_proprio where id = %s", (ligne["id"],))
            envoyes += 1
    return envoyes


def passer_la_main(conv, raison, prevenir_client=True):
    """Le propriétaire reprend la conversation : on lui envoie un résumé."""
    conv["mode"] = "humain"
    sauver_conversation(conv)
    try:
        resume = resumer_conversation(conv["historique"])
    except Exception as e:
        print(f"Erreur résumé : {e}")
        resume = texte_conversation(conv["historique"])[-800:]
    if prevenir_client:
        envoyer_message_whatsapp(conv["numero"], "Je transmets votre demande à une conseillère, elle vous répond très vite.")
    t = conv["ticket"]
    envoyer_au_proprio(
        f"Cliente #{t} (+{conv['numero']})\nRaison : {raison}\n\n{resume}\n\n"
        f"Pour lui répondre : réponds à ce message, ou commence par #{t}.\n"
        f"Pour rendre la main au bot : #{t} bot",
        conv["numero"],
    )


LIBELLES_STATUT = {
    "en_attente_paiement": "en attente de la capture de paiement",
    "paiement_a_verifier": "CAPTURE REÇUE, à vérifier",
    "a_payer_livraison": "espèces à la livraison",
    "payee": "payée",
    "annulee": "annulée",
}


def liste_commandes_en_cours():
    commandes = requete_sql(
        "select c.*, cl.ticket, "
        "(select count(*) from public.captures_paiement cp where cp.commande_id = c.id) as nb_captures "
        "from public.commandes c "
        "left join public.clientes cl on cl.numero_whatsapp = c.numero_whatsapp "
        "where c.statut in ('paiement_a_verifier', 'en_attente_paiement', 'a_payer_livraison') "
        "order by (c.statut = 'paiement_a_verifier') desc, c.id desc limit 15"
    )
    payees = requete_sql("select count(*) as n from public.commandes where statut = 'payee' "
                         "and created_at > now() - interval '7 days'")[0]["n"]
    if not commandes:
        return f"Aucune commande en cours.\nCommandes payées ces 7 derniers jours : {payees}"

    texte = f"Commandes en cours ({len(commandes)}) :\n"
    for c in commandes:
        ticket = c["ticket"] or obtenir_ticket(c["numero_whatsapp"])
        texte += (f"\nCommande n°{c['id']} - cliente #{ticket} ({c['nom']})\n"
                  f"Passée le {heure_abidjan(c['created_at'])}\n"
                  f"{formater_prix(c['total'])} - {c['commune']}, livraison {c['jour_livraison']}\n"
                  f"Statut : {LIBELLES_STATUT.get(c['statut'], c['statut'])}\n")
        if c["mode_paiement"] != "cash":
            texte += f"Capture : {'oui' if c['nb_captures'] else 'non'}\n"
        if c["statut"] == "paiement_a_verifier":
            texte += f"Si le paiement est bon : #{ticket} ok\n"
    texte += f"\nCommandes payées ces 7 derniers jours : {payees}\n"
    texte += "Détail et capture d'une commande : commande 12"
    return texte


AIDE_PROPRIETAIRE = (
    "Ce que tu peux m'écrire :\n"
    "commandes : la liste des commandes en cours\n"
    "commande 12 : le détail de la commande n°12, avec la capture du paiement\n"
    "#3 suivi de ton message : je le transmets à la cliente n°3\n"
    "#3 ok : paiement vérifié, la commande est confirmée\n"
    "#3 annule : la commande est annulée\n"
    "#3 bot : je reprends la conversation avec la cliente\n"
    "Tu peux aussi répondre directement (glisser) à un message de cliente."
)


def traiter_message_proprietaire(texte, id_message_cite):
    nb_debloques = vider_file_attente()
    cle = normaliser(texte)

    # ---- Détail d'une commande : "commande 12", "commande n°12" ----
    detail = re.match(r"^\s*commande\s*(?:n\s*°?|no|numero|#)?\s*(\d+)\s*$", texte, re.I)
    if detail:
        commande = commande_par_id(detail.group(1))
        if not commande:
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"Je ne trouve pas la commande n°{detail.group(1)}.")
            return
        ticket = obtenir_ticket(commande["numero_whatsapp"])
        captures = captures_de_commande(commande["id"])
        if not captures and commande["mode_paiement"] != "cash":
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE,
                                     f"Aucune capture de paiement enregistrée pour la commande n°{commande['id']}. "
                                     f"Pour la demander : #{ticket} Pouvez-vous renvoyer la capture de votre paiement ?")
        for cap in captures:
            try:
                id_media = televerser_media(bytes(cap["contenu"]), cap["type_mime"], cap["nom_fichier"])
                legende = f"Capture commande n°{commande['id']} reçue le {heure_abidjan(cap['created_at'])}"
                appel_whatsapp(NUMERO_PROPRIETAIRE,
                               contenu_media(cap["type_mime"], id_media, legende, cap["nom_fichier"]))
            except Exception as e:
                print(f"Erreur envoi capture : {e}")
                envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, "La capture n'a pas pu être envoyée, réessaie plus tard.")
        # Les infos complètes, juste sous la capture
        suite = f"Si le paiement est bon : #{ticket} ok\n" if commande["statut"] == "paiement_a_verifier" else ""
        envoyer_message_whatsapp(
            NUMERO_PROPRIETAIRE,
            f"{construire_recapitulatif(commande)}\n\n"
            f"Cliente #{ticket} - WhatsApp : +{commande['numero_whatsapp']}\n"
            f"Statut : {LIBELLES_STATUT.get(commande['statut'], commande['statut'])}\n{suite}".strip())
        return

    # ---- Retrouver la cliente concernée ----
    numero_client = None
    if id_message_cite:
        lignes = requete_sql("select numero_whatsapp from public.messages_proprio where id_message = %s",
                             (id_message_cite,))
        if lignes:
            numero_client = lignes[0]["numero_whatsapp"]
    correspondance = re.match(r"^\s*#(\d+)\s*(.*)$", texte, re.S)
    if correspondance:
        numero_client = numero_par_ticket(correspondance.group(1))
        texte = correspondance.group(2).strip()
        cle = normaliser(texte)
        if not numero_client:
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"Je ne trouve pas de cliente #{correspondance.group(1)}.")
            return

    # ---- Pas de cliente visée ----
    if not numero_client:
        if "commande" in cle:
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, liste_commandes_en_cours())
        elif nb_debloques == 0 and cle not in ("ok", "okay", "merci", "oui"):
            # "ok", "merci"... servent juste à garder la fenêtre de 24 h ouverte : pas de réponse
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, AIDE_PROPRIETAIRE)
        return

    with verrou_cliente(numero_client):
        conv = charger_conversation(numero_client)
        ticket = conv["ticket"]

        # ---- Paiement vérifié ----
        if cle in ("ok", "valide", "confirme", "paye"):
            commande = derniere_commande(numero_client)
            if commande and commande["statut"] in ("paiement_a_verifier", "en_attente_paiement"):
                changer_statut_commande(commande["id"], "payee")
                diminuer_stock(commande["articles"])
                message = (f"Paiement bien reçu, merci. Votre commande n°{commande['id']} est confirmée. "
                           f"Livraison prévue {commande['jour_livraison']} à {commande['commune']}.")
                envoyer_message_whatsapp(numero_client, message)
                ajouter_historique(conv, "assistant", message)
                conv["mode"] = "bot"
                sauver_conversation(conv)
                envoyer_message_whatsapp(NUMERO_PROPRIETAIRE,
                                         f"Commande n°{commande['id']} confirmée, la cliente #{ticket} est prévenue. "
                                         "Je reprends la conversation avec elle.")
                return
            cle = "bot"  # pas de paiement en attente : on rend simplement la main au bot

        # ---- Commande annulée ----
        if cle in ("annule", "annuler"):
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
        if cle in ("bot", "fin"):
            conv["mode"] = "bot"
            sauver_conversation(conv)
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"C'est noté, je reprends la conversation #{ticket}.")
            return

        if not texte:
            return

        # ---- Message à transmettre à la cliente ----
        conv["mode"] = "humain"
        try:
            message = nettoyer_texte(reformuler_consigne(texte, conv["historique"]))
        except Exception as e:
            print(f"Erreur reformulation : {e}")
            message = nettoyer_texte(texte)

        ok, _ = envoyer_message_whatsapp(numero_client, message)
        if ok:
            ajouter_historique(conv, "assistant", message)
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE, f"Envoyé à #{ticket} : {message}")
        else:
            envoyer_message_whatsapp(NUMERO_PROPRIETAIRE,
                                     f"Impossible d'écrire à #{ticket} : elle n'a pas écrit depuis plus de 24 h.")
        sauver_conversation(conv)


# ============================================
# MESSAGES DES CLIENTES
# ============================================
def traiter_message_client(numero, texte):
    with verrou_cliente(numero):
        conv = charger_conversation(numero)
        ajouter_historique(conv, "user", texte)
        sauver_conversation(conv, activite=True)

        # Une conseillère a la main (situation délicate) : on lui transmet le message, le bot se tait
        if conv["mode"] == "humain":
            envoyer_au_proprio(f"#{conv['ticket']} (+{numero}) : {texte}", numero)
            return

        repondre_avec_claude(conv, numero)


def repondre_avec_claude(conv, numero):
    """Le bot répond à la cliente (à appeler avec le verrou de la cliente)."""
    def envoyer_photo(url, legende):
        ok, _ = envoyer_image_whatsapp(numero, url, legende)
        return ok

    def envoyer_texte(message):
        envoyer_message_whatsapp(numero, message)
        ajouter_historique(conv, "assistant", message)

    ctx = {"numero": numero, "photos": 0, "passer_la_main": None,
           "envoyer_photo": envoyer_photo, "envoyer_texte": envoyer_texte}

    try:
        reponse = nettoyer_texte(generer_reponse_claude(list(conv["historique"]), ctx, OUTILS_WHATSAPP))
    except Exception as e:
        print(f"Erreur Claude : {e}")
        reponse = "Désolée, petit souci technique. Une conseillère va vous recontacter très vite."
        ctx["passer_la_main"] = "Erreur technique du bot"

    if reponse:
        envoyer_message_whatsapp(numero, reponse)
        ajouter_historique(conv, "assistant", reponse)
    sauver_conversation(conv)

    if ctx["passer_la_main"]:
        passer_la_main(conv, ctx["passer_la_main"], prevenir_client=False)


def envoyer_capture_au_proprio(commande, ticket, numero, fichier, type_mime, nom_fichier, moment):
    """Envoie au propriétaire la capture, puis juste en dessous toutes les infos de la cliente."""
    legende = f"Paiement commande n°{commande['id']} - cliente #{ticket}"
    fiche = (f"Capture reçue le {heure_abidjan(moment)}\n"
             f"Total attendu : {formater_prix(commande['total'])}\n\n"
             f"{construire_recapitulatif(commande)}\n\n"
             f"WhatsApp de la cliente : +{numero}\n\n"
             f"Si le paiement est bon : #{ticket} ok\n"
             f"Sinon : #{ticket} suivi de ton message")
    try:
        id_media = televerser_media(fichier, type_mime, nom_fichier)
        envoyer_au_proprio(contenu_media(type_mime, id_media, legende, nom_fichier), numero)
    except Exception as e:
        print(f"Erreur transfert image : {e}")
        envoyer_au_proprio(legende + "\n(L'image est enregistrée : écris \"commande N\" pour la recevoir.)", numero)
    envoyer_au_proprio(fiche, numero)


def traiter_media_client(numero, type_media, media):
    """
    La cliente envoie une image ou un document.
    - Une commande attend son paiement : c'est la capture. On l'enregistre, on prévient le propriétaire
      (capture + fiche complète, une seule fois) et le bot continue de répondre à la cliente.
    - Sinon : on garde l'image (elle sera rattachée à la commande si la cliente commande ensuite)
      et le bot continue la conversation, sans déranger le propriétaire.
    """
    try:
        fichier, type_mime = telecharger_media(media["id"])
    except Exception as e:
        print(f"Erreur téléchargement image : {e}")
        fichier, type_mime = None, None

    with verrou_cliente(numero):
        conv = charger_conversation(numero)
        ticket = conv["ticket"]
        commande = None
        try:
            commande = derniere_commande(numero)
        except Exception as e:
            print(f"Erreur lecture commande : {e}")
        est_capture = bool(commande and commande["statut"] in ("en_attente_paiement", "paiement_a_verifier"))

        nom_fichier = media.get("filename") or (
            f"capture-commande-{commande['id']}.jpg" if est_capture else "image.jpg")
        if fichier:
            try:
                enregistrer_capture(commande["id"] if est_capture else None, numero, type_mime, nom_fichier, fichier)
            except Exception as e:
                print(f"Erreur enregistrement capture : {e}")

        if est_capture:
            changer_statut_commande(commande["id"], "paiement_a_verifier")
            message_cliente = ("Merci, nous avons bien reçu votre capture. "
                               "Nous vérifions le paiement et revenons vers vous très vite.")
            ajouter_historique(conv, "user", "(La cliente a envoyé la capture de son paiement)")
            envoyer_message_whatsapp(numero, message_cliente)
            ajouter_historique(conv, "assistant", message_cliente)
            sauver_conversation(conv, activite=True)
        else:
            ajouter_historique(conv, "user", "(La cliente a envoyé une image. Aucune commande n'est encore enregistrée.)")
            sauver_conversation(conv, activite=True)
            if conv["mode"] == "humain":
                envoyer_au_proprio(f"#{ticket} (+{numero}) a envoyé une image.", numero)
            else:
                repondre_avec_claude(conv, numero)

    if est_capture:
        if fichier:
            envoyer_capture_au_proprio(commande, ticket, numero, fichier, type_mime, nom_fichier,
                                       datetime.now(timezone.utc))
        else:
            envoyer_au_proprio(f"Paiement commande n°{commande['id']} - cliente #{ticket} : la capture n'a pas pu "
                               f"être récupérée, demande-la à la cliente avec #{ticket} suivi de ton message.", numero)


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
    return "Le serveur du chatbot Chez Ella est en ligne ! (version 5)"


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


messages_traites = set()   # anti-doublon : Meta renvoie parfois le même message
_verrou_doublons = threading.Lock()


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
                    with _verrou_doublons:
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
